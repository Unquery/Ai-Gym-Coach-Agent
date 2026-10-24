"""Public HTTP response schemas."""

from pydantic import BaseModel, ConfigDict


class ExerciseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
