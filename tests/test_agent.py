import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx2
import pytest
from openai import DefaultHttpx2Client, OpenAI
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent import AgentError, run_read_only_agent
from app.llm import create_llm_client
from app.llm_config import LLMSettings
from app.models import Workout
from app.schemas import WorkoutRead
from app.tool_schemas import GetRecentWorkoutsInput

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
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
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

    assert run_read_only_agent(
        "Hello", client=client, settings=settings, session=Mock(spec=Session), user_id=uuid4()
    ) == "Hello!"
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
        "Show my history", client=client, settings=settings, session=session, user_id=owner
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
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
    assert messages[-2]["tool_calls"] == [call]
    assert messages[-1]["tool_call_id"] == "history_1"
    assert json.loads(messages[-1]["content"]) == [row.model_dump(mode="json") for row in workouts]


@pytest.mark.parametrize(
    "name, arguments, error_code",
    [
        ("log_workout_set", "not-json", "unknown_tool"),
        ("get_recent_workouts", "not-json", "invalid_arguments"),
        ("get_recent_workouts", "[]", "invalid_arguments"),
        ("get_recent_workouts", '{"limit":0}', "invalid_arguments"),
        ("get_recent_workouts", '{"limit":true}', "invalid_arguments"),
        ("get_recent_workouts", '{"user_id":"untrusted-owner"}', "invalid_arguments"),
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
    reader = Mock(side_effect=AssertionError("Invalid calls must not read the database"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.extend([
        completion(calls=[tool_call(arguments, name=name)]),
        completion("The requested history is unavailable."),
    ])

    assert run_read_only_agent(
        "Show my history", client=client, settings=settings,
        session=Mock(spec=Session), user_id=uuid4()
    ) == "The requested history is unavailable."
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
    responses.extend([
        completion(calls=[tool_call(), tool_call('{"limit":2}', call_id="call_2")]),
        completion("No workouts were found."),
    ])

    assert run_read_only_agent(
        "Show history", client=client, settings=settings,
        session=Mock(spec=Session), user_id=uuid4()
    ) == "No workouts were found."
    assert reader.call_count == 2
    assert [call.args[0].limit for call in reader.call_args_list] == [1, 2]
    assert len(requests) == 2
    results = requests[1]["messages"][-2:]
    assert [result["tool_call_id"] for result in results] == ["call_1", "call_2"]
    assert all(result["role"] == "tool" and result["content"] == "[]" for result in results)


def test_agent_rejects_unsupported_tool_types(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(side_effect=AssertionError("Only allowed function tools may run"))
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.append(completion(calls=[{
        "id": "custom_1", "type": "custom", "custom": {"name": "shell", "input": "anything"}
    }]))
    with pytest.raises(AgentError, match="Only function tool calls"):
        run_read_only_agent(
            "Show history", client=client, settings=settings,
            session=Mock(spec=Session), user_id=uuid4()
        )
    reader.assert_not_called()
    assert len(requests) == 1


def test_agent_can_recover_after_invalid_arguments(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    reader = Mock(return_value=[])
    monkeypatch.setattr("app.tool_dispatch.get_recent_workouts", reader)
    responses.extend([
        completion(calls=[tool_call("not-json")]),
        completion(calls=[tool_call("{}", call_id="call_2")]),
        completion("No workouts were found."),
    ])

    assert run_read_only_agent(
        "Show my history", client=client, settings=settings,
        session=Mock(spec=Session), user_id=uuid4()
    ) == "No workouts were found."
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
        [completion(calls=calls)] if one_batch
        else [completion(calls=[call]) for call in calls]
    )

    with pytest.raises(AgentError, match="tool call limit"):
        run_read_only_agent(
            "Read forever", client=client, settings=settings,
            session=Mock(spec=Session), user_id=uuid4()
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
    responses.append(completion(calls=[
        tool_call(), tool_call(call_id="call_1" if duplicate_id else "")
    ]))

    with pytest.raises(AgentError, match="tool call ID"):
        run_read_only_agent(
            "Show history", client=client, settings=settings,
            session=Mock(spec=Session), user_id=uuid4()
        )
    reader.assert_not_called()
    assert len(requests) == 1


@pytest.mark.parametrize("truncated", [False, True])
def test_agent_rejects_missing_or_truncated_answers(
    mocked_llm: MockedLLM, truncated: bool
) -> None:
    client, settings, requests, responses = mocked_llm
    responses.append(completion(
        "Partial" if truncated else None,
        finish_reason="length" if truncated else "stop",
    ))
    with pytest.raises(AgentError):
        run_read_only_agent(
            "Hello", client=client, settings=settings, session=Mock(spec=Session), user_id=uuid4()
        )
    assert len(requests) == 1


def test_agent_does_not_send_database_errors_to_the_model(
    mocked_llm: MockedLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, settings, requests, responses = mocked_llm
    monkeypatch.setattr(
        "app.tool_dispatch.get_recent_workouts",
        Mock(side_effect=SQLAlchemyError("Sensitive database diagnostic")),
    )
    responses.append(completion(calls=[tool_call()]))
    with pytest.raises(AgentError, match="^Workout history could not be read$"):
        run_read_only_agent(
            "Show history", client=client, settings=settings,
            session=Mock(spec=Session), user_id=uuid4()
        )
    assert len(requests) == 1


@pytest.mark.postgres
def test_agent_reads_only_the_server_selected_owners_history(
    mocked_llm: MockedLLM, session: Session
) -> None:
    client, settings, requests, responses = mocked_llm
    owner, other_owner = uuid4(), uuid4()
    owned = Workout(user_id=owner, started_at=datetime(2026, 1, 1, tzinfo=UTC))
    session.add_all([
        owned,
        Workout(user_id=other_owner, started_at=datetime(2026, 1, 9, tzinfo=UTC)),
    ])
    session.commit()
    expected = WorkoutRead.model_validate(owned).model_dump(mode="json")
    session.expunge_all()
    responses.extend([completion(calls=[tool_call()]), completion("Synthetic history summary")])

    assert run_read_only_agent(
        "Show history", client=client, settings=settings, session=session, user_id=owner
    ) == "Synthetic history summary"
    assert len(requests) == 2
    assert json.loads(requests[1]["messages"][-1]["content"]) == [expected]
