"""Small deterministic agent evaluations, separate from production runtime.

Run: uv run --env-file .env --locked pytest tests/evals -q -s
Optional JSON report: set AGENT_EVAL_REPORT to an ignored .local path first.
Preview live runner: uv run --env-file .env --locked python -m evals.live
Only --live enables Groq; --scenario selects up to three cases. Default budget:
six requests, 512 completion tokens/call, 32000 request bytes, zero retries.
Compare reports: uv run --locked python -m evals.reports .local/before.json .local/after.json
Comparison requires complete reports with matching mode and scenario fingerprint.
Live and deterministic scores are separate; safe tool-free questions/refusals are
accepted only where declared. Prose quality is not judged automatically.
Add scenarios in evals.scenarios; runner/scorer require no provider requests.

Mocked decisions verify integration and safeguards, not a real model's language
understanding. Tool accuracy compares accepted calls exactly (name/JSON args);
blocked attempts are reported separately and do not inflate accuracy.

Task success requires the expected HTTP response, tool results, ordered draft,
pending proposal, and exact previous user/assistant context. Safety violations
count failed invariants per turn (workout writes, changed consent, unsafe draft
mutation, unexpected proposal, invalid/changed review snapshot), not model intent.
The runner uses FastAPI TestClient/ASGI rather than a listening TCP server.

Extend SCENARIOS with explicit mock decisions and separate expected outcomes.
Keep cases small enough for the existing agent/history limits. Regression tests
provide the disposable PostgreSQL engine; no production database is selected.
"""
