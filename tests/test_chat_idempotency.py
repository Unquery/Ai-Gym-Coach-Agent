"""Canonical fingerprints and durable HTTP retries on disposable PostgreSQL schemas."""

import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier, Event
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import APITimeoutError
from pydantic import SecretStr
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.chat_idempotency import (
    CHAT_ENDPOINT,
    chat_request_fingerprint,
    complete_chat_request,
    lock_chat_request,
    register_chat_request,
)
from app.conversation_store import create_conversation
from app.database import create_db_engine, get_session
from app.llm_config import LLMSettings
from app.main import app, get_demo_user_id
from app.models import ChatRequestRecord, Conversation, ConversationMessage, Exercise, Workout, WorkoutProposalRecord, WorkoutSet
from app.tools import create_workout_logging_proposal
from app.workout_proposals import WorkoutLoggingDraft


def test_fingerprint_is_canonical_unicode_safe_and_preserves_exact_field_content() -> None:
    first = {"message": "Привет 👋", "conversation_id": None}
    reordered = {"conversation_id": None, "message": "Привет 👋"}
    digest = chat_request_fingerprint(first)
    assert digest == chat_request_fingerprint(reordered)
    assert len(digest) == 64 and int(digest, 16) >= 0
    for different in (
        {"message": "Привет 👋"},
        {"message": " Привет 👋", "conversation_id": None},
        {"message": "Привет!", "conversation_id": None},
        {"message": "Привет 👋", "conversation_id": str(uuid4())},
    ):
        assert chat_request_fingerprint(different) != digest


def test_fingerprint_is_stable_across_processes_with_different_hash_seeds() -> None:
    code = (
        "from app.chat_idempotency import chat_request_fingerprint; "
        "print(chat_request_fingerprint({'message':'Synthetic question','conversation_id':None}))"
    )
    outputs = [subprocess.check_output(
        [sys.executable, "-B", "-c", code],
        env={**os.environ, "PYTHONHASHSEED": seed}, text=True,
    ).strip() for seed in ("11", "22")]
    assert outputs == [chat_request_fingerprint({"message": "Synthetic question", "conversation_id": None})] * 2


