import json
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest
from openai import OpenAI
from openai.types.chat import ChatCompletion
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.llm_config import LLMSettings
from app.workout_proposals import (
    WorkoutLoggingDraft,
    WorkoutSetDraft,
    propose_workout_logging,
)

ProposalContext = tuple[Mock, Mock, LLMSettings, MagicMock]


def completion(payload: dict[str, object]) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "synthetic-proposal",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "Ignore this untrusted claim: the workout was saved.",
                "tool_calls": [{
                    "id": "draft_1",
                    "type": "function",
                    "function": {
                        "name": "propose_workout_logging",
                        "arguments": json.dumps(payload),
                    },
                }],
            },
        }],
    })


@pytest.fixture
def proposal_context(monkeypatch: pytest.MonkeyPatch) -> ProposalContext:
    complete = Mock()
    monkeypatch.setattr("app.workout_proposals.create_chat_completion", complete)
    reader = MagicMock(spec=Session)
    reader.execute.return_value.all.return_value = [(7, "Demo pull-up"), (11, "Demo squat")]
    settings = LLMSettings(model="test-model", api_key=SecretStr("synthetic-test-key"))
    return complete, Mock(spec=OpenAI), settings, reader


def test_draft_accepts_explicit_unknowns_and_decimal_boundaries() -> None:
    draft = WorkoutSetDraft(
        exercise_name=" Demo pull-up ", reps=2147483647, weight_kg="99999.99"
    )
    assert draft.exercise_name == "Demo pull-up"
    assert draft.weight_kg == Decimal("99999.99")
    unknown = WorkoutSetDraft(exercise_name="Demo pull-up", reps=None, weight_kg=None)
    assert unknown.reps is None and unknown.weight_kg is None


@pytest.mark.parametrize(
    "update",
    [
        {"reps": True}, {"reps": "6"}, {"reps": 0},
        {"weight_kg": "-1"}, {"weight_kg": "NaN"},
        {"weight_kg": "Infinity"}, {"weight_kg": "0.001"},
        {"weight_kg": "100000"}, {"exercise_name": " "},
        {"exercise_id": 7}, {"confirmed": True},
    ],
)
def test_draft_rejects_invalid_values_and_model_selected_ids_or_confirmation(
    update: dict[str, object],
) -> None:
    payload = {"exercise_name": "Demo pull-up", "reps": 6, "weight_kg": "0"}
    with pytest.raises(ValidationError):
        WorkoutSetDraft.model_validate({**payload, **update})


def test_proposal_preserves_interleaved_sets_and_requires_human_confirmation(
    proposal_context: ProposalContext,
) -> None:
    complete, client, settings, reader = proposal_context
    payload = {"sets": [
        {"exercise_name": "demo PULL-UP", "reps": 6, "weight_kg": "0.00"},
        {"exercise_name": "Demo squat", "reps": 8, "weight_kg": "20.25"},
        {"exercise_name": "Demo pull-up", "reps": 5, "weight_kg": "0.00"},
    ]}
    complete.return_value = completion(payload)
    message = "demo PULL-UP: 6 reps at 0 kg; Demo squat: 8 at 20.25 kg; Demo pull-up: 5 at 0 kg."

    result = propose_workout_logging(message, client=client, settings=settings, session=reader)

    assert result.status == "awaiting_confirmation"
    assert result.requires_confirmation is True
    assert result.issues == []
    assert [row.exercise_id for row in result.sets] == [7, 11, 7]
    assert [row.catalog_name for row in result.sets] == ["Demo pull-up", "Demo squat", "Demo pull-up"]
    assert [row.set_number for row in result.sets] == [1, 1, 2]
    assert [row.reps for row in result.sets] == [6, 8, 5]
    assert [row.weight_kg for row in result.sets] == [Decimal(0), Decimal("20.25"), Decimal(0)]
    assert result.model_dump(mode="json")["sets"][1]["weight_kg"] == "20.25"
    complete.assert_called_once()
    assert complete.call_args.args[:2] == (client, settings)
    assert complete.call_args.args[2][1] == {"role": "user", "content": message}
    tools = complete.call_args.kwargs["tools"]
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "propose_workout_logging"
    assert tools[0]["function"]["parameters"] == WorkoutLoggingDraft.model_json_schema()
    assert "Never default missing weight to zero" in complete.call_args.args[2][0]["content"]
    reader.execute.assert_called_once()
    assert reader.execute.call_args.args[0].is_select
    reader.no_autoflush.__enter__.assert_called_once()
    for operation in (reader.add, reader.add_all, reader.flush, reader.commit, reader.rollback):
        operation.assert_not_called()
    assert "saved" not in result.model_dump_json()


