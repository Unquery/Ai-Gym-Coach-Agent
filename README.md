# AI Gym Coach Agent

An AI-powered workout logging and progress analysis assistant built as a Junior AI Engineering portfolio project.

**Status:** Milestone 3 complete — PostgreSQL-backed exercise and workout APIs,
including set logging, workout details, and paginated history. Input validation
stabilization is complete. Milestone 4 includes typed, read-only
`get_recent_workouts` and `get_exercise_progress` tools, a provider-configurable
LLM client, and a minimal read-only tool-calling workflow.

See [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) and [`docs/ROADMAP.md`](docs/ROADMAP.md).

The MVP will use Python, FastAPI, PostgreSQL, and safe LLM tool calling to retrieve training logs, suggest sessions, record confirmed sets, and calculate progress.

Authentication, workout planning tools, and confirmed write tools remain planned
for later work.

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
boundaries. Exercise creation, workout/set logging, workout details, and history
are available in local demo mode; authentication is not implemented yet.

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
fields. Use an integer ID from 1 to 2147483647, for example `/exercises/1`.
A missing exercise returns HTTP 404 with `{"detail":"Exercise not found"}`.
A non-integer or out-of-range ID returns HTTP 422 before the lookup runs.

`POST /exercises` accepts `{"name":"Synthetic squat"}` and returns HTTP 201
with the committed exercise's `id` and `name`. The name is required, must be a
string, and is trimmed before checking its length (1–120 characters). NUL
characters are rejected before database access. Invalid input returns HTTP 422.
An existing name returns HTTP 409 with
`{"detail":"Exercise with this name already exists"}`; uniqueness is enforced by
PostgreSQL and is case-sensitive. A failed duplicate insert is rolled back.

`POST /workouts` accepts an object with optional `plan_id` (a strict integer from
1 to 2147483647, or `null`) and `started_at` (a timestamp with a timezone or
`null`). Sending `{}` creates an unplanned workout; omitting the time or passing
`null` uses the PostgreSQL time default. HTTP 201 returns `id`, `user_id`,
`plan_id`, and `started_at` after the commit succeeds. This creates the workout record only;
planned exercises are not copied into performed sets.

The server dependency `get_demo_user_id` supplies the existing synthetic owner
`00000000-0000-0000-0000-000000000001`. This is a local demo mode with no
authentication. The request accepts only `plan_id` and `started_at`; extra
fields, including `user_id`, return HTTP 422. A referenced plan must exist and
belong to the server-selected owner; missing or other owners' plans return HTTP
404 with `{"detail":"Workout plan not found"}`. The composite database foreign
key also protects the write if the plan changes after validation.

`POST /workouts/{workout_id}/sets` adds one performed set to an existing workout.
For example, send
`{"exercise_id":1,"set_number":1,"reps":10,"weight_kg":"30.25"}`.
The workout must belong to the server-selected demo owner; the exercise must
exist in the shared catalog but does not need to appear in the workout's plan.
HTTP 201 returns `id`, `workout_id`, `exercise_id`, `set_number`, `reps`, and
`weight_kg` after commit. Decimal weights are serialized as strings.

All four body fields are required. Exercise IDs, set numbers, and repetitions
must be strict positive integers up to 2147483647. The workout path ID must be
in the same range. Weight must be finite, between 0 and 99999.99 kg, with at most
two decimal places; zero means no external load. Invalid or extra fields return
422. Missing or other owners' workouts return 404 with `Workout not found`;
missing exercises return 404 with `Exercise not found`. Set numbers are unique
per workout and exercise: a duplicate returns 409 without overwriting the
existing set. Database constraint failures are rolled back, including when a
referenced record is deleted between validation and insertion.

`GET /workouts/{workout_id}` returns HTTP 200 with `id`, `user_id`, `plan_id`,
`started_at`, and `sets`. Each set has the same fields as the set-creation
response; decimal weights remain strings. Sets are ordered by `exercise_id`,
then `set_number`. A workout without sets returns `"sets": []`.

The workout must belong to the server-selected demo owner. Missing or other
owners' workouts return 404 with `{"detail":"Workout not found"}`. The path ID
must be an integer from 1 to 2147483647; invalid IDs return 422. The endpoint
loads the workout and its sets with two SELECTs using `selectinload`; missing
or inaccessible workouts require only one SELECT. Plans and exercise objects
are not loaded for this response.

`GET /workouts?limit=20&offset=0` lists the server-selected demo owner's workout
history. HTTP 200 returns an array of `id`, `user_id`, `plan_id`, and `started_at`
objects using `WorkoutRead`. Empty histories and pages past the end return `[]`.
The owner comes from `get_demo_user_id`; a `user_id` query parameter cannot
select another owner's history.

`limit` defaults to 20 and accepts integers from 1 to 100. `offset` defaults to
0 and accepts integers from 0 to 2147483647. Invalid pagination parameters
return 422. Results are ordered by `started_at` descending, then `id` descending
to break timestamp ties. Filtering, ordering, and pagination run in PostgreSQL
with one SELECT; the response does not load sets or plans.

