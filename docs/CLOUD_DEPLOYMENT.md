# Milestone 9: Cloud deployment and live verification

Status: local preparation complete; waiting for account access and explicit cloud
approval. No cloud resources or live requests have been authorized. M8's local
acceptance remains complete. This milestone closes
only after the external checks below actually pass; do not infer deployment from
the presence of a Blueprint, API key or local image.

## Architecture and decisions

One Render Docker web service calls Neon PostgreSQL over verified TLS and
GroqCloud over HTTPS. Render terminates public HTTPS; the application listens
on its assigned `PORT`. PostgreSQL holds durable conversations, retry bindings,
drafts and workouts. Render's local filesystem is disposable. Groq returns text
and tool requests; backend-owned validation, ownership and transactions decide
which tools can execute. Only a separate human confirmation saves an AI-proposed
workout. Never give the operator token or database credentials to a public client.

Use the existing single-operator boundary, one process and one instance. Keep
the small SQLAlchemy pool (3 + 2 connections) and direct Neon endpoint initially;
there is no need to introduce transaction pooling for one API instance. A separate
release process uses the table-owning migrator. The API receives only the SQL-created
`gym_runtime` role; Neon console/API-created roles inherit elevated membership.
See [Neon roles](https://neon.com/docs/manage/roles) and [existing release safety](MIGRATIONS.md).

Prepared defaults: Render Free, Frankfurt, repository branch `master`, auto-deploy
off, `/ready` health path and a 120-second shutdown allowance. The initial demo
allows 2 chat requests/minute and 60 authenticated requests/minute. These limits
reduce admission; they cannot guarantee a token quota or zero cost. One chat can
make up to three model calls, and context size also affects token consumption.

## Public documentation review (2026-11-08)

- Render Free is a portfolio demonstration target, not an uptime guarantee. Idle
  spin-down, workspace-wide free hours and outbound/build allowances affect the
  demo. Paid-only pre-deploy commands are not our release mechanism: migrations
  run explicitly before starting the API. Do not enable paid plans or keep-alive
  traffic. Check actual workspace usage before approval.
  [Free service limits](https://render.com/docs/free),
  [release phases](https://render.com/docs/deploys#pre-deploy-command).
- `autoDeployTrigger: off` disables service commit auto-deploys. Blueprint Auto
  Sync is a separate dashboard control: set Auto Sync to No before later pushes.
  Creating a Blueprint starts its initial deployment, so creation is itself an
  approval boundary. Verify the selected commit before any creation/sync action.
  [Blueprint sync](https://render.com/docs/infrastructure-as-code),
  [Blueprint fields](https://render.com/docs/blueprint-spec).
- Neon currently advertises 100 CU-hours/month and 1 GB storage per Free project.
  Recheck the account: published limits are not an account entitlement. Frequent
  DB readiness probes can prevent scale-to-zero and consume compute while the
  API runs. Keep the existing readiness check and do not add external keep-alives.
  [Pricing](https://neon.com/pricing).
- Preserve `sslmode=verify-full`, the image CA bundle and `channel_binding=require`
  in both Neon URLs. Use PostgreSQL 17 if available; another major version needs
  explicit compatibility review. [Neon TLS](https://neon.com/docs/connect/connect-securely).
- Keep `openai/gpt-oss-20b` through `https://api.groq.com/openai/v1`; it is currently
  listed and supports local tool calling. Check the organization's actual model
  permissions, free-plan limits, spend settings and data policy before a request.
  Groq's documented spend-limit feature requires a paid-tier organization owner
  and tracks spend with a delay; do not treat it as a free-plan zero-cost guard.
  No paid upgrade or spend limit change is authorized.
  [Models](https://console.groq.com/docs/models),
  [tool calling](https://console.groq.com/docs/tool-use/overview),
  [rate limits](https://console.groq.com/docs/rate-limits),
  [spend limits](https://console.groq.com/docs/spend-limits).

## Credentials and account handoff

| Destination | Required values | Must stay absent |
| --- | --- | --- |
| Local release job | Production mode, direct Neon migrator URL; runtime password only during separate role provisioning | API/LLM keys, test/demo DB URLs |
| Render API | Restricted runtime URL, random bearer token, non-demo owner UUID, exact allowed hosts, Groq model/key | Migrator/admin password, test URLs, local `.env` |
| Read-only smoke shell | Target HTTPS origin and `SMOKE_API_AUTH_TOKEN` | Migrator/admin credentials |

Use `deploy/runtime.env.example` and `deploy/migration.env.example` as the source
templates. Never import the development `.env` into Render. Supply actual values
only via local ignored files or each provider's secret UI; do not paste secrets
into chat or command arguments. Keep CORS empty until a specific HTTPS client is
approved. Include the actual Render hostname plus `localhost,127.0.0.1` for probes.

Validate each profile in a clean shell with only its own secret file loaded:

```powershell
uv run --env-file .local/m9-runtime.env --locked python -m app.preflight runtime
uv run --env-file .local/m9-migration.env --locked python -m app.preflight migration
```

These commands make no network requests. They reuse production settings validation
and check the selected direct Neon role/TLS profile, exact Groq model/endpoint,
Render hosts, initial admission limits and absence of unrelated credentials.
Use chat=2/min and API=60/min in the M9 runtime file, matching `render.yaml`;
generic deployment templates retain the earlier 10/120 defaults. A shell that
already loaded development/test/admin secrets fails separation checks: start a
clean shell instead of copying all development variables into the release job.
Exit 0 means offline configuration only; 1 means configuration failure; invalid
CLI usage exits 2. Output never contains secret values or validation tracebacks.
The CA path is checked for presence in the URL, not opened or verified locally;
the production image and target TLS handshake must verify the actual CA file.

Account access is the next external prerequisite: select the Render workspace,
Neon organization/project and Groq organization; inspect plan/quota, model access
and existing resources read-only before proposing creation. Render and Neon
integrations are available but are not connected. Connecting them is separate
from approving resource creation. No connection or cloud write happened locally.

## Ordered rollout after explicit approval

1. Select the exact release commit and approve any required push separately.
   Local commits are not available to Render's Git build until published. Review
   plan/region/estimated usage and obtain creation/deployment approval; do not
   open the Blueprint creation flow as a harmless preview.
2. Provision a synthetic-only Neon target; capture its identity and direct host
   without logging the password. Use a new target, never the local demo database.
   Verify TLS/CA and the chosen PostgreSQL version. Set a fresh runtime password.
3. Build the approved source. Run the existing guarded migration job, then run
   `docker/postgres/runtime-role.sql` as its owner with password from the secret
   environment. Verify head `0008`, runtime DML and denied DDL/revision writes.
   No automatic seed, schema reset, downgrade or write retry is allowed.
4. Verify a synthetic backup/restore into a separately approved empty target;
   never restore over the active database. Free-plan history is not a tested
   restore procedure. [Neon backup guidance](https://neon.com/docs/manage/backup-pg-dump).
5. Configure Render from the reviewed Blueprint: exact branch/commit, one instance,
   free plan, runtime-only secrets, `/ready`, required hosts and no CORS origins
   unless needed. Keep service auto-deploy off and Blueprint Auto Sync off.
   Only the separately approved creation/deploy action may start the service.
6. Verify Render's deployed commit, actual HTTPS, health/readiness and all six
   [smoke checks](SMOKE_TESTS.md). Inspect sanitized JSON logs and exercise
   controlled DB failure/recovery only on a separately approved test target.
7. Obtain separate permission for a bounded live Groq scenario and any cost.
   Use one synthetic scenario with an explicit call/token budget, zero retries
   and recorded usage. Stop on quota/provider errors. First verify reads, then
   proposals; inspect the proposal before a separate explicit human confirmation.
   Verify no workout before confirmation and exactly one workout after replay.
8. Record evidence, configure account usage/error notifications where available,
   verify a schema-compatible rollback, then decide whether to admit demo traffic.

## M9 acceptance and stop conditions

- [ ] Exact deployed commit, provider account/resource identities and approved cost boundary recorded without secrets.
- [ ] Real Neon TLS, migration head, restricted-role access and synthetic backup restore verified.
- [ ] Real Render HTTPS/readiness/authentication/ownership/smoke verified; auto-deploy and Blueprint Auto Sync both disabled.
- [ ] Separately approved bounded Groq scenario verified with usage, rollback/idempotency and human confirmation evidence.
- [ ] Monitoring/budget controls and a safe rollback verified; deployment documentation synchronized at milestone closure.

Stop on missing account access, unexpected existing data, paid-plan requirements,
unsafe runtime privileges, migration failure, TLS/auth/readiness failure or quota
errors. A failed gate leaves M9 incomplete. Public multi-user auth and new product
features remain outside this deployment milestone.

## Local handoff evidence

The reviewed Blueprint and rollout plan are committed separately from the offline
preflight. Preflight/configuration checks passed 30 cases; the final separation
change passed an 11-case focused rerun. Tests use synthetic values and forbid
DNS/connection calls. No existing agent, persistence or HITL implementation changed.

Private files prepared locally and confirmed ignored by Git:

- `.local/m9-runtime.env`: fresh local operator token/owner and runtime password,
  initial 2/60 admission limits; actual Neon/Render hosts and Groq key still missing.
- `.local/m9-migration.env`: migrator URL template, still awaiting the selected target.
- `.local/m9-role.env`: the matching runtime password for the separate role setup;
  never load this into the API or migration preflight process.

No existing private files were overwritten and generated values were never printed.
Both incomplete profiles correctly fail offline preflight with sanitized output.
This is expected refusal, not successful validation of production credentials.
Only the initial private-secret generation happened; no role or account key was
created or rotated at a provider. No push, resource creation, deployment, DB access,
live Groq request or paid action occurred. M9 remains incomplete pending account
inspection, a concrete approved deployment and the external acceptance evidence.
