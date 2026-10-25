"""Typed agent tools; identity and database sessions come from server code."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.queries import get_workout_history
from app.schemas import WorkoutRead
from app.tool_schemas import GetRecentWorkoutsInput


def get_recent_workouts(
    arguments: GetRecentWorkoutsInput, *, session: Session, user_id: UUID
) -> list[WorkoutRead]:
    """Return recent workouts for the server-selected owner without writing data."""
    workouts = get_workout_history(
        session, user_id=user_id, limit=arguments.limit, offset=arguments.offset
    )
    return [WorkoutRead.model_validate(workout) for workout in workouts]
