# Deployment migrations

Use `python -m app.db.migrate` as a single release job before admitting traffic.
It requires `MIGRATION_DATABASE_URL` with no runtime/test URL fallback. Use the
direct PostgreSQL endpoint, not a transaction-pooling endpoint. Production URLs
using a Neon `-pooler` host are rejected before creating an engine. The M9 offline
preflight additionally checks the selected role, CA path and channel binding.
They require `postgresql+psycopg` and hostname-verified TLS (`sslmode=verify-full`
with the provider's CA configuration). Migration jobs need no API or LLM secrets.

1. Take a provider snapshot/backup and verify the restore procedure before an
   upgrade of an existing database. Retain the previous application image.
2. Stop writes or use a maintenance window for this small single-instance MVP.
3. Run the release job with the table-owning migration role. The runner acquires
   a PostgreSQL advisory transaction lock, upgrades to `head`, and commits once.
   A concurrent runner fails immediately; a failed upgrade rolls back DDL and the
   Alembic revision together. Lock waits are capped at 5 seconds and each SQL
   statement at 120 seconds. Investigate a failure before retrying.
4. On the first deployment, provision the restricted runtime role using
   `docker/postgres/runtime-role.sql` via `psql` as the table-owning migration role.
   Supply its password through `RUNTIME_DATABASE_PASSWORD`, never a command-line
   argument. The script is transactional, one-time and assumes schema `public`.
   A managed provider must allow role creation; otherwise provision equivalent
   grants through its supported administrator workflow. Reuse this same migration
   owner for later releases so default grants apply to new tables/sequences.
5. Start the API with only the runtime role URL. Verify `/ready` before routing
   traffic. The API must have no migration password or DDL privileges.

For local development, copy the migration URL placeholders from `.env.example`.
An explicit `docker compose --profile backend up --build -d` waits for the
migration job before starting the backend. `docker compose up -d db` still starts
only PostgreSQL. Local Compose uses the existing development owner; production
must use separate roles. No seed, `create_all`, reset, downgrade or database
creation occurs in the release job or server startup. Re-running at head is safe.

On failure, keep the old API and investigate the sanitized release-job failure.
If the connection fails during commit, inspect the stored revision before retrying:
the client cannot know whether that commit reached PostgreSQL.
Do not run `downgrade`, stamp an unknown schema, delete the volume or recreate the
database automatically. Prefer a reviewed forward fix; restore a verified backup
only through an explicit operator decision. A schema-compatible application image
can be rolled back separately. Future migrations must remain transactional; a
nontransactional operation needs a separately reviewed workflow before use.
