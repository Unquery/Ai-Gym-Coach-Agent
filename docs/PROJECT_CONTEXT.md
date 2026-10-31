# Project Context

## Product
AI Gym Coach Agent: a conversational training-log and progress-analysis assistant, built as a portfolio project for a Junior AI Engineer application.

## MVP user journeys
1. "I'm at the gym. What am I training today?" — planned: suggest a session from a program and recent logs; plan tables exist, but planning tools do not.
2. "I did 3 sets of squats: 12, 10, 10 at 30 kg." — implemented: parse and preview, store a pending proposal, then persist a workout only after a separate explicit confirmation action.
3. "Show my pull-up progress in the past month." — implemented with an explicit exercise ID and optional timezone-aware date bounds: deterministic metrics and missing-data statuses. Exercise-name lookup and automatic relative-date context remain planned.
4. "What did I train last time?" — implemented: retrieve owner-scoped workout metadata; the workout detail API returns performed sets.

## Current architecture
Python 3.12+, FastAPI, synchronous SQLAlchemy 2/psycopg 3, PostgreSQL 17,
Alembic, Pydantic, pytest, and uv. The OpenAI SDK Chat Completions client is
configured for an OpenAI-compatible provider; Groq has recorded live verification,
while local Ollama configuration is documented but not live-verified.
The agent loop is explicit Python; no orchestration framework is installed.
Docker Compose currently runs the database only.

| Boundary | Implementation |
| --- | --- |
| HTTP and transactions | `app.main:app` entry point, `app.api.application`, `app.api.schemas`, `app.workouts.schemas`; request-scoped sessions and server-selected demo identity. |
| Agent and provider | `app.agent.loop`, `app.llm.client`, `app.llm.config`; bounded runs, typed results and server-loaded conversation context. |
| Tool authorization and validation | `app.agent.dispatch`, `app.agent.tool_schemas`, `app.agent.tools`; two reads plus either direct pending proposals or active-draft append/edit/remove/conversion. No confirmation/execution tools. |
| Conversation memory | `app.conversations.store`, `app.conversations.history`; owner-scoped row locks, ordered turns, bounded complete text pairs. |
| Durable HTTP retries | `app.api.idempotency`; owner/endpoint/key binding, raw-JSON fingerprint and atomically cached response. |
| Deterministic reads/calculations | `app.workouts.queries`, `app.workouts.progress`; owner-filtered SQL and Decimal metrics. |
| Workout proposals and consent | `app.workouts.proposals`, `app.workouts.proposal_store`, `app.workouts.execution`; catalog resolution, immutable snapshots, row locks, atomic confirmed writes. |
| Draft state | `app.workouts.drafts`; ordered UUID sets, optimistic versions, active state, parent-then-draft locks and immutable append receipts. |
| Storage | `app.db.session`, `app.db.models`, Alembic `0001`–`0008`; 11 tables at migration head. Chat stores completed text/JSON turns; tool metadata is supported for explicit storage callers. |

Complete the API-first, client-independent backend MVP before starting frontend
or mobile application development.

## Development priorities

Milestones 1–6 are complete for their verified scope. Remaining priorities:

1. Add workout planning and plan-retrieval tools; retain explicit human confirmation for AI-proposed workout writes.
2. Add real authentication, deployment role separation, broader security hardening, systematic evaluations, and observability. Existing error handling and safety tests must be preserved.
3. Prepare application Docker deployment and verify the complete backend MVP end to end.
4. Only after backend MVP verification, start frontend development, preferably an Android application using Kotlin and Jetpack Compose.

Keep frontend-specific behavior in client applications; agent and database layers
remain client-independent. Expose backend capabilities through stable API
contracts. See [ROADMAP.md](ROADMAP.md) for the delivery sequence.

## Implementation history

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
real PostgreSQL tests run in disposable schemas. This was the initial storage
milestone; subsequent statistics and LLM integration are implemented below.
Authentication remains planned.

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
reusing the existing workout and set response fields. Sets are ordered by global
`position` first (nulls last), then exercise ID and set number for legacy/manual
sets. Position is internal and is not included in `WorkoutSetRead`.
Empty workouts return an empty list. `selectinload`
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

Milestone 4 is complete: the configurable LLM client and bounded read-only agent
include `get_recent_workouts` and `get_exercise_progress` with deterministic
statistics. `GetRecentWorkoutsInput` validates strict integer pagination with the same
defaults/bounds as the API and rejects unknown fields. Server code supplies the
session and owner separately; the tool returns `list[WorkoutRead]`. The HTTP
history route and tool share an owner-filtered query in `app.workouts.queries`, with
autoflush disabled so reads cannot flush pending changes. The tool does not
manage transactions. Authentication remains planned.
The implemented progress tool reuses tested calculations, described below.

