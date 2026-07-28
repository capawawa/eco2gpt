import json
import unittest
from unittest.mock import Mock, patch

import requests

from app import (
    UpstreamResponseError,
    create_app,
    fetch_ecowit_data,
    get_bind_host,
)


TOKEN = "test-token-with-more-than-thirty-two-bytes"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class RelayRouteTests(unittest.TestCase):
    def make_app(self, **overrides):
        config = {
            "TESTING": True,
            "APPLICATION_KEY": "application-key",
            "API_KEY": "api-key",
            "MAC_ADDRESS": "AA:BB:CC:DD:EE:FF",
            "RELAY_AUTH_TOKEN": TOKEN,
            "ECOWITT_ALLOWED_FIELDS": (
                "data.outdoor.temperature,data.outdoor.humidity"
            ),
            "RELAY_RATE_LIMIT_PER_MINUTE": "30",
            "ECOWITT_CONNECT_TIMEOUT_SECONDS": "3",
            "ECOWITT_READ_TIMEOUT_SECONDS": "10",
            "MAX_REQUEST_BYTES": "16384",
            "MAX_UPSTREAM_RESPONSE_BYTES": "1048576",
        }
        config.update(overrides)
        return create_app(config)

    @patch("app.fetch_ecowit_data")
    def test_both_routes_require_authentication_before_calling_ecowitt(self, fetch):
        client = self.make_app().test_client()

        get_response = client.get("/get_ecowit_data")
        post_response = client.post("/chatgpt_webhook")

        for response in (get_response, post_response):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json(), {"error": "unauthorized"})
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        fetch.assert_not_called()

    @patch("app.fetch_ecowit_data")
    def test_query_parameter_cannot_bypass_bearer_authentication(self, fetch):
        client = self.make_app().test_client()

        response = client.get(f"/get_ecowit_data?token={TOKEN}")

        self.assertEqual(response.status_code, 401)
        fetch.assert_not_called()

    @patch("app.fetch_ecowit_data")
    def test_missing_server_token_fails_closed(self, fetch):
        client = self.make_app(RELAY_AUTH_TOKEN="").test_client()

        response = client.get("/get_ecowit_data", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "service_unavailable"})
        fetch.assert_not_called()

    @patch("app.fetch_ecowit_data")
    def test_missing_field_allowlist_fails_closed(self, fetch):
        client = self.make_app(ECOWITT_ALLOWED_FIELDS="").test_client()

        response = client.get("/get_ecowit_data", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "service_unavailable"})
        fetch.assert_not_called()

    @patch("app.fetch_ecowit_data")
    def test_authenticated_request_returns_only_allowed_sensor_fields(self, fetch):
        fetch.return_value = {
            "code": 0,
            "msg": "success",
            "time": "1720000000",
            "data": {
                "outdoor": {
                    "temperature": {"value": "72.5", "unit": "F"},
                    "humidity": {"value": "48", "unit": "%"},
                    "feels_like": {"value": "73.0", "unit": "F"},
                },
                "indoor": {"temperature": {"value": "70.0", "unit": "F"}},
            },
            "device": {"name": "private-station-name"},
        }
        client = self.make_app().test_client()

        response = client.get("/get_ecowit_data", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "code": 0,
                "msg": "success",
                "time": "1720000000",
                "data": {
                    "outdoor": {
                        "temperature": {"value": "72.5", "unit": "F"},
                        "humidity": {"value": "48", "unit": "%"},
                    }
                },
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        fetch.assert_called_once()

    @patch("app.fetch_ecowit_data")
    def test_post_body_limit_prevents_upstream_call(self, fetch):
        client = self.make_app(MAX_REQUEST_BYTES="8").test_client()

        response = client.post(
            "/chatgpt_webhook",
            headers=AUTH_HEADERS,
            data=b"123456789",
            content_type="application/octet-stream",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json(), {"error": "request_too_large"})
        fetch.assert_not_called()

    @patch("app.fetch_ecowit_data")
    def test_rate_limit_blocks_second_upstream_call(self, fetch):
        fetch.return_value = {"code": 0, "data": {}}
        client = self.make_app(RELAY_RATE_LIMIT_PER_MINUTE="1").test_client()

        first = client.get("/get_ecowit_data", headers=AUTH_HEADERS)
        second = client.get("/get_ecowit_data", headers=AUTH_HEADERS)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json(), {"error": "rate_limit_exceeded"})
        self.assertIn("Retry-After", second.headers)
        self.assertEqual(fetch.call_count, 1)

    @patch("app.fetch_ecowit_data")
    def test_webhook_preserves_authenticated_post_behavior(self, fetch):
        fetch.return_value = {"code": 0, "data": {}}
        client = self.make_app().test_client()

        response = client.post(
            "/chatgpt_webhook",
            headers=AUTH_HEADERS,
            json={"ignored": "existing behavior"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"code": 0})
        fetch.assert_called_once()

    @patch("app.fetch_ecowit_data", side_effect=requests.Timeout)
    def test_upstream_failure_returns_generic_error(self, fetch):
        client = self.make_app().test_client()

        response = client.get("/get_ecowit_data", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"error": "upstream_unavailable"})
        self.assertNotIn("api-key", response.get_data(as_text=True))
        fetch.assert_called_once()

    def test_default_bind_is_private(self):
        self.assertEqual(get_bind_host({}), "127.0.0.1")


class FetchEcowittTests(unittest.TestCase):
    @staticmethod
    def response_with_body(body):
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = [body]
        return response

    @patch("app.requests.get")
    def test_outbound_request_has_timeouts_and_redirects_disabled(self, get):
        get.return_value = self.response_with_body(b'{"code": 0, "data": {}}')

        payload = fetch_ecowit_data(
            "application-key",
            "api-key",
            "AA:BB:CC:DD:EE:FF",
            connect_timeout=2,
            read_timeout=7,
            max_response_bytes=1024,
        )

        self.assertEqual(payload, {"code": 0, "data": {}})
        get.assert_called_once()
        call = get.call_args
        self.assertEqual(call.kwargs["timeout"], (2, 7))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        get.return_value.close.assert_called_once()

    @patch("app.requests.get")
    def test_oversized_upstream_response_is_rejected(self, get):
        body = json.dumps({"data": "x" * 2000}).encode()
        get.return_value = self.response_with_body(body)

        with self.assertRaises(UpstreamResponseError):
            fetch_ecowit_data(
                "application-key",
                "api-key",
                "AA:BB:CC:DD:EE:FF",
                max_response_bytes=1024,
            )

        get.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
