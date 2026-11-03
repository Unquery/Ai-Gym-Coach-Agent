from typing import Annotated, Literal, Self
from uuid import UUID

from openai.types.chat import ChatCompletionToolUnionParam
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.workouts.proposals import DRAFT_TOOL_NAME, WorkoutLoggingDraft, WorkoutSetDraft

APPEND_DRAFT_SET_TOOL_NAME = "append_workout_draft_set"
UPDATE_DRAFT_SET_TOOL_NAME = "update_workout_draft_set"
REMOVE_DRAFT_SET_TOOL_NAME = "remove_workout_draft_set"
PROPOSE_DRAFT_TOOL_NAME = "propose_workout_draft_logging"
DRAFT_MUTATION_TOOL_NAMES = frozenset({
    APPEND_DRAFT_SET_TOOL_NAME, UPDATE_DRAFT_SET_TOOL_NAME, REMOVE_DRAFT_SET_TOOL_NAME,
})
DRAFT_WRITE_TOOL_NAMES = DRAFT_MUTATION_TOOL_NAMES | {PROPOSE_DRAFT_TOOL_NAME}


class ProposeDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftSetTargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Null means the user's target is missing or ambiguous, never "the last set".
    set_id: UUID | None


class UpdateDraftSetInput(DraftSetTargetInput):
    # The existing service validates the merged set with PersistentWorkoutDraftSet.
    changes: dict[Literal["reps", "weight_kg"], object] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        # Reuse the set's field rules before execution; do not fill omitted patch keys.
        # The service still revalidates the actual merged snapshot under its lock.
        WorkoutSetDraft.model_validate({
            "exercise_name": "Draft set", "reps": self.changes.get("reps"),
            "weight_kg": self.changes.get("weight_kg"),
        })
        return self


class GetRecentWorkoutsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(gt=0, le=100, strict=True)] = 20
    offset: Annotated[int, Field(ge=0, le=2147483647, strict=True)] = 0


class GetExerciseProgressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: Annotated[int, Field(gt=0, le=2147483647, strict=True)]
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        if self.start_at is None or self.end_at is None:
            return self
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be earlier than end_at")
        return self


def get_read_only_tool_schemas() -> list[ChatCompletionToolUnionParam]:
    """Describe the read-only tools available to the model."""
    return [
        {
            "type": "function",
            "function": {
                "name": "get_recent_workouts",
                "description": (
                    "List the current user's workout metadata, newest first. "
                    "No performed sets or statistics; use get_exercise_progress for comparisons."
                ),
                "parameters": GetRecentWorkoutsInput.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_exercise_progress",
                "description": (
                    "Read the current user's deterministic exercise statistics: session sets/reps, "
                    "external weight/volume and absolute changes. Ask for unknown exercise IDs. "
                    "Timezone-aware start_at is inclusive, end_at exclusive, on workout start time; "
                    "missing bounds are unrestricted. Zero external load is not zero effort."
                ),
                "parameters": GetExerciseProgressInput.model_json_schema(),
            },
        },
    ]


def get_agent_tool_schemas(*, draft_enabled: bool = False) -> list[ChatCompletionToolUnionParam]:
    """Expose reads and either draft mutations or pending proposal creation."""
    if draft_enabled:
        return [*get_read_only_tool_schemas(), {
            "type": "function",
            "function": {
                "name": APPEND_DRAFT_SET_TOOL_NAME,
                "description": (
                    "Append one explicitly requested performed set to the active draft, alone. "
                    "Use exercise_name, reps, weight_kg (external kg). Unknown reps/weight: null; "
                    "zero needs explicit no extra load. Unclear exercise/reps/set count: clarify. "
                    "Never confirms or logs a workout."
                ),
                "parameters": WorkoutSetDraft.model_json_schema(),
            },
        }, {
            "type": "function",
            "function": {
                "name": UPDATE_DRAFT_SET_TOOL_NAME,
                "description": (
                    "Edit one explicit draft set, alone. Copy set_id from the current snapshot; "
                    "null if unclear. In changes send only requested reps/weight_kg: omitted "
                    "stays unchanged, null explicitly clears. Never guess. Backend selects "
                    "the version. No confirmed-workout changes or consent."
                ),
                "parameters": UpdateDraftSetInput.model_json_schema(),
            },
        }, {
            "type": "function",
            "function": {
                "name": REMOVE_DRAFT_SET_TOOL_NAME,
                "description": (
                    "Remove one explicit draft set, alone. Copy set_id from the current snapshot; "
                    "null if unclear, never guess between repeated sets. Backend selects the "
                    "version. No confirmed-workout deletion or consent."
                ),
                "parameters": DraftSetTargetInput.model_json_schema(),
            },
        }, {
            "type": "function",
            "function": {
                "name": PROPOSE_DRAFT_TOOL_NAME,
                "description": (
                    "Submit the saved draft for review only on explicit user request, alone with {}. "
                    "Backend copies ordered saved sets; no model-supplied sets. Empty/incomplete "
                    "drafts require clarification. Creates an immutable pending proposal, "
                    "never confirmation or a saved workout."
                ),
                "parameters": ProposeDraftInput.model_json_schema(),
            },
        }]
    return [*get_read_only_tool_schemas(), {
        "type": "function",
        "function": {
            "name": DRAFT_TOOL_NAME,
            "description": (
                "Propose explicitly described performed sets for human review, alone. Each item "
                "has exactly exercise_name, reps, weight_kg (external kg). Preserve names/reps/order. "
                "Explicit 'no additional weight'/'no extra weight': weight_kg=0 for affected sets; "
                "missing/unclear reps or weight: null, never invented zero. Backend resolves "
                "catalog names and stores only a pending proposal, never confirmation or a workout."
            ),
            "parameters": WorkoutLoggingDraft.model_json_schema(),
        },
    }]
