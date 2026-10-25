"""Public HTTP request and response schemas."""

from decimal import Decimal
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


class WorkoutSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: Annotated[int, Field(gt=0, le=2147483647, strict=True)]
    set_number: Annotated[int, Field(gt=0, le=2147483647, strict=True)]
    reps: Annotated[int, Field(gt=0, le=2147483647, strict=True)]
    weight_kg: Annotated[
        Decimal, Field(ge=0, lt=100000, decimal_places=2, allow_inf_nan=False)
    ]


class WorkoutSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workout_id: int
    exercise_id: int
    set_number: int
    reps: int
    weight_kg: Decimal
