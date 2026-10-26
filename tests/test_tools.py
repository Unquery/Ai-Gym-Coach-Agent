import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, event
from sqlalchemy.orm import Session

from app.models import Exercise, Workout, WorkoutPlan, WorkoutSet
from app.schemas import WorkoutRead
from app.tool_dispatch import execute_read_only_tool
from app.tool_schemas import GetExerciseProgressInput, GetRecentWorkoutsInput
from app.tools import get_exercise_progress, get_recent_workouts


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


@pytest.mark.parametrize(
    "bounds",
    [
        {},
        {"start_at": "2026-01-01T00:00:00Z"},
        {"end_at": "2026-02-01T00:00:00Z"},
        {"start_at": "2026-01-01T02:00:00+02:00", "end_at": "2026-01-01T01:00:00Z"},
    ],
)
def test_progress_input_accepts_optional_aware_time_bounds(bounds: dict[str, str]) -> None:
    arguments = GetExerciseProgressInput.model_validate({"exercise_id": 1, **bounds})
    assert arguments.exercise_id == 1
    assert GetExerciseProgressInput(exercise_id=2147483647).start_at is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        *[{"exercise_id": value} for value in (0, 2147483648, True, "1", 1.0)],
        {"exercise_id": 1, "user_id": str(uuid4())},
        {"exercise_id": 1, "start_at": "2026-01-01T00:00:00"},
        {"exercise_id": 1, "end_at": "invalid"},
        {"exercise_id": 1, "start_at": "2026-01-02T00:00:00Z", "end_at": "2026-01-01T00:00:00Z"},
        {"exercise_id": 1, "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-01-01T01:00:00+01:00"},
    ],
)
def test_progress_dispatch_rejects_invalid_arguments_before_sql(
    payload: dict[str, object],
) -> None:
    reader = Mock(spec=Session)
    with pytest.raises(ValidationError):
        execute_read_only_tool(
            "get_exercise_progress", json.dumps(payload),
            session=reader, user_id=uuid4(),
        )
    assert reader.mock_calls == []


@pytest.fixture
def exercise_progress_history(session: Session) -> tuple[UUID, UUID, int, list[int]]:
    owner, other_owner = uuid4(), uuid4()
    exercise = Exercise(name="Synthetic progress exercise")
    other_exercise = Exercise(name="Synthetic unrelated exercise")
    owned = [
        Workout(
            user_id=owner,
            started_at=datetime(2026, 1, day, tzinfo=UTC),
            sets=[WorkoutSet(
                exercise=exercise,
                set_number=1,
                reps={1: 6, 2: 8, 3: 10}.get(day, 1),
                weight_kg=Decimal(0 if day == 2 else 10 if day <= 3 else 1),
            )],
        )
        for day in range(1, 23)
    ]
    owned[1].sets.append(WorkoutSet(
        exercise=other_exercise, set_number=1, reps=100, weight_kg=Decimal(100)
    ))
    session.add_all([
        *reversed(owned),
        Workout(
            user_id=other_owner,
            started_at=datetime(2026, 1, 2, tzinfo=UTC),
            sets=[WorkoutSet(
                exercise=exercise, set_number=1, reps=999, weight_kg=Decimal(99)
            )],
        ),
        Workout(
            user_id=owner,
            started_at=datetime(2026, 1, 2, tzinfo=UTC),
            sets=[WorkoutSet(
                exercise=other_exercise, set_number=1, reps=100, weight_kg=Decimal(100)
            )],
        ),
        Workout(user_id=owner, started_at=datetime(2026, 1, 2, tzinfo=UTC)),
    ])
    session.commit()
    identity = owner, other_owner, exercise.id, [row.id for row in owned]
    session.expunge_all()
    return identity


