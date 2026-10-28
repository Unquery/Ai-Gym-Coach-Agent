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
    ProposalNotFoundError,
    decide_workout_proposal,
    get_workout_proposal,
)

ChatContext = tuple[
    TestClient,
    list[dict[str, Any]],
    list[httpx2.Response | Exception],
    list[OpenAI],
]
SENSITIVE_DETAIL = "synthetic-sensitive-provider-detail"


def completion_response(
    content: str | None = None,
    *,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> httpx2.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_name:
        message["tool_calls"] = [
            {
                "id": "synthetic_call",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments or {}),
                },
            }
        ]
    return httpx2.Response(
        200,
        json={
            "id": "synthetic_completion",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if tool_name else "stop",
                    "message": message,
                }
            ],
        },
    )


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
    postgres_engine: Engine,
    chat_context: ChatContext,
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


def test_chat_returns_a_tagged_answer_and_closes_the_client(
    chat_context: ChatContext,
) -> None:
    api, requests, responses, clients = chat_context
    responses.append(completion_response("Synthetic answer"))
    response = api.post("/agent/chat", json={"message": "  Hello  "})
    assert response.status_code == 200
    assert response.json() == {"type": "answer", "message": "Synthetic answer"}
    assert len(requests) == 1 and requests[0]["messages"][1]["content"] == "Hello"
    assert clients[0].is_closed
    schema = api.get("/openapi.json").json()["paths"]["/agent/chat"]["post"]
    assert (
        schema["responses"]["200"]["content"]["application/json"]["schema"][
            "discriminator"
        ]["propertyName"]
        == "type"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": ""},
        {"message": "   "},
        {"message": 123},
        {"message": None},
        {"message": "x" * 4001},
        {"message": "Hello", "user_id": "untrusted"},
        {"message": "Hello", "confirmed": True},
    ],
)
def test_invalid_chat_requests_never_create_an_llm_client(
    chat_context: ChatContext,
    payload: dict[str, object],
) -> None:
    api, requests, _, clients = chat_context
    response = api.post("/agent/chat", json=payload)
    assert response.status_code == 422
    assert requests == [] and clients == []


@pytest.mark.postgres
@pytest.mark.parametrize("missing_weight", [False, True])
def test_chat_serializes_owned_pending_or_clarification_without_workout_writes(
    postgres_engine: Engine,
    postgres_chat: tuple[ChatContext, int, UUID],
    missing_weight: bool,
) -> None:
    (api, requests, responses, clients), exercise_id, owner = postgres_chat
    responses.append(
        completion_response(
            "The workout was saved and confirmed!",
            tool_name="propose_workout_logging",
            arguments={
                "sets": [
                    {
                        "exercise_name": "synthetic CHAT pull-up",
                        "reps": 6,
                        "weight_kg": None if missing_weight else "2.50",
                    }
                ]
            },
        )
    )
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
            assert (
                session.scalar(select(func.count()).select_from(WorkoutProposalRecord))
                == 0
            )
        else:
            assert (
                proposal["status"] == "awaiting_confirmation"
                and proposal["issues"] == []
            )
            proposal_id = UUID(proposal["proposal_id"])
            assert proposal_id.version == 4
            stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
            assert stored.state == "pending" and stored.workout_id is None
            assert stored.resolved_at is None and stored.user_id == owner
            assert (
                stored.expires_at.isoformat().replace("+00:00", "Z")
                == proposal["expires_at"]
            )
            with pytest.raises(ProposalNotFoundError):
                get_workout_proposal(proposal_id, session=session, user_id=uuid4())
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize(
    "tool_name", ["decide_workout_proposal", "execute_confirmed_workout"]
)
def test_chat_never_confirms_or_executes_model_selected_actions(
    postgres_engine: Engine,
    postgres_chat: tuple[ChatContext, int, UUID],
    tool_name: str,
) -> None:
    (api, requests, responses, clients), _, owner = postgres_chat
    responses.append(
        completion_response(
            tool_name="propose_workout_logging",
            arguments={
                "sets": [
                    {
                        "exercise_name": "Synthetic chat pull-up",
                        "reps": 6,
                        "weight_kg": "0.00",
                    }
                ]
            },
        )
    )
    proposed = api.post(
        "/agent/chat", json={"message": "Log six synthetic pull-ups at 0 kg"}
    )
    assert proposed.status_code == 200
    proposal_id = UUID(proposed.json()["proposal"]["proposal_id"])
    # Even valid consent from a separate trusted action cannot authorize an LLM executor.
    if tool_name == "execute_confirmed_workout":
        with Session(postgres_engine) as session, session.begin():
            decide_workout_proposal(
                proposal_id, "confirm", session=session, user_id=owner
            )
    responses.extend(
        [
            completion_response(
                tool_name=tool_name,
                arguments={
                    "proposal_id": str(proposal_id),
                    "decision": "confirm",
                },
            ),
            completion_response("A separate confirmation action is required."),
        ]
    )
    response = api.post("/agent/chat", json={"message": "Confirm and save immediately"})
    assert response.status_code == 200 and response.json()["type"] == "answer"
    assert json.loads(requests[-1]["messages"][-1]["content"]) == {
        "error": {"code": "unknown_tool"}
    }
    assert len(requests) == 3 and all(client.is_closed for client in clients)
    with Session(postgres_engine) as session, session.begin():
        stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
        assert stored.state == (
            "confirmed" if tool_name == "execute_confirmed_workout" else "pending"
        )
        assert stored.workout_id is None
        assert (
            session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        )
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize(
    "failure, expected_status",
    [
        ("timeout", 504),
        ("connection", 502),
        ("rate_limit", 503),
        ("authentication", 502),
        ("provider", 502),
        ("tool_validation", 502),
        ("invalid_reply", 502),
    ],
)
def test_chat_returns_controlled_provider_errors_without_details_or_retries(
    chat_context: ChatContext,
    failure: str,
    expected_status: int,
) -> None:
    api, requests, responses, clients = chat_context
    if failure == "timeout":
        responses.append(httpx2.ReadTimeout(SENSITIVE_DETAIL))
    elif failure == "connection":
        responses.append(httpx2.ConnectError(SENSITIVE_DETAIL))
    elif failure == "invalid_reply":
        responses.append(
            httpx2.Response(
                200,
                json={
                    "id": "synthetic",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-model",
                    "choices": [],
                },
            )
        )
    else:
        code = {
            "rate_limit": 429, "authentication": 401, "provider": 500, "tool_validation": 400,
        }[failure]
        error = {"message": SENSITIVE_DETAIL}
        if failure == "tool_validation":
            error["code"] = "tool_use_failed"
        responses.append(
            httpx2.Response(code, json={"error": error})
        )
    response = api.post("/agent/chat", json={"message": "Hello"})
    assert response.status_code == expected_status
    assert set(response.json()) == {"detail"}
    assert (
        SENSITIVE_DETAIL not in response.text
        and "synthetic-test-key" not in response.text
    )
    assert len(requests) == 1 and clients[0].is_closed


