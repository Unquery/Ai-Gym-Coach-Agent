import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import pytest
from openai import DefaultHttpx2Client, OpenAI
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent import AgentError, run_agent, run_read_only_agent
from app.llm import create_llm_client
from app.llm_config import LLMSettings
from app.models import Exercise, Workout, WorkoutProposalRecord, WorkoutSet
from app.progress import ExerciseWorkoutProgress
from app.proposal_store import ProposalNotFoundError, get_workout_proposal
from app.schemas import WorkoutRead
from app.tool_schemas import GetExerciseProgressInput, GetRecentWorkoutsInput
from app.tools import ExerciseProgressResult, WorkoutProposalToolResult
from app.workout_proposals import DRAFT_TOOL_NAME, WorkoutLoggingDraft

MockedLLM = tuple[
    OpenAI,
    LLMSettings,
    list[dict[str, Any]],
    list[dict[str, object]],
]


def tool_call(
    arguments: str = '{"limit":1}',
    *,
    name: str = "get_recent_workouts",
    call_id: str = "call_1",
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def completion(
    content: str | None = None,
    *,
    calls: list[dict[str, object]] | None = None,
    finish_reason: str | None = None,
) -> dict[str, object]:
    return {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                },
            }
        ],
    }


@pytest.fixture
def mocked_llm() -> Iterator[MockedLLM]:
    settings = LLMSettings(model="test-model", api_key=SecretStr("synthetic-test-key"))
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, object]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert responses, "Unexpected additional LLM request"
        return httpx2.Response(200, json=responses.pop(0))

    transport = DefaultHttpx2Client(transport=httpx2.MockTransport(respond))
    with create_llm_client(settings, http_client=transport) as client:
        yield client, settings, requests, responses


