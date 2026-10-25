from typing import Annotated

from openai.types.chat import ChatCompletionToolUnionParam
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class GetRecentWorkoutsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(gt=0, le=100, strict=True)] = 20
    offset: Annotated[int, Field(ge=0, le=2147483647, strict=True)] = 0


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
        }
    ]
