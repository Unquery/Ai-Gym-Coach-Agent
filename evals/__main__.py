"""Offline evaluations: uv run --env-file .env --locked python -m evals

Requires TEST_DATABASE_URL; LLM credentials are never loaded or used.
Exit codes: 0 success, 1 failed scenario/regression, 2 configuration/infrastructure
or incompatible baseline, 130 interrupted. Uses disposable PostgreSQL schemas.
"""

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

from evals.live import disposable_engine
from evals.reports import build_report, compare_reports, format_report, report_path, write_report
from evals.runner import EvaluationResult, run_scenario
from evals.scenarios import SCENARIOS

BUNDLED_BASELINE = Path(__file__).with_name("baseline.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", choices=[s.name for s in SCENARIOS],
                        help="Run selected scenarios; default: all")
    parser.add_argument("--list", action="store_true", help="List scenarios without accessing the database")
    parser.add_argument("--report", default=".local/agent_eval_offline.json")
    parser.add_argument("--baseline", type=Path,
                        help="Compare a compatible JSON report; full runs default to evals/baseline.json")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(s.name for s in SCENARIOS))
        return 0
    names = args.scenario or [s.name for s in SCENARIOS]
    if len(names) != len(set(names)):
        parser.error("Select each scenario only once")
    selected = [next(s for s in SCENARIOS if s.name == name) for name in names]
    baseline_path = args.baseline or (BUNDLED_BASELINE if not args.scenario else None)
    try:
        destination = report_path(args.report)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path else None
    except (OSError, ValueError) as exc:
        print(f"Invalid report path or baseline ({type(exc).__name__})")
        return 2
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        print("Set TEST_DATABASE_URL; use --env-file .env when running through uv. DATABASE_URL is never used.")
        return 2
    results: list[EvaluationResult] = []
    stop_reason = None
    exit_code = 0
    try:
        for scenario in selected:
            with disposable_engine(url) as engine:
                results.append(run_scenario(scenario, engine=engine))  # Always mocked, no live option.
    except KeyboardInterrupt:
        stop_reason, exit_code = "interrupted", 130
    except Exception as exc:
        # Never print database URLs, provider bodies, messages or exception text.
        stop_reason, exit_code = f"runner_error:{type(exc).__name__}", 2
    report = build_report(results, selected, stop_reason=stop_reason)
    if not exit_code and (not report["complete"] or not all(r.task_success for r in results)):
        exit_code = 1
    print(format_report(report))
    try:
        write_report(report, destination)
    except (OSError, ValueError) as exc:
        print(f"Could not save report ({type(exc).__name__})")
        return 130 if exit_code == 130 else 2
    print(f"Report: {destination}")
    if baseline_path and not stop_reason:
        try:
            comparison = compare_reports(baseline, report)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"Comparison unavailable ({type(exc).__name__}); check completeness, mode and suite")
            return 2
        print("Regressions: " + ", ".join(comparison["regressions"]) if comparison["regression"]
              else "Comparison: no regressions")
        if comparison["regression"]:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
