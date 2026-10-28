from typing import Annotated, Self

from openai.types.chat import ChatCompletionToolUnionParam
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.workout_proposals import DRAFT_TOOL_NAME, WorkoutLoggingDraft


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
                    "Read the current user's recent workouts, newest first. "
                    "Returns workout IDs, owner IDs, optional plan IDs, and start times; "
                    "does not include performed sets or progress statistics."
                ),
                "parameters": GetRecentWorkoutsInput.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_exercise_progress",
                "description": (
                    "Read deterministic statistics for one exercise belonging to the "
                    "current user's workout history. Returns per-session reps, sets, "
                    "external weight/volume and absolute changes. Optional timezone-aware "
                    "start_at is inclusive and end_at is exclusive, filtering workout "
                    "start times. Missing bounds are unrestricted. Zero external load "
                    "does not mean zero physical effort."
                ),
                "parameters": GetExerciseProgressInput.model_json_schema(),
            },
        },
    ]


def get_agent_tool_schemas() -> list[ChatCompletionToolUnionParam]:
    """Allow catalog-validated pending proposals alongside the existing read tools."""
    return [*get_read_only_tool_schemas(), {
        "type": "function",
        "function": {
            "name": DRAFT_TOOL_NAME,
            "description": (
                "Propose logging explicitly described performed sets for human review. "
                "Each sets item has exactly exercise_name, reps, and weight_kg. "
                "weight_kg is external weight in kilograms; do not rename this field. "
                "Preserve original exercise names, reps and set order. "
                "Explicit 'no additional weight' or 'no extra weight' means "
                "weight_kg=0 for every affected set. Use null for missing/unclear "
                "reps or weight; never invent zero. The backend "
                "resolves catalog names and may store a pending proposal only. "
                "Never confirms or creates a workout. Request this tool alone."
            ),
            "parameters": WorkoutLoggingDraft.model_json_schema(),
        },
    }]
