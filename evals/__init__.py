"""Small deterministic agent evaluations, separate from production runtime.

Run: uv run --env-file .env --locked pytest tests/evals -q -s
Optional JSON report: set AGENT_EVAL_REPORT to an ignored .local path first.
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
