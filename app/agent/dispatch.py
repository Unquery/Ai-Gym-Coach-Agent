from uuid import UUID

from sqlalchemy.orm import Session

from app.workouts.schemas import WorkoutRead
from app.agent.tool_schemas import (
    APPEND_DRAFT_SET_TOOL_NAME, PROPOSE_DRAFT_TOOL_NAME, REMOVE_DRAFT_SET_TOOL_NAME, UPDATE_DRAFT_SET_TOOL_NAME,
    DraftSetTargetInput, GetExerciseProgressInput, GetRecentWorkoutsInput, ProposeDraftInput, UpdateDraftSetInput,
)
from app.agent.tools import (
    DraftMutationContext, DraftMutationToolResult, ExerciseProgressResult, WorkoutProposalToolResult,
    append_set_to_active_draft, change_set_in_active_draft, create_workout_logging_proposal,
    get_exercise_progress, get_recent_workouts, propose_active_workout_draft,
)
from app.workouts.proposals import DRAFT_TOOL_NAME, WorkoutLoggingDraft, WorkoutSetDraft


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
    draft_context: DraftMutationContext | None = None,
) -> list[WorkoutRead] | ExerciseProgressResult | WorkoutProposalToolResult | DraftMutationToolResult:
    """Allow reads and the backend-selected draft/proposal workflow, never confirmation."""
    if name == PROPOSE_DRAFT_TOOL_NAME and draft_context is not None:
        ProposeDraftInput.model_validate_json(arguments_json)
        return propose_active_workout_draft(context=draft_context, session=session, user_id=user_id)
    if name == APPEND_DRAFT_SET_TOOL_NAME and draft_context is not None:
        set_arguments = WorkoutSetDraft.model_validate_json(arguments_json)
        return append_set_to_active_draft(
            set_arguments, context=draft_context, session=session, user_id=user_id,
        )
    if name in {UPDATE_DRAFT_SET_TOOL_NAME, REMOVE_DRAFT_SET_TOOL_NAME} and draft_context is not None:
        change_arguments = (
            UpdateDraftSetInput.model_validate_json(arguments_json)
            if name == UPDATE_DRAFT_SET_TOOL_NAME
            else DraftSetTargetInput.model_validate_json(arguments_json)
        )
        return change_set_in_active_draft(
            change_arguments, context=draft_context, session=session, user_id=user_id,
        )
    if name == DRAFT_TOOL_NAME and draft_context is None:
        arguments = WorkoutLoggingDraft.model_validate_json(arguments_json)
        return create_workout_logging_proposal(arguments, session=session, user_id=user_id)
    return execute_read_only_tool(name, arguments_json, session=session, user_id=user_id)