To run only `/health` without a local `.env` file or database configuration, omit
`--env-file .env` from the server command. Database access is initialized lazily.

## Read-only agent tool

`app.tools.get_recent_workouts` reads the server-selected owner's history using
the same query as `GET /workouts`. It accepts `GetRecentWorkoutsInput` from
`app.tool_schemas`: strict integer `limit` (default 20, range 1-100) and `offset`
(default 0, range 0-2147483647). Unknown fields, including `user_id`, are rejected.
The input model exposes a JSON Schema through `model_json_schema()`.

```python
from app.tool_schemas import GetRecentWorkoutsInput
from app.tools import get_recent_workouts

# session and user_id are supplied by trusted server code.
arguments = GetRecentWorkoutsInput.model_validate({"limit": 3})
workouts = get_recent_workouts(arguments, session=session, user_id=user_id)
result = [workout.model_dump(mode="json") for workout in workouts]
```

The typed result is `list[WorkoutRead]`, with the existing `id`, `user_id`,
`plan_id`, and timezone-aware `started_at` fields. Empty histories/pages return
`[]`; database errors propagate. Ownership filtering, descending time/ID
ordering, and pagination run in one SELECT. The tool does not load sets or plans,
flush pending changes, commit, or roll back the caller's transaction.

This tool can be called directly or through the read-only agent workflow below.
The caller must validate model-supplied arguments with
`GetRecentWorkoutsInput` before execution; identity never comes from those
arguments. Authentication remains deferred; the HTTP API still uses its demo
owner dependency.

## LLM client configuration

The official OpenAI Python SDK sends Chat Completions to the configured API.
GroqCloud is the initial provider; switching providers changes configuration,
not workout tools or database queries. The low-level client performs one call;
`app.agent` implements the read-only workflow. FastAPI startup and `/health`
do not load LLM settings or send LLM requests.

Set these values in the ignored `.env` file or process environment:

| Variable | GroqCloud example | Local Ollama example |
| --- | --- | --- |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | `http://localhost:11434/v1/` |
| `LLM_MODEL` | `openai/gpt-oss-20b` | `gpt-oss:20b` |
| `LLM_API_KEY` | Your Groq API key | `ollama` (dummy value) |

