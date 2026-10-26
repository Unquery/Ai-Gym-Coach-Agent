# Roadmap

- [x] Phase 0 (Milestone 1) — Confirm MVP, Python/uv environment, minimal FastAPI health endpoint, README, and pytest setup.
- [x] Phase 1 (Milestone 2) — Define workout/exercise/set/plan schema and PostgreSQL persistence; add Alembic migrations, synthetic seed, and relationship tests.
- [x] Phase 2 (Milestone 3) — Exercise catalog reads/creation, workout/set creation, workout details and paginated history; owner filtering, input validation, and PostgreSQL-backed API tests.
- [ ] Phase 3 (Milestone 4) — Finish the AI agent with provider-independent LLM integration, read-only tools, and a bounded agent loop.
- [ ] Phase 4 — Deterministic workout statistics and progress analysis, with tested Python/SQL calculations exposed through read tools.
- [ ] Phase 5 — Workout planning and safe write tools with explicit confirmation, validation, and user-scoped access.
- [ ] Phase 6 — Authentication, security hardening, error handling, agent evaluations, mocked-tool tests, and observability.
- [ ] Phase 7 — Docker deployment and full backend MVP verification; finalize README, architecture diagram, demo, and interview materials.
- [ ] Phase 8 (after backend MVP verification) — Start frontend development; prefer an Android application using Kotlin and Jetpack Compose.

Complete and verify the entire backend MVP before starting any frontend or
mobile application development. Keep the backend API-first and client-independent;
agent and database layers must not contain frontend-specific logic. Ownership,
validation, transaction safety, and error handling remain requirements throughout
development; Phase 6 extends and verifies them.

Milestone 4 foundations are implemented: the typed `get_recent_workouts` tool,
the configurable OpenAI-compatible LLM client, and the first bounded read-only
tool-calling workflow with explicit dispatch and mocked LLM tests.

The first pure exercise-progress calculation is implemented: per-session metrics
and absolute changes. The read-only `get_exercise_progress` tool exposes it through
owner-scoped database retrieval, optional time bounds, and explicit missing-data
statuses. It is registered in the bounded agent workflow with mocked LLM tests.

Deterministic progress statistics are outside the completed Milestone 3 API
scope. Implement and test each calculation before exposing its corresponding
progress-analysis tool to the LLM.

## Definition of backend MVP done

- A reviewer can run the app following README instructions.
- At least 3 realistic end-to-end scenarios work against seeded data.
- The agent uses real database-backed tools and does not invent measurements.
- Write actions require confirmation, and failing requests surface clear errors.
- Core calculations and agent/tool behavior have reproducible automated tests.
- Authentication and user isolation protect workout data; safety and failure paths are covered by tests and agent evaluations.
- The complete backend runs through the documented Docker deployment and passes end-to-end verification without a frontend.
- Demo and architecture can be explained clearly in 3–5 minutes.
