from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas import WorkoutRead
from app.tool_schemas import GetExerciseProgressInput, GetRecentWorkoutsInput
from app.tools import (
    ExerciseProgressResult, WorkoutProposalToolResult, create_workout_logging_proposal,
    get_exercise_progress, get_recent_workouts,
)
from app.workout_proposals import DRAFT_TOOL_NAME, WorkoutLoggingDraft


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


def execute_agent_tool(
    name: str,
    arguments_json: str,
    *,
    session: Session,
    user_id: UUID,
) -> list[WorkoutRead] | ExerciseProgressResult | WorkoutProposalToolResult:
    """Allow only reads and pending proposal creation, never confirmation/execution."""
    if name == DRAFT_TOOL_NAME:
        arguments = WorkoutLoggingDraft.model_validate_json(arguments_json)
        return create_workout_logging_proposal(arguments, session=session, user_id=user_id)
    return execute_read_only_tool(name, arguments_json, session=session, user_id=user_id)