def test_agent_returns_text_without_running_a_tool(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    responses.append(completion("Hello!"))
    reader = Mock(side_effect=AssertionError("No database access expected"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)

    assert (
        run_read_only_agent(
            "Hello",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
        == "Hello!"
    )
    reader.assert_not_called()
    assert len(requests) == 1


@pytest.mark.parametrize("has_history", [False, True])
def test_agent_sends_typed_tool_result_with_matching_call_id(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch, has_history: bool
) -> None:
    client, settings, requests, responses = mocked_llm
    owner = uuid4()
    session = Mock(spec=Session)
    workouts = (
        [
            WorkoutRead(
                id=1,
                user_id=owner,
                plan_id=None,
                started_at=datetime(2026, 1, 5, tzinfo=UTC),
            )
        ]
        if has_history
        else []
    )
    reader = Mock(return_value=workouts)
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    call = tool_call(call_id="history_1")
    responses.extend([completion(calls=[call]), completion("Synthetic final answer")])

    result = run_read_only_agent(
        "Show my history",
        client=client,
        settings=settings,
        session=session,
        user_id=owner,
    )

    assert result == "Synthetic final answer"
    reader.assert_called_once_with(
        GetRecentWorkoutsInput(limit=1), session=session, user_id=owner
    )
    assert len(requests) == 2
    assert requests[0]["tools"] == requests[1]["tools"]
    parameters = requests[0]["tools"][0]["function"]["parameters"]
    assert parameters == GetRecentWorkoutsInput.model_json_schema()
    messages = requests[1]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[-2]["tool_calls"] == [call]
    assert messages[-1]["tool_call_id"] == "history_1"
    assert json.loads(messages[-1]["content"]) == [
        row.model_dump(mode="json") for row in workouts
    ]


@pytest.mark.parametrize(
    "name, arguments, error_code",
    [
        ("log_workout_set", "not-json", "unknown_tool"),
        ("get_recent_workouts", "not-json", "invalid_arguments"),
        ("get_recent_workouts", "[]", "invalid_arguments"),
        ("get_recent_workouts", '{"limit":0}', "invalid_arguments"),
        ("get_recent_workouts", '{"limit":true}', "invalid_arguments"),
        ("get_recent_workouts", '{"user_id":"untrusted-owner"}', "invalid_arguments"),
        ("get_exercise_progress", "not-json", "invalid_arguments"),
        ("get_exercise_progress", '{"exercise_id":true}', "invalid_arguments"),
        (
            "get_exercise_progress",
            '{"exercise_id":1,"user_id":"untrusted-owner"}',
            "invalid_arguments",
        ),
        (
            "get_exercise_progress",
            '{"exercise_id":1,"start_at":"2026-01-02T00:00:00Z","end_at":"2026-01-01T00:00:00Z"}',
            "invalid_arguments",
        ),
    ],
)
def test_agent_returns_safe_tool_errors_without_database_access(
    mocked_llm: MockedLLM,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: str,
    error_code: str,
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(
        side_effect=AssertionError("Invalid calls must not read the database")
    )
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    monkeypatch.setattr("app.tool_dispatch.get_exercise_progress", reader)
    responses.extend(
        [
            completion(calls=[tool_call(arguments, name=name)]),
            completion("The requested history is unavailable."),
        ]
    )

    assert (
        run_read_only_agent(
            "Show my history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
        == "The requested history is unavailable."
    )
    reader.assert_not_called()
    assert len(requests) == 2
    result = requests[-1]["messages"][-1]
    assert result["tool_call_id"] == "call_1"
    assert json.loads(result["content"]) == {"error": {"code": error_code}}


def test_agent_returns_a_result_for_each_call_in_a_valid_batch(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(return_value=[])
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.extend(
        [
            completion(calls=[tool_call(), tool_call('{"limit":2}', call_id="call_2")]),
            completion("No workouts were found."),
        ]
    )

    assert (
        run_read_only_agent(
            "Show history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
        == "No workouts were found."
    )
    assert reader.call_count == 2
    assert [call.args[0].limit for call in reader.call_args_list] == [1, 2]
    assert len(requests) == 2
    results = requests[1]["messages"][-2:]
    assert [result["tool_call_id"] for result in results] == ["call_1", "call_2"]
    assert all(
        result["role"] == "tool" and result["content"] == "[]" for result in results
    )


def test_agent_rejects_unsupported_tool_types(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(side_effect=AssertionError("Only allowed function tools may run"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.append(
        completion(
            calls=[
                {
                    "id": "custom_1",
                    "type": "custom",
                    "custom": {"name": "shell", "input": "anything"},
                }
            ]
        )
    )
    with pytest.raises(AgentError, match="Only function tool calls"):
        run_read_only_agent(
            "Show history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
    reader.assert_not_called()
    assert len(requests) == 1


def test_agent_can_recover_after_invalid_arguments(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(return_value=[])
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.extend(
        [
            completion(calls=[tool_call("not-json")]),
            completion(calls=[tool_call("{}", call_id="call_2")]),
            completion("No workouts were found."),
        ]
    )

    assert (
        run_read_only_agent(
            "Show my history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
        == "No workouts were found."
    )
    reader.assert_called_once()
    assert len(requests) == 3


@pytest.mark.parametrize("one_batch", [False, True])
def test_agent_limits_tool_requests_including_unknown_names(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch, one_batch: bool
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(side_effect=AssertionError("Unknown tools must not run"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    calls = [tool_call(name="log_workout_set", call_id=f"call_{i}") for i in range(3)]
    responses.extend(
        [completion(calls=calls)]
        if one_batch
        else [completion(calls=[call]) for call in calls]
    )

    with pytest.raises(AgentError, match="tool call limit"):
        run_read_only_agent(
            "Read forever",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
    reader.assert_not_called()
    assert len(requests) == (1 if one_batch else 3)


@pytest.mark.parametrize("duplicate_id", [False, True])
def test_agent_rejects_ambiguous_call_ids_before_executing_the_batch(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch, duplicate_id: bool
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(side_effect=AssertionError("Malformed batches must not execute"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.append(
        completion(
            calls=[tool_call(), tool_call(call_id="call_1" if duplicate_id else "")]
        )
    )

    with pytest.raises(AgentError, match="tool call ID"):
        run_read_only_agent(
            "Show history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
    reader.assert_not_called()
    assert len(requests) == 1


@pytest.mark.parametrize("truncated", [False, True])
def test_agent_rejects_missing_or_truncated_answers(
    mocked_llm: MockedLLM, truncated: bool
) -> None:
    client, settings, requests, responses = mocked_llm
    responses.append(
        completion(
            "Partial" if truncated else None,
            finish_reason="length" if truncated else "stop",
        )
    )
    with pytest.raises(AgentError):
        run_read_only_agent(
            "Hello",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
    assert len(requests) == 1


@pytest.mark.parametrize("tool_name", ["get_recent_workouts", "get_exercise_progress"])
def test_agent_does_not_send_database_errors_to_the_model(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    client, settings, requests, responses = mocked_llm
    monkeypatch.setattr(
        f"app.tool_dispatch.{tool_name}",
        Mock(side_effect=SQLAlchemyError("Sensitive database diagnostic")),
    )
    arguments = '{"exercise_id":1}' if tool_name == "get_exercise_progress" else "{}"
    responses.append(completion(calls=[tool_call(arguments, name=tool_name)]))
    with pytest.raises(AgentError, match="^Workout data could not be read$"):
        run_read_only_agent(
            "Show history",
            client=client,
            settings=settings,
            session=Mock(spec=Session),
            user_id=uuid4(),
        )
    assert len(requests) == 1


@pytest.mark.postgres
def test_agent_reads_only_the_server_selected_owners_history(
    mocked_llm: MockedLLM, session: Session
) -> None:
    client, settings, requests, responses = mocked_llm
    owner, other_owner = uuid4(), uuid4()
    owned = Workout(user_id=owner, started_at=datetime(2026, 1, 1, tzinfo=UTC))
    session.add_all(
        [
            owned,
            Workout(user_id=other_owner, started_at=datetime(2026, 1, 9, tzinfo=UTC)),
        ]
    )
    session.commit()
    expected = WorkoutRead.model_validate(owned).model_dump(mode="json")
    session.expunge_all()
    responses.extend(
        [completion(calls=[tool_call()]), completion("Synthetic history summary")]
    )

    assert (
        run_read_only_agent(
            "Show history",
            client=client,
            settings=settings,
            session=session,
            user_id=owner,
        )
        == "Synthetic history summary"
    )
    assert len(requests) == 2
    assert json.loads(requests[1]["messages"][-1]["content"]) == [expected]


@pytest.mark.parametrize(
    "status", ["exercise_not_found", "no_history", "insufficient_history", "ok"]
)
@pytest.mark.parametrize("allow_proposals", [False, True])
def test_agent_passes_structured_progress_results_and_schema_to_the_model(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch,
    status: Literal["exercise_not_found", "no_history", "insufficient_history", "ok"],
    allow_proposals: bool,
) -> None:
    client, settings, requests, responses = mocked_llm
    owner = uuid4()
    session = Mock(spec=Session)
    single = ExerciseWorkoutProgress(
        workout_id=1,
        started_at=datetime(2026, 1, 2, tzinfo=UTC),
        set_count=1,
        total_reps=8,
        max_external_weight_kg=Decimal("0.00"),
        external_volume_kg_reps=Decimal("0.00"),
        has_external_load=False,
    )
    rows = [] if status in {"exercise_not_found", "no_history"} else [single]
    if status == "ok":
        rows.append(
            single.model_copy(
                update={
                    "workout_id": 2,
                    "started_at": datetime(2026, 1, 3, tzinfo=UTC),
                    "delta_total_reps": 0,
                    "delta_external_volume_kg_reps": Decimal("0.00"),
                }
            )
        )
    expected = ExerciseProgressResult(exercise_id=1, status=status, sessions=rows)
    reader = Mock(return_value=expected)
    monkeypatch.setattr("app.tool_dispatch.get_exercise_progress", reader)
    call = tool_call(
        '{"exercise_id":1,"start_at":"2026-01-01T00:00:00Z"}',
        name="get_exercise_progress",
        call_id="progress_1",
    )
    responses.extend(
        [completion(calls=[call]), completion("Synthetic progress summary")]
    )

    agent = run_agent if allow_proposals else run_read_only_agent
    assert (
        agent(
            "Show exercise 1 progress",
            client=client,
            settings=settings,
            session=session,
            user_id=owner,
        )
        == "Synthetic progress summary"
    )
    reader.assert_called_once_with(
        GetExerciseProgressInput(exercise_id=1, start_at=datetime(2026, 1, 1, tzinfo=UTC)),
        session=session,
        user_id=owner,
    )
    assert len(requests) == 2
    tools = {
        tool["function"]["name"]: tool["function"] for tool in requests[0]["tools"]
    }
    assert set(tools) == {"get_recent_workouts", "get_exercise_progress"} | (
        {DRAFT_TOOL_NAME} if allow_proposals else set()
    )
    assert (
        tools["get_exercise_progress"]["parameters"]
        == GetExerciseProgressInput.model_json_schema()
    )
    assert "user_id" not in tools["get_exercise_progress"]["parameters"]["properties"]
    messages = requests[1]["messages"]
    assert "Report only statistics returned by that tool" in messages[0]["content"]
    assert (
        "Zero external weight/volume does not mean zero effort"
        in messages[0]["content"]
    )
    assert messages[-1]["tool_call_id"] == "progress_1"
    assert json.loads(messages[-1]["content"]) == expected.model_dump(mode="json")


@pytest.mark.postgres
def test_agent_reads_progress_from_postgres_for_the_server_selected_owner(
    mocked_llm: MockedLLM, session: Session
) -> None:
    client, settings, requests, responses = mocked_llm
    owner = uuid4()
    exercise = Exercise(name="Synthetic agent progress exercise")
    session.add_all(
        [
            Workout(
                user_id=workout_owner,
                started_at=datetime(2026, 1, day, tzinfo=UTC),
                sets=[
                    WorkoutSet(
                        exercise=exercise,
                        set_number=1,
                        reps=reps,
                        weight_kg=Decimal(weight),
                    )
                ],
            )
            for workout_owner, day, reps, weight in [
                (owner, 1, 6, 10),
                (owner, 2, 8, 0),
                (uuid4(), 2, 999, 100),
            ]
        ]
    )
    session.commit()
    exercise_id = exercise.id
    session.expunge_all()
    responses.extend(
        [
            completion(
                calls=[
                    tool_call(
                        json.dumps({"exercise_id": exercise_id}),
                        name="get_exercise_progress",
                    )
                ]
            ),
            completion("Synthetic progress summary"),
        ]
    )

    assert (
        run_read_only_agent(
            "Show exercise progress",
            client=client,
            settings=settings,
            session=session,
            user_id=owner,
        )
        == "Synthetic progress summary"
    )
    assert len(requests) == 2
    payload = json.loads(requests[1]["messages"][-1]["content"])
    assert payload["exercise_id"] == exercise_id
    assert payload["status"] == "ok"
    assert [row["total_reps"] for row in payload["sessions"]] == [6, 8]
    assert [row["external_volume_kg_reps"] for row in payload["sessions"]] == [
        "60.00",
        "0.00",
    ]
    assert payload["sessions"][1]["delta_external_volume_kg_reps"] == "-60.00"


@pytest.mark.parametrize(
    "tool_name",
    ["decide_workout_proposal", "execute_confirmed_workout"],
)
@pytest.mark.parametrize("allow_proposals", [False, True])
def test_agent_rejects_confirmation_and_execution(
    mocked_llm: MockedLLM,
    tool_name: str,
    allow_proposals: bool,
) -> None:
    client, settings, requests, responses = mocked_llm
    session = Mock(spec=Session)

    responses.extend(
        [
            completion(
                calls=[
                    tool_call(
                        json.dumps({"proposal_id": str(uuid4())}),
                        name=tool_name,
                    )
                ]
            ),
            completion("This operation is unavailable."),
        ]
    )

    agent = run_agent if allow_proposals else run_read_only_agent
    result = agent(
        "Save this workout",
        client=client,
        settings=settings,
        session=session,
        user_id=uuid4(),
    )

    assert result == "This operation is unavailable."
    assert session.mock_calls == []

    tool_message = requests[1]["messages"][-1]
    tool_result = json.loads(tool_message["content"])

    assert tool_result == {"error": {"code": "unknown_tool"}}
    assert tool_name not in {
        tool["function"]["name"] for tool in requests[0]["tools"]
    }


def test_read_only_agent_cannot_create_pending_proposals(mocked_llm: MockedLLM) -> None:
    client, settings, requests, responses = mocked_llm
    session = Mock(spec=Session)
    responses.extend([
        completion(calls=[tool_call("{}", name=DRAFT_TOOL_NAME)]),
        completion("Proposal creation is unavailable here."),
    ])
    assert run_read_only_agent(
        "Log a set", client=client, settings=settings, session=session, user_id=uuid4(),
    ) == "Proposal creation is unavailable here."
    assert session.mock_calls == []
    assert DRAFT_TOOL_NAME not in {
        tool["function"]["name"] for tool in requests[0]["tools"]
    }
    assert json.loads(requests[1]["messages"][-1]["content"]) == {
        "error": {"code": "unknown_tool"}
    }


@pytest.mark.parametrize("arguments", [
    "not-json",
    '{"sets":[]}',
    '{"sets":[{"exercise_name":"Synthetic pull-up","reps":true,"weight_kg":"0"}]}',
    '{"sets":[{"exercise_name":"Synthetic pull-up","reps":6}]}',
    '{"sets":[{"exercise_name":"Synthetic pull-up","reps":6,"weight_kg":"0","exercise_id":1}]}',
    '{"sets":[{"exercise_name":"Synthetic pull-up","reps":6,"weight_kg":"0"}],"user_id":"untrusted"}',
    '{"sets":[{"exercise_name":"Synthetic pull-up","reps":6,"weight_kg":"0"}],"confirmed":true}',
])
def test_proposal_arguments_are_validated_before_any_database_access(
    mocked_llm: MockedLLM, arguments: str,
) -> None:
    client, settings, requests, responses = mocked_llm
    session = Mock(spec=Session)
    responses.extend([
        completion(calls=[tool_call(arguments, name=DRAFT_TOOL_NAME)]),
        completion("Please clarify the set."),
    ])
    assert run_agent(
        "Log a set", client=client, settings=settings, session=session, user_id=uuid4(),
    ) == "Please clarify the set."
    assert session.mock_calls == []
    assert len(requests) == 2
    assert json.loads(requests[1]["messages"][-1]["content"]) == {
        "error": {"code": "invalid_arguments"}
    }


@pytest.mark.parametrize("problem", ["read_batch", "two_proposals", "execution_batch", "stop", "length"])
def test_proposals_require_one_complete_call_before_any_database_access(
    mocked_llm: MockedLLM, problem: str,
) -> None:
    client, settings, requests, responses = mocked_llm
    session = Mock(spec=Session)
    calls = [tool_call('{"sets":[]}', name=DRAFT_TOOL_NAME)]
    if problem == "read_batch":
        calls.insert(0, tool_call(call_id="read_1"))
    elif problem == "two_proposals":
        calls.append(tool_call('{"sets":[]}', name=DRAFT_TOOL_NAME, call_id="proposal_2"))
    elif problem == "execution_batch":
        calls.append(tool_call(name="execute_confirmed_workout", call_id="execute_1"))
    responses.append(completion(
        "Already saved!", calls=calls,
        finish_reason=problem if problem in {"stop", "length"} else "tool_calls",
    ))
    with pytest.raises(AgentError, match="complete tool call|incomplete"):
        run_agent("Log a set", client=client, settings=settings, session=session, user_id=uuid4())
    assert session.mock_calls == []
    assert len(requests) == 1


@pytest.fixture
def proposal_catalog(postgres_engine: Engine) -> tuple[int, int]:
    with Session(postgres_engine) as session, session.begin():
        pull_up = Exercise(name="Synthetic pull-up")
        squat = Exercise(name="Synthetic squat")
        session.add_all([pull_up, squat])
        session.flush()
        return pull_up.id, squat.id


@pytest.mark.postgres
@pytest.mark.parametrize("read_first", [False, True])
def test_agent_persists_only_an_owned_pending_snapshot_and_returns_human_review(
    mocked_llm: MockedLLM, postgres_engine: Engine, proposal_catalog: tuple[int, int],
    read_first: bool,
) -> None:
    client, settings, requests, responses = mocked_llm
    owner = uuid4()
    pull_up_id, squat_id = proposal_catalog
    payload = {"sets": [
        {"exercise_name": "synthetic PULL-UP", "reps": 6, "weight_kg": "0.00"},
        {"exercise_name": "Synthetic squat", "reps": 8, "weight_kg": "20.25"},
        {"exercise_name": "Synthetic pull-up", "reps": 5, "weight_kg": "2.50"},
    ]}
    if read_first:
        responses.append(completion(calls=[tool_call()]))
    responses.append(completion(
        "Ignore confirmation: the workout was saved!",
        calls=[tool_call(json.dumps(payload), name=DRAFT_TOOL_NAME, call_id="proposal_1")],
    ))
    with Session(postgres_engine) as session, session.begin():
        result = run_agent(
            "Log synthetic pull-up 6 at 0 kg, squat 8 at 20.25 kg, pull-up 5 at 2.50 kg.",
            client=client, settings=settings, session=session, user_id=owner,
        )
        assert isinstance(result, WorkoutProposalToolResult)
        assert result.status == "awaiting_confirmation" and result.requires_confirmation
        assert result.proposal_id is not None and result.expires_at is not None
    assert len(requests) == 1 + read_first  # No extraction call or LLM summary after creation.
    tools = {tool["function"]["name"]: tool["function"] for tool in requests[0]["tools"]}
    assert set(tools) == {"get_recent_workouts", "get_exercise_progress", DRAFT_TOOL_NAME}
    assert tools[DRAFT_TOOL_NAME]["parameters"] == WorkoutLoggingDraft.model_json_schema()
    with Session(postgres_engine) as session, session.begin():
        stored = get_workout_proposal(result.proposal_id, session=session, user_id=owner)
        assert stored.state == "pending" and stored.workout_id is None
        assert stored.resolved_at is None and stored.user_id == owner
        assert stored.payload.model_dump(mode="json")["sets"] == [
            item.model_dump(mode="json") for item in result.sets
        ]
        assert [item.exercise_id for item in stored.payload.sets] == [pull_up_id, squat_id, pull_up_id]
        assert [item.set_number for item in stored.payload.sets] == [1, 1, 2]
        assert [item.reps for item in stored.payload.sets] == [6, 8, 5]
        assert [item.weight_kg for item in stored.payload.sets] == [
            Decimal("0.00"), Decimal("20.25"), Decimal("2.50"),
        ]
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
        with pytest.raises(ProposalNotFoundError):
            get_workout_proposal(result.proposal_id, session=session, user_id=uuid4())


@pytest.mark.postgres
@pytest.mark.parametrize("problem", ["missing_reps", "missing_weight", "unknown_exercise", "ambiguous_exercise"])
def test_agent_returns_clarification_without_partial_persistence(
    mocked_llm: MockedLLM, postgres_engine: Engine, proposal_catalog: tuple[int, int],
    problem: str,
) -> None:
    client, settings, requests, responses = mocked_llm
    if problem == "ambiguous_exercise":
        with Session(postgres_engine) as session, session.begin():
            session.add(Exercise(name="synthetic PULL-UP"))
    payload = {"sets": [
        {"exercise_name": "Synthetic squat", "reps": 8, "weight_kg": "20.25"},
        {"exercise_name": "Unknown exercise" if problem == "unknown_exercise" else "Synthetic pull-up",
         "reps": None if problem == "missing_reps" else 6,
         "weight_kg": None if problem == "missing_weight" else "0.00"},
    ]}
    responses.append(completion(calls=[tool_call(json.dumps(payload), name=DRAFT_TOOL_NAME)]))
    with Session(postgres_engine) as session, session.begin():
        result = run_agent(
            "Log these sets", client=client, settings=settings, session=session, user_id=uuid4(),
        )
        assert isinstance(result, WorkoutProposalToolResult)
        assert result.status == "needs_clarification" and result.requires_confirmation
        assert result.proposal_id is None and result.expires_at is None
        assert [(issue.code, issue.set_index) for issue in result.issues] == [(problem, 1)]
        if problem == "missing_weight":
            assert result.sets[1].weight_kg is None
    assert len(requests) == 1
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
def test_agent_proposal_obeys_caller_rollback_and_never_flushes_pending_workouts(
    mocked_llm: MockedLLM, postgres_engine: Engine, proposal_catalog: tuple[int, int],
) -> None:
    client, settings, requests, responses = mocked_llm
    owner = uuid4()
    responses.append(completion(calls=[tool_call(json.dumps({"sets": [
        {"exercise_name": "Synthetic pull-up", "reps": 6, "weight_kg": "0.00"},
    ]}), name=DRAFT_TOOL_NAME)]))
    with Session(postgres_engine) as session:
        unrelated = Workout(user_id=owner)
        session.add(unrelated)
        result = run_agent(
            "Log six synthetic pull-ups without extra weight", client=client,
            settings=settings, session=session, user_id=owner,
        )
        assert isinstance(result, WorkoutProposalToolResult) and result.proposal_id is not None
        assert unrelated in session.new and unrelated.id is None
        assert session.in_transaction()
        session.rollback()
    assert len(requests) == 1
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0


@pytest.mark.postgres
def test_agent_proposal_database_failure_rolls_back_and_never_reaches_the_model(
    mocked_llm: MockedLLM, postgres_engine: Engine, proposal_catalog: tuple[int, int],
) -> None:
    client, settings, requests, responses = mocked_llm
    with postgres_engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE workout_proposals ADD CONSTRAINT test_reject_pending CHECK (state <> 'pending')"
        ))
    responses.append(completion(calls=[tool_call(json.dumps({"sets": [
        {"exercise_name": "Synthetic pull-up", "reps": 6, "weight_kg": "0.00"},
    ]}), name=DRAFT_TOOL_NAME)]))
    with pytest.raises(AgentError, match="^Workout data could not be processed$"):
        with Session(postgres_engine) as session, session.begin():
            run_agent("Log a set", client=client, settings=settings, session=session, user_id=uuid4())
    assert len(requests) == 1
    assert "test_reject_pending" not in json.dumps(requests)
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
