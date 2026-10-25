from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


class GetRecentWorkoutsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(gt=0, le=100, strict=True)] = 20
    offset: Annotated[int, Field(ge=0, le=2147483647, strict=True)] = 0
