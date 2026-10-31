"""Versioned draft snapshots and locks; never execute workout writes.

Call these services with a dedicated Session inside the caller's session.begin()
block. They never commit or flush unrelated ORM objects. A successful return is
durable only after commit. Ownership is inherited from the parent conversation.
"""

from decimal import Decimal
from typing import Annotated, Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import RowMapping, Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.conversations.store import ConversationNotFoundError
from app.db.models import (
    Conversation,
    Exercise,
    WorkoutDraftAppendRecord,
    WorkoutDraftRecord,
)
from app.workouts.proposals import WorkoutSetDraft


class WorkoutDraftNotFoundError(LookupError):
    """No draft exists for the server-selected owner and conversation."""


class WorkoutDraftConflictError(ValueError):
    """A stale version or reused operation key conflicts with stored state."""


class WorkoutDraftInactiveError(ValueError):
    """A discarded draft cannot accept modifications."""


class WorkoutDraftExerciseNotFoundError(LookupError):
    """The requested catalog exercise does not exist."""


class WorkoutDraftSetNotFoundError(LookupError):
    """The requested set does not belong to this draft."""


class PersistentWorkoutDraftSet(WorkoutSetDraft):
    # Required and retained across corrections/reorders; identical sets may be distinct.
    id: UUID
    # Legacy snapshots may still contain unresolved exercise names only.
    exercise_id: Annotated[int, Field(gt=0, le=2147483647, strict=True)] | None = None


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
    state: Literal["active", "discarded"]
    payload: WorkoutDraftPayload
    created_at: AwareDatetime
    updated_at: AwareDatetime