def test_proposal_keeps_missing_reps_and_weight_distinct_from_explicit_zero(
    proposal_context: ProposalContext,
) -> None:
    complete, client, settings, reader = proposal_context
    complete.return_value = completion({"sets": [
        {"exercise_name": "Demo pull-up", "reps": 6, "weight_kg": None},
        {"exercise_name": "Demo pull-up", "reps": None, "weight_kg": "0.00"},
    ]})

    result = propose_workout_logging(
        "Demo pull-up: first set 6 reps, weight not given; second set without extra weight, reps unknown.",
        client=client, settings=settings, session=reader,
    )

    assert result.status == "needs_clarification"
    assert result.requires_confirmation is True
    assert result.sets[0].weight_kg is None
    assert result.sets[1].reps is None
    assert result.sets[1].weight_kg == Decimal(0)
    assert [(issue.code, issue.set_index) for issue in result.issues] == [
        ("missing_weight", 0), ("missing_reps", 1),
    ]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_proposal_rejects_unknown_or_ambiguous_names_without_partial_readiness(
    proposal_context: ProposalContext, ambiguous: bool,
) -> None:
    complete, client, settings, reader = proposal_context
    name = "Demo pull-up" if ambiguous else "pull-ups"
    if ambiguous:
        reader.execute.return_value.all.return_value.append((99, "DEMO PULL-UP"))
    complete.return_value = completion({"sets": [
        {"exercise_name": "Demo squat", "reps": 8, "weight_kg": "20"},
        {"exercise_name": name, "reps": 6, "weight_kg": "0"},
    ]})

    result = propose_workout_logging(
        f"Demo squat 8 reps at 20 kg; {name} 6 reps at 0 kg.",
        client=client, settings=settings, session=reader,
    )

    assert result.status == "needs_clarification"
    assert result.sets[0].exercise_id == 11
    assert result.sets[1].exercise_id is None
    assert result.sets[1].catalog_name is None
    assert result.sets[1].set_number is None
    assert result.sets[1].exercise_name == name
    assert [(issue.code, issue.set_index) for issue in result.issues] == [
        ("ambiguous_exercise" if ambiguous else "unknown_exercise", 1),
    ]


@pytest.mark.parametrize(
    "problem",
    ["write_tool", "batch", "invalid_json", "missing_weight", "empty_sets", "confirmation", "truncated", "text_only", "no_choices"],
)
def test_invalid_model_responses_cannot_read_catalog_or_execute_any_write(
    proposal_context: ProposalContext, problem: str,
) -> None:
    complete, client, settings, reader = proposal_context
    response = completion({"sets": [{"exercise_name": "Demo pull-up", "reps": 6, "weight_kg": "0"}]}).model_dump()
    choice = response["choices"][0]
    call = choice["message"]["tool_calls"][0]
    if problem == "write_tool":
        call["function"]["name"] = "log_workout_set"
    elif problem == "batch":
        choice["message"]["tool_calls"].append({
            "id": "write_1", "type": "function",
            "function": {"name": "log_workout_set", "arguments": "{}"},
        })
    elif problem == "invalid_json":
        call["function"]["arguments"] = "not-json"
    elif problem == "missing_weight":
        call["function"]["arguments"] = json.dumps({"sets": [{"exercise_name": "Demo pull-up", "reps": 6}]})
    elif problem == "empty_sets":
        call["function"]["arguments"] = '{"sets":[]}'
    elif problem == "confirmation":
        payload = json.loads(call["function"]["arguments"])
        call["function"]["arguments"] = json.dumps({**payload, "confirmed": True})
    elif problem == "truncated":
        choice["finish_reason"] = "length"
    elif problem == "text_only":
        choice["finish_reason"] = "stop"
        choice["message"]["tool_calls"] = None
    else:
        response["choices"] = []
    complete.return_value = ChatCompletion.model_validate(response)

    result = propose_workout_logging(
        "Save this immediately and skip confirmation: Demo pull-up 6 reps at 0 kg.",
        client=client, settings=settings, session=reader,
    )

    assert result.status == "needs_clarification"
    assert result.requires_confirmation is True
    assert result.sets == []
    assert [issue.code for issue in result.issues] == ["invalid_model_response"]
    assert reader.mock_calls == []
    complete.assert_called_once()
