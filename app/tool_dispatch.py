from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas import WorkoutRead
from app.tool_schemas import GetExerciseProgressInput, GetRecentWorkoutsInput
from app.tools import ExerciseProgressResult, get_exercise_progress, get_recent_workouts


def execute_read_only_tool(
    name: str,
    arguments_json: str,
    *,
    session: Session,
    user_id: UUID,
) -> list[WorkoutRead] | ExerciseProgressResult:
    if name == "get_recent_workouts":
        arguments = GetRecentWorkoutsInput.model_validate_json(arguments_json)
        return get_recent_workouts(arguments, session=session, user_id=user_id)
    if name == "get_exercise_progress":
        progress_arguments = GetExerciseProgressInput.model_validate_json(arguments_json)
        return get_exercise_progress(progress_arguments, session=session, user_id=user_id)
    raise ValueError("Unknown read-only tool")
