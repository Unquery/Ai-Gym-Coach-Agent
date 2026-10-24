"""Public HTTP request and response schemas."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class ExerciseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ExerciseCreate(BaseModel):
    name: Annotated[
        str, StringConstraints(min_length=1, max_length=120, strip_whitespace=True)
    ]
