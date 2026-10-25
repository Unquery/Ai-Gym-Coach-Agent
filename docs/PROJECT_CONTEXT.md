# Project Context

## Product
AI Gym Coach Agent: a conversational training-log and progress-analysis assistant, built as a portfolio project for a Junior AI Engineer application.

## MVP user journeys
1. "I'm at the gym. What am I training today?" — get a proposed session based on program and recent logs.
2. "I did 3 sets of squats: 12, 10, 10 at 30 kg." — parse and preview the entries, ask for confirmation, then store them.
3. "Show my pull-up progress in the past month." — retrieve entries, calculate accurate metrics, summarize trends without fabricating data.
4. "What did I train last time?" — retrieve workout history.

## Initial engineering direction
Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy, Pydantic, pytest, Docker Compose. A tool-calling LLM agent; optional orchestration framework only if needed. Simple API-first demo, with a minimal UI later if time allows.

Milestone 1 uses Python 3.12, uv with a reproducible lockfile, FastAPI/Uvicorn, and
pytest with HTTPX2/TestClient. The application entry point is `app.main:app`;
`GET /health` returns HTTP 200 with `{"status":"ok"}`. PostgreSQL and LLM
integration were deferred to later milestones.

Milestone 2 adds synchronous SQLAlchemy 2 models, psycopg 3, PostgreSQL 17, and
Alembic revision `0001`. Five tables separate a shared exercise catalog, user-owned
plans and ordered plan entries, and performed workouts/sets. Plans are reusable
single-session templates; actual sets are independent historical facts. Owner UUIDs
anticipate authentication without introducing a users service. A composite FK
prevents cross-owner plan attachment. See [DATABASE.md](DATABASE.md) for the schema,
transaction boundaries, and permissions. Docker Compose provides local storage;
real PostgreSQL tests run in disposable schemas. Authentication, statistics,
and LLM integration remain deferred.

Milestone 3 is complete: exercise catalog reads/creation, workout/set creation,
workout details with performed sets, and paginated workout history.
Workout creation accepts optional plan/time fields; the server dependency uses
the existing synthetic demo owner until authentication is implemented. Requests
cannot choose an owner. Plan validation filters by both plan ID and owner; the
composite foreign key remains the final consistency check. Creating a workout
does not create performed sets. This is a local demo mode without authentication.

`POST /workouts/{workout_id}/sets` records one performed set after checking the
workout's owner and the shared exercise's existence. Required typed inputs are
exercise ID, explicit set number, repetitions, and finite nonnegative decimal
external load. Duplicate workout/exercise/set-number combinations return 409;
missing or other owners' workouts and missing exercises return 404. Database
constraint failures are rolled back; 201 is returned only after commit.

`GET /workouts/{workout_id}` returns an owner's workout and its performed sets,
reusing the existing workout and set response fields. Sets are ordered by
exercise ID and set number; empty workouts return an empty list. `selectinload`
loads the collection explicitly in a second SELECT, avoiding serialization-time
queries. Missing or other owners' workouts return 404 after a single SELECT.

`GET /workouts` lists the server-selected owner's history using `WorkoutRead`.
Pagination uses `limit` (default 20, range 1-100) and `offset` (default 0, range
0-2147483647); invalid values return 422. The owner filter and descending
`started_at`/`id` ordering run before pagination in one SELECT. Empty histories
or pages return an empty list. No client-supplied owner selection is exposed.

API resource IDs use the positive PostgreSQL INTEGER range (1-2147483647),
including exercise lookup and optional workout plan input. Exercise creation
rejects NUL characters in Pydantic validation. Invalid values return 422 before
executing SQL instead of surfacing database errors as 500.

Milestone 4 LLM integration has not started. Authentication and deterministic
progress statistics remain planned; statistics require tested calculations
before progress-analysis tools can expose them to the model.

## Core boundaries
- Database and deterministic fitness calculations are authoritative; LLM interprets requests and selects narrow tools.
- A write operation (including workout logging/editing) needs explicit user confirmation.
- Start with synthetic fixtures; real personal logs are opt-in and private.
- This is general training support, not medical advice.

## Learning goals for interview
Explain request flow, function/tool calling, agent loop, data model, permission boundaries, validation, unit/integration tests, and evaluation of agent behaviors.

## Out of scope for MVP
Nutrition, wearable integration, medical advice, advanced periodization, social features, a complex mobile app, and multi-agent architecture.