The first LLM integration uses the official OpenAI Python SDK and synchronous
Chat Completions. GroqCloud is the initial provider; `LLM_BASE_URL` defaults to
`https://api.groq.com/openai/v1`, while `LLM_MODEL` and `LLM_API_KEY` are required.
Settings are loaded explicitly, with `SecretStr` masking the key. Local Ollama
uses its compatible `/v1/` URL, an installed model ID, and a dummy `ollama` key.
No provider subclasses or provider branches exist in workout tools. The client
has a 30-second HTTP timeout and no automatic retries; errors propagate. A single
call returns the full typed response, including tool calls, without executing
tools. Clients are closed by their callers. Tests use a mocked HTTP transport
with synthetic keys and responses; no live LLM calls are made automatically.

The first read-only workflow in `app.agent` connects the LLM client to the
existing tool through the user-written `execute_read_only_tool` dispatcher.
Its explicit allowlist contains `get_recent_workouts` and `get_exercise_progress`; typed tool metadata
reuses the Pydantic input schema. Python validates arguments before reads and
supplies the server-selected owner/session outside model arguments. Assistant
tool calls and JSON results are linked by `tool_call_id`. Invalid arguments and
unknown names return fixed error codes without raw inputs or diagnostics.
Each agent run allows at most three LLM requests and two tool requests,
including invalid/unknown attempts. Invalid call IDs/types, incomplete responses,
and exhausted budgets raise `AgentError`; database failures stop with a generic
error instead of leaking diagnostics to the model. SDK errors propagate.
The caller owns client/session lifetime and transactions. This read-only entry
cannot create proposals or workouts; HTTP chat uses the separate `run_agent`
entry described below. Mocked LLM tests include a real PostgreSQL ownership
check in a disposable schema.

Deterministic exercise calculations now live in `app.workouts.progress`.
`analyze_exercise_progress(exercise_id, workouts)` accepts already loaded
`WorkoutDetailRead` records for one owner and returns typed session metrics:
set/repetition counts, maximum external weight, Decimal external volume in
kg * reps, and an external-load flag. Results are ordered by UTC instant and
workout ID. Absolute changes compare adjacent supplied sessions containing the
exercise; first-session changes are None and missing exercise sessions are skipped.
Zero external load preserves reps/sets and does not measure bodyweight effort.
The function rejects inconsistent identities/duplicates and invalid selected
set values without mutating inputs. The calculation performs no SQL or LLM calls,
and numeric changes alone do not establish strength improvement.

`get_exercise_progress` now retrieves owner-scoped exercise history and reuses
this calculation. Its Pydantic input requires a strict positive exercise ID and
optional aware `start_at`/`end_at` bounds with `start_at < end_at`. SQL filters
workout start times in the half-open interval [start_at, end_at); absent bounds
are unrestricted and history is not silently truncated. Changes use only the
selected period. `ExerciseProgressResult` returns an exercise ID, typed session
metrics, and one of `exercise_not_found`, `no_history`, `insufficient_history`
(one session), or `ok` (at least two). The shared catalog is separate from
user-scoped performed history. Queries and serialization disable autoflush;
the tool does not manage transactions. The agent serializes the structured
result and its prompt requires returned statistics and honest missing-data
responses, without inferring bodyweight effort or strength improvement.

The standalone workout logging proposal step remains available in
`app.workouts.proposals`. One explicit
LLM call extracts a nonempty ordered set list; required nullable reps/external
weights distinguish missing values from explicit zero, with existing numeric
validation limits. The model is instructed to preserve source names and leave
missing values unknown. Python matches the shared catalog using outer trim and casefold, rejects
unknown/ambiguous names, and assigns set numbers per resolved exercise while
preserving global proposal order. Catalog reads disable autoflush.

The typed result has server-selected `needs_clarification` or
`awaiting_confirmation` status, `requires_confirmation=True`, proposed sets,
and issue codes with zero-based set indices. No model-selected function is
executed; invalid/extra arguments, write-tool calls, truncated responses, and
multiple calls fail safely before SQL. Assistant prose cannot claim persistence
through this result. The function never writes or manages transactions.
Pydantic does not establish extraction fidelity; human review remains required.
Confirmation storage is now a separate backend step in `app.workouts.proposal_store`.
Revision `0002` adds `workout_proposals`: server-generated UUID v4, server-selected
owner, immutable typed set snapshot in JSONB, state, and timestamps. Only complete
proposals with no issues can become pending; payloads are revalidated before SQL.
The lifetime is 15 minutes, measured using database wall-clock time.

