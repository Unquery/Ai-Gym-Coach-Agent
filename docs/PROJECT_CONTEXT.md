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
| HTTP and transactions | `app.main`, `app.schemas`, `app.agent_schemas`; request-scoped sessions and server-selected demo identity. |
| Agent and provider | `app.agent`, `app.llm`, `app.llm_config`; bounded runs, typed responses, no implicit cross-request history. |
| Tool authorization and validation | `app.tool_dispatch`, `app.tool_schemas`, `app.tools`; two read tools and pending proposal creation only. |
| Deterministic reads/calculations | `app.queries`, `app.progress`; owner-filtered SQL and Decimal metrics. |
| Workout proposals and consent | `app.workout_proposals`, `app.proposal_store`, `app.workout_execution`; catalog resolution, immutable snapshots, row locks, atomic confirmed writes. |
| Storage | `app.database`, `app.models`, Alembic `0001`–`0005`; eight tables at migration head. Conversation storage supports tool metadata but is not yet connected to chat. |

Complete the API-first, client-independent backend MVP before starting frontend
or mobile application development.

## Development priorities

1. Complete Milestone 6 incrementally: owner-scoped conversation access, safe message ordering, chat persistence, bounded context loading, and multi-turn tests/integration.
2. Add workout planning and plan-retrieval tools; retain explicit human confirmation for AI-proposed workout writes.
3. Add real authentication, deployment role separation, broader security hardening, systematic evaluations, and observability. Existing error handling and safety tests must be preserved.
4. Prepare application Docker deployment and verify the complete backend MVP end to end.
5. Only after backend MVP verification, start frontend development, preferably an Android application using Kotlin and Jetpack Compose.

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
history route and tool share an owner-filtered query in `app.queries`, with
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

Deterministic exercise calculations now live in `app.progress`.
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
`app.workout_proposals`. One explicit
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
Confirmation storage is now a separate backend step in `app.proposal_store`.
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
The store records confirmed consent only; the separate `app.workout_execution` service
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

`POST /agent/chat` now provides the minimal synchronous HTTP adapter for run_agent.
Its strict message-only request trims whitespace, limits length to 1–4000 and
forbids extra fields. Server-side demo identity and request-scoped sessions are
injected; authentication remains planned. Successful JSON uses `type="answer"`
with text or `type="proposal"` with the existing structured human-review result.
Pending/clarification status stays explicit, and Decimal weights serialize as
strings. The route owns one transaction and closes the LLM client before commit;
it returns success only after commit. Settings are loaded lazily. Fixed HTTP
errors handle provider/network/agent/configuration/commit failures without raw
diagnostics. The existing 30-second SDK timeout is not an overall conversation
deadline; automatic retries remain disabled. Chat cannot confirm or execute
workouts, including proposals already confirmed through a trusted backend action.
Conversation persistence and UI remain outside this adapter.

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

Milestone 5 workout logging and explicit human confirmation are complete.
Milestone 6 begins with PostgreSQL conversation persistence: revision `0004`
adds owner-bound `Conversation` records and ordered messages without changing
workout tables. Revision `0005` adds assistant `tool_calls` JSONB and tool-result
`tool_call_id`, with role/content constraints. The model is `ConversationMessage`;
`Message` remains an import alias. Existing text and workout/proposal data are
preserved; downgrade refuses metadata loss. Explicit positive positions preserve
order independently of timestamps. Ownership is inherited
through the conversation and must be checked by future access operations.
The storage schema is not yet used by `/agent/chat`: history/context loading,
structured proposal responses, automatic tool-trace persistence, and multi-turn
LLM integration remain separate tasks. See [DATABASE.md](DATABASE.md#conversation-history).

## Verification status

Implementation and available verification are distinct. The test modules cover
HTTP contracts, PostgreSQL integrity/ownership/transactions, deterministic metrics,
mocked LLM protocol handling, and safe proposal execution. Tests do not make live
provider calls; PostgreSQL cases use disposable schemas and skip explicitly when
`TEST_DATABASE_URL` is absent.

Recorded runs include a 453-test PostgreSQL-enabled full suite at the HTTP decision
stage, 167 targeted agent/chat/execution/tool tests after the final Milestone 5
prompt clarification, and 123 targeted PostgreSQL tests for conversation storage
at commit `ad650e5`. These are historical results at their respective snapshots,
not a claim that the full suite was rerun at the current head.

The `0005` storage update passed 33 conversation/migration tests and 104 related
persistence/proposal/execution tests (137 total, no skips). These runs used
disposable PostgreSQL schemas; checks include metadata/model agreement, legacy
data preservation, JSONB round-tripping, and downgrade refusal when metadata exists.
The demo schema was not migrated, and no provider requests were made.

Separately authorized synthetic Groq scenarios verified history retrieval,
deterministic exercise progress, and HTTP workout proposal creation. The successful
logging proposal was separately approved and confirmed through HTTP; a fresh
database read verified the saved workout and ordered exact set values. Confirmation
made no LLM request. Earlier live extraction failures motivated the existing prompt
clarifications; successful scenarios do not guarantee arbitrary extraction fidelity.

Conversation models/migration are PostgreSQL-tested, but multi-turn chat is neither
implemented nor live-verified. There is no authentication or full application
deployment verification. The earlier documentation audit did not rerun application
tests or live provider calls; the later storage checks are recorded above.

## Core boundaries
- Database and deterministic fitness calculations are authoritative; LLM interprets requests and selects narrow tools.
- A write operation (including workout logging/editing) needs explicit user confirmation.
- Start with synthetic fixtures; real personal logs are opt-in and private.
- This is general training support, not medical advice.

## Learning goals for interview
Explain request flow, function/tool calling, agent loop, data model, permission boundaries, validation, unit/integration tests, and evaluation of agent behaviors.

## Out of scope for MVP
Nutrition, wearable integration, medical advice, advanced periodization, social features, a complex mobile app, and multi-agent architecture.
