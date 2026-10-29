# Roadmap

Current milestone: **6 — Conversation Memory & Multi-Turn AI Agent (in progress)**.
Milestones 1–5 are complete for the scope below. This milestone list replaces the
earlier zero-based phase numbering, which mixed completed work with future scope.

## Milestone status

| Milestone | Status | Implemented scope and available verification |
| --- | --- | --- |
| 1 — Application foundation | Complete | Python/uv, locked dependencies, FastAPI/Uvicorn, `GET /health`; HTTP contract tests and recorded local HTTP smoke verification. |
| 2 — PostgreSQL persistence | Complete | Workout/catalog models, Alembic `0001`, database-only Docker Compose, restricted app role, synthetic seed; PostgreSQL persistence, constraints, and migration tests. |
| 3 — Workout HTTP APIs | Complete | Exercise reads/creation, workout/set creation, workout details/history, validation and owner filtering; PostgreSQL-backed API tests and recorded local HTTP verification. |
| 4 — LLM and read-only agent | Complete | Configurable OpenAI-compatible client, bounded tool-calling loop, `get_recent_workouts`, and deterministic `get_exercise_progress`; mocked LLM/PostgreSQL tests and recorded synthetic Groq history/progress scenarios. |
| 5 — Confirmed workout logging | Complete | Catalog-validated proposals, PostgreSQL lifecycle, atomic/idempotent workout execution, chat and confirm/reject HTTP routes; mocked-provider PostgreSQL workflow tests and recorded live Groq logging followed by separate human HTTP confirmation. |
| 6 — Conversation Memory & Multi-Turn AI Agent | In progress | Conversation/message models and Alembic `0004`–`0005`, including tool metadata storage, are implemented and PostgreSQL-tested. Chat persistence, context loading, and multi-turn integration are not implemented. |

"Tested" refers to recorded runs, including targeted storage tests for `0005`;
it does not imply a full-suite rerun. "Live-verified" refers only to explicitly
authorized synthetic scenarios; it does not establish general model reliability
or production readiness.
See [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md#verification-status) for evidence and limits.

## Milestone 6 remaining work

- [x] Add `Conversation` and ordered `Message` models, relationships, database constraints, migration `0004`, and PostgreSQL preservation tests.
- [x] Extend storage with `ConversationMessage` (`Message` alias), assistant tool calls/tool results, migration `0005`, and legacy-data/downgrade protection tests.
- [ ] Add owner-scoped conversation/message access and concurrency-safe position allocation.
- [ ] Extend the chat contract to identify a conversation and persist turns transactionally.
- [ ] Load bounded conversation history into subsequent agent runs; preserve existing tool-call and consent boundaries.
- [ ] Decide how structured proposal results are represented across turns; message storage supports text and tool metadata but is not yet connected to chat.
- [ ] Add mocked-LLM/PostgreSQL multi-turn tests for isolation, ordering, failures, and confirmation safety.
- [ ] Complete an explicitly authorized end-to-end multi-turn verification before closing the milestone.

Each item is a separate incremental task requiring agreement before implementation.
No vector database, embeddings, or RAG are part of this milestone. The existence
of migrations `0004`–`0005` does not mean they have been applied to an existing
demo database.

## Remaining backend priorities

Workout planning and plan-retrieval tools remain planned despite the existing
plan tables. Exercise-name lookup for progress and automatic relative-date context
are not implemented. Authentication, deployment role separation, broader security
hardening, systematic agent evaluations, and observability remain future work;
existing validation, owner filtering, and mocked safety tests already exist.

Docker Compose currently runs PostgreSQL only. Application containerization,
complete backend deployment verification, and final portfolio/demo materials
remain planned. Complete and verify the API-first, client-independent backend
MVP before frontend/mobile development; the preferred later client remains
Android with Kotlin and Jetpack Compose.

## Definition of backend MVP done

- A reviewer can run the app following README instructions.
- At least 3 realistic end-to-end scenarios work against seeded data.
- The agent uses real database-backed tools and does not invent measurements.
- Write actions require confirmation, and failing requests surface clear errors.
- Core calculations and agent/tool behavior have reproducible automated tests.
- Authentication and user isolation protect workout data; safety and failure paths are covered by tests and agent evaluations.
- The complete backend runs through the documented Docker deployment and passes end-to-end verification without a frontend.
- Demo and architecture can be explained clearly in 3–5 minutes.
