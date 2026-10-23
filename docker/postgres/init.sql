-- The application owns its tables but is not a PostgreSQL superuser.
\getenv app_password APP_DATABASE_PASSWORD
CREATE ROLE gym_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;
REVOKE ALL ON DATABASE gym_coach FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE gym_coach TO gym_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO gym_app;
