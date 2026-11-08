# Health and application logs

Start production with `python -m app.server` (the Docker default). This configures
JSON stdout logging and disables Uvicorn's plaintext access logs. No reload,
automatic migrations, seed data, LLM request tracing or paid readiness requests.
Configuration and `PORT` (1–65535, default 8000) are validated before binding;
failure exits with code 1 and a generic `startup_configuration_error` JSON event.
On SIGTERM, Uvicorn drains requests for up to 100 seconds and completes ASGI
shutdown, then the entrypoint disposes any existing database pool. Keep the
platform/container shutdown allowance at 120 seconds. Migration execution stays
an explicit prior release step, never part of application startup.
The entrypoint forces one worker even with `WEB_CONCURRENCY` set. Keep one replica:
API/chat rate budgets and the two-chat admission limit are process-local. See
[security limits](SECURITY.md) and [read-only deployment smoke checks](SMOKE_TESTS.md).

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

## Database connections and outage recovery

One process keeps at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections (default
3 + 2). `DB_CONNECT_TIMEOUT` bounds pool waits and each new TCP connection attempt
(default 5 seconds); `DB_STATEMENT_TIMEOUT_MS` bounds server-side SQL execution
(default 15000 ms). `pool_pre_ping` detects dead idle connections on checkout and
invalidates stale pooled connections. The next request can reconnect after an
outage without restarting the application. Session exit rolls back unfinished work.

TCP keepalives use idle=30s, interval=10s, count=3; `tcp_user_timeout=15000` caps
unacknowledged transmitted data on supported systems, including Linux production.
These socket settings complement connect/SQL timeouts; they are not an overall
request deadline and cannot guarantee a bound for every server/network stall.
Some socket options have no effect on unsupported operating systems. See
[libpq connection options](https://www.postgresql.org/docs/17/libpq-connect.html#LIBPQ-PARAMKEYWORDS).

A disconnect during SQL/commit fails that request; no transaction or write is
automatically replayed. A lost commit acknowledgement has an unknown outcome.
Retry chat only with the original idempotency key; confirmation reuses its
proposal ID. Direct create routes have no durable retry key: inspect state before
retrying an uncertain write. Do not add blanket retries around commits or provider
calls. See [SQLAlchemy disconnect handling](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic).
Readiness returns sanitized 503 during an outage and 200 after connectivity and
the expected schema/permissions recover; liveness remains independent of the DB.

Monitor 5xx rates, failed readiness, 429 responses, restart frequency and provider
usage/budget separately in the hosting dashboard. Request logs record latency,
not token counts or success of a provider call. Provider timeouts remain mapped
to 504; unknown provider/tool failures are sanitized. These logs are not an audit
trail or an external metrics/alerting service.
