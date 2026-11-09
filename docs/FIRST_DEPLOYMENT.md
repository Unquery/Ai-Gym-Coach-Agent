# First cloud deployment

Local preparation is verified. No cloud target or account configuration has been
verified, and no cloud action is approved. M9 remains incomplete until its
[external acceptance gates](CLOUD_DEPLOYMENT.md#m9-acceptance-and-stop-conditions)
pass. This guide is for the existing single-operator, synthetic-data demo.

Record the missing target values privately before requesting creation/deployment
approval. Do not invent hostnames, account IDs, quotas or credentials:

| Provider | Prepared configuration | Still needed from the selected account |
| --- | --- | --- |
| Render | `ai-gym-coach`, Free, Frankfurt, `master`, one instance, auto-deploy off | Workspace, actual hostname, approved commit, available quota, Blueprint Auto Sync off |
| Neon | PostgreSQL 17, direct endpoint, migration owner plus SQL-created `gym_runtime`, verified TLS | Project/branch/database, actual host and owner, separate credentials, quota and verified restore target |
| GroqCloud | `https://api.groq.com/openai/v1`, `openai/gpt-oss-20b`, zero retries | Organization/project, model permission, key, actual usage/spend policy and approved live-test budget |

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
   $sourceStatus = git status --porcelain
   if ($LASTEXITCODE -ne 0 -or $sourceStatus) { throw 'Use a clean reviewed source tree' }
   $releaseCommit = git rev-parse HEAD
   if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the release commit' }
   docker build --tag "ai-gym-coach:$releaseCommit" .
   if ($LASTEXITCODE -ne 0) { throw 'Image build failed; stop the release' }
   docker image inspect --format '{{.Id}}' "ai-gym-coach:$releaseCommit"
   ```

5. **Migrate and verify grants after approval.** For existing data, stop writes
   and verify backup/restore first. Run the
   following command against the approved target using the image from step 4:

   ```powershell
   docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --env-file .local/m9-migration.env --entrypoint python "ai-gym-coach:$releaseCommit" -m app.release --execute-migrations
   if ($LASTEXITCODE -ne 0) { throw 'Release failed; keep traffic closed and inspect the revision' }
   ```

   On first deployment, provision the runtime role
   through a trusted table-owner `psql` session using
   `docker/postgres/runtime-role.sql`; supply credentials via its secret
   environment. `psql` uses `postgresql://` or `PG*` variables, not the SQLAlchemy
   `postgresql+psycopg://` scheme. Verify revision `0008`, runtime DML and denied
   DDL/revision writes. On any failure, stop; inspect revision/grants before retry.
   In the trusted provisioning environment, the script command is
   `psql --no-psqlrc --file docker/postgres/runtime-role.sql`; it must exit 0.
6. **Deploy manually after approval.** Use `render.yaml`, Free/Frankfurt, branch
   `master`, one instance and worker, exact approved commit, `/ready` and 120-second
   shutdown. Set runtime-only secrets and exact service host plus loopback hosts;
   leave CORS empty. Keep service auto-deploy off and Blueprint Auto Sync off.
   Before Blueprint creation, verify remote `master` is still the approved
   `$releaseCommit`; stop if it changed. Confirm that same commit after deployment.
   Blueprint creation starts a deployment. If Render generates the operator token,
   use that actual deployed token for smoke and update the private runtime profile;
   the locally generated token is not automatically the same credential.
7. **Verify cloud HTTP after approval.** Confirm the deployed commit and HTTPS.
   Load only `SMOKE_API_AUTH_TOKEN` into the smoke shell, then run:

   ```powershell
   uv run --locked python -m app.smoke https://YOUR_API_HOST
   ```

   Require seven PASS results and exit 0, including security headers on each
   response. Check sanitized logs, restore evidence,
   usage notifications and a schema-compatible rollback. Do not admit demo traffic
   after an unexplained TLS, readiness, authentication or database failure.
8. **Verify Groq separately.** Obtain approval for an exact synthetic scenario,
   request/token budget and any cost. Stop on provider/quota errors. Confirm an
   AI proposal only after reviewing it through the separate human confirmation
   flow; verify rollback and replay safety. Record evidence before closing M9.
   One deployed chat can make up to three provider calls, each capped at 1024
   completion tokens, with a separate 64 KiB context cap. Admission limits do not
   impose a monetary budget or guarantee zero cost.

The existing live-evaluation runner can preview a bounded scenario without any
credentials, database connection or provider request:

```powershell
uv run --locked python -m evals.live --scenario recent_workout_tool --max-requests 3 --max-completion-tokens 512
```

Do not add `--live` without separate approval. That runner uses local ASGI and
disposable `TEST_DATABASE_URL` schemas; it does not verify the deployed Render
API. A deployed end-to-end Groq scenario needs its own approved HTTP requests,
synthetic data and evidence. Use generated request IDs to correlate `chat_failed`
and HTTP events; monitor `readiness_changed` for outage/recovery transitions.

Never reset, seed, downgrade, stamp or retry writes automatically during release.
See [failure recovery](RECOVERY.md), [migration guarantees](MIGRATIONS.md), [smoke checks](SMOKE_TESTS.md) and
[security limits](SECURITY.md) for details.
