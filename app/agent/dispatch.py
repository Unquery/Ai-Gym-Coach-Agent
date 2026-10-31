from uuid import UUID

from pydantic import BaseModel, ValidationError
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


class UnknownToolError(ValueError):
    """The selected workflow does not allow this tool."""


class InvalidToolArgumentsError(ValueError):
    """Arguments failed validation before tool execution started."""


def _validate_arguments[Arguments: BaseModel](model: type[Arguments], arguments_json: str) -> Arguments:
    try:
        return model.model_validate_json(arguments_json)
    except ValidationError:
        raise InvalidToolArgumentsError("Invalid tool arguments") from None


def execute_read_only_tool(
    name: str,
    arguments_json: str,
    *,
    session: Session,
    user_id: UUID,
) -> list[WorkoutRead] | ExerciseProgressResult:
    if name == "get_recent_workouts":
        arguments = _validate_arguments(GetRecentWorkoutsInput, arguments_json)
        return get_recent_workouts(arguments, session=session, user_id=user_id)
    if name == "get_exercise_progress":
        progress_arguments = _validate_arguments(GetExerciseProgressInput, arguments_json)
        return get_exercise_progress(progress_arguments, session=session, user_id=user_id)
    raise UnknownToolError("Unknown read-only tool")


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
        _validate_arguments(ProposeDraftInput, arguments_json)
        return propose_active_workout_draft(context=draft_context, session=session, user_id=user_id)
    if name == APPEND_DRAFT_SET_TOOL_NAME and draft_context is not None:
        set_arguments = _validate_arguments(WorkoutSetDraft, arguments_json)
        return append_set_to_active_draft(
            set_arguments, context=draft_context, session=session, user_id=user_id,
        )
    if name in {UPDATE_DRAFT_SET_TOOL_NAME, REMOVE_DRAFT_SET_TOOL_NAME} and draft_context is not None:
        change_arguments = (
            _validate_arguments(UpdateDraftSetInput, arguments_json)
            if name == UPDATE_DRAFT_SET_TOOL_NAME
            else _validate_arguments(DraftSetTargetInput, arguments_json)
        )
        return change_set_in_active_draft(
            change_arguments, context=draft_context, session=session, user_id=user_id,
        )
    if name == DRAFT_TOOL_NAME and draft_context is None:
        arguments = _validate_arguments(WorkoutLoggingDraft, arguments_json)
        return create_workout_logging_proposal(arguments, session=session, user_id=user_id)
    return execute_read_only_tool(name, arguments_json, session=session, user_id=user_id)
