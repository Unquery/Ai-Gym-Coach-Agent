"""Execute owner-scoped, explicitly confirmed workout snapshots atomically."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.models import Exercise, Workout, WorkoutProposalRecord, WorkoutSet
from app.proposal_store import WorkoutProposalPayload, get_workout_proposal
from app.schemas import WorkoutDetailRead


class ProposalNotConfirmedError(ValueError):
    """Only an explicitly confirmed proposal may be executed."""


class ProposalCatalogChangedError(ValueError):
    """A reviewed exercise was removed or renamed; obtain a new proposal and consent."""


def build_workout_set_values(
    payload: WorkoutProposalPayload,
) -> list[dict[str, int | Decimal]]:
    result: list[dict[str, int | Decimal]] = []

    for position, item in enumerate(payload.sets, start=1):
        result.append(
            {
                "exercise_id": item.exercise_id,
                "set_number": item.set_number,
                "reps": item.reps,
                "weight_kg": item.weight_kg,
                "position": position,
            },
        )
    return result


def execute_confirmed_workout(
    proposal_id: UUID, *, session: Session, user_id: UUID,
) -> WorkoutDetailRead:
    """Execute stored human consent once; never expose this function as an LLM tool.

    Use a dedicated session inside the caller's session.begin() block. The caller
    must let failures roll back that entire block and report success only after
    commit. This function neither commits nor flushes unrelated ORM objects.
    The proposal row lock is held until the transaction ends, including on replay.
    """
    with session.no_autoflush:
        proposal = get_workout_proposal(proposal_id, session=session, user_id=user_id)
        if proposal.state != "confirmed":
            raise ProposalNotConfirmedError("Proposal has not been confirmed")

        workout_id = proposal.workout_id
        if workout_id is None:
            exercise_ids = {item.exercise_id for item in proposal.payload.sets}
            # FOR SHARE prevents catalog edits/deletions until the write commits.
            catalog = dict(session.execute(
                select(Exercise.id, Exercise.name).where(Exercise.id.in_(exercise_ids))
                .order_by(Exercise.id).with_for_update(read=True)
            ).tuples().all())
            if any(
                catalog.get(item.exercise_id) != item.catalog_name
                for item in proposal.payload.sets
            ):
                raise ProposalCatalogChangedError("Proposal exercise catalog has changed")

            workouts = Workout.__table__
            workout_id = session.execute(
                insert(workouts).values(user_id=user_id).returning(workouts.c.id)
            ).scalar_one()
            for values in build_workout_set_values(proposal.payload):
                session.execute(insert(WorkoutSet.__table__).values(workout_id=workout_id, **values))
            proposals = WorkoutProposalRecord.__table__
            session.execute(
                update(proposals).where(
                    proposals.c.id == proposal_id, proposals.c.user_id == user_id,
                    proposals.c.state == "confirmed", proposals.c.workout_id.is_(None),
                ).values(workout_id=workout_id).returning(proposals.c.id)
            ).scalar_one()

        workouts = Workout.__table__
        workout = session.execute(select(workouts).where(
            workouts.c.id == workout_id, workouts.c.user_id == user_id,
        )).mappings().one()
        sets = WorkoutSet.__table__
        rows = session.execute(select(sets).where(sets.c.workout_id == workout_id).order_by(
            sets.c.position.asc().nulls_last(), sets.c.exercise_id, sets.c.set_number,
        )).mappings().all()
        return WorkoutDetailRead.model_validate({**workout, "sets": rows})
