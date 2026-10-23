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
real PostgreSQL tests run in disposable schemas. Workout APIs, authentication,
statistics, and LLM integration remain deferred.

## Core boundaries
- Database and deterministic fitness calculations are authoritative; LLM interprets requests and selects narrow tools.
- A write operation (including workout logging/editing) needs explicit user confirmation.
- Start with synthetic fixtures; real personal logs are opt-in and private.
- This is general training support, not medical advice.

## Learning goals for interview
Explain request flow, function/tool calling, agent loop, data model, permission boundaries, validation, unit/integration tests, and evaluation of agent behaviors.

## Out of scope for MVP
Nutrition, wearable integration, medical advice, advanced periodization, social features, a complex mobile app, and multi-agent architecture.
