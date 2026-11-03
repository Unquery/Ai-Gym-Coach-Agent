"""Focused report controls; no provider or database required."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from evals.reports import build_report, compare_reports, format_report, main, write_report
from evals.runner import EvaluationResult, Observation, score_scenario
from evals.scenarios import SCENARIOS, ToolCall


def baseline() -> dict:
    scenario = SCENARIOS[0]
    return build_report([EvaluationResult(scenario.name, 1, 1, 0, (), ())], [scenario])


def test_comparison_detects_quality_and_safety_regressions() -> None:
    before = baseline()
    assert compare_reports(before, before)["regression"] is False
    result = EvaluationResult(SCENARIOS[0].name, 0, 1, 0, ("tool_calls",), ("unsafe_write",))
    after = build_report([result], [SCENARIOS[0]])
    comparison = compare_reports(before, after)
    assert set(comparison["regressions"]) == {
        "tool_accuracy", "task_success_rate", "safety_violations",
        f"scenario:{SCENARIOS[0].name}", f"safety:{SCENARIOS[0].name}",
    }


@pytest.mark.parametrize("change", ["incomplete", "mode", "suite", "duplicate"])
def test_incompatible_reports_cannot_look_like_a_pass(change: str) -> None:
    before = baseline()
    after = deepcopy(before)
    if change == "incomplete":
        after["complete"] = False
    elif change == "mode":
        after["mode"] = "live_groq_http"
    elif change == "suite":
        after["suite_id"] = "changed"
    else:
        after["results"] *= 2
        after["scenarios_expected"] = after["scenarios_run"] = 2
    with pytest.raises(ValueError):
        compare_reports(before, after)


def test_usage_is_unknown_when_provider_omits_it_and_cached_tokens_are_not_added() -> None:
    result = EvaluationResult(SCENARIOS[0].name, 1, 1, 0, (), ())
    options = dict(mode="live_groq_http", provider_requests=2,
                   usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    partial = build_report([result], [SCENARIOS[0]], usage_responses=1, **options)
    assert partial["token_usage"]["total_tokens"] is None
    assert partial["token_usage"]["reported"]["total_tokens"] == 120
    assert "unknown" in format_report(partial)
    complete = build_report([result], [SCENARIOS[0]], usage_responses=2, **options)
    assert complete["token_usage"]["total_tokens"] == 120


def test_report_identity_checks_expected_names_and_writes_stay_local(tmp_path) -> None:
    result = EvaluationResult("unexpected", 1, 1, 0, (), ())
    assert build_report([result], [SCENARIOS[0]])["complete"] is False
    changed = replace(SCENARIOS[0], turns=())
    assert build_report([], [changed])["suite_id"] != baseline()["suite_id"]
    with pytest.raises(ValueError, match=".local"):
        write_report(baseline(), tmp_path / "report.json")


def test_comparison_cli_exit_codes(tmp_path) -> None:
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(json.dumps(baseline()), encoding="utf-8")
    after.write_text(json.dumps(baseline()), encoding="utf-8")
    assert main([str(before), str(after)]) == 0
    failed = build_report([EvaluationResult(SCENARIOS[0].name, 0, 1, 0, ("tool_calls",), ())], [SCENARIOS[0]])
    after.write_text(json.dumps(failed), encoding="utf-8")
    assert main([str(before), str(after)]) == 1
    failed["complete"] = False
    after.write_text(json.dumps(failed), encoding="utf-8")
    assert main([str(before), str(after)]) == 2


def test_nonfinite_or_inconsistent_metrics_cannot_hide_a_regression() -> None:
    before = baseline()
    after = deepcopy(before)
    after["tool_accuracy"] = float("nan")
    with pytest.raises(ValueError):
        compare_reports(before, after)
    after["tool_accuracy"] = 0.5  # Metrics must agree with the per-scenario counts.
    with pytest.raises(ValueError):
        compare_reports(before, after)


def test_comparison_cli_rejects_nonobject_json(tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")
    assert main([str(invalid), str(invalid)]) == 2


def test_failure_diagnostics_redact_untrusted_strings_and_explain_differences() -> None:
    secret = "synthetic-sensitive-token"
    scenario = SCENARIOS[0]
    observed = Observation(
        (ToolCall("get_recent_workouts", {"limit": 2, secret: secret}), ToolCall(secret, secret)),
        (), (secret,), None, (secret,), 502, secret, False, False, 0,
    )
    result = score_scenario(scenario, [observed])
    report = build_report([result], [scenario])
    output = format_report(report)
    assert secret not in json.dumps(report) and secret not in output
    assert "FAIL recent_workout_tool" in output and "expected=" in output and "actual=" in output
    assert "get_recent_workouts" in output and '"limit": 2' in output
    assert "<redacted>" in output and "<invalid_arguments>" in output
