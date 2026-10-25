from uuid import UUID

from sqlalchemy.orm import Session

from app.schemas import WorkoutRead
from app.tool_schemas import GetRecentWorkoutsInput
from app.tools import get_recent_workouts


def execute_read_only_tool(
    name: str,
    arguments_json: str,
    *,
    session: Session,
    user_id: UUID,
) -> list[WorkoutRead]:
    if name != "get_recent_workouts":
        raise ValueError("Unknown read-only tool")

    result = GetRecentWorkoutsInput.model_validate_json(arguments_json)

    return get_recent_workouts(result, session=session, user_id=user_id)
