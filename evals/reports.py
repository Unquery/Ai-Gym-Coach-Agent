"""Versioned evaluation reports and offline regression comparison.

Compare: uv run --locked python -m evals.reports .local/before.json .local/after.json
Exit codes: 0 no regression, 1 regression, 2 incomplete/incompatible/invalid input.
Reports include allowlisted expected/actual diagnostics, never prompts or replies.
"""

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from evals.runner import EvaluationResult, summarize
from evals.scenarios import Scenario


def report_path(destination: str | Path) -> Path:
    path = Path(destination).resolve()
    local = (Path(__file__).resolve().parents[1] / ".local").resolve()
    if not path.is_relative_to(local) or path == local:
        raise ValueError("Evaluation reports must be files inside the ignored .local directory")
    return path


def write_report(report: dict, destination: str | Path) -> None:
    path = report_path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_report(
    results: Sequence[EvaluationResult], scenarios: Sequence[Scenario], *,
    mode: str = "deterministic_mock_http", model: str | None = None,
    provider_requests: int = 0, usage: dict[str, int] | None = None,
    usage_responses: int = 0, stop_reason: str | None = None,
) -> dict:
    report = summarize(results, expected_scenarios=len(scenarios))
    expected_names = {s.name for s in scenarios}
    report["complete"] &= (
        {r.name for r in results} == expected_names and len(expected_names) == len(scenarios)
        and stop_reason is None
    )
    script = json.dumps([asdict(s) for s in sorted(scenarios, key=lambda s: s.name)],
                        sort_keys=True, ensure_ascii=False, default=str)
    offline = mode == "deterministic_mock_http"
    usage_complete = offline or (provider_requests > 0 and usage_responses == provider_requests)
    reported = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    report.update(
        schema_version=1, suite_id=hashlib.sha256(script.encode()).hexdigest(), mode=mode, model=model,
        provider_requests=provider_requests, stop_reason=stop_reason,
        token_usage={"source": "offline" if offline else "provider", "complete": usage_complete,
                     "responses_with_usage": usage_responses, "reported": reported,
                     **{key: value if usage_complete else None for key, value in reported.items()}},
    )
    return report


def format_report(report: dict) -> str:
    def rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    usage = report["token_usage"]
    tokens = usage["total_tokens"] if usage["complete"] else "unknown (partial usage)"
    details: list[str] = []
    for result in report["results"]:
        if result["task_success"]:
            continue
        details.append(f"FAIL {result['name']}: " + ", ".join((*result["failures"], *result["safety_violations"])))
        for detail in result.get("diagnostics", ()):
            details.append(
                f"  turn {detail.get('turn', '-')}, {detail['reason']}: "
                f"expected={json.dumps(detail['expected'], ensure_ascii=False)} "
                f"actual={json.dumps(detail['actual'], ensure_ascii=False)}"
            )
    return (
        f"Agent evaluation [{report['mode']}]: {report['scenarios_run']}/{report['scenarios_expected']} scenarios; "
        f"complete={report['complete']}\n"
        f"Task success: {rate(report['task_success_rate'])}; tool accuracy: {rate(report['tool_accuracy'])}; "
        f"safety violations: {report['safety_violations']}; blocked attempts: {report['blocked_tool_attempts']}\n"
        f"Provider requests: {report['provider_requests']}; total tokens: {tokens}"
        + (f"\nStop reason: {report['stop_reason']}" if report.get("stop_reason") else "")
        + ("\n" + "\n".join(details) if details else "")
    )


def compare_reports(baseline: dict, candidate: dict) -> dict:
    for report in (baseline, candidate):
        if not isinstance(report, dict):
            raise ValueError("Report must be a JSON object")
        results = report.get("results", [])
        if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
            raise ValueError("Report results must be a list of objects")
        names = [r["name"] for r in results]
        if any(not isinstance(name, str) for name in names):
            raise ValueError("Scenario names must be strings")
        if (report.get("schema_version") != 1 or report.get("complete") is not True
                or not results or len(names) != len(set(names))
                or len(results) != report.get("scenarios_expected")
                or len(results) != report.get("scenarios_run")):
            raise ValueError("Comparison requires complete, nonempty version-1 reports with unique scenarios")
        checked = []
        for result in results:
            counts = [result[key] for key in ("correct_tools", "tool_slots", "blocked_tool_attempts")]
            labels = [result[key] for key in ("failures", "safety_violations")]
            if (any(type(value) is not int or value < 0 for value in counts)
                    or counts[0] > counts[1]
                    or any(not isinstance(items, list | tuple) or any(not isinstance(s, str) for s in items)
                           for items in labels)):
                raise ValueError("Invalid per-scenario metrics")
            parsed = EvaluationResult(result["name"], *counts, tuple(labels[0]), tuple(labels[1]))
            if type(result["task_success"]) is not bool or result["task_success"] != parsed.task_success:
                raise ValueError("Task success disagrees with failures/safety violations")
            checked.append(parsed)
        recalculated = summarize(checked, expected_scenarios=len(results))
        if any(report[key] != recalculated[key] for key in (
            "task_success_rate", "tool_accuracy", "safety_violations", "blocked_tool_attempts",
        )):
            raise ValueError("Aggregate metrics disagree with per-scenario results")
    if (baseline["mode"] != candidate["mode"] or baseline["suite_id"] != candidate["suite_id"]
            or {r["name"] for r in baseline["results"]} != {r["name"] for r in candidate["results"]}):
        raise ValueError("Compare runs of the same mode and unchanged scenario suite")
    regressions: list[str] = []
    deltas: dict[str, float | int | None] = {}
    for metric in ("task_success_rate", "tool_accuracy", "safety_violations"):
        before, after = baseline[metric], candidate[metric]
        deltas[metric] = after - before if before is not None and after is not None else None
        if before is not None and after is not None:
            if (after > before if metric == "safety_violations" else after < before):
                regressions.append(metric)
        elif before != after:
            regressions.append(f"{metric}:coverage_changed")
    previous = {r["name"]: r for r in baseline["results"]}
    for result in candidate["results"]:
        old = previous[result["name"]]
        if old["task_success"] and not result["task_success"]:
            regressions.append(f"scenario:{result['name']}")
        if set(result["safety_violations"]) - set(old["safety_violations"]):
            regressions.append(f"safety:{result['name']}")
    a, b = baseline["token_usage"]["total_tokens"], candidate["token_usage"]["total_tokens"]
    deltas["total_tokens"] = b - a if a is not None and b is not None else None
    return {"regression": bool(regressions), "regressions": regressions, "deltas": deltas}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    try:
        result = compare_reports(json.loads(args.baseline.read_text(encoding="utf-8")),
                                 json.loads(args.candidate.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Comparison unavailable ({type(exc).__name__}); check report format, completeness and suite")
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return int(result["regression"])


if __name__ == "__main__":
    raise SystemExit(main())
