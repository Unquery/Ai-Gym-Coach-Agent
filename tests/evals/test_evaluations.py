"""Small workflow benchmark plus negative controls for the scorer."""

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine

from evals.runner import EvaluationResult, Observation, run_scenario, score_scenario, summarize
from evals.scenarios import FIRST_SET, SCENARIOS, Scenario, ToolCall


@pytest.fixture(scope="module")
def evaluation_results() -> Iterator[list[EvaluationResult]]:
    results: list[EvaluationResult] = []
    yield results
    report = summarize(results, expected_scenarios=len(SCENARIOS))
    print("\nAgent evaluation: " + json.dumps(report, ensure_ascii=False))
    if destination := os.environ.get("AGENT_EVAL_REPORT"):
        path = Path(destination).resolve()
        local = (Path(__file__).resolve().parents[2] / ".local").resolve()
        if not path.is_relative_to(local):
            raise ValueError("AGENT_EVAL_REPORT must be inside the ignored .local directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.postgres
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_agent_evaluation(
    scenario: Scenario, postgres_engine: Engine, evaluation_results: list[EvaluationResult],
) -> None:
    result = run_scenario(scenario, engine=postgres_engine)
    evaluation_results.append(result)
    assert result.task_success, result


@pytest.mark.postgres
def test_wrong_arguments_lower_scores(postgres_engine: Engine) -> None:
    # Change only provider output, keeping the independent expected outcome fixed.
    scenario = SCENARIOS[0]
    wrong = replace(scenario.turns[0], mocked_calls=(ToolCall("get_recent_workouts", {"limit": 2}),))
    result = run_scenario(replace(scenario, turns=(wrong,)), engine=postgres_engine)
    report = summarize([result], expected_scenarios=1)
    assert report["tool_accuracy"] == 0
    assert report["task_success_rate"] == 0
    assert report["safety_violations"] == 0
    assert result.failures == ("turn_1:tool_calls",)


@pytest.mark.postgres
def test_guessed_retry_after_clarification_is_reported(postgres_engine: Engine) -> None:
    scenario = SCENARIOS[3]
    turn = scenario.turns[0]
    guessed = ToolCall("update_workout_draft_set", {"set_id": str(FIRST_SET), "changes": {"reps": 10}})
    turn = replace(turn, mocked_calls=(*turn.mocked_calls, guessed))
    result = run_scenario(replace(scenario, turns=(turn,)), engine=postgres_engine)
    assert not result.task_success
    assert result.blocked_tool_attempts == 1
    assert not result.safety_violations  # The loop blocked the guessed mutation.
    assert set(result.failures) == {"turn_1:blocked_calls", "turn_1:tool_statuses"}


def test_unsafe_outcomes_and_missing_turns_cannot_pass() -> None:
    scenario = SCENARIOS[3]  # Ambiguous target must remain unchanged.
    turn = scenario.turns[0]
    unsafe = Observation(
        turn.expected_calls, (), turn.expected_statuses, turn.expected_sets, ("confirmed",),
        200, "answer", True, True, 1, False,
    )
    result = score_scenario(scenario, [unsafe])
    assert not result.task_success
    assert set(result.safety_violations) == {
        "turn_1:write_without_confirmation", "turn_1:consent_changed_without_human_action",
        "turn_1:unsafe_draft_mutation", "turn_1:unexpected_proposal", "turn_1:invalid_review_snapshot",
    }
    missing = score_scenario(scenario, [])
    assert not missing.task_success and missing.tool_slots == 1 and missing.correct_tools == 0
    extra = score_scenario(scenario, [unsafe, unsafe])
    assert not extra.task_success and extra.tool_slots == 2
    assert "turn_2:write_without_confirmation" in extra.safety_violations
    incomplete = summarize([], expected_scenarios=len(SCENARIOS))
    assert incomplete["complete"] is False
    assert incomplete["task_success_rate"] is None and incomplete["tool_accuracy"] is None


def test_duplicate_results_do_not_make_a_complete_report() -> None:
    result = EvaluationResult("repeated", 1, 1, 0, (), ())
    assert summarize([result, result], expected_scenarios=2)["complete"] is False


@pytest.mark.postgres
def test_malformed_arguments_reach_the_real_dispatcher(postgres_engine: Engine) -> None:
    scenario = SCENARIOS[0]
    malformed = ToolCall("get_recent_workouts", "{")
    turn = replace(scenario.turns[0], mocked_calls=(malformed,), expected_calls=(),
                   expected_blocked=(malformed,), expected_statuses=("invalid_arguments",))
    result = run_scenario(replace(scenario, turns=(turn,)), engine=postgres_engine)
    assert result.task_success, result
    assert result.blocked_tool_attempts == 1
