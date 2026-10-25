"""Database reads shared by HTTP handlers and agent tools."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Workout


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
