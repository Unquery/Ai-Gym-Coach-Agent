"""Persist proposal snapshots and human decisions; never execute workout writes."""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import RowMapping, func, insert, select, update
from sqlalchemy.orm import Session

from app.models import WorkoutProposalRecord
from app.schemas import WorkoutSetCreate
from app.workout_proposals import WorkoutLoggingProposal

ProposalState = Literal["pending", "confirmed", "rejected", "expired"]
ProposalDecision = Literal["confirm", "reject"]
PROPOSAL_TTL = timedelta(minutes=15)


class ProposalNotFoundError(LookupError):
    """No proposal exists for the server-selected owner and ID."""


class ProposalClosedError(ValueError):
    """A terminal proposal cannot accept another decision."""


class StoredWorkoutSet(WorkoutSetCreate):
    exercise_name: Annotated[
        str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
    ]
    catalog_name: Annotated[
        str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
    ]


class WorkoutProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sets: Annotated[list[StoredWorkoutSet], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_set_order(self) -> Self:
        counts: dict[int, int] = {}
        for item in self.sets:
            expected = counts.get(item.exercise_id, 0) + 1
            if item.set_number != expected:
                raise ValueError("Set numbers must start at one and follow each exercise's order")
            counts[item.exercise_id] = expected
        return self


class StoredWorkoutProposal(BaseModel):
    id: UUID
    user_id: UUID
    state: ProposalState
    payload: WorkoutProposalPayload
    created_at: AwareDatetime
    expires_at: AwareDatetime
    resolved_at: AwareDatetime | None


def next_proposal_state(
    state: ProposalState,
    decision: ProposalDecision,
    *,
    now: datetime,
    expires_at: datetime,
) -> ProposalState:
    if state not in ("pending", "confirmed", "rejected", "expired"):
        raise ValueError("State is not supported")

    if decision not in ("confirm", "reject"):
        raise ValueError("Decision is not supported")

    if state != "pending":
        raise ValueError("Proposal is no longer pending")

    if now >= expires_at:
        return "expired"

    if decision == "confirm":
        return "confirmed"

    return "rejected"


def create_workout_proposal(
    proposal: WorkoutLoggingProposal, *, session: Session, user_id: UUID
) -> StoredWorkoutProposal:
    """Store a complete snapshot. The caller must commit before reporting success."""
    if (
        proposal.status != "awaiting_confirmation"
        or proposal.requires_confirmation is not True
        or proposal.issues != []
    ):
        raise ValueError("Only complete proposals awaiting confirmation can be stored")
    payload = WorkoutProposalPayload.model_validate(proposal.model_dump(include={"sets"}))
    table = WorkoutProposalRecord.__table__
    with session.no_autoflush:
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        record = session.execute(
            insert(table).values(
                id=uuid4(), user_id=user_id, payload=payload.model_dump(mode="json"),
                state="pending", created_at=now, expires_at=now + PROPOSAL_TTL,
            ).returning(table)
        ).mappings().one()
    return StoredWorkoutProposal.model_validate(record)


def _locked_record(session: Session, proposal_id: UUID, user_id: UUID) -> RowMapping:
    table = WorkoutProposalRecord.__table__
    record = session.execute(
        select(table).where(table.c.id == proposal_id, table.c.user_id == user_id).with_for_update()
    ).mappings().one_or_none()
    if record is None:
        raise ProposalNotFoundError("Proposal not found")
    return record


def _close_record(
    session: Session, record: RowMapping, state: ProposalState, now: datetime
) -> StoredWorkoutProposal:
    table = WorkoutProposalRecord.__table__
    updated = session.execute(
        update(table).where(
            table.c.id == record["id"], table.c.user_id == record["user_id"],
            table.c.state == "pending",
        ).values(state=state, resolved_at=now).returning(table)
    ).mappings().one_or_none()
    if updated is None:
        raise ProposalClosedError("Proposal is no longer pending")
    return StoredWorkoutProposal.model_validate(updated)


def get_workout_proposal(
    proposal_id: UUID, *, session: Session, user_id: UUID
) -> StoredWorkoutProposal:
    """Read an owned snapshot, lazily recording expiration in the caller's transaction."""
    with session.no_autoflush:
        record = _locked_record(session, proposal_id, user_id)
        if record["state"] == "pending":
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            if now >= record["expires_at"]:
                return _close_record(session, record, "expired", now)
        return StoredWorkoutProposal.model_validate(record)


def decide_workout_proposal(
    proposal_id: UUID,
    decision: ProposalDecision,
    *,
    session: Session,
    user_id: UUID,
) -> StoredWorkoutProposal:
    """Apply one explicit human decision. This function is never an LLM tool.

    Ownership, state and database wall-clock time are checked under a row lock.
    Expired attempts return state=expired, never a successful confirmation.
    The caller commits the transition, including expiration, or rolls it back.
    """
    if decision not in ("confirm", "reject"):
        raise ValueError("Decision is not supported")
    with session.no_autoflush:
        record = _locked_record(session, proposal_id, user_id)
        if record["state"] != "pending":
            raise ProposalClosedError("Proposal is no longer pending")
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        state = next_proposal_state(
            record["state"], decision, now=now, expires_at=record["expires_at"]
        )
        return _close_record(session, record, state, now)
