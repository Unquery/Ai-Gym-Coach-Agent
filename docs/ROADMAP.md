# Roadmap

- [x] Phase 0 (Milestone 1) — Confirm MVP, Python/uv environment, minimal FastAPI health endpoint, README, and pytest setup.
- [x] Phase 1 (Milestone 2) — Define workout/exercise/set/plan schema and PostgreSQL persistence; add Alembic migrations, synthetic seed, and relationship tests.
- [ ] Phase 2 — CRUD and stats APIs; deterministic progress calculations and tests.
- [ ] Phase 3 — LLM tool calling with read tools and a clear agent loop.
- [ ] Phase 4 — Write tools with explicit confirmation, validation, and user-scoped access.
- [ ] Phase 5 — Agent evaluations, mocked-tool tests, failure handling, observability.
- [ ] Phase 6 — Dockerized demo, optional simple interface, README, architecture diagram, interview prep, CV bullets.

## Definition of MVP done
- A reviewer can run the app following README instructions.
- At least 3 realistic end-to-end scenarios work against seeded data.
- The agent uses real database-backed tools and does not invent measurements.
- Write actions require confirmation, and failing requests surface clear errors.
- Core calculations and agent/tool behavior have reproducible automated tests.
- Demo and architecture can be explained clearly in 3–5 minutes.
