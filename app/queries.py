"""Database reads shared by HTTP handlers and agent tools."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Workout, WorkoutSet


def get_workout_history(
    session: Session, *, user_id: UUID, limit: int, offset: int
) -> list[Workout]:
    """Read one owner's history without flushing pending session changes."""
    with session.no_autoflush:
        result = session.scalars(
            select(Workout)
            .where(Workout.user_id == user_id)
            .order_by(Workout.started_at.desc(), Workout.id.desc())
            .limit(limit=limit)
            .offset(offset=offset)
        )
        return list(result)


def get_exercise_workouts(
    session: Session,
    *,
    user_id: UUID,
    exercise_id: int,
    start_at: datetime | None,
    end_at: datetime | None,
) -> list[Workout]:
    """Load matching owned sessions and sets without truncating or flushing history."""
    statement = (
        select(Workout)
        .where(
            Workout.user_id == user_id,
            Workout.sets.any(WorkoutSet.exercise_id == exercise_id),
        )
        .options(selectinload(Workout.sets))
        .order_by(Workout.started_at, Workout.id)
    )
    if start_at is not None:
        statement = statement.where(Workout.started_at >= start_at)
    if end_at is not None:
        statement = statement.where(Workout.started_at < end_at)
    with session.no_autoflush:
        return list(session.scalars(statement))
