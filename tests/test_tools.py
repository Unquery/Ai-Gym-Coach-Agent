from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, event
from sqlalchemy.orm import Session

from app.models import Workout, WorkoutPlan
from app.schemas import WorkoutRead
from app.tool_schemas import GetRecentWorkoutsInput
from app.tools import get_recent_workouts


def test_recent_workouts_input_defaults_and_boundaries() -> None:
    assert GetRecentWorkoutsInput().model_dump() == {"limit": 20, "offset": 0}
    assert GetRecentWorkoutsInput(limit=1, offset=0).limit == 1
    assert GetRecentWorkoutsInput(limit=100, offset=2147483647).offset == 2147483647


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 2147483648},
        *[
            {field: value}
            for field in ("limit", "offset")
            for value in (None, "3", 3.0, True)
        ],
        {"user_id": str(uuid4())},
    ],
)
def test_recent_workouts_input_rejects_invalid_arguments(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GetRecentWorkoutsInput.model_validate(payload)


@pytest.fixture
def tool_query_log(postgres_engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record_query(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record_query)
    try:
        yield statements
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record_query)


@pytest.mark.postgres
@pytest.mark.parametrize("limit, offset", [(20, 0), (1, 0), (1, 1), (100, 3)])
def test_recent_workouts_filters_orders_and_paginates(
    session: Session, tool_query_log: list[str], limit: int, offset: int
) -> None:
    owner, other_owner = uuid4(), uuid4()
    started_at = datetime(2026, 1, 5, tzinfo=UTC)
    plan = WorkoutPlan(user_id=owner, name="Synthetic tool plan")
    owned = [
        Workout(user_id=owner, started_at=datetime(2026, 1, 1, tzinfo=UTC)),
        Workout(user_id=owner, started_at=started_at, plan=plan),
        Workout(user_id=owner, started_at=started_at),
    ]
    other = Workout(user_id=other_owner, started_at=datetime(2026, 1, 9, tzinfo=UTC))
    session.add_all([*owned, other])
    session.commit()
    expected = [WorkoutRead.model_validate(row) for row in reversed(owned)]
    other_expected = WorkoutRead.model_validate(other)
    session.expunge_all()
    tool_query_log.clear()

    result = get_recent_workouts(
        GetRecentWorkoutsInput(limit=limit, offset=offset), session=session, user_id=owner
    )

    assert result == expected[offset : offset + limit]
    assert len(tool_query_log) == 1
    assert tool_query_log[0].lstrip().upper().startswith("SELECT")
    assert all(isinstance(row.user_id, UUID) for row in result)
    if result:
        serialized = result[0].model_dump(mode="json")
        assert serialized["user_id"] == str(owner)
        assert isinstance(serialized["started_at"], str)

    tool_query_log.clear()
    assert get_recent_workouts(
        GetRecentWorkoutsInput(), session=session, user_id=other_owner
    ) == [other_expected]
    assert len(tool_query_log) == 1
    assert tool_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.postgres
def test_recent_workouts_returns_empty_for_an_owner_without_history(
    session: Session, tool_query_log: list[str]
) -> None:
    session.add(Workout(user_id=uuid4()))
    session.commit()
    tool_query_log.clear()

    assert get_recent_workouts(
        GetRecentWorkoutsInput(), session=session, user_id=uuid4()
    ) == []
    assert len(tool_query_log) == 1
    assert tool_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.postgres
def test_recent_workouts_does_not_flush_or_commit_pending_changes(
    session: Session, tool_query_log: list[str]
) -> None:
    owner = uuid4()
    session.add(Workout(user_id=owner))
    session.commit()
    pending = Workout(user_id=owner)
    session.add(pending)
    tool_query_log.clear()

    result = get_recent_workouts(
        GetRecentWorkoutsInput(), session=session, user_id=owner
    )

    assert len(result) == 1
    assert pending in session.new
    assert pending.id is None
    assert session.in_transaction()
    assert len(tool_query_log) == 1
    assert tool_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.postgres
def test_invalid_tool_arguments_execute_no_sql(
    session: Session, tool_query_log: list[str]
) -> None:
    tool_query_log.clear()
    with pytest.raises(ValidationError):
        get_recent_workouts(
            GetRecentWorkoutsInput.model_validate({"user_id": str(uuid4())}),
            session=session,
            user_id=uuid4(),
        )
    assert tool_query_log == []
