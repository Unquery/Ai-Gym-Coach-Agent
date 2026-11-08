# Deployment gate

Preparation only; no cloud deployment or paid/live-provider check has run.
Use the full [release instructions](DEPLOYMENT.md), [operations](OPERATIONS.md)
and [security boundary](SECURITY.md) when deployment is explicitly authorized.

## Locally verified

- [x] Production entrypoint validates settings/port, emits sanitized startup errors,
  binds the platform port and handles SIGTERM; no startup migration or seed.
- [x] Locked non-root/read-only image excludes credentials and dev artifacts.
- [x] Business routes require an operator token; demo auth is forbidden in production.
  Host/CORS/input/error/log boundaries are covered; stalled/trickled bodies time out.
- [x] Owner-isolation tests cover conversations, drafts, cached responses, proposals
  and workouts. Shared API/chat budgets, two chat slots, prompt/output/token limits
  and cooperative time budgets preserve rollback and retry safety.
- [x] Production requires hostname-verified DB TLS and usable secrets; the entrypoint
  forces one worker. Read-only [smoke checks](SMOKE_TESTS.md) have offline HTTP tests.
- [x] Release migrations have separate secrets, a transaction lock and rollback;
  runtime cannot run DDL or change the Alembic revision.
- [x] Stale checkout, interrupted transaction and SQL timeout tests pass. A disposable
  PostgreSQL stop/start restored readiness through the same Engine, preserving data.
- [x] Render auto-deploy is disabled; readiness and 120-second shutdown allowance
  are configured. Compose parses and does not expose a public plaintext API.

## Required before admitting cloud traffic

- [ ] Approve deployment of an exact commit; recheck Render/Neon quotas and billing.
- [ ] Rebuild/test that exact image; the previous image checks predate this follow-up.
- [ ] Provision synthetic-only PostgreSQL; verify backup and restore outside production.
- [ ] Set separate migrator/runtime credentials, hostname-verified TLS, a fresh owner
  UUID and random operator token; keep all credentials out of Git/public clients.
- [ ] Set the exact allowed API hostname (plus loopback probes) and required HTTPS
  CORS origins; leave demo mode and auto-deploy disabled, use one instance/worker.
- [ ] Back up, pause writes and run the explicit release job. Stop on failure;
  verify head `0008` and restricted role before starting the selected application.
- [ ] Verify real HTTPS, `/ready`=200, anonymous business access=401, docs=404,
  authenticated reads, owner isolation, sanitized logs and graceful stop/recovery.
  Run `python -m app.smoke https://YOUR_API_HOST` with `SMOKE_API_AUTH_TOKEN` in
  the secret environment; require exit 0. This performs no writes or LLM calls.
- [ ] Run any real-provider synthetic write scenario only after separate approval;
  review the proposal and explicitly confirm persistence, then verify replay safety.
- [ ] Verify monitoring/budget limits and a schema-compatible application rollback.

The 2026-11-08 follow-up passed 455 focused regression tests without skips,
including offline evaluations and real isolated PostgreSQL tests. No image rebuild,
cloud deployment, deployed-target smoke check or live LLM call ran in this follow-up.

Open limits: single trusted operator only; no public multi-user authentication,
hard overall cancellation, persisted-draft bound or external alerting. Socket timeouts
do not prove a deadline for every network/server stall. Cloud DNS/TLS, provider
grants, quotas, restore and actual cloud HTTP/LLM behavior remain unverified.
