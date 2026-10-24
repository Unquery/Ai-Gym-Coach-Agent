"""Public HTTP request and response schemas."""

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


class ExerciseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ExerciseCreate(BaseModel):
    name: Annotated[
        str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
    ]


class WorkoutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: Annotated[int, Field(gt=0, strict=True)] | None = None
    started_at: AwareDatetime | None = None


class WorkoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: UUID
    plan_id: int | None
    started_at: AwareDatetime
