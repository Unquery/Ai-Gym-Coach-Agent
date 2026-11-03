"""Offline CLI controls; the real schema/HTTP runner already has integration tests."""

from contextlib import nullcontext
import json
from unittest.mock import Mock

import pytest

from evals.__main__ import main
from evals.reports import build_report
from evals.runner import EvaluationResult
from evals.scenarios import SCENARIOS


def test_list_and_missing_configuration_never_load_llm_or_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    forbidden = Mock(side_effect=AssertionError("No database or LLM access expected"))
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr("evals.__main__.disposable_engine", forbidden)
    monkeypatch.setattr("evals.live.load_llm_settings", forbidden)
    assert main(["--list"]) == 0
    assert SCENARIOS[0].name in capsys.readouterr().out
    assert main([]) == 2
    assert "TEST_DATABASE_URL" in capsys.readouterr().out
    forbidden.assert_not_called()


@pytest.mark.parametrize("outcome", ["pass", "fail", "error", "incompatible_baseline", "report_error"])
def test_cli_exit_codes_and_sanitized_partial_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str], outcome: str,
) -> None:
    scenario = SCENARIOS[0]
    result = EvaluationResult(scenario.name, 1, 1, 0, (), ())
    if outcome == "fail":
        result = EvaluationResult(scenario.name, 0, 1, 0, ("turn_1:tool_calls",), ())
    run = Mock(return_value=result)
    if outcome == "error":
        run.side_effect = RuntimeError("synthetic-sensitive-detail")
    saved: list[dict] = []

    def save(report: dict, path: object) -> None:
        saved.append(report)
        if outcome == "report_error":
            raise OSError("synthetic-sensitive-detail")

    monkeypatch.setenv("TEST_DATABASE_URL", "synthetic-not-used")
    monkeypatch.setattr("evals.__main__.disposable_engine", lambda url: nullcontext(None))
    monkeypatch.setattr("evals.__main__.run_scenario", run)
    monkeypatch.setattr("evals.__main__.write_report", save)
    forbidden = Mock(side_effect=AssertionError("Offline must not load LLM credentials"))
    monkeypatch.setattr("evals.live.load_llm_settings", forbidden)
    args = ["--scenario", scenario.name]
    if outcome in {"pass", "fail", "incompatible_baseline"}:
        baseline = build_report([EvaluationResult(scenario.name, 1, 1, 0, (), ())], [scenario])
        if outcome == "incompatible_baseline":
            baseline["suite_id"] = "changed"
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        args.extend(["--baseline", str(path)])
    assert main(args) == {"pass": 0, "fail": 1, "error": 2, "incompatible_baseline": 2, "report_error": 2}[outcome]
    assert saved[0]["complete"] is (outcome != "error")
    assert saved[0]["provider_requests"] == 0
    assert "synthetic-sensitive-detail" not in capsys.readouterr().out + json.dumps(saved)
    forbidden.assert_not_called()
