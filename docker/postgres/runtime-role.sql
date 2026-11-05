-- One-time provisioning AFTER migrations, connected as the table-owning migrator.
-- Supply RUNTIME_DATABASE_PASSWORD via a secret environment variable to psql.
-- This script intentionally fails if gym_runtime already exists; rotate separately.
\set ON_ERROR_STOP on
BEGIN;
\getenv runtime_password RUNTIME_DATABASE_PASSWORD
CREATE ROLE gym_runtime LOGIN PASSWORD :'runtime_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
SELECT format('GRANT CONNECT ON DATABASE %I TO gym_runtime', current_database()) \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO gym_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gym_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO gym_runtime;
REVOKE ALL ON public.alembic_version FROM gym_runtime;
GRANT SELECT ON public.alembic_version TO gym_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gym_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO gym_runtime;
COMMIT;
