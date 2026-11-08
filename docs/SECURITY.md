# Backend security boundary

Production currently supports **one trusted operator**, not public registration
or multiple account tokens. All business routes, including the shared catalog,
require `Authorization: Bearer <API_AUTH_TOKEN>`. The token is compared in constant
time and resolves only to the server-configured `API_OWNER_ID`; client-supplied
owner IDs never grant access. Existing owner filters, transaction locks and human
confirmation remain authoritative. Never give this operator token to the LLM or
embed it in a public frontend/mobile build. Future public clients need proper
per-user authentication, token expiry/revocation and a separate authorization model.

Generate a random token with at least 32 characters and a fresh owner UUID. Store
the token in the hosting secret manager and rotate it by replacing the secret and
restarting the single API instance. Do not use real private workout data for a
portfolio demonstration. Restarting revokes the old token. No token is stored in
the database, responses or application logs.

`APP_ENV=production` rejects demo identity, placeholder/short/non-ASCII or whitespace-bearing tokens,
the synthetic owner, insecure database/provider URLs, wildcard hosts/origins and
debug logging. Interactive docs and OpenAPI are disabled. Local synthetic access
requires both development mode and explicit `ENABLE_DEMO_AUTH=true`; local
Compose binds the backend only to loopback. Never publish the development profile.
Runtime and release database URLs require `sslmode=verify-full` and a trusted CA
configuration. Provider URLs require HTTPS without credentials, query or fragment.

TLS terminates at the hosting platform's HTTPS edge; expose no direct HTTP service
to the public internet. Forwarded headers are not trusted by the application.
Configure `ALLOWED_HOSTS` for the exact API hostname plus `localhost,127.0.0.1`
for internal probes. CORS defaults to no allowed origins; explicitly list HTTPS
frontend origins if needed. CORS is browser policy, not authentication.

HTTP bodies are capped at 16 KiB, even without Content-Length. Incomplete/slow
bodies have a total 10-second read deadline, including trickled chunks; timeout
returns 408 and closes the connection before business execution. Chat messages also
retain their 4,000-character schema limit. Validation responses omit supplied
values, unknown field names and exception context. Database/unexpected failures
return generic 503/500 responses. The server admits at most 16 concurrent HTTP
requests; authenticated production traffic is limited to 120 requests per rolling
minute, with a separate 10-request chat budget. Configure these via
`API_REQUESTS_PER_MINUTE` and `CHAT_REQUESTS_PER_MINUTE`; rejected requests return
429 and `Retry-After`. At most two production chats execute concurrently; excess
requests receive 503 before opening a DB session. These conservative in-memory
limits reset on restart. The production entrypoint explicitly uses one worker,
including when `WEB_CONCURRENCY` is present. Do not
scale replicas/workers without an edge/shared rate limiter and budget controls.

Provider messages/tool schemas are limited to 64 KiB of serialized UTF-8 context;
successful decoded response bodies to 128 KiB and generation to 1024 completion
tokens per call. Truncated/oversized results fail without committing the turn.
Provider error/redirect responses close without reading their bodies. Default
provider transport disables redirects and proxy environment settings.
The agent checks a shared 60-second budget between calls, reads and tools; each
new provider operation uses at most the remaining budget or 30 seconds for socket
phase timeouts. Expiration returns 504 and rolls back the turn; no background
thread continues after that response. This is cooperative, not a hard wall-clock
deadline: synchronous DNS/socket/SQL/commit operations can exceed the budget before
control returns. Persisted-draft storage bounds and forcible overall cancellation
remain follow-ups. Bounded turns/tools and zero automatic retries are retained.

`/health` and `/ready` are intentionally unauthenticated, return only status and
never call an LLM. Use a separate restricted runtime DB role; see
[MIGRATIONS.md](MIGRATIONS.md). Production CORS and authentication do not replace
the distinct human confirmation required before an agent proposal is saved.