@pytest.mark.postgres
def test_progress_reads_only_owned_exercise_and_period_without_writes(
    session: Session,
    tool_query_log: list[str],
    exercise_progress_history: tuple[UUID, UUID, int, list[int]],
) -> None:
    owner, _, exercise_id, workout_ids = exercise_progress_history
    pending = Workout(user_id=owner)
    session.add(pending)
    tool_query_log.clear()

    result = get_exercise_progress(
        GetExerciseProgressInput(
            exercise_id=exercise_id,
            start_at="2026-01-02T02:00:00+02:00",
            end_at="2026-01-04T00:00:00Z",
        ),
        session=session, user_id=owner,
    )

    assert result.status == "ok"
    assert result.exercise_id == exercise_id
    assert [row.workout_id for row in result.sessions] == workout_ids[1:3]
    assert [row.total_reps for row in result.sessions] == [8, 10]
    assert [row.set_count for row in result.sessions] == [1, 1]
    assert [row.external_volume_kg_reps for row in result.sessions] == [Decimal(0), Decimal(100)]
    assert [row.has_external_load for row in result.sessions] == [False, True]
    assert result.sessions[0].delta_total_reps is None
    assert result.sessions[1].delta_total_reps == 2
    assert result.sessions[1].delta_external_volume_kg_reps == Decimal(100)
    assert result.model_dump(mode="json")["sessions"][1]["external_volume_kg_reps"] == "100.00"
    assert pending in session.new and pending.id is None
    assert session.in_transaction()
    assert len(tool_query_log) == 3
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in tool_query_log)


@pytest.mark.postgres
@pytest.mark.parametrize("bound", [None, "start_at", "end_at"])
def test_progress_does_not_truncate_history_and_supports_single_bounds(
    session: Session,
    exercise_progress_history: tuple[UUID, UUID, int, list[int]],
    bound: str | None,
) -> None:
    owner, _, exercise_id, workout_ids = exercise_progress_history
    bounds = {} if bound is None else {
        bound: "2026-01-03T00:00:00Z" if bound == "start_at" else "2026-01-04T00:00:00Z"
    }
    result = get_exercise_progress(
        GetExerciseProgressInput.model_validate({"exercise_id": exercise_id, **bounds}),
        session=session, user_id=owner,
    )
    expected_ids = workout_ids if bound is None else (
        workout_ids[2:] if bound == "start_at" else workout_ids[:3]
    )
    assert result.status == "ok"
    assert [row.workout_id for row in result.sessions] == expected_ids
    assert result.sessions[0].delta_total_reps is None


@pytest.mark.postgres
@pytest.mark.parametrize("status", ["exercise_not_found", "no_history", "insufficient_history"])
def test_progress_distinguishes_missing_exercise_and_insufficient_history(
    session: Session,
    tool_query_log: list[str],
    exercise_progress_history: tuple[UUID, UUID, int, list[int]],
    status: str,
) -> None:
    owner, other_owner, exercise_id, _ = exercise_progress_history
    if status == "exercise_not_found":
        exercise_id = 2147483647
    bounds = {"start_at": "2027-01-01T00:00:00Z"} if status == "no_history" else {}
    tool_query_log.clear()
    result = get_exercise_progress(
        GetExerciseProgressInput.model_validate({"exercise_id": exercise_id, **bounds}),
        session=session, user_id=other_owner if status == "insufficient_history" else owner,
    )
    assert result.status == status
    assert result.exercise_id == exercise_id
    if status == "insufficient_history":
        assert len(result.sessions) == 1
        assert result.sessions[0].total_reps == 999
        assert result.sessions[0].delta_total_reps is None
        assert result.sessions[0].delta_external_volume_kg_reps is None
    else:
        assert result.sessions == []
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in tool_query_log)


@pytest.mark.postgres
def test_progress_does_not_use_another_owners_history(
    session: Session,
    exercise_progress_history: tuple[UUID, UUID, int, list[int]],
) -> None:
    _, _, exercise_id, _ = exercise_progress_history
    result = get_exercise_progress(
        GetExerciseProgressInput(exercise_id=exercise_id), session=session, user_id=uuid4()
    )
    assert result.status == "no_history"
    assert result.sessions == []
