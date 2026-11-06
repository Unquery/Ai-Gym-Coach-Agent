# Roadmap

**Milestones 1–8 are complete for the verified scope below.** Milestone 8 covers
local deployment preparation, not a live deployment. The next work is
the remaining backend MVP priorities. This list replaces the
earlier zero-based phase numbering, which mixed completed work with future scope.

## Milestone status

| Milestone | Status | Implemented scope and available verification |
| --- | --- | --- |
| 1 — Application foundation | Complete | Python/uv, locked dependencies, FastAPI/Uvicorn, `GET /health`; HTTP contract tests and recorded local HTTP smoke verification. |
| 2 — PostgreSQL persistence | Complete | Workout/catalog models, Alembic `0001`, database-only Docker Compose, restricted app role, synthetic seed; PostgreSQL persistence, constraints, and migration tests. |
| 3 — Workout HTTP APIs | Complete | Exercise reads/creation, workout/set creation, workout details/history, validation and owner filtering; PostgreSQL-backed API tests and recorded local HTTP verification. |
| 4 — LLM and read-only agent | Complete | Configurable OpenAI-compatible client, bounded tool-calling loop, `get_recent_workouts`, and deterministic `get_exercise_progress`; mocked LLM/PostgreSQL tests and recorded synthetic Groq history/progress scenarios. |
| 5 — Confirmed workout logging | Complete | Catalog-validated proposals, PostgreSQL lifecycle, atomic/idempotent workout execution, chat and confirm/reject HTTP routes; mocked-provider PostgreSQL workflow tests and recorded live Groq logging followed by separate human HTTP confirmation. |
| 6 — Conversation Memory & Multi-Turn AI Agent | Complete | Owner-scoped transactional chat, bounded memory, versioned draft append/edit/remove, saved-draft proposals and durable HTTP retries; migrations `0004`–`0008`. Verified by offline PostgreSQL tests and separately approved real HTTP/Groq scenarios followed by human confirmation. |
| 7 — Agent Reliability & Evaluation | Complete (offline scope) | Safe provider/tool failures, bounded-history checks, useful argument feedback, refreshed draft state, compact context, sanitized reports/CLI and a reviewed nine-scenario baseline. Mocked-provider/PostgreSQL verification; live-model quality and production observability remain unverified. |
| 8 — Backend Deployment & Production Readiness | Complete (local preparation) | Validated settings/secrets, lightweight non-root image, transactional locked release migrations, restricted runtime role, single-operator bearer authentication, CORS/Host/input limits, readiness and JSON logs; production Compose and Render/Neon configuration. No cloud deployment or live-provider verification. |

The 2026-10-31 closure audit passed all 772 tests without skips or warnings.
"Live-verified" refers only to explicitly
authorized synthetic scenarios; it does not establish general model reliability
or production readiness.
See [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md#verification-status) for evidence and limits.

## Milestone 6 completion checklist

- [x] Add `Conversation` and ordered `Message` models, relationships, database constraints, migration `0004`, and PostgreSQL preservation tests.
- [x] Extend storage with `ConversationMessage` (`Message` alias), assistant tool calls/tool results, migration `0005`, and legacy-data/downgrade protection tests.
- [x] Add owner-scoped conversation/message access and concurrency-safe position allocation.
- [x] Extend the chat contract with conversation IDs and persist complete turns transactionally.
- [x] Load bounded text history; keep historical proposal JSON as context, never proof of consent. Chat does not replay/store its intermediate tool protocol.
- [x] Add versioned draft services and allowlisted append/edit/remove tools; clarify ambiguous targets and preserve unknown versus zero load.
- [x] Convert the current saved draft into an immutable pending proposal without model-supplied sets; retain separate human confirmation.
- [x] Add durable owner/endpoint-scoped HTTP idempotency and atomic response/turn/draft/proposal persistence.
- [x] Add mocked-LLM/PostgreSQL tests for isolation, ordering, concurrent changes, rollback, retries and HITL safety.
- [x] Complete approved synthetic real HTTP/Groq verification and separately approved confirmation; verify PostgreSQL contents and confirmation replay.

Completion covers the agreed backend memory/draft workflow. Production readiness
requires the remaining security and deployment work below.
Empty drafts are initialized by trusted backend services/test setup; there is no
draft-creation HTTP endpoint or automatic creation. Drafts remain independent of
immutable proposals after submission. Streaming, automatic tool-trace persistence,
and a public draft-management API are not part of the verified scope.
No vector database, embeddings, or RAG are included. Migrations `0004`–`0008`
were verified in isolated schemas; no demo-database upgrade is implied.

## Milestone 7 completion checklist

- [x] Validate provider protocol and reject unsafe/incomplete responses before dispatch.
- [x] Recover only from known pre-execution tool errors within existing limits; preserve transactional rollback after execution failures.
- [x] Verify bounded long-conversation context and references; clarify missing context instead of guessing.
- [x] Validate invalid/incomplete/inconsistent arguments and return bounded, sanitized feedback.
- [x] Refresh draft context after mutations; preserve stable IDs/order, stale-version protection, idempotency and separate human confirmation.
- [x] Reduce redundant context without dropping unknown-versus-zero semantics or safety instructions.
- [x] Provide deterministic scenarios, negative controls, sanitized reports and a reviewed regression baseline with CLI exit codes.
- [x] Complete focused safety/integrity verification and synchronize milestone documentation.

Closure on 2026-11-04 verified 553 focused regression cases plus two new long-history
controls, with a 48-test targeted rerun after final fixes. The offline CLI passed
nine scenarios and matched baseline (15/15
correct tools, zero safety violations). No full-suite rerun or live provider calls.
This scope excludes live-model language-quality guarantees, overall agent deadlines,
production request/draft limits, runtime observability and production readiness.

## Milestone 8 completion checklist

- [x] Validate production configuration, disable demo identity and separate runtime/release secrets.
- [x] Build a locked multi-stage non-root production image with a minimal build context.
- [x] Run guarded transactional Alembic upgrades as an explicit release job; verify repeatability, rollback and runtime DDL restrictions.
- [x] Require single-operator bearer authentication, preserve owner checks and harden CORS/Host/input/error boundaries.
- [x] Add migration-aware readiness, role checks and sanitized structured logging.
- [x] Prepare manual Render Free + Neon configuration, release/recovery instructions and separate environment templates without deploying.

The 2026-11-07 audit passed 403 focused tests with no skips, and the offline CLI
matched the nine-scenario baseline. The image built and passed offline non-root
runtime checks. Disposable PostgreSQL role checks confirmed runtime DML, denied
DDL/revision writes and rejected elevated membership/bypass-RLS. External cloud
TLS/routing, provider grants, restore procedures and live requests remain unverified.
Free-tier hosting and single-operator access do not establish public multi-user readiness.

## Remaining backend priorities

Workout planning and plan-retrieval tools remain planned despite the existing
plan tables. Exercise-name lookup for progress and automatic relative-date context
are not implemented. Public multi-user authentication, broader security
hardening, live-model evaluations, overall agent deadlines, persisted-draft bounds
and external metrics/alerting remain future work;
existing validation, owner filtering, and mocked safety tests already exist.

Docker Compose defaults to PostgreSQL and now offers an application profile;
production containerization/configuration are prepared. Cloud deployment,
complete backend end-to-end verification, and final portfolio/demo materials
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
