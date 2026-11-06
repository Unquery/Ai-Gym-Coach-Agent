# Backend deployment preparation

Prepared target: one Docker web service on Render with managed PostgreSQL on Neon
in a nearby EU region. No cloud service, database or deployment has been created
or verified. `render.yaml` is an input for a later approved deployment, not a
declaration that the backend is already hosted. It disables automatic subsequent
deploys; creating a Blueprint still initiates its first deployment.

## Cost and platform limits (reviewed 2026-11-07)

Render Free is suitable for this synthetic portfolio demo, not an uptime promise.
It sleeps after 15 idle minutes and lacks pre-deploy commands. Run migrations as
an explicit operator release job before creating/updating the service. Free
Render PostgreSQL expires after 30 days, so use Neon for a persistent demo.
See [Render Free](https://render.com/docs/free) and
[release commands](https://render.com/docs/deploys#pre-deploy-command).

Neon currently lists 100 CU-hours/month and 1 GB of database storage per Free
project. Monitor quotas and actual provider billing; free availability is not
guaranteed. [Neon pricing](https://neon.com/pricing) is authoritative.
Repeated DB readiness probes can keep database compute awake while the backend
is running. Do not add external keep-alive pings; sleeping services have cold
starts. An always-on backend may need a paid database plan even with little chat
traffic. LLM quotas/billing are separate.

## Configuration and release order

1. After explicit deployment approval, select the exact application commit and
   provision a synthetic-only Neon database. Keep its direct endpoint and
   migration/table-owner credential in an operator secret store. The app supports
   PostgreSQL 17; use the verified version where the provider offers it.
2. Copy `deploy/migration.env.example` to ignored `.local/migration.env` and
   `deploy/runtime.env.example` to ignored `.local/runtime.env`. Replace every
   placeholder. Change the provider URL scheme from `postgresql://` to
   `postgresql+psycopg://`, URL-encode passwords and preserve TLS/channel-binding
   parameters. Templates use hostname-verified TLS and the Debian CA bundle
   included in the image. See [Neon TLS](https://neon.com/docs/connect/connect-securely).
3. Back up any existing data. Build the reviewed image and run only the release
   job; see [migration safety and recovery](MIGRATIONS.md):

   ```powershell
   docker compose -f compose.production.yaml build
   docker compose -f compose.production.yaml --env-file .local/migration.env run --rm migrate
   ```

   Stop if this command fails. It neither deploys an API nor creates/resets a DB.
4. Provision `gym_runtime` using `docker/postgres/runtime-role.sql` through a
   trusted `psql` session connected as the migration owner. Supply its distinct
   random password using `RUNTIME_DATABASE_PASSWORD`. In Neon, create this role
   through SQL: ordinary console-created roles inherit administrative privileges.
   Do not add `neon_superuser` membership. See [Neon role rules](https://neon.com/docs/manage/roles).
   Put only this restricted role's URL in runtime `DATABASE_URL`.
5. Generate the non-demo owner with `python -c "from uuid import uuid4; print(uuid4())"`.
   For Compose, generate a bearer token using
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` and store it
   privately. Render's Blueprint can generate its own token. Do not commit or
   paste credentials into chat, URLs, command arguments or public clients.
6. In a later approved Render setup, use `render.yaml` with Docker/Free and set
   its unsynced variables from the reviewed runtime configuration. Set
   `ALLOWED_HOSTS` to the actual `onrender.com` hostname, any verified custom
   hostnames, `localhost,127.0.0.1`. Render health checks use a verified custom
   hostname or the service subdomain, so include both when applicable.
   Do not upload `.env`, the migration secret, bootstrap passwords or
   `TEST_DATABASE_URL` to the API. Leave CORS empty unless a specific HTTPS client
   origin is needed. The Docker command binds `0.0.0.0:$PORT`; Render terminates
   HTTPS. See [Blueprint fields](https://render.com/docs/blueprint-spec) and
   [health checks](https://render.com/docs/health-checks).
7. Admit traffic only after `/ready` returns 200. Check `/health`, anonymous
   business access = 401, docs = 404, authenticated history and expected CORS/Host
   rejection. Run a separately approved synthetic logging/confirmation scenario,
   verify DB contents and replay safety, and inspect JSON logs for sensitive data.
   Keep live provider checks separately authorized; they were not run here.

For a local production-image check against an approved managed database, after
the release and role setup:

```powershell
docker compose -f compose.production.yaml --env-file .local/runtime.env up -d backend
docker compose -f compose.production.yaml ps
```

This Compose file binds only loopback. If used on a VPS, provide a separately
configured HTTPS reverse proxy before exposing traffic. Keep one worker/instance;
the process-local chat limiter is not shared. Runtime containers receive no
migration credential, and the release job receives no API or LLM credentials.
Compose allows 120 seconds for shutdown, matching the Render configuration and
exceeding the server's 100-second graceful shutdown timeout.

## Updates, rollback and remaining verification

Use [the deployment gate checklist](DEPLOYMENT_CHECKLIST.md) to distinguish local
verification from the checks still required in the target environment.

For every release: test/build the chosen commit, back up, pause writes, run the
guarded migration job, then manually deploy that exact commit. No push or deploy
is implied by a local Git commit. Keep auto-deploy disabled and confirm the
dashboard hasn't overridden the Blueprint setting.

On a failed release, do not reset, downgrade or stamp the DB automatically. Follow
[MIGRATIONS.md](MIGRATIONS.md), inspect the stored revision and keep/restore a
schema-compatible prior application image. Monitor health, latency, errors and
usage using [OPERATIONS.md](OPERATIONS.md).

Cloud DNS/TLS, the provider's exact grants/limits, account quota, backup restore,
HTTPS routing and real cloud HTTP/provider behavior remain unverified. Free-tier
hosting does not establish production reliability. Public multi-user access,
planning tools, overall agent deadlines and persisted-draft bounds remain outside
this milestone; see [SECURITY.md](SECURITY.md).
