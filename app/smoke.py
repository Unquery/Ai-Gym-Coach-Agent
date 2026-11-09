"""Read-only deployment checks: python -m app.smoke https://api.example.com."""

import argparse
import json
import os
import re
from urllib.parse import urlsplit

import httpx2


def validate_target(base_url: str, token: str) -> str:
    try:
        parsed = urlsplit(base_url)
        # Validate before opening a client; InvalidURL is not an HTTPError.
        parsed.port
        httpx2.URL(base_url)
    except (ValueError, httpx2.InvalidURL):
        raise ValueError("Invalid smoke target") from None
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname or (parsed.scheme != "https" and not local_http)
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"} or not token.strip()
        or any(ord(char) < 33 or ord(char) == 127 for char in base_url)
        or not token.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in token)
    ):
        raise ValueError("Use an HTTPS origin and set SMOKE_API_AUTH_TOKEN; HTTP is loopback-only")
    return base_url.rstrip("/")


def run_smoke(base_url: str, token: str, *, transport: httpx2.BaseTransport | None = None) -> bool:
    base_url = validate_target(base_url, token)
    checks = (
        ("health", "/health", False, 200, {"status": "ok"}),
        ("readiness_database", "/ready", False, 200, {"status": "ready"}),
        ("anonymous_access", "/workouts?limit=1", False, 401, None),
        ("invalid_token", "/workouts?limit=1", "invalid", 401, None),
        ("authenticated_database_read", "/workouts?limit=1", True, 200, []),
        ("api_validation_error", "/workouts?limit=0", True, 422, {"detail": [
            {"loc": ["query", "limit"], "type": "greater_than_equal", "msg": "Invalid request value"},
        ]}),
        ("production_docs", "/openapi.json", False, 404, None),
    )
    success = True
    with httpx2.Client(transport=transport, timeout=10, follow_redirects=False, trust_env=False) as client:
        for name, path, authenticated, status, expected in checks:
            passed = False
            headers = {}
            if authenticated:
                credential = token + "-invalid" if authenticated == "invalid" else token
                headers["Authorization"] = "Bearer " + credential
            try:
                with client.stream("GET", base_url + path, headers=headers) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > 16384:
                            raise ValueError("Oversized smoke response")
                        body.extend(chunk)
                    passed = (
                        response.status_code == status
                        and re.fullmatch(r"[0-9a-f]{32}", response.headers.get("x-request-id", "")) is not None
                        and "no-store" in {
                            item.strip().lower() for item in response.headers.get("cache-control", "").split(",")
                        }
                        and response.headers.get("x-content-type-options", "").lower() == "nosniff"
                    )
                    if base_url.lower().startswith("https://"):
                        hsts = re.search(
                            r"(?:^|;)\s*max-age\s*=\s*(\d+)(?:\s*;|\s*$)",
                            response.headers.get("strict-transport-security", "").lower(),
                        )
                        passed = passed and hsts is not None and int(hsts.group(1)) > 0
                    if expected is not None:
                        value = json.loads(body)
                        passed = passed and (isinstance(value, list) if expected == [] else value == expected)
                    if status == 401:
                        passed = passed and response.headers.get("www-authenticate", "").lower() == "bearer"
            except (httpx2.HTTPError, ValueError):
                passed = False
            print(f"{'PASS' if passed else 'FAIL'} {name}")
            success = success and passed
    return success


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="HTTPS API origin; loopback HTTP is allowed for local checks")
    args = parser.parse_args(argv)
    try:
        return 0 if run_smoke(args.base_url, os.environ.get("SMOKE_API_AUTH_TOKEN", "")) else 1
    except ValueError:
        print("Invalid configuration: use an HTTPS origin and set SMOKE_API_AUTH_TOKEN")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
