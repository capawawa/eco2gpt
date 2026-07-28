import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from collections import deque

import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

load_dotenv()

ECOWITT_URL = "https://api.ecowitt.net/api/v3/device/real_time"
PROTECTED_ENDPOINTS = {"get_ecowit_data", "chatgpt_webhook"}
PUBLIC_METADATA_FIELDS = ("code", "msg", "time")
FIELD_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigurationError(ValueError):
    pass


class UpstreamResponseError(RuntimeError):
    pass


class SlidingWindowRateLimiter:
    def __init__(self, limit, window_seconds=60, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._requests = deque()
        self._lock = threading.Lock()

    def check(self):
        now = self.clock()
        cutoff = now - self.window_seconds

        with self._lock:
            while self._requests and self._requests[0] <= cutoff:
                self._requests.popleft()

            if len(self._requests) >= self.limit:
                retry_after = max(
                    1, math.ceil(self.window_seconds - (now - self._requests[0]))
                )
                return False, 0, retry_after

            self._requests.append(now)
            remaining = self.limit - len(self._requests)
            return True, remaining, 0


def _parse_int(name, value, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc

    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _parse_float(name, value, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc

    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def parse_allowed_fields(raw_fields):
    if not raw_fields:
        raise ConfigurationError("ECOWITT_ALLOWED_FIELDS is required")

    fields = []
    for raw_field in raw_fields.split(","):
        field = raw_field.strip()
        if not field:
            continue

        parts = field.split(".")
        if (
            len(parts) < 2
            or parts[0] != "data"
            or len(field) > 200
            or any(not FIELD_COMPONENT_RE.fullmatch(part) for part in parts)
        ):
            raise ConfigurationError(
                "ECOWITT_ALLOWED_FIELDS must contain dotted data paths"
            )

        if field not in fields:
            fields.append(field)

    if not fields:
        raise ConfigurationError("ECOWITT_ALLOWED_FIELDS is required")
    if len(fields) > 50:
        raise ConfigurationError("ECOWITT_ALLOWED_FIELDS cannot exceed 50 paths")
    return tuple(fields)


def _runtime_config(config):
    required = {
        "APPLICATION_KEY": config.get("APPLICATION_KEY"),
        "API_KEY": config.get("API_KEY"),
        "MAC_ADDRESS": config.get("MAC_ADDRESS"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigurationError(f"Missing required setting: {', '.join(missing)}")

    auth_token = config.get("RELAY_AUTH_TOKEN") or ""
    auth_token_length = len(auth_token.encode("utf-8"))
    if not 32 <= auth_token_length <= 256:
        raise ConfigurationError("RELAY_AUTH_TOKEN must be 32 to 256 bytes")

    return {
        **required,
        "RELAY_AUTH_TOKEN": auth_token,
        "ALLOWED_FIELDS": parse_allowed_fields(config.get("ECOWITT_ALLOWED_FIELDS")),
        "CONNECT_TIMEOUT": _parse_float(
            "ECOWITT_CONNECT_TIMEOUT_SECONDS",
            config.get("ECOWITT_CONNECT_TIMEOUT_SECONDS"),
            0.1,
            30,
        ),
        "READ_TIMEOUT": _parse_float(
            "ECOWITT_READ_TIMEOUT_SECONDS",
            config.get("ECOWITT_READ_TIMEOUT_SECONDS"),
            0.1,
            60,
        ),
        "MAX_UPSTREAM_RESPONSE_BYTES": _parse_int(
            "MAX_UPSTREAM_RESPONSE_BYTES",
            config.get("MAX_UPSTREAM_RESPONSE_BYTES"),
            1024,
            10 * 1024 * 1024,
        ),
    }


def _authorized(header_value, expected_token):
    scheme, separator, candidate = header_value.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not candidate
        or any(character.isspace() for character in candidate)
    ):
        return False

    candidate_bytes = candidate.encode("utf-8")
    if len(candidate_bytes) > 256:
        return False

    candidate_digest = hashlib.sha256(candidate_bytes).digest()
    expected_digest = hashlib.sha256(expected_token.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_digest, expected_digest)


def _copy_path(source, destination, path):
    source_cursor = source

    for component in path:
        if not isinstance(source_cursor, dict) or component not in source_cursor:
            return False
        source_cursor = source_cursor[component]

    destination_cursor = destination
    for component in path[:-1]:
        destination_cursor = destination_cursor.setdefault(component, {})

    leaf = path[-1]
    destination_cursor[leaf] = source_cursor
    return True


def minimize_response(payload, allowed_fields):
    minimized = {
        field: payload[field] for field in PUBLIC_METADATA_FIELDS if field in payload
    }
    for field in allowed_fields:
        _copy_path(payload, minimized, field.split("."))
    return minimized


def fetch_ecowit_data(
    application_key,
    api_key,
    mac,
    *,
    connect_timeout=3,
    read_timeout=10,
    max_response_bytes=1024 * 1024,
):
    params = {
        "application_key": application_key,
        "api_key": api_key,
        "mac": mac,
        "call_back": "all",
    }
    response = requests.get(
        ECOWITT_URL,
        params=params,
        headers={"Accept": "application/json"},
        timeout=(connect_timeout, read_timeout),
        allow_redirects=False,
        stream=True,
    )

    try:
        response.raise_for_status()

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_response_bytes:
                    raise UpstreamResponseError("Ecowitt response exceeded size limit")
            except ValueError:
                pass

        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > max_response_bytes:
                raise UpstreamResponseError("Ecowitt response exceeded size limit")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamResponseError("Ecowitt returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise UpstreamResponseError("Ecowitt returned an unexpected JSON value")
        return payload
    finally:
        response.close()


def get_bind_host(environ=None):
    if environ is None:
        environ = os.environ
    return environ.get("HOST", "127.0.0.1")


def create_app(test_config=None):
    relay_app = Flask(__name__)
    relay_app.config.from_mapping(
        APPLICATION_KEY=os.getenv("APPLICATION_KEY"),
        API_KEY=os.getenv("API_KEY"),
        MAC_ADDRESS=os.getenv("MAC_ADDRESS"),
        RELAY_AUTH_TOKEN=os.getenv("RELAY_AUTH_TOKEN"),
        ECOWITT_ALLOWED_FIELDS=os.getenv("ECOWITT_ALLOWED_FIELDS"),
        RELAY_RATE_LIMIT_PER_MINUTE=os.getenv("RELAY_RATE_LIMIT_PER_MINUTE", "30"),
        ECOWITT_CONNECT_TIMEOUT_SECONDS=os.getenv(
            "ECOWITT_CONNECT_TIMEOUT_SECONDS", "3"
        ),
        ECOWITT_READ_TIMEOUT_SECONDS=os.getenv("ECOWITT_READ_TIMEOUT_SECONDS", "10"),
        MAX_REQUEST_BYTES=os.getenv("MAX_REQUEST_BYTES", str(16 * 1024)),
        MAX_UPSTREAM_RESPONSE_BYTES=os.getenv(
            "MAX_UPSTREAM_RESPONSE_BYTES", str(1024 * 1024)
        ),
    )
    if test_config is not None:
        relay_app.config.update(test_config)

    relay_app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    try:
        relay_app.config["MAX_CONTENT_LENGTH"] = _parse_int(
            "MAX_REQUEST_BYTES",
            relay_app.config.get("MAX_REQUEST_BYTES"),
            1,
            1024 * 1024,
        )
        rate_limit = _parse_int(
            "RELAY_RATE_LIMIT_PER_MINUTE",
            relay_app.config.get("RELAY_RATE_LIMIT_PER_MINUTE"),
            1,
            600,
        )
        relay_app.extensions["relay_runtime"] = _runtime_config(relay_app.config)
        relay_app.extensions["relay_rate_limiter"] = SlidingWindowRateLimiter(
            rate_limit
        )
        relay_app.extensions["relay_config_error"] = None
    except ConfigurationError as exc:
        relay_app.extensions["relay_runtime"] = None
        relay_app.extensions["relay_rate_limiter"] = None
        relay_app.extensions["relay_config_error"] = str(exc)

    @relay_app.before_request
    def protect_relay():
        if request.endpoint not in PROTECTED_ENDPOINTS:
            return None

        if request.method == "POST":
            request.get_data(cache=False)

        runtime = relay_app.extensions["relay_runtime"]
        if runtime is None:
            return jsonify({"error": "service_unavailable"}), 503

        if not _authorized(
            request.headers.get("Authorization", ""), runtime["RELAY_AUTH_TOKEN"]
        ):
            response = jsonify({"error": "unauthorized"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response

        allowed, remaining, retry_after = relay_app.extensions[
            "relay_rate_limiter"
        ].check()
        g.rate_limit_remaining = remaining
        if not allowed:
            response = jsonify({"error": "rate_limit_exceeded"})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        return None

    @relay_app.after_request
    def set_response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if hasattr(g, "rate_limit_remaining"):
            response.headers["X-RateLimit-Remaining"] = str(g.rate_limit_remaining)
        return response

    @relay_app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        return jsonify({"error": "request_too_large"}), 413

    def relay_ecowitt_data():
        runtime = relay_app.extensions["relay_runtime"]
        try:
            payload = fetch_ecowit_data(
                runtime["APPLICATION_KEY"],
                runtime["API_KEY"],
                runtime["MAC_ADDRESS"],
                connect_timeout=runtime["CONNECT_TIMEOUT"],
                read_timeout=runtime["READ_TIMEOUT"],
                max_response_bytes=runtime["MAX_UPSTREAM_RESPONSE_BYTES"],
            )
        except (requests.RequestException, UpstreamResponseError):
            relay_app.logger.warning("Ecowitt request failed")
            return jsonify({"error": "upstream_unavailable"}), 502

        return jsonify(minimize_response(payload, runtime["ALLOWED_FIELDS"]))

    @relay_app.route("/get_ecowit_data", methods=["GET"])
    def get_ecowit_data():
        return relay_ecowitt_data()

    @relay_app.route("/chatgpt_webhook", methods=["POST"])
    def chatgpt_webhook():
        return relay_ecowitt_data()

    return relay_app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = get_bind_host()
    print(f"Starting Flask server on {host}:{port}")
    app.run(host=host, port=port)
