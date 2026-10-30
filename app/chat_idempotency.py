"""Durable retry bindings; execution locks and responses share the chat transaction.

Registration uses a separate short caller-owned transaction. A processing record
is resumable whenever its row is unlocked: no committed worker token or lease can
strand it after a crash. Execution must hold its row lock through all chat writes
and response storage, then commit before returning. External LLM calls may repeat
after rollback; this is not exactly-once execution of external effects.
"""

import json
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import ChatRequestRecord

CHAT_ENDPOINT = "POST /agent/chat"


class ChatRequestError(RuntimeError):
    """The durable request could not be processed safely."""


class ChatRequestConflictError(ChatRequestError):
    """The key is already bound to different input."""


class ChatRequestInProgressError(ChatRequestError):
    """Another transaction currently owns this request."""


class StoredChatRequest(BaseModel):
    id: UUID
    user_id: UUID
    endpoint: str
    idempotency_key: UUID
    fingerprint: str
    state: Literal["processing", "completed"]
    response_status: int | None
    response_body: dict[str, object] | None
    conversation_id: UUID | None


def chat_request_fingerprint(payload: dict[str, object]) -> str:
    """Canonicalize parsed raw JSON, preserving string content and absent fields."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def register_chat_request(
    key: UUID, fingerprint: str, *, session: Session, user_id: UUID,
) -> StoredChatRequest:
    """Bind a key durably before execution; the caller commits this short transaction.

    The first SELECT avoids contending on an already registered execution row.
    Concurrent first registrations serialize via the unique constraint; a bounded
    registration lock wait is reported as in-progress. No unrelated ORM autoflush.
    """
    table = cast(Table, ChatRequestRecord.__table__)
    statement = select(table).where(
        table.c.user_id == user_id, table.c.endpoint == CHAT_ENDPOINT,
        table.c.idempotency_key == key,
    )
    with session.no_autoflush:
        row = session.execute(statement).mappings().one_or_none()
        if row is None:
            # Transaction-local; registration must end before the execution starts.
            session.execute(select(func.set_config("lock_timeout", "1s", True)))
            try:
                session.execute(insert(table).values(
                    id=uuid4(), user_id=user_id, endpoint=CHAT_ENDPOINT,
                    idempotency_key=key, fingerprint=fingerprint,
                ).on_conflict_do_nothing(
                    index_elements=[table.c.user_id, table.c.endpoint, table.c.idempotency_key],
                ))
            except OperationalError as error:
                if getattr(error.orig, "sqlstate", None) == "55P03":
                    raise ChatRequestInProgressError("Chat request is processing") from None
                raise
            row = session.execute(statement).mappings().one()
        stored = StoredChatRequest.model_validate(row)
        if stored.fingerprint != fingerprint:
            raise ChatRequestConflictError("Idempotency key has different request input")
        return stored


def lock_chat_request(
    request_id: UUID, *, session: Session, user_id: UUID,
) -> StoredChatRequest:
    """Acquire execution ownership until commit/rollback, across backend workers."""
    table = ChatRequestRecord.__table__
    with session.no_autoflush:
        try:
            row = session.execute(select(table).where(
                table.c.id == request_id, table.c.user_id == user_id,
                table.c.endpoint == CHAT_ENDPOINT,
            ).with_for_update(nowait=True)).mappings().one_or_none()
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                raise ChatRequestInProgressError("Chat request is processing") from None
            raise
        if row is None:
            raise ChatRequestError("Chat request not found")
        return StoredChatRequest.model_validate(row)


def complete_chat_request(
    request_id: UUID, *, conversation_id: UUID, response_status: int,
    response_body: dict[str, object], session: Session, user_id: UUID,
) -> None:
    """Store the response in the SAME transaction as the caller's chat side effects.

    The caller must already hold the execution row lock. Never commit separately
    from conversation/proposal writes or delete a binding to retry a failed turn.
    """
    table = cast(Table, ChatRequestRecord.__table__)
    with session.no_autoflush:
        completed = session.scalar(update(table).where(
            table.c.id == request_id, table.c.user_id == user_id,
            table.c.endpoint == CHAT_ENDPOINT, table.c.state == "processing",
        ).values(
            state="completed", conversation_id=conversation_id,
            response_status=response_status, response_body=response_body,
        ).returning(table.c.id))
        if completed is None:
            raise ChatRequestError("Chat request cannot be completed")
