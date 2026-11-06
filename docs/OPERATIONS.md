# Health and application logs

Start production with `python -m app.server` (the Docker default). This configures
JSON stdout logging and disables Uvicorn's plaintext access logs. No reload,
automatic migrations, seed data, LLM request tracing or paid readiness requests.

- `GET /health`: process liveness, no database/provider dependency.
- `GET /ready`: PostgreSQL reachable and Alembic revision matches the packaged
  head. In production it also rejects superuser/role-creation/database-creation,
  schema-creation, bypass-RLS and table-owner/member credentials, including
  membership in elevated roles. Returns 200
  `{"status":"ready"}` or sanitized 503 `{"status":"not_ready"}`. It cannot
  prove provider availability or validate every future schema permission.

Use `/ready` for cloud traffic admission and Docker health checks. A failing
database should remove the instance from traffic; `/health` remains available.
Readiness uses the runtime pool (5-second connection/pool waits by default) and a
2-second SQL statement timeout. Managed databases that sleep may briefly fail
readiness during cold starts. Probes require an allowed Host, but no API token.

HTTP logs contain generated request ID, normalized method, route **template**,
status and duration in milliseconds. Unknown paths use `<unmatched>`. Successful
health/readiness probes are omitted. Clients receive `X-Request-ID`; incoming
request IDs are not trusted. Responses are marked no-store and nosniff.

The JSON formatter never formats arbitrary logger messages, arguments, exception
text/tracebacks or SQL. Application failures expose only a code-owned exception
class and request ID. Provider/HTTP-library logging is reduced to warnings, whose
messages are also suppressed. Do not enable separate provider debug tracing or
plaintext exception handlers in production. Host-platform access logs have their
own controls: never put credentials in query strings and restrict log access.

Monitor 5xx rates, failed readiness, 429 responses, restart frequency and provider
usage/budget separately in the hosting dashboard. Request logs record latency,
not token counts or success of a provider call. Provider timeouts remain mapped
to 504; unknown provider/tool failures are sanitized. These logs are not an audit
trail or an external metrics/alerting service.
