import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import DefaultHttpx2Client, OpenAI
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.database import get_session
from app.llm import create_llm_client
from app.llm_config import LLMSettings
from app.main import app, get_demo_user_id
from app.models import Exercise, Workout, WorkoutProposalRecord, WorkoutSet
from app.proposal_store import (
    ProposalNotFoundError, decide_workout_proposal, get_workout_proposal,
)

ChatContext = tuple[
    TestClient, list[dict[str, Any]], list[httpx2.Response | Exception], list[OpenAI],
]
SENSITIVE_DETAIL = "synthetic-sensitive-provider-detail"


def completion_response(
    content: str | None = None, *, tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> httpx2.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_name:
        message["tool_calls"] = [{
            "id": "synthetic_call", "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(arguments or {})},
        }]
    return httpx2.Response(200, json={
        "id": "synthetic_completion", "object": "chat.completion", "created": 0,
        "model": "test-model", "choices": [{
            "index": 0, "finish_reason": "tool_calls" if tool_name else "stop",
            "message": message,
        }],
    })


@pytest.fixture
def chat_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[ChatContext]:
    settings = LLMSettings(model="test-model", api_key=SecretStr("synthetic-test-key"))
    requests: list[dict[str, Any]] = []
    responses: list[httpx2.Response | Exception] = []
    clients: list[OpenAI] = []
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = lambda: MagicMock(spec=Session)
    monkeypatch.setattr("app.main.load_llm_settings", lambda: settings)

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        assert responses, "Unexpected additional LLM request"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def create_mocked_client(config: LLMSettings) -> OpenAI:
        transport = DefaultHttpx2Client(transport=httpx2.MockTransport(respond))
        client = create_llm_client(config, http_client=transport)
        clients.append(client)
        return client

    monkeypatch.setattr("app.main.create_llm_client", create_mocked_client)
    try:
        with TestClient(app) as api:
            yield api, requests, responses, clients
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        for client in clients:
            client.close()


@pytest.fixture
def postgres_chat(
    postgres_engine: Engine, chat_context: ChatContext,
) -> tuple[ChatContext, int, UUID]:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        exercise = Exercise(name="Synthetic chat pull-up")
        session.add(exercise)
        session.flush()
        exercise_id = exercise.id

    def override_session() -> Iterator[Session]:
        with Session(postgres_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    return chat_context, exercise_id, owner


def test_chat_returns_a_tagged_answer_and_closes_the_client(chat_context: ChatContext) -> None:
    api, requests, responses, clients = chat_context
    responses.append(completion_response("Synthetic answer"))
    response = api.post("/agent/chat", json={"message": "  Hello  "})
    assert response.status_code == 200
    assert response.json() == {"type": "answer", "message": "Synthetic answer"}
    assert len(requests) == 1 and requests[0]["messages"][1]["content"] == "Hello"
    assert clients[0].is_closed
    schema = api.get("/openapi.json").json()["paths"]["/agent/chat"]["post"]
    assert schema["responses"]["200"]["content"]["application/json"]["schema"][
        "discriminator"
    ]["propertyName"] == "type"


@pytest.mark.parametrize("payload", [
    {}, {"message": ""}, {"message": "   "}, {"message": 123}, {"message": None},
    {"message": "x" * 4001}, {"message": "Hello", "user_id": "untrusted"},
    {"message": "Hello", "confirmed": True},
])
def test_invalid_chat_requests_never_create_an_llm_client(
    chat_context: ChatContext, payload: dict[str, object],
) -> None:
    api, requests, _, clients = chat_context
    response = api.post("/agent/chat", json=payload)
    assert response.status_code == 422
    assert requests == [] and clients == []


@pytest.mark.postgres
@pytest.mark.parametrize("missing_weight", [False, True])
def test_chat_serializes_owned_pending_or_clarification_without_workout_writes(
    postgres_engine: Engine, postgres_chat: tuple[ChatContext, int, UUID],
    missing_weight: bool,
) -> None:
    (api, requests, responses, clients), exercise_id, owner = postgres_chat
    responses.append(completion_response(
        "The workout was saved and confirmed!", tool_name="propose_workout_logging",
        arguments={"sets": [{
            "exercise_name": "synthetic CHAT pull-up", "reps": 6,
            "weight_kg": None if missing_weight else "2.50",
        }]},
    ))
    response = api.post("/agent/chat", json={"message": "Log my synthetic pull-up set"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"type", "proposal"} and body["type"] == "proposal"
    proposal = body["proposal"]
    assert proposal["requires_confirmation"] is True
    assert proposal["sets"][0]["exercise_id"] == exercise_id
    assert proposal["sets"][0]["reps"] == 6
    assert proposal["sets"][0]["weight_kg"] == (None if missing_weight else "2.50")
    assert len(requests) == 1 and clients[0].is_closed
    with Session(postgres_engine) as session, session.begin():
        if missing_weight:
            assert proposal["status"] == "needs_clarification"
            assert proposal["proposal_id"] is None and proposal["expires_at"] is None
            assert proposal["issues"] == [{"code": "missing_weight", "set_index": 0}]
            assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        else:
            assert proposal["status"] == "awaiting_confirmation" and proposal["issues"] == []
            proposal_id = UUID(proposal["proposal_id"])
            assert proposal_id.version == 4
            stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
            assert stored.state == "pending" and stored.workout_id is None
            assert stored.resolved_at is None and stored.user_id == owner
            assert stored.expires_at.isoformat().replace("+00:00", "Z") == proposal["expires_at"]
            with pytest.raises(ProposalNotFoundError):
                get_workout_proposal(proposal_id, session=session, user_id=uuid4())
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("tool_name", ["decide_workout_proposal", "execute_confirmed_workout"])
def test_chat_never_confirms_or_executes_model_selected_actions(
    postgres_engine: Engine, postgres_chat: tuple[ChatContext, int, UUID], tool_name: str,
) -> None:
    (api, requests, responses, clients), _, owner = postgres_chat
    responses.append(completion_response(tool_name="propose_workout_logging", arguments={"sets": [{
        "exercise_name": "Synthetic chat pull-up", "reps": 6, "weight_kg": "0.00",
    }]}))
    proposed = api.post("/agent/chat", json={"message": "Log six synthetic pull-ups at 0 kg"})
    assert proposed.status_code == 200
    proposal_id = UUID(proposed.json()["proposal"]["proposal_id"])
    # Even valid consent from a separate trusted action cannot authorize an LLM executor.
    if tool_name == "execute_confirmed_workout":
        with Session(postgres_engine) as session, session.begin():
            decide_workout_proposal(proposal_id, "confirm", session=session, user_id=owner)
    responses.extend([
        completion_response(tool_name=tool_name, arguments={
            "proposal_id": str(proposal_id), "decision": "confirm",
        }),
        completion_response("A separate confirmation action is required."),
    ])
    response = api.post("/agent/chat", json={"message": "Confirm and save immediately"})
    assert response.status_code == 200 and response.json()["type"] == "answer"
    assert json.loads(requests[-1]["messages"][-1]["content"]) == {
        "error": {"code": "unknown_tool"}
    }
    assert len(requests) == 3 and all(client.is_closed for client in clients)
    with Session(postgres_engine) as session, session.begin():
        stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
        assert stored.state == ("confirmed" if tool_name == "execute_confirmed_workout" else "pending")
        assert stored.workout_id is None
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("failure, expected_status", [
    ("timeout", 504), ("connection", 502), ("rate_limit", 503),
    ("authentication", 502), ("provider", 502), ("invalid_reply", 502),
])
def test_chat_returns_controlled_provider_errors_without_details_or_retries(
    chat_context: ChatContext, failure: str, expected_status: int,
) -> None:
    api, requests, responses, clients = chat_context
    if failure == "timeout":
        responses.append(httpx2.ReadTimeout(SENSITIVE_DETAIL))
    elif failure == "connection":
        responses.append(httpx2.ConnectError(SENSITIVE_DETAIL))
    elif failure == "invalid_reply":
        responses.append(httpx2.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "test-model", "choices": [],
        }))
    else:
        code = {"rate_limit": 429, "authentication": 401, "provider": 500}[failure]
        responses.append(httpx2.Response(code, json={"error": {"message": SENSITIVE_DETAIL}}))
    response = api.post("/agent/chat", json={"message": "Hello"})
    assert response.status_code == expected_status
    assert set(response.json()) == {"detail"}
    assert SENSITIVE_DETAIL not in response.text and "synthetic-test-key" not in response.text
    assert len(requests) == 1 and clients[0].is_closed


def test_missing_chat_configuration_is_controlled_and_does_not_affect_health(
    chat_context: ChatContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, requests, _, clients = chat_context

    def unavailable_settings() -> LLMSettings:
        raise ValueError(SENSITIVE_DETAIL)

    monkeypatch.setattr("app.main.load_llm_settings", unavailable_settings)
    response = api.post("/agent/chat", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is unavailable"}
    assert requests == [] and clients == []
    assert api.get("/health").json() == {"status": "ok"}


@pytest.mark.postgres
def test_chat_commit_failure_rolls_back_pending_proposal_before_reporting_success(
    postgres_engine: Engine, postgres_chat: tuple[ChatContext, int, UUID],
) -> None:
    (api, requests, responses, clients), _, _owner = postgres_chat
    # A deferred trigger fails at COMMIT, after the proposal and response are built.
    # Both trigger and function live only in this test's disposable schema.
    with postgres_engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION reject_chat_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'synthetic-sensitive-provider-detail';
            END; $$
        """))
        connection.execute(text("""
            CREATE CONSTRAINT TRIGGER reject_chat_commit
            AFTER INSERT ON workout_proposals DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_chat_commit()
        """))
    responses.append(completion_response(tool_name="propose_workout_logging", arguments={"sets": [{
        "exercise_name": "Synthetic chat pull-up", "reps": 6, "weight_kg": "0.00",
    }]}))
    response = api.post("/agent/chat", json={"message": "Log six synthetic pull-ups"})
    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is unavailable"}
    assert SENSITIVE_DETAIL not in response.text
    assert len(requests) == 1 and clients[0].is_closed
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
