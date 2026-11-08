# Deployment gate

Local release verification is complete; no cloud deployment or live-provider check has run.
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
  forces one worker. Read-only [smoke checks](SMOKE_TESTS.md) passed mocks and real
  local HTTP with restricted-role PostgreSQL and hostname-verified TLS.
- [x] Image built from exact runtime commit `d615e91` passed UID 10001, read-only
  filesystem, excluded development artifacts, guarded release jobs, runtime DML,
  denied DDL/revision writes, owner isolation, failed-provider rollback and 429 checks.
- [x] Release migrations have separate secrets, a transaction lock and rollback;
  runtime cannot run DDL or change the Alembic revision.
- [x] Stale checkout, interrupted transaction and SQL timeout tests pass. A disposable
  PostgreSQL stop/start restored readiness through the same Engine, preserving data.
- [x] Render auto-deploy is disabled; readiness and 120-second shutdown allowance
  are configured. Compose parses and does not expose a public plaintext API.

## Required before admitting cloud traffic

- [ ] Approve deployment of an exact commit; recheck Render/Neon quotas and billing.
- [ ] Select the reviewed image for the release commit; rebuild/reverify if runtime
  source, dependencies, base images or deployment settings change.
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

Evidence: the 2026-11-08 hardening passed 455 focused tests; final closure passed
70 targeted tests without skips and built `ai-gym-coach:verify-d615e91` (image ID
`sha256:7fb3450c1e18a41494306b6eb8fc3f50c888168da101c7ecfa209c559448afe9`).
On a new internal Docker network, TLS PostgreSQL and the read-only production
image passed all six smoke checks, readiness 200 -> 503 -> 200 without API restart,
retained committed synthetic data, sanitized JSON logs and graceful exit 0.
Temporary containers/volumes/network were removed. No demo DB, cloud deployment,
target-cloud smoke or live LLM calls were involved. Local TLS does not verify
the cloud certificate, CA bundle, DNS, routing or account permissions.

Open limits: single trusted operator only; no public multi-user authentication,
hard overall cancellation, persisted-draft bound or external alerting. Socket timeouts
do not prove a deadline for every network/server stall. Cloud DNS/TLS, provider
grants, quotas, restore and actual cloud HTTP/LLM behavior remain unverified.