def _owned_record(
    session: Session,
    conversation_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> RowMapping:
    table = WorkoutDraftRecord.__table__
    statement = (
        select(table, Conversation.user_id)
        .join(
            Conversation,
            table.c.conversation_id == Conversation.id,
        )
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    )
    if lock:
        statement = statement.with_for_update(of=table)
    record = session.execute(statement).mappings().one_or_none()
    if record is None:
        raise WorkoutDraftNotFoundError("Workout draft not found")
    return record


def lock_active_workout_draft(
    session: Session, conversation_id: UUID, user_id: UUID,
) -> StoredWorkoutDraft:
    """Lock the owned parent and active snapshot in the caller's transaction."""
    # Match create/chat lock order before locking the snapshot row.
    owned_id = session.scalar(
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .with_for_update()
    )
    if owned_id is None:
        raise WorkoutDraftNotFoundError("Workout draft not found")
    draft = StoredWorkoutDraft.model_validate(
        _owned_record(session, conversation_id, user_id, lock=True)
    )
    if draft.state != "active":
        raise WorkoutDraftInactiveError("Workout draft is not active")
    return draft


def create_workout_draft(
    conversation_id: UUID,
    *,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    """Create one empty draft, or return the existing snapshot without resetting it."""
    with session.no_autoflush:
        owned_id = session.scalar(
            select(Conversation.id)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            .with_for_update()
        )
        if owned_id is None:
            raise ConversationNotFoundError("Conversation not found")
        table = cast(Table, WorkoutDraftRecord.__table__)
        session.execute(
            insert(table)
            .values(conversation_id=conversation_id)
            .on_conflict_do_nothing(
                index_elements=[table.c.conversation_id],
            )
        )
        return StoredWorkoutDraft.model_validate(
            _owned_record(session, conversation_id, user_id)
        )


def get_workout_draft(
    conversation_id: UUID,
    *,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    with session.no_autoflush:
        return StoredWorkoutDraft.model_validate(
            _owned_record(session, conversation_id, user_id)
        )


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
        record = (
            session.execute(
                update(table)
                .where(
                    table.c.conversation_id == conversation_id,
                    table.c.conversation_id == Conversation.id,
                    Conversation.user_id == user_id,
                    table.c.version == expected_version,
                    table.c.state == "active",
                )
                .values(
                    payload=validated.model_dump(mode="json"),
                    version=table.c.version + 1,
                    updated_at=func.clock_timestamp(),
                )
                .returning(table)
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            current = _owned_record(session, conversation_id, user_id)
            if current["state"] != "active":
                raise WorkoutDraftInactiveError("Workout draft is not active")
            raise WorkoutDraftConflictError("Workout draft version conflict")
        return StoredWorkoutDraft.model_validate({**record, "user_id": user_id})


def append_workout_draft_set(
    conversation_id: UUID,
    *,
    exercise_id: int,
    reps: int | None,
    weight_kg: Decimal | None,
    idempotency_key: UUID,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    """Append one catalog-validated set in the caller's transaction.

    The trusted backend caller supplies user_id, never the LLM or a request body.
    Lock the parent before its draft, matching create/chat lock order. The draft
    lock also serializes with full snapshot replacement. Concurrent appends are
    ordered by lock acquisition, not request arrival time.

    A matching retry returns the current snapshot without a write, even if a
    later replacement removed the original set or the catalog has changed.
    Receipts survive replacements; a key with different arguments conflicts.
    Let failures escape session.begin() so both snapshot and receipt roll back.
    """
    if type(exercise_id) is not int or not 0 < exercise_id <= 2147483647:
        raise ValueError("Exercise ID must be a positive database integer")
    if not isinstance(idempotency_key, UUID):
        raise ValueError("Idempotency key must be a UUID")
    with session.no_autoflush:
        draft = lock_active_workout_draft(session, conversation_id, user_id)
        receipts = cast(Table, WorkoutDraftAppendRecord.__table__)
        previous = session.scalar(
            select(receipts.c.set_payload).where(
                receipts.c.conversation_id == conversation_id,
                receipts.c.idempotency_key == idempotency_key,
            )
        )
        if previous is not None:
            original = PersistentWorkoutDraftSet.model_validate(previous)
            retried = PersistentWorkoutDraftSet(
                id=original.id,
                exercise_id=exercise_id,
                exercise_name=original.exercise_name,
                reps=reps,
                weight_kg=weight_kg,
            )
            if retried != original:
                raise WorkoutDraftConflictError(
                    "Idempotency key has different set arguments"
                )
            return draft

        # FOR SHARE protects both the catalog ID and name until the caller commits.
        name = session.scalar(
            select(Exercise.name)
            .where(
                Exercise.id == exercise_id,
            )
            .with_for_update(read=True)
        )
        if name is None:
            raise WorkoutDraftExerciseNotFoundError("Draft exercise not found")
        item = PersistentWorkoutDraftSet(
            id=uuid4(),
            exercise_id=exercise_id,
            exercise_name=name,
            reps=reps,
            weight_kg=weight_kg,
        )
        saved = replace_workout_draft(
            conversation_id,
            WorkoutDraftPayload(sets=[*draft.payload.sets, item]),
            expected_version=draft.version,
            session=session,
            user_id=user_id,
        )
        session.execute(
            insert(receipts).values(
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                set_payload=item.model_dump(mode="json"),
            )
        )
        return saved


def remove_set_from_payload(
    payload: WorkoutDraftPayload,
    set_id: UUID,
) -> WorkoutDraftPayload:

    remaining_sets = []

    for item in payload.sets:
        if item.id != set_id:
            remaining_sets.append(item)

    if len(remaining_sets) == len(payload.sets):
        raise WorkoutDraftSetNotFoundError("Workout draft set not found")

    return WorkoutDraftPayload(sets=remaining_sets)


def _lock_draft_for_set_change(
    session: Session, conversation_id: UUID, user_id: UUID,
    set_id: UUID, expected_version: int,
) -> StoredWorkoutDraft:
    if not isinstance(set_id, UUID):
        raise ValueError("Set ID must be a UUID, not a position")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("Expected version must be a positive integer")
    draft = lock_active_workout_draft(session, conversation_id, user_id)
    # Check before set lookup: replaying a successful removal is a version conflict.
    if draft.version != expected_version:
        raise WorkoutDraftConflictError("Workout draft version conflict")
    return draft


def update_workout_draft_set(
    conversation_id: UUID,
    set_id: UUID,
    changes: dict[str, object],
    *,
    expected_version: int,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    """Correct only explicitly supplied reps/weight, preserving identity and order.

    Missing keys leave values unchanged; None explicitly marks a value unknown.
    Even a same-value nonempty update increments the version. A retry using the
    original version conflicts without writing; never auto-retry with a new one.
    Append receipts remain immutable so old append retries cannot undo corrections.
    The trusted caller supplies user_id and owns the transaction and rollback.
    """
    if not isinstance(changes, dict):
        raise ValueError("Set changes must be a dictionary")
    # Copy before waiting for locks; do not fill missing keys with defaults.
    patch = changes.copy()
    if not patch or patch.keys() - {"reps", "weight_kg"}:
        raise ValueError("Supply only repetitions and/or external weight")
    with session.no_autoflush:
        draft = _lock_draft_for_set_change(
            session, conversation_id, user_id, set_id, expected_version,
        )
        for index, item in enumerate(draft.payload.sets):
            if item.id == set_id:
                # model_copy(update=...) would bypass validation of corrected values.
                corrected = PersistentWorkoutDraftSet.model_validate({**item.model_dump(), **patch})
                sets = list(draft.payload.sets)
                sets[index] = corrected
                return replace_workout_draft(
                    conversation_id, WorkoutDraftPayload(sets=sets),
                    expected_version=expected_version, session=session, user_id=user_id,
                )
        raise WorkoutDraftSetNotFoundError("Workout draft set not found")


def remove_workout_draft_set(
    conversation_id: UUID,
    set_id: UUID,
    *,
    expected_version: int,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutDraft:
    """Remove by stable UUID, keeping survivor order and append retry receipts.

    A stale version (including a repeated successful request) always conflicts.
    A missing set at the current version raises WorkoutDraftSetNotFoundError.
    Let failures escape the caller's session.begin() to roll back all changes.
    """
    with session.no_autoflush:
        draft = _lock_draft_for_set_change(
            session, conversation_id, user_id, set_id, expected_version,
        )
        return replace_workout_draft(
            conversation_id, remove_set_from_payload(draft.payload, set_id),
            expected_version=expected_version, session=session, user_id=user_id,
        )
