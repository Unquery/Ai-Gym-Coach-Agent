# Deployment failure recovery

Use the exact deployed commit, generated request ID, HTTP status and sanitized
event names to investigate. Keep raw logs, URLs, credentials and workout data out
of issues/chat. Cloud changes and restore operations require explicit approval.

| Symptom | Safe first action | Resume only when |
| --- | --- | --- |
| Process exits before serving HTTP | Check Render deploy status, selected commit, `PORT`, runtime-only secret names and `startup_configuration_error`; run offline runtime preflight in a clean shell | Configuration passes and the selected image starts with the platform port |
| Image build/import/bind failure | Keep the previous compatible release; inspect build/startup phase and sanitized platform logs; reproduce locally | The exact candidate image builds and starts; do not change the database to fix an image problem |
| Migration exits nonzero | Stop the release; leave traffic paused; check direct endpoint, verified TLS, credentials and concurrent runner | Stored revision and actual schema are known; cause is resolved and one reviewed retry is approved |
| `/health` is 200, `/ready` is 503 | Check Neon availability, then revision and runtime grants; retain the API process and existing data | Readiness recovers and authenticated read-only smoke passes |
| Provider/agent returns 502/503/504 | Check request ID, provider permissions/quota and timeout; do not reset the DB or automatically repeat model calls | Cause is understood and any retry/live request is authorized |

## Startup and database outage

Run `python -m app.preflight runtime` with only the selected runtime environment
to diagnose settings offline. Missing/placeholder credentials fail safely. Check
hostnames, CA path in the actual Linux image and role separation; do not disable
TLS verification or use the migration owner to make readiness green. A running
process with failed readiness may have a stale revision or unsafe grants as well
as an unavailable database. Check these separately through a trusted operator
connection. Never expose a diagnostic endpoint containing connection details.

Once connectivity returns, `pool_pre_ping` replaces stale idle connections on
checkout. The API does not need a restart just to reconnect. A request interrupted
inside a transaction fails; its writes are not automatically replayed. Check
`/ready`, then run the approved read-only smoke command. Do not use keep-alive
pings or restart loops to hide a database/configuration failure.

## Failed or uncertain migration

The release runner holds an advisory transaction lock and rolls back transactional
DDL, data and Alembic revision together on a known failure. A competing runner
fails rather than waiting indefinitely. Do not launch parallel retries.

If the connection was lost during commit, the result is unknown to the client.
Before any retry, use a trusted read-only `psql` session against the selected
database, with credentials supplied by its secret environment:

```sql
SELECT version_num FROM public.alembic_version;
```

Compare the result and actual schema with the candidate's packaged migration head
(`0008` currently). A missing table, unexpected revision or incompatible schema
needs investigation; never stamp, downgrade, reset, seed or recreate it to silence
the error. Re-running an upgrade at a verified head is a no-op, but do not assume
that exit code alone established the outcome of a disconnected commit.

## Application rollback and uncertain writes

Roll back only to an explicitly selected image/commit compatible with the stored
schema. Review migrations between the old and new releases before changing the
application version. Rolling back the application does not roll back database
contents. If no compatible release exists, keep traffic closed and prepare a
reviewed forward fix. Restore a verified backup only into a separately approved
empty target; verify revision, grants and synthetic checks before switching the
runtime connection. Never restore over the active database automatically.

For uncertain chat outcomes, retain the original `Idempotency-Key` and request
payload. A retry with a new key is a new operation and may repeat provider work.
Confirmation retries reuse the original proposal ID; do not create or confirm a
replacement proposal to compensate for an unknown result. Direct create routes
have no durable retry key: inspect state before retrying. Human confirmation
remains a separate required action, including during recovery.

See [migration guarantees](MIGRATIONS.md), [operational diagnostics](OPERATIONS.md)
and [first deployment](FIRST_DEPLOYMENT.md).