An owner-scoped row lock protects pending -> confirmed/rejected/expired
transitions. Time is checked after lock acquisition, including lock waits;
expiration is recorded lazily on access. The low-level decision function rejects
repeated or conflicting terminal decisions; the HTTP layer implements same-action
replay without repeating a transition. Unknown/foreign IDs are indistinguishable. The caller owns a
dedicated session/transaction and commits before reporting success. Core DML and
disabled autoflush prevent store methods from flushing unrelated ORM objects.
These functions never create workouts/sets and are not advertised to the LLM.
The store records confirmed consent only; the separate `app.workouts.execution` service
executes it atomically in the caller's transaction. Revision `0003` adds a nullable
unique execution link from proposals to same-owner workouts and nullable global
set positions for backward compatibility. A row lock serializes execution/replay;
the service returns the existing workout on retries. Catalog IDs/names are checked
under shared locks before first execution. Workout, all sets, and the execution
link commit together or roll back together. Linked workouts cannot be deleted
while referenced. IDs, per-exercise numbering, reps, Decimal external weights, and
interleaved set order are preserved. Confirmation TTL limits consent, not later
execution/retries. No executor is exposed to the LLM; human-action HTTP routing
is described below, and authentication remains future work.

Milestone 5 is complete. `app.agent.run_agent` integrates pending proposal creation alongside the two
read tools through the same bounded loop. The separate `run_read_only_agent`
entry retains its strict read-only allowlist and text contract. The new dispatcher
accepts only `WorkoutLoggingDraft` data for `propose_workout_logging`; Pydantic
rejects model-selected ownership, IDs, numbering, or consent. The deterministic
catalog resolver is shared with standalone extraction, avoiding a nested LLM call.
Complete resolved drafts create only an owner-bound pending snapshot. Missing or
ambiguous data return clarification without persistence. A proposal must be the
sole call in a complete batch; both outcomes terminate the loop with a typed
`WorkoutProposalToolResult`, discarding assistant prose and requiring human review.
The caller owns the transaction and reports storage only after commit; failures
roll back. One run creates at most one proposal; separate runs can create new
pending snapshots. Neither confirmation nor workout execution is registered as
an agent tool. Human-action HTTP routing is described below; authentication remains planned.

Milestone 5 added the synchronous `POST /agent/chat` adapter for run_agent.
Its strict message field trims whitespace and limits length to 1–4000;
Milestone 6 adds optional conversation_id, as described below. Server-side demo identity and request-scoped sessions are
injected; authentication remains planned. Successful JSON uses `type="answer"`
with text or `type="proposal"` with the existing structured human-review result.
Pending/clarification status stays explicit, and Decimal weights serialize as
strings. The route owns one transaction and closes the LLM client before commit;
it returns success only after commit. Settings are loaded lazily. Fixed HTTP
errors handle provider/network/agent/configuration/commit failures without raw
diagnostics. The existing 30-second SDK timeout is not an overall conversation
deadline; automatic retries remain disabled. Chat cannot confirm or execute
workouts, including proposals already confirmed through a trusted backend action.
Conversation persistence is now integrated below; UI remains deferred.

Separate `POST /workout-proposals/{proposal_id}/confirm` and `/reject` actions
accept an omitted/empty body and forbid client fields, including snapshot edits
and identity/authorization flags. Both inject the existing server-side demo owner
and request-scoped session; authentication remains planned. They never call the
LLM. A shared HTTP helper holds the owner-scoped row lock and uses the existing
store/executor in one transaction. Confirm atomically commits consent, workout,
all sets and the execution link; rejection creates no workout. Responses follow
commit and contain proposal_id, confirmed/rejected state, and workout_id (null
for rejection). Same-action retries return the existing result without changing
consent timestamps; opposite decisions return 409. The proposal ID scopes replay
protection. Existing confirmed consent can execute after its original deadline.
Missing/foreign proposals return 404; expired proposals return 410 after committing
the expiration transition. Catalog changes return 409; database or stored-payload
errors return controlled 503 responses and roll back the entire action. Invalid
UUIDs/bodies return 422. PostgreSQL API tests cover repeats, conflicting/concurrent
decisions, ownership, expiry, forbidden edits and insert/commit failure recovery.

## Conversation storage (Milestone 6)

Milestone 6 is complete. Revision `0004` adds owner-bound conversations/ordered
messages; `0005` adds tool metadata; `0006` adds draft snapshots; `0007` adds
active/discarded state and append receipts; `0008` adds durable chat request
bindings. Existing workout/proposal data and legacy messages are preserved.
`ConversationMessage` retains the `Message` import alias.

`POST /agent/chat` accepts message plus optional conversation_id and returns the
conversation ID in both tagged response variants. Omitting the ID creates a new
owned conversation. The parent row lock serializes complete user/assistant turns;
success is returned only after the messages and any draft/proposal writes commit.
Assistant proposal/clarification results are stored as JSON text. Intermediate
tool calls/results and system instructions are not automatically stored by chat.

