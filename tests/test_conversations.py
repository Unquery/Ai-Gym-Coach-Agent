"""Conversation persistence contracts against isolated PostgreSQL schemas."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Engine, delete, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Conversation, Message

pytestmark = pytest.mark.postgres


def test_conversation_persists_ordered_messages_across_sessions(
    postgres_engine: Engine,
) -> None:
    owner = uuid4()
    timestamp = datetime(2026, 10, 28, tzinfo=UTC)
    with Session(postgres_engine) as session, session.begin():
        conversation = Conversation(user_id=owner)
        # Insert out of order with equal timestamps: only position defines order.
        conversation.messages = [
            Message(
                position=2,
                role="assistant",
                content="Recorded for review.",
                created_at=timestamp,
            ),
            Message(
                position=1,
                role="user",
                content="Three squat sets.",
                created_at=timestamp,
            ),
        ]
        session.add(conversation)
        session.flush()
        conversation_id = conversation.id
        assert conversation_id.version == 4
        assert conversation.created_at.utcoffset() is not None
        assert all(
            message.conversation is conversation for message in conversation.messages
        )

    with Session(postgres_engine) as session:
        saved = session.get(Conversation, conversation_id)
        assert saved is not None and saved.user_id == owner
        assert [
            (message.position, message.role, message.content)
            for message in saved.messages
        ] == [
            (1, "user", "Three squat sets."),
            (2, "assistant", "Recorded for review."),
        ]
        assert all(message.created_at == timestamp for message in saved.messages)


def test_owner_scoped_conversation_and_message_queries(session: Session) -> None:
    owner, other_owner = uuid4(), uuid4()
    first = Conversation(
        user_id=owner,
        messages=[
            Message(position=1, role="user", content="First synthetic conversation.")
        ],
    )
    second = Conversation(
        user_id=other_owner,
        messages=[
            Message(position=1, role="user", content="Other synthetic conversation.")
        ],
    )
    session.add_all([first, second])
    session.commit()
    assert first.id != second.id

    assert (
        session.scalar(
            select(Conversation).where(
                Conversation.id == second.id, Conversation.user_id == owner
            )
        )
        is None
    )
    # Messages inherit ownership from their parent; an ID alone is insufficient.
    statement = (
        select(Message).join(Message.conversation).where(Conversation.user_id == owner)
    )
    assert list(session.scalars(statement)) == first.messages
    assert (
        list(session.scalars(statement.where(Message.conversation_id == second.id)))
        == []
    )


@pytest.mark.parametrize(
    ("field", "value", "sqlstate"),
    [
        ("position", 0, "23514"),
        ("position", None, "23502"),
        ("role", "tool", "23514"),
        ("role", None, "23502"),
        ("content", "", "23514"),
        ("content", " \t\n", "23514"),
        ("content", None, "23514"),
    ],
)
def test_message_constraints_are_enforced_by_postgres(
    session: Session,
    field: str,
    value: str | int | None,
    sqlstate: str,
) -> None:
    conversation = Conversation(user_id=uuid4())
    session.add(conversation)
    session.commit()
    values: dict[str, object] = dict(
        conversation_id=conversation.id,
        position=1,
        role="user",
        content="Synthetic text.",
    )
    values[field] = value
    with pytest.raises(IntegrityError) as error:
        session.execute(insert(Message).values(**values))
    assert getattr(error.value.orig, "sqlstate", None) == sqlstate
    session.rollback()


def test_message_position_is_unique_within_each_conversation(session: Session) -> None:
    first, second = Conversation(user_id=uuid4()), Conversation(user_id=uuid4())
    first.messages = [Message(position=1, role="user", content="First.")]
    second.messages = [Message(position=1, role="user", content="Second.")]
    session.add_all([first, second])
    session.commit()
    first_id = first.id
    # A Core INSERT also checks that the timestamp default lives in PostgreSQL.
    saved_time = session.scalar(
        insert(Message)
        .values(
            conversation_id=first_id, position=2, role="assistant", content="Reply."
        )
        .returning(Message.created_at)
    )
    assert saved_time is not None and saved_time.utcoffset() is not None
    session.commit()

    with pytest.raises(IntegrityError) as error:
        session.execute(
            insert(Message).values(
                conversation_id=first_id,
                position=1,
                role="assistant",
                content="Duplicate.",
            )
        )
    assert getattr(error.value.orig, "sqlstate", None) == "23505"
    session.rollback()
    saved = session.get(Conversation, first_id)
    assert saved is not None
    assert [message.position for message in saved.messages] == [1, 2]


def test_message_requires_an_existing_conversation(session: Session) -> None:
    session.add(
        Message(conversation_id=uuid4(), position=1, role="user", content="No parent.")
    )
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert getattr(error.value.orig, "sqlstate", None) == "23503"
    session.rollback()


@pytest.mark.parametrize("mode", ["orm_loaded", "orm_unloaded", "sql"])
def test_deleting_conversation_removes_only_its_messages(
    session: Session,
    mode: str,
) -> None:
    first, second = Conversation(user_id=uuid4()), Conversation(user_id=uuid4())
    first.messages = [Message(position=1, role="user", content="Delete this.")]
    second.messages = [Message(position=1, role="user", content="Keep this.")]
    session.add_all([first, second])
    session.commit()
    first_id, second_id = first.id, second.id
    if mode == "sql":
        session.execute(delete(Conversation).where(Conversation.id == first_id))
    else:
        if mode == "orm_loaded":
            assert len(first.messages) == 1
        session.delete(first)
    session.commit()
    assert session.get(Conversation, first_id) is None
    assert session.get(Conversation, second_id) is not None
    assert list(session.scalars(select(Message.conversation_id))) == [second_id]


def test_conversation_and_messages_roll_back_together(session: Session) -> None:
    conversation = Conversation(
        user_id=uuid4(),
        messages=[Message(position=1, role="user", content="Do not commit.")],
    )
    session.add(conversation)
    session.flush()
    conversation_id = conversation.id
    session.rollback()
    assert session.get(Conversation, conversation_id) is None
    assert session.scalar(select(Message.id)) is None


def test_tool_exchange_persists_across_sessions(
    postgres_engine: Engine,
) -> None:

    with Session(postgres_engine) as session:
        conversation = Conversation(
            user_id=uuid4(),
            messages=[
                Message(position=1, role="user", content="Show my last two workouts."),
                Message(
                    position=2,
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_history_1",
                            "type": "function",
                            "function": {
                                "name": "get_recent_workouts",
                                "arguments": '{"limit": 2}',
                            },
                        }
                    ],
                ),
                Message(
                    position=3, role="tool", content="[]", tool_call_id="call_history_1"
                ),
            ],
        )
        session.add(conversation)
        session.commit()
        conversation_id = conversation.id

    with Session(postgres_engine) as session:
        saved = session.get(Conversation, conversation_id)
        assert saved is not None
        assert [(message.position, message.role) for message in saved.messages] == [
            (1, "user"),
            (2, "assistant"),
            (3, "tool"),
        ]
        assert saved.messages[1].content is None
        assert saved.messages[1].tool_calls == [
            {
                "id": "call_history_1",
                "type": "function",
                "function": {
                    "name": "get_recent_workouts",
                    "arguments": '{"limit": 2}',
                },
            }
        ]
        assert saved.messages[2].tool_call_id == saved.messages[1].tool_calls[0]["id"]
        assert saved.messages[2].content == "[]"


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "system"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "tool_calls": []},
        {"role": "assistant", "tool_calls": {}},
        {"role": "assistant", "tool_calls": JSON.NULL},
        {"tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": " \t\n"},
        {"role": "tool", "tool_call_id": "call_1", "content": None},
        {"role": "assistant", "tool_call_id": "call_1"},
        {"role": "tool", "tool_call_id": "call_1", "tool_calls": [{}]},
    ],
)
def test_message_tool_metadata_constraints(
    session: Session, overrides: dict[str, Any],
) -> None:
    conversation = Conversation(user_id=uuid4())
    session.add(conversation)
    session.commit()
    values: dict[str, Any] = dict(
        conversation_id=conversation.id, position=1, role="user", content="Synthetic."
    )
    values.update(overrides)
    # Core INSERT verifies PostgreSQL constraints rather than Python validation.
    with pytest.raises(IntegrityError) as error:
        session.execute(insert(Message).values(**values))
    assert getattr(error.value.orig, "sqlstate", None) == "23514"
    session.rollback()


def test_optional_tool_metadata_stores_sql_null(session: Session) -> None:
    conversation = Conversation(user_id=uuid4(), messages=[
        Message(position=1, role="assistant", content="No tool needed.", tool_calls=None)
    ])
    session.add(conversation)
    session.commit()
    assert session.scalar(text("SELECT tool_calls IS NULL FROM messages")) is True
