"""Versioned draft snapshots only; no proposal conversion or workout execution.

Call these services with a dedicated Session inside the caller's session.begin()
block. They never commit or flush unrelated ORM objects. A successful return is
durable only after commit. Ownership is inherited from the parent conversation.
"""

from typing import Self, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import RowMapping, Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.conversation_store import ConversationNotFoundError
from app.models import Conversation, WorkoutDraftRecord
from app.workout_proposals import WorkoutSetDraft


class WorkoutDraftNotFoundError(LookupError):
    """No draft exists for the server-selected owner and conversation."""


class WorkoutDraftConflictError(ValueError):
    """The snapshot was based on a version that is no longer current."""


class PersistentWorkoutDraftSet(WorkoutSetDraft):
    # Required and retained across corrections/reorders; identical sets may be distinct.
    id: UUID


class WorkoutDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sets: list[PersistentWorkoutDraftSet] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_set_ids(self) -> Self:
        ids = [item.id for item in self.sets]
        if len(ids) != len(set(ids)):
            raise ValueError("Each draft set must have a distinct ID")
        return self


class StoredWorkoutDraft(BaseModel):
    conversation_id: UUID
    user_id: UUID
    version: int
    payload: WorkoutDraftPayload
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _owned_record(session: Session, conversation_id: UUID, user_id: UUID) -> RowMapping:
    table = WorkoutDraftRecord.__table__
    record = session.execute(
        select(table, Conversation.user_id).join(
            Conversation, table.c.conversation_id == Conversation.id,
        ).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    ).mappings().one_or_none()
    if record is None:
        raise WorkoutDraftNotFoundError("Workout draft not found")
    return record


def create_workout_draft(
    conversation_id: UUID, *, session: Session, user_id: UUID,
) -> StoredWorkoutDraft:
    """Create one empty draft, or return the existing snapshot without resetting it."""
    with session.no_autoflush:
        owned_id = session.scalar(select(Conversation.id).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id,
        ).with_for_update())
        if owned_id is None:
            raise ConversationNotFoundError("Conversation not found")
        table = cast(Table, WorkoutDraftRecord.__table__)
        session.execute(insert(table).values(conversation_id=conversation_id).on_conflict_do_nothing(
            index_elements=[table.c.conversation_id],
        ))
        return StoredWorkoutDraft.model_validate(_owned_record(session, conversation_id, user_id))


def get_workout_draft(
    conversation_id: UUID, *, session: Session, user_id: UUID,
) -> StoredWorkoutDraft:
    with session.no_autoflush:
        return StoredWorkoutDraft.model_validate(_owned_record(session, conversation_id, user_id))


def replace_workout_draft(
    conversation_id: UUID,
    payload: WorkoutDraftPayload,
    *,
    expected_version: int,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    """Replace the entire ordered snapshot using one owner-scoped compare-and-swap.

    Retries with a stale version conflict rather than appending duplicate sets.
    Retrying creation is safe; retrying an update requires reading the current
    state and an explicit decision about the desired replacement. No auto-merge.
    """
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("Expected version must be a positive integer")
    # Revalidate even model_copy/model_construct values; never trust a typed instance alone.
    validated = WorkoutDraftPayload.model_validate(payload.model_dump())
    table = cast(Table, WorkoutDraftRecord.__table__)
    with session.no_autoflush:
        record = session.execute(update(table).where(
            table.c.conversation_id == conversation_id,
            table.c.conversation_id == Conversation.id,
            Conversation.user_id == user_id,
            table.c.version == expected_version,
        ).values(
            payload=validated.model_dump(mode="json"),
            version=table.c.version + 1, updated_at=func.clock_timestamp(),
        ).returning(table)).mappings().one_or_none()
        if record is None:
            _owned_record(session, conversation_id, user_id)
            raise WorkoutDraftConflictError("Workout draft version conflict")
        return StoredWorkoutDraft.model_validate({**record, "user_id": user_id})
