# AI Gym Coach Agent

An AI-powered workout logging and progress analysis assistant built as a Junior AI Engineering portfolio project.

**Status:** Milestone 3 in progress — exercise catalog API backed by PostgreSQL.

See [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) and [`docs/ROADMAP.md`](docs/ROADMAP.md).

The MVP will use Python, FastAPI, PostgreSQL, and safe LLM tool calling to retrieve training logs, suggest sessions, record confirmed sets, and calculate progress.

## Setup (Windows PowerShell)

Prerequisites: Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
If uv is missing, install it, then reopen PowerShell so PATH is refreshed:

```powershell
winget install --id=astral-sh.uv -e
```

From the repository root:

```powershell
Set-Location E:\AIGymCoach
uv --version
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".uv-cache"
# Only needed if Python 3.12 is not already installed:
uv python install 3.12
uv sync --locked
uv run --locked python --version
```

`.python-version` selects Python 3.12. `uv sync --locked` creates `.venv` and
installs the dependencies (including test tools) at the versions in `uv.lock`.
The session variable keeps uv's cache inside this repository; set it again in each
new shell if the default user cache is not writable. No virtual environment
activation is needed. The health endpoint needs no database or API key.

## PostgreSQL storage

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
with Docker Compose, or use an existing PostgreSQL 17 instance.

```powershell
Copy-Item .env.example .env
```

Edit the local `.env`: replace both password placeholders with distinct random
passwords, then put the URL-encoded application password in `DATABASE_URL` and
`TEST_DATABASE_URL`. The example contains placeholders only; `.env` is ignored.

```powershell
docker compose up -d --wait
uv run --env-file .env --locked alembic upgrade head
uv run --env-file .env --locked alembic current
# Optional: explicitly populate synthetic demonstration data.
uv run --env-file .env --locked python -m app.seed
```

The database binds only to `127.0.0.1:5432`. The application connects as `gym_app`,
a non-superuser that owns its migrated tables; it must not use the bootstrap
`postgres` account. Initialization grants `gym_app` schema-creation permission
for migrations and isolated tests. For a deployed application, use separate
migration/test credentials and reduce runtime privileges.

The demo seed is repeatable and contains no real workout history. Its fixed user
UUID is `00000000-0000-0000-0000-000000000001`. It adds two demo exercises, one
plan, and one historical workout with three sets. It does not repair or overwrite
an existing demo plan.

The named Docker volume preserves data across container restarts and
`docker compose down`. Initialization SQL runs only for a new volume; editing
`.env` later does not change existing database passwords.

See [the storage design](docs/DATABASE.md) for tables, ownership, and transaction
boundaries. Workout HTTP endpoints and authentication are not implemented yet.

## Run

From the repository root in PowerShell:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".uv-cache"
uv run --env-file .env --locked uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.
In a second PowerShell window, check the running application:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
Invoke-RestMethod -Uri http://127.0.0.1:8000/exercises
```

`GET /health` returns HTTP 200 with JSON `{"status":"ok"}`. This endpoint checks
that the application responds; it does not check database or LLM availability.
Stop the server with Ctrl+C.

`GET /exercises` returns HTTP 200 with the shared exercise catalog, ordered by
`id`, for example `[{"id":1,"name":"Demo squat"}]`. An empty catalog returns `[]`.
The endpoint reads existing records only; it requires `DATABASE_URL`, a running
PostgreSQL database, and applied migrations. Each request gets its own SQLAlchemy
session; the Pydantic response schema exposes only `id` and `name`.

`GET /exercises/{exercise_id}` returns one exercise with the same `id` and `name`
fields. Use an integer ID from the catalog, for example `/exercises/1`.
A missing exercise returns HTTP 404 with `{"detail":"Exercise not found"}`.
A non-integer ID such as `/exercises/abc` returns HTTP 422 before the lookup runs.

`POST /exercises` accepts `{"name":"Synthetic squat"}` and returns HTTP 201
with the committed exercise's `id` and `name`. The name is required, must be a
string, and is trimmed before checking its length (1–120 characters). Invalid
input returns HTTP 422. An existing name returns HTTP 409 with
`{"detail":"Exercise with this name already exists"}`; uniqueness is enforced by
PostgreSQL and is case-sensitive. A failed duplicate insert is rolled back.

To run only `/health` without a local `.env` file or database configuration, omit
`--env-file .env` from the server command. Database access is initialized lazily.

## Test

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) ".uv-cache"
uv run --locked pytest
# Include persistence and migration tests against a real PostgreSQL database:
uv run --env-file .env --locked pytest
```

API tests use FastAPI's `TestClient` without starting a server. PostgreSQL tests
create a unique `test_<uuid>` schema, apply the actual Alembic migration, and
remove only that schema afterward. They cover commit/reload, rollback,
relationships, constraints, ownership, seed repeatability, and migration
upgrade/downgrade/model agreement. Use a dedicated local test database; the role
needs permission to create schemas. No paid API calls are made.

Without `TEST_DATABASE_URL`, PostgreSQL tests are explicitly skipped. Skipped
tests do not validate persistence. With the variable set, connection or migration
failures fail the tests. To run only database tests, add `-m postgres`.

If a Windows sandbox reports `PytestCacheWarning` while creating its temporary
cache directory, pre-create the ignored cache directory, then rerun the test:

```powershell
New-Item -ItemType Directory -Path .pytest_cache -Force | Out-Null
uv run --locked pytest
```

## Layout

```text
app/main.py           FastAPI application, health and exercise catalog routes
app/database.py       PostgreSQL engine, request sessions, and declarative base
app/models.py         Exercise, plan, workout, and set models
app/schemas.py        Public HTTP request and response schemas
app/seed.py           Explicit synthetic demo seed
migrations/           Versioned schema changes
alembic.ini           Migration configuration (no credentials)
compose.yaml          Local PostgreSQL with persistent volume
docker/postgres/      Non-superuser application-role initialization
tests/                HTTP, PostgreSQL persistence, and migration tests
pyproject.toml        Runtime/dev dependencies and pytest configuration
uv.lock               Resolved dependency versions; keep in version control
docs/                 Technical context, storage design, and roadmap
```
