# Deployment smoke checks

After an explicitly approved deployment, set `SMOKE_API_AUTH_TOKEN` in the shell
or CI secret store to the deployed operator token. Do not put it in command-line
arguments, source code or logs. Run from the locked local environment:

```sh
uv run --locked python -m app.smoke https://YOUR_API_HOST
```

This performs seven GET requests: liveness, readiness, anonymous rejection, invalid
token rejection, an authenticated workout list limited to one entry, and disabled
production OpenAPI, plus a sanitized 422 for an invalid workout-list limit.
Readiness checks database connectivity, packaged migration
head and runtime role restrictions. The authenticated read also checks runtime
table access. No LLM, writes, migrations or paid provider calls are made.

Prepare and verify these checks offline, without a target or token:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_smoke.py -q
```

The test transport simulates success, database failure, broken authentication,
redirects, network errors, malformed/oversized bodies and leaked validation input.
It sends no real HTTP requests. The CLI command above the tests is a real network
action and remains gated on explicit deployment/smoke approval.

Exit codes: 0 = all checks passed, 1 = check/network failure, 2 = invalid local
configuration. Output contains only PASS/FAIL and fixed check names, never tokens,
URLs, server errors or workout data. Requests use TLS verification, 10-second
socket phase timeouts, bounded response reads and no redirects/proxy environment.
Cold starts can fail the check; rerun after the service becomes ready. This is a
smoke check, not a hard overall deadline or a substitute for deployment approval.
Only loopback origins may use HTTP for local verification.
Malformed URLs/ports and control characters in URLs/tokens are rejected before
opening a connection, with exit 2 and no raw input or traceback.

M8 local closure ran this CLI inside the exact production image over actual TCP
HTTP with restricted-role, hostname-verified TLS PostgreSQL: all six checks passed,
including after database stop/start without restarting the API. The added seventh
validation-error check is currently verified offline. This validates
the workflow locally; it has not been run against a deployed cloud endpoint.
