"""Typed agent tools; identity and database sessions come from server code."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Exercise
from app.progress import ExerciseWorkoutProgress, analyze_exercise_progress
from app.queries import get_exercise_workouts, get_workout_history
from app.schemas import WorkoutDetailRead, WorkoutRead
from app.tool_schemas import GetExerciseProgressInput, GetRecentWorkoutsInput


class ExerciseProgressResult(BaseModel):
    exercise_id: int
    status: Literal[
        "exercise_not_found", "no_history", "insufficient_history", "ok"
    ]
    sessions: list[ExerciseWorkoutProgress]


def get_recent_workouts(
    arguments: GetRecentWorkoutsInput, *, session: Session, user_id: UUID
) -> list[WorkoutRead]:
    """Return recent workouts for the server-selected owner without writing data."""
    workouts = get_workout_history(
        session, user_id=user_id, limit=arguments.limit, offset=arguments.offset
    )
    return [WorkoutRead.model_validate(workout) for workout in workouts]


def get_exercise_progress(
    arguments: GetExerciseProgressInput, *, session: Session, user_id: UUID
) -> ExerciseProgressResult:
    """Read one owner's exercise statistics without writing or calling an LLM."""
    with session.no_autoflush:
        exercise_id = session.scalar(
            select(Exercise.id).where(Exercise.id == arguments.exercise_id)
        )
        if exercise_id is None:
            return ExerciseProgressResult(
                exercise_id=arguments.exercise_id,
                status="exercise_not_found",
                sessions=[],
            )
        workouts = get_exercise_workouts(
            session,
            user_id=user_id,
            exercise_id=exercise_id,
            start_at=arguments.start_at,
            end_at=arguments.end_at,
        )
        progress = analyze_exercise_progress(
            exercise_id, [WorkoutDetailRead.model_validate(row) for row in workouts]
        )
        if not progress:
            return ExerciseProgressResult(
                exercise_id=exercise_id, status="no_history", sessions=[]
            )
        return ExerciseProgressResult(
            exercise_id=exercise_id,
            status="insufficient_history" if len(progress) == 1 else "ok",
            sessions=progress,
        )
