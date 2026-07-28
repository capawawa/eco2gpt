# eco2gpt

A small authenticated Flask relay for selected Ecowitt real-time sensor fields.
The existing endpoints remain:

- `GET /get_ecowit_data`
- `POST /chatgpt_webhook`

Both endpoints require `Authorization: Bearer <RELAY_AUTH_TOKEN>`.

## Configure

Copy `.env.example` to `.env` and provide:

- `APPLICATION_KEY`, `API_KEY`, and `MAC_ADDRESS` for Ecowitt
- `RELAY_AUTH_TOKEN`, generated from at least 32 random bytes
- `ECOWITT_ALLOWED_FIELDS`, a comma-separated list of dotted paths below
  `data`, such as
  `data.outdoor.temperature,data.outdoor.humidity`

The relay always preserves Ecowitt's `code`, `msg`, and `time` metadata when
present. All other response fields are dropped unless explicitly allowed.
Do not use `*` or select a broad object when individual sensor values suffice.

Generate a token without putting it in shell history:

```powershell
py -3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store the token in the deployment's secret manager. Do not commit `.env`.

## Run

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The development server binds to `127.0.0.1` by default. For remote use, put it
behind a TLS-terminating reverse proxy and explicitly configure that proxy to
reach the private bind address. If the platform requires a public process bind,
set `HOST=0.0.0.0` only there and keep bearer authentication enabled.

Example request:

```powershell
$headers = @{ Authorization = "Bearer $env:RELAY_AUTH_TOKEN" }
Invoke-RestMethod http://127.0.0.1:5000/get_ecowit_data -Headers $headers
```

The authenticated upstream rate limit defaults to 30 requests per minute.
It is enforced per application process. With multiple Gunicorn workers, divide
the configured limit by the worker count or add a shared reverse-proxy/Redis
limit so the total deployment cap remains intentional.

## Safeguards

- Missing or weak relay configuration fails closed with HTTP 503.
- Invalid credentials return HTTP 401 without calling Ecowitt.
- Authenticated requests are rate-limited before the upstream call.
- Request bodies, upstream response sizes, and upstream connect/read time are
  bounded.
- Redirects are disabled and upstream errors return a generic HTTP 502.
- Responses are marked `no-store` and include only configured sensor fields.

Run the regression tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
