"""Conversation services against disposable PostgreSQL schemas, without an LLM."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.conversation_store import (
    ConversationNotFoundError,
    append_message,
    create_conversation,
    get_conversation_messages,
)
from app.models import Conversation, ConversationMessage, Exercise

pytestmark = pytest.mark.postgres


def tool_call(call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id, "type": "function",
        "function": {"name": "get_recent_workouts", "arguments": '{"limit": 2}'},
    }


def test_services_persist_conversation_and_text_across_sessions(
    postgres_engine: Engine,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        conversation = create_conversation(session=session, user_id=owner)
        other = create_conversation(session=session, user_id=owner)
        conversation_id = conversation.id
        assert conversation_id.version == 4 and other.id != conversation_id
        assert conversation.user_id == owner
        assert conversation.created_at.utcoffset() is not None
        first = append_message(
            conversation_id, session=session, user_id=owner,
            role="user", content="  Show my history.\n",
        )
        second = append_message(
            conversation_id, session=session, user_id=owner,
            role="assistant", content="No workouts yet.",
        )
        assert first.position == 1 and second.position == 2
        assert first.created_at.utcoffset() is not None
        independent = append_message(
            other.id, session=session, user_id=owner, role="user", content="Other chat."
        )
        assert independent.position == 1

    with Session(postgres_engine) as session:
        saved = get_conversation_messages(conversation_id, session=session, user_id=owner)
        assert [(item.position, item.role, item.content) for item in saved] == [
            (1, "user", "  Show my history.\n"), (2, "assistant", "No workouts yet.")
        ]
        assert all(item.tool_calls is None and item.tool_call_id is None for item in saved)


def test_reads_filter_owner_and_conversation_and_order_by_position(session: Session) -> None:
    owner, stranger = uuid4(), uuid4()
    timestamp = datetime(2026, 10, 29, tzinfo=UTC)
    conversation = Conversation(user_id=owner, messages=[
        ConversationMessage(position=position, role="user", content=str(position),
                            created_at=timestamp)
        for position in (3, 1, 2)
    ])
    same_owner = Conversation(user_id=owner, messages=[
        ConversationMessage(position=1, role="user", content="Another conversation.")
    ])
    other_owner = Conversation(user_id=stranger, messages=[
        ConversationMessage(position=1, role="user", content="Other owner's text.")
    ])
    empty = Conversation(user_id=owner)
    session.add_all([conversation, same_owner, other_owner, empty])
    session.commit()

    messages = get_conversation_messages(conversation.id, session=session, user_id=owner)
    assert [item.content for item in messages] == ["1", "2", "3"]
    assert all(item.created_at == timestamp for item in messages)
    for conversation_id in (conversation.id, same_owner.id, empty.id, uuid4()):
        assert get_conversation_messages(conversation_id, session=session, user_id=stranger) == []
    assert get_conversation_messages(other_owner.id, session=session, user_id=owner) == []
    assert get_conversation_messages(empty.id, session=session, user_id=owner) == []


@pytest.mark.parametrize("missing", [False, True])
def test_append_rejects_missing_or_foreign_parent_without_flushing(
    session: Session, missing: bool,
) -> None:
    owner = uuid4()
    conversation = create_conversation(session=session, user_id=owner)
    conversation_id = conversation.id
    session.commit()
    unrelated = Exercise(name="")  # Invalid if any service triggers an autoflush.
    session.add(unrelated)
    with pytest.raises(ConversationNotFoundError, match="Conversation not found"):
        append_message(
            uuid4() if missing else conversation_id, session=session, user_id=uuid4(),
            role="user", content="Not authorized.",
        )
    assert unrelated in session.new and unrelated.id is None
    with session.no_autoflush:
        assert session.scalar(select(func.count()).select_from(ConversationMessage)) == 0
    session.rollback()


def test_services_never_commit_or_flush_unrelated_changes(postgres_engine: Engine) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        conversation = create_conversation(session=session, user_id=owner)
        conversation_id = conversation.id
        message = append_message(
            conversation_id, session=session, user_id=owner, role="user", content="Uncommitted."
        )
        assert get_conversation_messages(conversation_id, session=session, user_id=owner) == [message]
        assert unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            assert observer.get(Conversation, conversation_id) is None
            assert observer.scalar(select(func.count()).select_from(ConversationMessage)) == 0
        session.rollback()
    with Session(postgres_engine) as observer:
        assert observer.get(Conversation, conversation_id) is None
        assert observer.scalar(select(func.count()).select_from(ConversationMessage)) == 0


@pytest.mark.parametrize("database_error", [False, True])
def test_transaction_rolls_back_all_service_writes_on_failure(
    postgres_engine: Engine, database_error: bool,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session:
        with pytest.raises(IntegrityError if database_error else ValueError):
            with session.begin():
                conversation = create_conversation(session=session, user_id=owner)
                conversation_id = conversation.id
                append_message(
                    conversation_id, session=session, user_id=owner,
                    role="user", content="First write must roll back too.",
                )
                if database_error:
                    # A real PostgreSQL failure after the first successful write.
                    session.execute(insert(ConversationMessage).values(
                        conversation_id=conversation_id, position=1,
                        role="assistant", content="Duplicate position.",
                    ))
                else:
                    append_message(
                        conversation_id, session=session, user_id=owner,
                        role="assistant", content=" \n",
                    )
    with Session(postgres_engine) as observer:
        assert observer.get(Conversation, conversation_id) is None
        assert observer.scalar(select(func.count()).select_from(ConversationMessage)) == 0


def test_tool_calls_and_results_round_trip_without_executing_tools(
    postgres_engine: Engine,
) -> None:
    owner = uuid4()
    calls = [tool_call(), tool_call("call_2")]
    # Failed attempts also need faithful history; arguments are stored as text.
    calls[1]["function"]["arguments"] = "invalid JSON"
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
        assistant = append_message(
            conversation_id, session=session, user_id=owner,
            role="assistant", content=None, tool_calls=calls,
        )
        assert assistant.tool_calls == calls
        for call_id, content in (("call_1", "[]"), ("call_2", "Invalid arguments.")):
            append_message(
                conversation_id, session=session, user_id=owner,
                role="tool", content=content, tool_call_id=call_id,
            )
    with Session(postgres_engine) as session:
        messages = get_conversation_messages(conversation_id, session=session, user_id=owner)
        assert [item.position for item in messages] == [1, 2, 3]
        assert messages[0].content is None and messages[0].tool_calls == calls
        assert [(item.role, item.tool_call_id, item.content) for item in messages[1:]] == [
            ("tool", "call_1", "[]"), ("tool", "call_2", "Invalid arguments.")
        ]


@pytest.mark.parametrize(
    "values",
    [
        {"role": "system"},
        {"content": " \t\n"},
        {"content": 123},
        {"role": "assistant", "content": None},
        {"role": "assistant", "tool_calls": []},
        {"role": "assistant", "tool_calls": {}},
        {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
        {"role": "assistant", "tool_calls": [tool_call(), tool_call()]},
        {"tool_calls": [tool_call()]},
        {"tool_call_id": "call_1"},
        {"role": "tool", "tool_call_id": " \t"},
        {"role": "tool", "content": None, "tool_call_id": "call_1"},
    ],
)
def test_invalid_message_inputs_leave_no_partial_rows(
    session: Session, values: dict[str, Any],
) -> None:
    owner = uuid4()
    conversation_id = create_conversation(session=session, user_id=owner).id
    session.commit()
    arguments: dict[str, Any] = {"role": "user", "content": "Synthetic text."}
    arguments.update(values)
    with pytest.raises(ValueError):
        append_message(conversation_id, session=session, user_id=owner, **arguments)
    assert get_conversation_messages(conversation_id, session=session, user_id=owner) == []
    valid = append_message(
        conversation_id, session=session, user_id=owner, role="user", content="Valid."
    )
    assert valid.position == 1
    session.rollback()


def test_tool_result_ids_must_match_this_conversation_and_cannot_repeat(session: Session) -> None:
    owner = uuid4()
    first = create_conversation(session=session, user_id=owner).id
    second = create_conversation(session=session, user_id=uuid4()).id
    append_message(
        first, session=session, user_id=owner,
        role="assistant", content="Checking history.", tool_calls=[tool_call()],
    )
    other = session.get(Conversation, second)
    assert other is not None
    append_message(
        second, session=session, user_id=other.user_id,
        role="assistant", content=None, tool_calls=[tool_call("foreign_call")],
    )
    for call_id in ("unknown_call", "foreign_call"):
        with pytest.raises(ValueError, match="reference a call in this conversation"):
            append_message(
                first, session=session, user_id=owner, role="tool", content="[]",
                tool_call_id=call_id,
            )
    result = append_message(
        first, session=session, user_id=owner, role="tool", content="[]", tool_call_id="call_1"
    )
    assert result.position == 2
    with pytest.raises(ValueError, match="already exists"):
        append_message(
            first, session=session, user_id=owner, role="tool", content="Duplicate.",
            tool_call_id="call_1",
        )
    # Call IDs may be reused in a later exchange; results match the latest call.
    append_message(
        first, session=session, user_id=owner,
        role="assistant", content=None, tool_calls=[tool_call()],
    )
    last = append_message(
        first, session=session, user_id=owner, role="tool", content="[]", tool_call_id="call_1"
    )
    assert last.position == 4
    session.rollback()


@pytest.mark.parametrize("commit_first", [False, True])
def test_concurrent_appends_wait_for_parent_then_allocate_position(
    postgres_engine: Engine, commit_first: bool,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
    started = Event()
    backend: dict[str, int] = {}

    def append_in_another_transaction() -> int:
        with Session(postgres_engine) as worker, worker.begin():
            worker.execute(text("SET LOCAL lock_timeout = '5s'"))
            backend["pid"] = worker.execute(select(func.pg_backend_pid())).scalar_one()
            started.set()
            return append_message(
                conversation_id, session=worker, user_id=owner,
                role="assistant", content="Concurrent response.",
            ).position

    with Session(postgres_engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
        append_message(
            conversation_id, session=first, user_id=owner, role="user", content="First."
        )
        future = pool.submit(append_in_another_transaction)
        assert started.wait(timeout=3)
        deadline = monotonic() + 3
        blocked = False
        while monotonic() < deadline and not future.done():
            blocked = first.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid AND NOT granted)"
            ), {"pid": backend["pid"]})
            if blocked:
                break
            sleep(0.01)
        assert blocked, "Second writer must wait while the first transaction holds the parent"
        if commit_first:
            first.commit()
        else:
            first.rollback()
        assert future.result(timeout=7) == (2 if commit_first else 1)
    with Session(postgres_engine) as session:
        messages = get_conversation_messages(conversation_id, session=session, user_id=owner)
        assert [item.position for item in messages] == ([1, 2] if commit_first else [1])
