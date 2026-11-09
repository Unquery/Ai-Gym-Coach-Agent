# First cloud deployment

Local preparation is verified. No cloud target or account configuration has been
verified, and no cloud action is approved. M9 remains incomplete until its
[external acceptance gates](CLOUD_DEPLOYMENT.md#m9-acceptance-and-stop-conditions)
pass. This guide is for the existing single-operator, synthetic-data demo.

1. **Inspect accounts and approve the target.** Select Render workspace, Neon
   project/region and Groq organization. Check actual quotas, model permissions,
   billing and backup/restore options. Approve the exact resources, release commit
   and cost boundary before creating anything. A Git push needs separate approval;
   Render cannot deploy commits that exist only locally.
2. **Prepare the database and separated secrets.** After creation approval,
   provision the selected synthetic-only Neon target (PostgreSQL 17 where
   available). Copy `deploy/runtime.env.example` and
   `deploy/migration.env.example` to ignored `.local/m9-runtime.env` and
   `.local/m9-migration.env` only if those files do not already exist. Replace all
   placeholders privately. Use direct Neon URLs, `postgresql+psycopg`, URL-encoded
   passwords, `sslmode=verify-full`, CA bundle and `channel_binding=require`.
   Runtime uses SQL-created `gym_runtime`; migration uses the table owner. Keep
   runtime-password provisioning in a separate secret environment. No test/demo
   URL, migrator password or provisioning password belongs in Render runtime.
3. **Validate offline.** In clean, separate shells, run:

   ```powershell
   uv run --env-file .local/m9-runtime.env --locked python -m app.preflight runtime
   uv run --env-file .local/m9-migration.env --locked python -m app.release
   ```

   Require exit 0 for both. These commands do not connect. The Linux CA path must
   also exist in the release container; Windows preflight cannot verify its TLS.
4. **Build the reviewed commit.** Require a clean source tree, record the commit,
   build the image and retain its ID plus the previous compatible release:

   ```powershell
   git status --short
   $releaseCommit = git rev-parse HEAD
   docker build --tag "ai-gym-coach:$releaseCommit" .
   if ($LASTEXITCODE -ne 0) { throw 'Image build failed; stop the release' }
   docker image inspect --format '{{.Id}}' "ai-gym-coach:$releaseCommit"
   ```

5. **Migrate and verify grants after approval.** For existing data, stop writes
   and verify backup/restore first. Run the
   [explicit release command](CLOUD_DEPLOYMENT.md#repeatable-release-commands)
   against the approved target. On first deployment, provision the runtime role
   through a trusted table-owner `psql` session using
   `docker/postgres/runtime-role.sql`; supply credentials via its secret
   environment. `psql` uses `postgresql://` or `PG*` variables, not the SQLAlchemy
   `postgresql+psycopg://` scheme. Verify revision `0008`, runtime DML and denied
   DDL/revision writes. On any failure, stop; inspect revision/grants before retry.
6. **Deploy manually after approval.** Use `render.yaml`, Free/Frankfurt, branch
   `master`, one instance and worker, exact approved commit, `/ready` and 120-second
   shutdown. Set runtime-only secrets and exact service host plus loopback hosts;
   leave CORS empty. Keep service auto-deploy off and Blueprint Auto Sync off.
   Blueprint creation starts a deployment. If Render generates the operator token,
   use that actual deployed token for smoke and update the private runtime profile;
   the locally generated token is not automatically the same credential.
7. **Verify cloud HTTP after approval.** Confirm the deployed commit and HTTPS.
   Load only `SMOKE_API_AUTH_TOKEN` into the smoke shell, then run:

   ```powershell
   uv run --locked python -m app.smoke https://YOUR_API_HOST
   ```

   Require seven PASS results and exit 0. Check sanitized logs, restore evidence,
   usage notifications and a schema-compatible rollback. Do not admit demo traffic
   after an unexplained TLS, readiness, authentication or database failure.
8. **Verify Groq separately.** Obtain approval for an exact synthetic scenario,
   request/token budget and any cost. Stop on provider/quota errors. Confirm an
   AI proposal only after reviewing it through the separate human confirmation
   flow; verify rollback and replay safety. Record evidence before closing M9.

Never reset, seed, downgrade, stamp or retry writes automatically during release.
See [migration recovery](MIGRATIONS.md), [smoke checks](SMOKE_TESTS.md) and
[security limits](SECURITY.md) for details.