The history query loads at most 20 earlier rows for the trusted owner. Selection
keeps at most five complete text pairs / 20,000 characters; each message must fit
4,000 characters. Oversized/invalid messages and tool traces form barriers rather
than being truncated or replayed. The current message is excluded. History,
including old proposal JSON, is untrusted context, never consent or proof of a
workout. Pairs containing the configured provider key are omitted.

Optional UUID Idempotency-Key binds owner/endpoint/key to canonicalized raw JSON
before trimming/defaults. A short transaction retains the fingerprint even after
execution failure. Execution uses NOWAIT row locking, then conversation locking;
contention returns 409 request_in_progress / Retry-After: 1. Changed input returns
409 idempotency_key_reused. Completed replay rechecks ownership and returns stored
status/body without LLM setup, tools or new messages. The completed response and
all turn changes commit together. Rolled-back attempts may repeat provider calls;
different keys are new requests, not content deduplication.

Keyed chats with an existing active owned draft expose reads plus append, update,
remove and no-argument propose_workout_draft_logging. Other chats retain the direct
proposal workflow. Draft writes reuse validation/ownership/catalog checks, stable
set UUIDs, parent-then-draft locks and versions. Backend context supplies the current
snapshot/version and stable request identity; append keys are derived server-side.
Updates preserve omitted fields; null means unknown and explicit zero means no
added load. Ambiguous targets require clarification. Calls must stand alone; one
successful mutation per request is allowed, with receipt-backed matching append
replay. Clarification/conflict/unavailability blocks further draft writes that turn.

Conversion uses the saved snapshot, not model-supplied sets, and rejects incomplete
or empty data and catalog rebinding. It creates only an immutable pending proposal.
Draft edits after submission cannot change that reviewed snapshot. Confirmation
and execution remain separate trusted human HTTP actions; chat cannot invoke them.
Draft creation is currently a backend service/test setup step, not an HTTP endpoint
or automatic creation. Drafts are not automatically discarded/reset on submission
or confirmation. Streaming and automatic tool-trace persistence remain outside the
milestone's verified scope. See [DATABASE.md](DATABASE.md#conversation-history).

## Verification status

Implementation and available verification are distinct. The test modules cover
HTTP contracts, PostgreSQL integrity/ownership/transactions, deterministic metrics,
mocked LLM protocol handling, and safe proposal execution. Tests do not make live
provider calls; PostgreSQL cases use disposable schemas and skip explicitly when
`TEST_DATABASE_URL` is absent.

The 2026-10-31 closure audit reran all 772 tests at application commit `bd7f032`:
772 passed in 85.87s, with no skips or warnings. The suite covers migrations through
`0008`, model/schema agreement, owner isolation, concurrent locks/versions, durable
HTTP retries, rollback/commit failures, bounded memory, draft operations and HITL.
Only disposable PostgreSQL schemas and mocked providers were used in this run.

Previously authorized synthetic live checks verified history/progress/logging and
multi-turn draft append/edit/removal, ambiguity handling and proposal creation.
An early null-versus-zero extraction error motivated an explicit prompt example;
the recovered scenario re-tested the same phrase successfully. That historical
run is not presented as an uninterrupted success or a general fidelity guarantee.

The final minimal live E2E used the actual FastAPI app under localhost Uvicorn/TCP
HTTP, a fresh synthetic schema/owner, and real Groq responses. Four messages
(append, append, edit, propose) used seven calls: 13,075 prompt + 610 completion =
13,685 tokens. It verified exact tool arguments, context, UUID/order, draft versions,
same-key replay without more provider calls, changed-payload 409s, and no workout
before approval. Separately approved real HTTP confirmation and replay produced
exactly one workout/two reviewed sets; independent read-only PostgreSQL inspection
confirmed the values. Confirmation added no Groq calls/tokens. The closure audit
rechecked that evidence without new provider calls or data writes.

Completion does not mean production readiness: demo identity is not authentication;
deployment roles, app containerization, broader security/evaluation/observability,
planning tools, public draft management and local Ollama live verification remain
future work. Migrations were tested in isolated schemas; no demo upgrade is implied.
History character bounds do not cap the whole prompt or current draft; overall
agent deadlines and production request/draft-size limits remain future hardening.

## Core boundaries
- Database and deterministic fitness calculations are authoritative; LLM interprets requests and selects narrow tools.
- AI workout persistence requires a separate explicit human confirmation of the reviewed proposal; draft preparation/edit requests never grant that consent.
- Start with synthetic fixtures; real personal logs are opt-in and private.
- This is general training support, not medical advice.

## Learning goals for interview
Explain request flow, function/tool calling, agent loop, data model, permission boundaries, validation, unit/integration tests, and evaluation of agent behaviors.

## Out of scope for MVP
Nutrition, wearable integration, medical advice, advanced periodization, social features, a complex mobile app, and multi-agent architecture.
