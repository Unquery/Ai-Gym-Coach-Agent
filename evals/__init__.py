"""Small deterministic agent evaluations, separate from production runtime.

Run offline: uv run --env-file .env --locked python -m evals
Writes .local/agent_eval_offline.json; --scenario selects cases, --list lists them,
--report selects an ignored .local destination, --baseline compares a prior run.
Full CLI runs automatically compare evals/baseline.json, a reviewed synthetic
regression input: nine scenarios, 15/15 correct calls, all tasks successful,
zero safety violations and one blocked confirmation attempt. Subsets compare
only when --baseline is explicit. Offline token counts are zero by construction,
not measured model efficiency. Runtime reports remain ignored in .local/.
When changing scenarios, first review the scenario diff and run the changed
cases. Then run all cases with --baseline pointing to a compatible reviewed
report (or accept exit 2 for the old suite), inspect the saved complete report,
and deliberately update the baseline's fingerprint and expected metrics.
Never refresh the baseline just to silence a regression; retain negative controls.
Exit codes: 0 success, 1 failed scenario/regression, 2 config/infrastructure or
incompatible baseline, 130 interrupted. Requires TEST_DATABASE_URL; no LLM key.
Regression tests: uv run --env-file .env --locked pytest tests/evals -q -s
Optional pytest JSON report: set AGENT_EVAL_REPORT to an ignored .local path first.
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
pending proposal, and exact previous user/assistant context. Failed checks include
bounded expected/actual tool/state diagnostics with unknown strings redacted;
no raw prompts, replies, credentials or provider error bodies. Safety violations
count failed invariants per turn (workout writes, changed consent, unsafe draft
mutation, unexpected proposal, invalid/changed review snapshot), not model intent.
The runner uses FastAPI TestClient/ASGI rather than a listening TCP server.

Extend SCENARIOS with explicit mock decisions and separate expected outcomes.
Keep cases small enough for the existing agent/history limits. Regression tests
and the offline CLI provide disposable PostgreSQL schemas; demo tables are unused.
"""