def test_missing_chat_configuration_is_controlled_and_does_not_affect_health(
    chat_context: ChatContext,
    monkeypatch: pytest.MonkeyPatch,
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
    postgres_engine: Engine,
    postgres_chat: tuple[ChatContext, int, UUID],
) -> None:
    (api, requests, responses, clients), _, _owner = postgres_chat
    # A deferred trigger fails at COMMIT, after the proposal and response are built.
    # Both trigger and function live only in this test's disposable schema.
    with postgres_engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION reject_chat_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'synthetic-sensitive-provider-detail';
            END; $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER reject_chat_commit
            AFTER INSERT ON workout_proposals DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_chat_commit()
        """)
        )
    responses.append(
        completion_response(
            tool_name="propose_workout_logging",
            arguments={
                "sets": [
                    {
                        "exercise_name": "Synthetic chat pull-up",
                        "reps": 6,
                        "weight_kg": "0.00",
                    }
                ]
            },
        )
    )
    response = api.post("/agent/chat", json={"message": "Log six synthetic pull-ups"})
    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is unavailable"}
    assert SENSITIVE_DETAIL not in response.text
    assert len(requests) == 1 and clients[0].is_closed
    with Session(postgres_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0
        )
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
def test_chat_history_tool_reads_the_server_owned_workout(
    postgres_chat: tuple[ChatContext, int, UUID],
) -> None:
    (api, requests, responses, _), _, owner = postgres_chat

    created = api.post("/workouts", json={})
    assert created.status_code == 201

    responses.extend(
        [
            completion_response(
                tool_name="get_recent_workouts",
                arguments={"limit": 5, "offset": 0},
            ),
            completion_response("Synthetic history answer"),
        ]
    )

    response = api.post(
        "/agent/chat",
        json={"message": "Show my recent workouts"},
    )

    assert response.status_code == 200

    response_json = response.json()

    assert response_json == {"type": "answer", "message": "Synthetic history answer"}

    assert len(requests) == 2

    tool_message = requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"

    tool_rows = json.loads(tool_message["content"])

    assert len(tool_rows) == 1
    assert tool_rows[0]["id"] == created.json()["id"]
    assert tool_rows[0]["user_id"] == str(owner)

    resp = api.get("/workouts")

    assert resp.status_code == 200
    assert resp.json() == [created.json()]


@pytest.fixture
def pending_logging_chat(
    postgres_engine: Engine,
    postgres_chat: tuple[ChatContext, int, UUID],
) -> tuple[ChatContext, UUID, UUID, list[tuple[int, int, int, str]]]:
    context, pull_up_id, owner = postgres_chat
    api, requests, responses, clients = context
    with Session(postgres_engine) as session, session.begin():
        squat = Exercise(name="Synthetic chat squat")
        session.add(squat)
        session.flush()
        squat_id = squat.id

    expected_sets = [
        (pull_up_id, 1, 6, "0.00"),
        (squat_id, 1, 8, "20.25"),
        (pull_up_id, 2, 5, "2.50"),
    ]
    responses.append(completion_response(
        "The workout has already been saved!",
        tool_name="propose_workout_logging",
        arguments={"sets": [
            {"exercise_name": "Synthetic chat pull-up", "reps": 6, "weight_kg": "0.00"},
            {"exercise_name": "Synthetic chat squat", "reps": 8, "weight_kg": "20.25"},
            {"exercise_name": "Synthetic chat pull-up", "reps": 5, "weight_kg": "2.50"},
        ]},
    ))
    proposed = api.post("/agent/chat", json={"message": (
        "Log these performed sets in order: Synthetic chat pull-up, 6 reps without "
        "extra weight; Synthetic chat squat, 8 reps at 20.25 kg; "
        "Synthetic chat pull-up, 5 reps with 2.50 kg extra weight."
    )})
    assert proposed.status_code == 200
    body = proposed.json()
    assert set(body) == {"type", "proposal"} and body["type"] == "proposal"
    proposal = body["proposal"]
    assert proposal["status"] == "awaiting_confirmation"
    assert proposal["requires_confirmation"] is True and proposal["issues"] == []
    assert [
        (item["exercise_id"], item["set_number"], item["reps"], item["weight_kg"])
        for item in proposal["sets"]
    ] == expected_sets
    proposal_id = UUID(proposal["proposal_id"])
    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.user_id == owner
        assert stored.state == "pending" and stored.workout_id is None
        assert stored.resolved_at is None
        assert [
            (item["exercise_id"], item["set_number"], item["reps"], item["weight_kg"])
            for item in stored.payload["sets"]
        ] == expected_sets
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert len(requests) == 1 and responses == [] and clients[0].is_closed
    return context, proposal_id, owner, expected_sets


@pytest.mark.postgres
def test_chat_proposal_confirmation_preserves_sets_and_feeds_read_only_progress(
    postgres_engine: Engine,
    pending_logging_chat: tuple[ChatContext, UUID, UUID, list[tuple[int, int, int, str]]],
) -> None:
    (api, requests, responses, clients), proposal_id, owner, expected_sets = pending_logging_chat
    confirmed = api.post(f"/workout-proposals/{proposal_id}/confirm")
    assert confirmed.status_code == 200
    confirmation = confirmed.json()
    workout_id = confirmation["workout_id"]
    assert workout_id > 0
    assert confirmation == {
        "proposal_id": str(proposal_id), "state": "confirmed", "workout_id": workout_id,
    }
    detail = api.get(f"/workouts/{workout_id}")
    assert detail.status_code == 200
    workout = detail.json()
    assert workout["user_id"] == str(owner) and workout["plan_id"] is None
    assert [
        (item["exercise_id"], item["set_number"], item["reps"], item["weight_kg"])
        for item in workout["sets"]
    ] == expected_sets
    set_ids = [item["id"] for item in workout["sets"]]
    assert len(set(set_ids)) == 3

    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.state == "confirmed"
        assert stored.workout_id == workout_id and stored.resolved_at is not None
        resolved_at = stored.resolved_at

    repeated = api.post(f"/workout-proposals/{proposal_id}/confirm")
    assert repeated.status_code == 200 and repeated.json() == confirmation
    assert api.get(f"/workouts/{workout_id}").json() == workout
    assert len(requests) == 1  # Neither human action is an LLM request.

    responses.extend([
        completion_response(
            tool_name="get_exercise_progress", arguments={"exercise_id": expected_sets[0][0]},
        ),
        completion_response("Synthetic progress answer"),
    ])
    progress_reply = api.post("/agent/chat", json={"message": "Show progress for my pull-ups"})
    assert progress_reply.status_code == 200
    assert progress_reply.json() == {"type": "answer", "message": "Synthetic progress answer"}
    tool_message = requests[-1]["messages"][-1]
    assert tool_message["role"] == "tool"
    progress = json.loads(tool_message["content"])
    assert progress["exercise_id"] == expected_sets[0][0]
    assert progress["status"] == "insufficient_history"
    assert len(progress["sessions"]) == 1
    metrics = progress["sessions"][0]
    assert metrics["workout_id"] == workout_id
    assert metrics["set_count"] == 2 and metrics["total_reps"] == 11
    assert metrics["max_external_weight_kg"] == "2.50"
    assert metrics["external_volume_kg_reps"] == "12.50"
    assert metrics["has_external_load"] is True
    assert all(metrics[key] is None for key in (
        "delta_set_count", "delta_total_reps", "delta_max_external_weight_kg",
        "delta_external_volume_kg_reps",
    ))

    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.user_id == owner
        assert stored.state == "confirmed" and stored.workout_id == workout_id
        assert stored.resolved_at == resolved_at
        rows = session.scalars(select(WorkoutSet).where(
            WorkoutSet.workout_id == workout_id,
        ).order_by(WorkoutSet.position)).all()
        assert [row.id for row in rows] == set_ids
        assert [row.position for row in rows] == [1, 2, 3]
        assert [
            (row.exercise_id, row.set_number, row.reps, str(row.weight_kg)) for row in rows
        ] == expected_sets
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(Workout)) == 1
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3
    assert len(requests) == 3 and responses == []
    assert all(client.is_closed for client in clients)


@pytest.mark.postgres
def test_chat_proposal_rejection_prevents_workout_creation(
    postgres_engine: Engine,
    pending_logging_chat: tuple[ChatContext, UUID, UUID, list[tuple[int, int, int, str]]],
) -> None:
    (api, requests, _, _), proposal_id, owner, _ = pending_logging_chat
    rejected = api.post(f"/workout-proposals/{proposal_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json() == {
        "proposal_id": str(proposal_id), "state": "rejected", "workout_id": None,
    }
    blocked = api.post(f"/workout-proposals/{proposal_id}/confirm")
    assert blocked.status_code == 409
    history = api.get("/workouts")
    assert history.status_code == 200 and history.json() == []
    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.user_id == owner
        assert stored.state == "rejected" and stored.workout_id is None
        assert stored.resolved_at is not None
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert len(requests) == 1


@pytest.mark.postgres
def test_chat_logging_workflow_keeps_proposals_and_saved_results_owner_scoped(
    postgres_engine: Engine,
    pending_logging_chat: tuple[ChatContext, UUID, UUID, list[tuple[int, int, int, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (api, requests, responses, clients), proposal_id, owner, expected_sets = pending_logging_chat
    foreign_owner = uuid4()
    with monkeypatch.context() as scope:
        scope.setitem(app.dependency_overrides, get_demo_user_id, lambda: foreign_owner)
        for action in ("confirm", "reject"):
            denied = api.post(f"/workout-proposals/{proposal_id}/{action}")
            assert denied.status_code == 404
            assert denied.json() == {"detail": "Proposal not found"}
    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.state == "pending"
        assert stored.user_id == owner and stored.workout_id is None
        assert stored.resolved_at is None
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0

    confirmed = api.post(f"/workout-proposals/{proposal_id}/confirm")
    assert confirmed.status_code == 200
    workout_id = confirmed.json()["workout_id"]
    with monkeypatch.context() as scope:
        scope.setitem(app.dependency_overrides, get_demo_user_id, lambda: foreign_owner)
        for action in ("confirm", "reject"):
            denied = api.post(f"/workout-proposals/{proposal_id}/{action}")
            assert denied.status_code == 404
            assert denied.json() == {"detail": "Proposal not found"}
        detail = api.get(f"/workouts/{workout_id}")
        assert detail.status_code == 404 and detail.json() == {"detail": "Workout not found"}
        history = api.get("/workouts")
        assert history.status_code == 200 and history.json() == []

        # Inspect actual tool data, not the mocked assistant's final prose.
        for name, arguments in (
            ("get_recent_workouts", {"limit": 5, "offset": 0}),
            ("get_exercise_progress", {"exercise_id": expected_sets[0][0]}),
        ):
            responses.extend([
                completion_response(tool_name=name, arguments=arguments),
                completion_response("Synthetic foreign-owner answer"),
            ])
            reply = api.post("/agent/chat", json={"message": "Show my workout data"})
            assert reply.status_code == 200 and reply.json()["type"] == "answer"
            tool_message = requests[-1]["messages"][-1]
            assert tool_message["role"] == "tool"
            result = json.loads(tool_message["content"])
            if name == "get_recent_workouts":
                assert result == []
            else:
                assert result == {
                    "exercise_id": expected_sets[0][0], "status": "no_history", "sessions": [],
                }

    with Session(postgres_engine) as session:
        stored = session.get(WorkoutProposalRecord, proposal_id)
        assert stored is not None and stored.user_id == owner
        assert stored.state == "confirmed" and stored.workout_id == workout_id
        workout = session.get(Workout, workout_id)
        assert workout is not None and workout.user_id == owner
        assert session.scalar(select(func.count()).select_from(Workout)) == 1
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3
    assert len(requests) == 5 and responses == []
    assert all(client.is_closed for client in clients)