Only the base URL has a default: GroqCloud. Model and key must be explicitly set
and nonblank. Models are provider-specific; select an available Groq model or
an installed local Ollama model. Local Ollama ignores the key, but the SDK requires a nonempty
value. Other compatible servers use the same three settings. Compatibility,
including tool calling, depends on the server and selected model.
See [Groq compatibility](https://console.groq.com/docs/openai) and
[Ollama compatibility](https://docs.ollama.com/api/openai-compatibility).

`load_llm_settings()` validates configuration only when called. The key is held
as `SecretStr` and is masked in the settings representation and JSON dump; raw
keys are passed only to the SDK. Do not print keys or request headers. `.env.example`
contains placeholders only; real `.env` files remain ignored. The existing
`uv run --env-file .env ...` workflow loads environment variables; application
imports do not read `.env` files automatically.

For local Windows smoke tests, keep `PYTHONIOENCODING=utf-8` in `.env`, as shown
in `.env.example`. It configures Python standard streams at startup, including
piped output, so Unicode punctuation, Cyrillic, and emoji can be printed without
`UnicodeEncodeError`. The first live test's failure occurred in the temporary
report script's `print(...)` with `cp1251` stdout after the agent had returned.
See [Python's output encoding setting](https://docs.python.org/3.12/using/cmdline.html#envvar-PYTHONIOENCODING).
Verify the launch configuration without an LLM request:

```powershell
uv run --env-file .env --locked python -c "import sys; print(sys.stdout.encoding)"
# Expected: utf-8
```

This Python example makes one real request only if explicitly run. It is not
executed by the API or test suite:

```python
from app.llm import create_chat_completion, create_llm_client
from app.llm_config import load_llm_settings

settings = load_llm_settings()
with create_llm_client(settings) as client:
    completion = create_chat_completion(
        client, settings, [{"role": "user", "content": "Say hello briefly."}]
    )
```

`create_chat_completion` accepts optional function-tool descriptions and returns
the full typed `ChatCompletion`, preserving `content=None` and `tool_calls`.
It does not execute proposed calls or access PostgreSQL. Each client uses a
30-second HTTP timeout and disables automatic retries. SDK authentication,
rate-limit, connection, and timeout exceptions propagate to the caller. Use a
context manager as above, or explicitly close the client when finished.

Run the focused mocked tests without LLM credentials, Ollama, or PostgreSQL:

```powershell
uv run --locked pytest tests/test_llm.py tests/test_health.py -q
```

## Read-only tool-calling workflow

`app.agent.run_read_only_agent(user_message, *, client, settings, session, user_id)`
uses the existing client and tools for one conversation: user request, model tool
request, Python execution, model summary. The caller supplies the database session
and current owner from trusted server code and owns client/session cleanup.
The function returns the final text; no HTTP endpoint or client UI is added.

`get_recent_workouts` and `get_exercise_progress` are advertised through typed
function-tool schemas generated from their Pydantic inputs. The dispatcher
also enforces this explicit allowlist before parsing arguments. Arguments are
validated before database access; an argument cannot override the server's owner.
The history tool returns workout metadata; the progress tool returns structured
statistics and a history-availability status.

The assistant tool-call message is preserved before each `role="tool"` result,
and every result references its original `tool_call_id`. Unknown tools and
invalid JSON/arguments produce fixed error codes without raw input or diagnostics.
The model can correct a failed request within the same budget. The workflow allows
at most three LLM requests and two tool requests, including invalid/unknown ones.
A batch exceeding the remaining tool budget is rejected before execution.

Unsupported tool types, missing/repeated call IDs, empty/truncated answers, and
exhausted limits raise `AgentError`. Database errors stop the workflow with a
generic error; raw database diagnostics are not sent to the model. SDK errors
propagate. The workflow does not commit or roll back the caller's transaction.
It performs no write operations. Tests mock all LLM responses, including the
PostgreSQL-backed owner-isolation case; no live LLM requests run automatically.

```powershell
# Include PostgreSQL ownership and tool tests in disposable schemas:
uv run --env-file .env --locked pytest tests/test_agent.py tests/test_tools.py tests/test_llm.py tests/test_health.py -q
```

## Deterministic exercise progress

`app.progress.analyze_exercise_progress(exercise_id, workouts)` is a pure Python
calculation over already loaded `WorkoutDetailRead` records for one user. It
returns typed `ExerciseWorkoutProgress` rows for sessions containing that
exercise, ordered by time (compared in UTC), then workout ID, oldest first.
It neither queries PostgreSQL nor calls an LLM. The caller must obtain
owner-scoped records; rejecting mixed owners does not replace authorization.

Each row reports set count, total repetitions, maximum external weight,
`external_volume_kg_reps = sum(reps * weight_kg)`, and `has_external_load`.
Weights and volumes use Decimal arithmetic. Four `delta_*` fields subtract the
previous supplied session containing the exercise; the first row's deltas are
`None`. Empty histories and sessions without the exercise produce no rows.
The calculation preserves input records and rejects invalid exercise IDs,
mixed owners, duplicate workouts/selected set numbers, mismatched set parents,
and nonpositive reps or negative/nonfinite weights in selected sets.

Zero external weight means no recorded additional load, so reps/sets still
count while external volume is zero and `has_external_load` is false. Body mass,
physical effort, technique, and strength improvements are not inferred. Mixed
weighted/unweighted sets contribute only their recorded external load. Absolute
changes describe the supplied data; no percentage changes or medical conclusions
are calculated.

`app.tools.get_exercise_progress(arguments, *, session, user_id)` exposes this
calculation to the agent. `GetExerciseProgressInput` requires a strict integer
`exercise_id` in 1-2147483647 and accepts optional timezone-aware `start_at` and
`end_at` boundaries. When both are supplied, `start_at < end_at` is required.
Unknown fields, including `user_id`, are rejected.

The SQL query filters the server-selected owner, exercise, and workout start
time: `start_at <= started_at < end_at`. Omitted boundaries are unrestricted.
Matching workouts and their sets are loaded explicitly without per-workout
queries. History is not silently paginated or truncated. Changes compare only
matching sessions within the selected period; no earlier baseline is fetched.

The typed `ExerciseProgressResult` contains `exercise_id`, `status`, and
`sessions: list[ExerciseWorkoutProgress]`:

| Status | Meaning |
| --- | --- |
| `exercise_not_found` | The shared exercise catalog has no such ID; sessions are empty. |
| `no_history` | No matching sessions for this owner and period; sessions are empty. |
| `insufficient_history` | One session is returned with metrics and null changes. |
| `ok` | At least two sessions are available for comparison, without implying improvement. |

The tool uses only SELECTs, disables autoflush through query/serialization, and
does not commit or roll back the caller's transaction. It reuses the existing
Decimal calculation. The agent receives JSON with decimal strings and is
instructed to report returned statistics, explain absent comparisons, and avoid
inventing exercise IDs or treating zero external load as zero effort.
Authentication remains deferred; trusted server code must supply the owner.

```powershell
uv run --locked pytest tests/test_progress.py -q
```

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

Test migrations initialize only disposable schemas. Passing tests do not
initialize the application's schema; run `alembic upgrade head` with
`DATABASE_URL` configured before normal API use, as shown in the setup steps.

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
app/main.py           FastAPI routes and demo owner dependency
app/database.py       PostgreSQL engine, request sessions, and declarative base
app/models.py         Exercise, plan, workout, and set models
app/schemas.py        Public HTTP request and response schemas
app/queries.py        Owner-scoped history query shared by the API and tool
app/tool_schemas.py   Validated agent-tool arguments
app/tools.py          Typed read-only agent tools
app/tool_dispatch.py  Explicit read-only tool allowlist and execution
app/agent.py          Bounded tool-calling workflow with server-selected ownership
app/progress.py       Pure exercise-session metrics and absolute changes
app/llm_config.py     Validated LLM environment settings and masked key
app/llm.py            Configurable OpenAI SDK client and one completion call
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
