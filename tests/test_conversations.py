"""Conversation persistence contracts against isolated PostgreSQL schemas."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Conversation, Message

pytestmark = pytest.mark.postgres


def test_conversation_persists_ordered_messages_across_sessions(
    postgres_engine: Engine,
) -> None:
    owner = uuid4()
    timestamp = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with Session(postgres_engine) as session, session.begin():
        conversation = Conversation(user_id=owner)
        # Insert out of order with equal timestamps: only position defines order.
        conversation.messages = [
            Message(position=2, role="assistant", content="Recorded for review.",
                    created_at=timestamp),
            Message(position=1, role="user", content="Three squat sets.",
                    created_at=timestamp),
        ]
        session.add(conversation)
        session.flush()
        conversation_id = conversation.id
        assert conversation_id.version == 4
        assert conversation.created_at.utcoffset() is not None
        assert all(message.conversation is conversation for message in conversation.messages)

    with Session(postgres_engine) as session:
        saved = session.get(Conversation, conversation_id)
        assert saved is not None and saved.user_id == owner
        assert [(message.position, message.role, message.content) for message in saved.messages] == [
            (1, "user", "Three squat sets."),
            (2, "assistant", "Recorded for review."),
        ]
        assert all(message.created_at == timestamp for message in saved.messages)


def test_owner_scoped_conversation_and_message_queries(session: Session) -> None:
    owner, other_owner = uuid4(), uuid4()
    first = Conversation(user_id=owner, messages=[
        Message(position=1, role="user", content="First synthetic conversation.")
    ])
    second = Conversation(user_id=other_owner, messages=[
        Message(position=1, role="user", content="Other synthetic conversation.")
    ])
    session.add_all([first, second])
    session.commit()
    assert first.id != second.id

    assert session.scalar(select(Conversation).where(
        Conversation.id == second.id, Conversation.user_id == owner
    )) is None
    # Messages inherit ownership from their parent; an ID alone is insufficient.
    statement = select(Message).join(Message.conversation).where(
        Conversation.user_id == owner
    )
    assert list(session.scalars(statement)) == first.messages
    assert list(session.scalars(statement.where(Message.conversation_id == second.id))) == []


@pytest.mark.parametrize(
    ("field", "value", "sqlstate"),
    [
        ("position", 0, "23514"),
        ("position", None, "23502"),
        ("role", "tool", "23514"),
        ("role", None, "23502"),
        ("content", "", "23514"),
        ("content", " \t\n", "23514"),
        ("content", None, "23502"),
    ],
)
def test_message_constraints_are_enforced_by_postgres(
    session: Session, field: str, value: str | int | None, sqlstate: str,
) -> None:
    conversation = Conversation(user_id=uuid4())
    session.add(conversation)
    session.commit()
    values: dict[str, object] = dict(
        conversation_id=conversation.id, position=1, role="user", content="Synthetic text."
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
    saved_time = session.scalar(insert(Message).values(
        conversation_id=first_id, position=2, role="assistant", content="Reply."
    ).returning(Message.created_at))
    assert saved_time is not None and saved_time.utcoffset() is not None
    session.commit()

    with pytest.raises(IntegrityError) as error:
        session.execute(insert(Message).values(
            conversation_id=first_id, position=1, role="assistant", content="Duplicate."
        ))
    assert getattr(error.value.orig, "sqlstate", None) == "23505"
    session.rollback()
    saved = session.get(Conversation, first_id)
    assert saved is not None
    assert [message.position for message in saved.messages] == [1, 2]


def test_message_requires_an_existing_conversation(session: Session) -> None:
    session.add(Message(
        conversation_id=uuid4(), position=1, role="user", content="No parent."
    ))
    with pytest.raises(IntegrityError) as error:
        session.flush()
    assert getattr(error.value.orig, "sqlstate", None) == "23503"
    session.rollback()


@pytest.mark.parametrize("mode", ["orm_loaded", "orm_unloaded", "sql"])
def test_deleting_conversation_removes_only_its_messages(
    session: Session, mode: str,
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
    conversation = Conversation(user_id=uuid4(), messages=[
        Message(position=1, role="user", content="Do not commit.")
    ])
    session.add(conversation)
    session.flush()
    conversation_id = conversation.id
    session.rollback()
    assert session.get(Conversation, conversation_id) is None
    assert session.scalar(select(Message.id)) is None