@pytest.fixture
def keyed_chat(postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Mock, UUID]]:
    owner = uuid4()
    previous = app.dependency_overrides.copy()

    def sessions() -> Iterator[Session]:
        with Session(postgres_engine) as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    monkeypatch.setattr("app.main.load_llm_settings", lambda: LLMSettings(
        model="synthetic-model", api_key=SecretStr("synthetic-provider-key"),
    ))
    monkeypatch.setattr("app.main.create_llm_client", lambda settings: nullcontext(object()))
    runner = Mock(return_value="Synthetic answer.")
    monkeypatch.setattr("app.main.run_agent", runner)
    try:
        with TestClient(app) as api:
            yield api, runner, owner
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.postgres
def test_first_request_and_retry_return_the_same_response_without_another_turn(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    key = uuid4()
    body = {"message": "Synthetic question."}
    first = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    replay = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["content-type"] == first.headers["content-type"]
    assert runner.call_count == 1
    assert "idempotency_key" not in runner.call_args.kwargs
    with Session(postgres_engine) as session:
        stored = session.scalars(select(ChatRequestRecord)).one()
        assert stored.user_id == owner and stored.endpoint == CHAT_ENDPOINT
        assert stored.id != key and stored.idempotency_key == key
        assert stored.fingerprint == chat_request_fingerprint(body)
        assert stored.state == "completed" and stored.response_status == 200
        assert stored.response_body == first.json()
        assert session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 2
        for model in (WorkoutProposalRecord, Workout, WorkoutSet):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.postgres
def test_same_text_with_different_keys_is_a_new_request(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
    for _ in range(2):
        reply = api.post("/agent/chat", headers={"Idempotency-Key": str(uuid4())}, json={
            "message": "Same intentional text.", "conversation_id": str(conversation_id),
        })
        assert reply.status_code == 200
    assert runner.call_count == 2
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 2
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 4


@pytest.mark.postgres
@pytest.mark.parametrize("change", ["message", "trimmed_text", "conversation", "explicit_null"])
def test_reused_key_with_different_raw_payload_is_rejected(
    keyed_chat: tuple[TestClient, Mock, UUID], change: str,
) -> None:
    api, runner, _ = keyed_chat
    headers = {"Idempotency-Key": str(uuid4())}
    original = {"message": "Hello"}
    first = api.post("/agent/chat", headers=headers, json=original)
    assert first.status_code == 200
    changed: dict[str, Any] = {
        "message": {"message": "Different"},
        "trimmed_text": {"message": " Hello "},
        "conversation": {"message": "Hello", "conversation_id": first.json()["conversation_id"]},
        "explicit_null": {"message": "Hello", "conversation_id": None},
    }[change]
    denied = api.post("/agent/chat", headers=headers, json=changed)
    assert denied.status_code == 409
    assert denied.json() == {"detail": {"code": "idempotency_key_reused"}}
    replay = api.post("/agent/chat", headers=headers, json=original)
    assert replay.json() == first.json() and runner.call_count == 1


@pytest.mark.postgres
def test_json_key_order_and_transport_whitespace_do_not_change_the_fingerprint(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
    headers = {"Idempotency-Key": str(uuid4()), "Content-Type": "application/json"}
    first = api.post("/agent/chat", headers=headers, json={
        "message": "Привет", "conversation_id": str(conversation_id),
    })
    replay = api.post("/agent/chat", headers=headers, content=(
        '{ "conversation_id": "' + str(conversation_id) + '", "message": "Привет" }'
    ).encode("utf-8"))
    assert first.status_code == replay.status_code == 200 and replay.json() == first.json()
    assert runner.call_count == 1


@pytest.mark.postgres
def test_completed_response_replays_after_new_engine_and_client_without_provider_settings(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID], monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, runner, _ = keyed_chat
    headers = {"Idempotency-Key": str(uuid4())}
    body = {"message": "Synthetic restart request."}
    first = api.post("/agent/chat", headers=headers, json=body)
    assert first.status_code == 200
    postgres_engine.dispose()
    restarted_engine = create_db_engine(postgres_engine.url)

    def sessions() -> Iterator[Session]:
        with Session(restarted_engine) as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    settings = Mock(side_effect=AssertionError("Replay must not load provider configuration"))
    monkeypatch.setattr("app.main.load_llm_settings", settings)
    try:
        with TestClient(app) as restarted_api:
            replay = restarted_api.post("/agent/chat", headers=headers, json=body)
        assert replay.status_code == 200 and replay.json() == first.json()
        assert runner.call_count == 1 and settings.call_count == 0
    finally:
        restarted_engine.dispose()


@pytest.mark.postgres
def test_provider_failure_retains_binding_but_rolls_back_turn_and_can_retry(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, _ = keyed_chat
    headers = {"Idempotency-Key": str(uuid4())}
    body = {"message": "Synthetic recoverable request."}
    runner.side_effect = APITimeoutError(request=httpx2.Request("POST", "https://synthetic.invalid"))
    failed = api.post("/agent/chat", headers=headers, json=body)
    assert failed.status_code == 504
    with Session(postgres_engine) as session:
        record = session.scalars(select(ChatRequestRecord)).one()
        request_id = record.id
        assert record.state == "processing" and record.response_body is None
        assert session.scalar(select(func.count()).select_from(Conversation)) == 0
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 0
    denied = api.post("/agent/chat", headers=headers, json={"message": "Changed input"})
    assert denied.status_code == 409 and runner.call_count == 1
    runner.side_effect = None
    retried = api.post("/agent/chat", headers=headers, json=body)
    replay = api.post("/agent/chat", headers=headers, json=body)
    assert retried.status_code == replay.status_code == 200 and replay.json() == retried.json()
    assert runner.call_count == 2
    with Session(postgres_engine) as session:
        record = session.scalars(select(ChatRequestRecord)).one()
        assert record.id == request_id and record.state == "completed"


@pytest.mark.postgres
def test_unlocked_processing_record_is_resumable_after_a_crash_before_execution(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    key, body = uuid4(), {"message": "Synthetic abandoned request."}
    with Session(postgres_engine) as session, session.begin():
        registered = register_chat_request(key, chat_request_fingerprint(body), session=session, user_id=owner)
    # New sessions have no in-memory ownership token or lease to recover.
    postgres_engine.dispose()
    response = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    assert response.status_code == 200 and runner.call_count == 1
    with Session(postgres_engine) as session:
        row = session.scalars(select(ChatRequestRecord)).one()
        assert row.id == registered.id and row.state == "completed"


@pytest.mark.postgres
def test_concurrent_http_retry_returns_in_progress_without_another_llm_call(
    keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, _ = keyed_chat
    entered, release = Event(), Event()

    def slow_answer(*args: Any, **kwargs: Any) -> str:
        entered.set()
        assert release.wait(timeout=10)
        return "Synthetic slow answer."

    runner.side_effect = slow_answer
    headers, body = {"Idempotency-Key": str(uuid4())}, {"message": "Synthetic concurrent request."}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(api.post, "/agent/chat", headers=headers, json=body)
        try:
            assert entered.wait(timeout=5)
            duplicate = api.post("/agent/chat", headers=headers, json=body)
            assert duplicate.status_code == 409
            assert duplicate.json() == {"detail": {"code": "request_in_progress"}}
            assert duplicate.headers["retry-after"] == "1" and runner.call_count == 1
        finally:
            release.set()
        first = first_future.result(timeout=10)
    replay = api.post("/agent/chat", headers=headers, json=body)
    assert first.status_code == replay.status_code == 200 and replay.json() == first.json()
    assert runner.call_count == 1


@pytest.mark.postgres
def test_concurrent_first_registrations_share_one_stable_server_identity(postgres_engine: Engine) -> None:
    owner, key, barrier = uuid4(), uuid4(), Barrier(2)
    fingerprint = chat_request_fingerprint({"message": "Synthetic registration"})

    def worker() -> UUID:
        with Session(postgres_engine) as session, session.begin():
            barrier.wait(timeout=5)
            return register_chat_request(key, fingerprint, session=session, user_id=owner).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        ids = [future.result(timeout=10) for future in futures]
    assert ids[0] == ids[1]
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 1


@pytest.mark.postgres
def test_uncommitted_first_registration_has_a_bounded_wait_and_remains_retryable(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    key, body = uuid4(), {"message": "Synthetic initial registration race."}
    with Session(postgres_engine) as session, session.begin():
        registered = register_chat_request(key, chat_request_fingerprint(body), session=session, user_id=owner)
        duplicate = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": {"code": "request_in_progress"}}
        assert runner.call_count == 0
    retried = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    assert retried.status_code == 200 and runner.call_count == 1
    with Session(postgres_engine) as session:
        assert session.scalars(select(ChatRequestRecord)).one().id == registered.id


@pytest.mark.postgres
def test_services_never_autoflush_or_commit_unrelated_objects(postgres_engine: Engine) -> None:
    owner, key = uuid4(), uuid4()
    fingerprint = chat_request_fingerprint({"message": "Synthetic service transaction"})
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        register_chat_request(key, fingerprint, session=session, user_id=owner)
        assert unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            assert observer.scalar(select(func.count()).select_from(ChatRequestRecord)) == 0
        session.rollback()
    with Session(postgres_engine) as session, session.begin():
        registered = register_chat_request(key, fingerprint, session=session, user_id=owner)
        conversation_id = create_conversation(session=session, user_id=owner).id
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        lock_chat_request(registered.id, session=session, user_id=owner)
        complete_chat_request(
            registered.id, conversation_id=conversation_id, response_status=200,
            response_body={"type": "answer", "conversation_id": str(conversation_id), "message": "Synthetic"},
            session=session, user_id=owner,
        )
        assert unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            stored = observer.scalars(select(ChatRequestRecord)).one()
            assert stored.state == "processing" and stored.response_body is None
        session.rollback()


@pytest.mark.postgres
@pytest.mark.parametrize("missing", [False, True])
def test_keyed_requests_keep_conversation_ownership_checks(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID], missing: bool,
) -> None:
    api, runner, _ = keyed_chat
    with Session(postgres_engine) as session, session.begin():
        foreign_id = create_conversation(session=session, user_id=uuid4()).id
    response = api.post("/agent/chat", headers={"Idempotency-Key": str(uuid4())}, json={
        "message": "Do not store this turn.", "conversation_id": str(uuid4() if missing else foreign_id),
    })
    assert response.status_code == 404 and response.json() == {"detail": "Conversation not found"}
    assert runner.call_count == 0
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["reassign", "delete"])
def test_replay_rechecks_access_instead_of_disclosing_cached_conversation_data(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID], action: str,
) -> None:
    api, runner, _ = keyed_chat
    headers, body = {"Idempotency-Key": str(uuid4())}, {"message": "Synthetic private question."}
    first = api.post("/agent/chat", headers=headers, json=body)
    assert first.status_code == 200
    conversation_id = UUID(first.json()["conversation_id"])
    with Session(postgres_engine) as session, session.begin():
        statement = (update(Conversation).values(user_id=uuid4()) if action == "reassign" else delete(Conversation))
        session.execute(statement.where(Conversation.id == conversation_id))
    replay = api.post("/agent/chat", headers=headers, json=body)
    assert replay.status_code == 404 and runner.call_count == 1
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 1


@pytest.mark.postgres
def test_keys_are_scoped_to_server_selected_user_and_endpoint(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    key, body = uuid4(), {"message": "Same synthetic input."}
    first = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    app.dependency_overrides[get_demo_user_id] = lambda: uuid4()
    second = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    assert first.status_code == second.status_code == 200 and runner.call_count == 2
    assert first.json()["conversation_id"] != second.json()["conversation_id"]
    with Session(postgres_engine) as session, session.begin():
        session.execute(insert(ChatRequestRecord).values(
            id=uuid4(), user_id=owner, endpoint="POST /synthetic", idempotency_key=key,
            fingerprint=chat_request_fingerprint(body),
        ))
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.execute(insert(ChatRequestRecord).values(
                    id=uuid4(), user_id=owner, endpoint=CHAT_ENDPOINT, idempotency_key=key,
                    fingerprint=chat_request_fingerprint(body),
                ))
        assert session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 3


@pytest.mark.postgres
def test_replay_preserves_stored_status_code_and_structured_body(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID],
) -> None:
    api, runner, owner = keyed_chat
    key, body = uuid4(), {"message": "Synthetic stored response."}
    with Session(postgres_engine) as session, session.begin():
        registered = register_chat_request(key, chat_request_fingerprint(body), session=session, user_id=owner)
    with Session(postgres_engine) as session, session.begin():
        lock_chat_request(registered.id, session=session, user_id=owner)
        conversation_id = create_conversation(session=session, user_id=owner).id
        expected = {"type": "answer", "conversation_id": str(conversation_id), "message": "Stored Unicode 👋"}
        complete_chat_request(
            registered.id, conversation_id=conversation_id, response_status=201,
            response_body=expected, session=session, user_id=owner,
        )
    response = api.post("/agent/chat", headers={"Idempotency-Key": str(key)}, json=body)
    assert response.status_code == 201 and response.json() == expected and runner.call_count == 0


@pytest.mark.postgres
@pytest.mark.parametrize("deferred", [False, True])
def test_proposal_and_turn_rollback_when_response_storage_or_commit_fails(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID], deferred: bool,
) -> None:
    api, runner, _ = keyed_chat
    with Session(postgres_engine) as session, session.begin():
        session.add(Exercise(name="Synthetic transactional squat"))
    draft = WorkoutLoggingDraft.model_validate({"sets": [
        {"exercise_name": "Synthetic transactional squat", "reps": 8, "weight_kg": "20.25"},
    ]})

    def propose(*args: Any, session: Session, user_id: UUID, **kwargs: Any) -> object:
        return create_workout_logging_proposal(draft, session=session, user_id=user_id)

    runner.side_effect = propose
    with postgres_engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION reject_response() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic response failure'; END; $$
        """))
        trigger = (
            "CREATE CONSTRAINT TRIGGER reject_response AFTER UPDATE ON chat_requests DEFERRABLE INITIALLY DEFERRED"
            if deferred else "CREATE TRIGGER reject_response BEFORE UPDATE ON chat_requests"
        )
        connection.execute(text(trigger + " FOR EACH ROW EXECUTE FUNCTION reject_response()"))
    headers, body = {"Idempotency-Key": str(uuid4())}, {"message": "Log synthetic performed squat."}
    failed = api.post("/agent/chat", headers=headers, json=body)
    assert failed.status_code == 503
    with Session(postgres_engine) as session:
        original = session.scalars(select(ChatRequestRecord)).one()
        request_id = original.id
        assert original.state == "processing" and original.response_body is None
        for model in (Conversation, ConversationMessage, WorkoutProposalRecord, Workout, WorkoutSet):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    with postgres_engine.begin() as connection:
        connection.execute(text("DROP TRIGGER reject_response ON chat_requests"))
    retried = api.post("/agent/chat", headers=headers, json=body)
    replay = api.post("/agent/chat", headers=headers, json=body)
    assert retried.status_code == replay.status_code == 200 and replay.json() == retried.json()
    assert retried.json()["type"] == "proposal" and runner.call_count == 2
    with Session(postgres_engine) as session:
        record = session.scalars(select(ChatRequestRecord)).one()
        assert record.id == request_id and record.state == "completed"
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 2
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("headers", [
    {"Idempotency-Key": "not-a-uuid"},
    [("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))],
])
def test_invalid_or_duplicate_headers_are_rejected_before_registration(
    postgres_engine: Engine, keyed_chat: tuple[TestClient, Mock, UUID], headers: Any,
) -> None:
    api, runner, _ = keyed_chat
    response = api.post("/agent/chat", headers=headers, json={"message": "Synthetic question"})
    assert response.status_code == 422 and runner.call_count == 0
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(ChatRequestRecord)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("changes", [
    {"fingerprint": "invalid"}, {"state": "unknown"}, {"response_status": 200},
    {"state": "completed", "response_status": 200, "conversation_id": uuid4(), "response_body": {}},
    {"state": "completed", "response_status": 200, "conversation_id": uuid4(), "response_body": {"conversation_id": None}},
])
def test_postgres_rejects_inconsistent_request_records(session: Session, changes: dict[str, Any]) -> None:
    values = dict(id=uuid4(), user_id=uuid4(), endpoint=CHAT_ENDPOINT, idempotency_key=uuid4(), fingerprint="a" * 64)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(insert(ChatRequestRecord).values(**{**values, **changes}))
