from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, event, func, select
from sqlalchemy.orm import Session

from app import database
from app.main import app, get_demo_user_id
from app.models import Exercise, Workout, WorkoutPlan, WorkoutPlanExercise, WorkoutSet
from app.seed import DEMO_USER_ID

pytestmark = pytest.mark.postgres


@pytest.fixture
def workout_client(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    with TestClient(app) as client:
        yield client


def make_plan(engine: Engine, owner: UUID) -> int:
    with Session(engine) as writer, writer.begin():
        plan = WorkoutPlan(
            user_id=owner,
            name="Synthetic plan",
            exercises=[
                WorkoutPlanExercise(
                    exercise=Exercise(name="Synthetic squat"),
                    position=1,
                    target_sets=3,
                    target_reps=8,
                )
            ],
        )
        writer.add(plan)
        writer.flush()
        plan_id = plan.id
    return plan_id


@pytest.mark.parametrize("payload", [{}, {"plan_id": None}, {"started_at": None}])
def test_create_workout_without_plan_uses_database_time(
    workout_client: TestClient, postgres_engine: Engine, payload: dict[str, object]
) -> None:
    with postgres_engine.connect() as connection:
        before = connection.scalar(select(func.now()))
        assert before is not None

    response = workout_client.post("/workouts", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "user_id", "plan_id", "started_at"}
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["user_id"] == str(DEMO_USER_ID)
    assert body["plan_id"] is None
    started_at = datetime.fromisoformat(body["started_at"])
    assert started_at.utcoffset() is not None
    with Session(postgres_engine) as reader:
        saved = reader.get(Workout, body["id"])
        assert saved is not None
        assert saved.user_id == DEMO_USER_ID
        assert saved.plan_id is None
        assert saved.sets == []
        assert saved.started_at == started_at
        after = reader.scalar(select(func.now()))
        assert after is not None
        assert before <= saved.started_at <= after


def test_create_workout_preserves_explicit_time(
    workout_client: TestClient, postgres_engine: Engine
) -> None:
    response = workout_client.post(
        "/workouts", json={"started_at": "2026-01-05T12:00:00+02:00"}
    )

    assert response.status_code == 201
    body = response.json()
    expected = datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert datetime.fromisoformat(body["started_at"]) == expected
    with Session(postgres_engine) as reader:
        saved = reader.get(Workout, body["id"])
        assert saved is not None
        assert saved.started_at == expected


def test_create_workout_with_owned_plan_does_not_create_performed_sets(
    workout_client: TestClient, postgres_engine: Engine
) -> None:
    plan_id = make_plan(postgres_engine, DEMO_USER_ID)

    response = workout_client.post("/workouts", json={"plan_id": plan_id})

    assert response.status_code == 201
    body = response.json()
    assert body["plan_id"] == plan_id
    assert body["user_id"] == str(DEMO_USER_ID)
    with Session(postgres_engine) as reader:
        saved = reader.get(Workout, body["id"])
        assert saved is not None
        assert saved.plan_id == plan_id
        assert saved.user_id == DEMO_USER_ID
        assert saved.plan is not None
        assert saved.plan.id == plan_id
        assert len(saved.plan.exercises) == 1
        assert saved.sets == []


@pytest.mark.parametrize("plan_exists", [False, True])
def test_missing_or_other_owners_plan_returns_404_without_writing(
    workout_client: TestClient, postgres_engine: Engine, plan_exists: bool
) -> None:
    plan_id = make_plan(postgres_engine, uuid4()) if plan_exists else 999

    response = workout_client.post("/workouts", json={"plan_id": plan_id})

    assert response.status_code == 404
    assert response.json() == {"detail": "Workout plan not found"}
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Workout)) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"plan_id": 0},
        {"plan_id": -1},
        {"plan_id": "abc"},
        {"plan_id": 1.5},
        {"plan_id": True},
        {"plan_id": 2147483648},
        {"plan_id": 10**40},
        {"started_at": "2026-01-05T10:00:00"},
        {"started_at": "not-a-date"},
        {"user_id": str(uuid4())},
        {"id": 42},
        {"sets": []},
        [],
        None,
    ],
)
def test_invalid_workout_input_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    workout_query_log: list[str],
    payload: object,
) -> None:
    response = workout_client.post("/workouts", json=payload)

    assert response.status_code == 422
    assert workout_query_log == []
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Workout)) == 0


@pytest.mark.parametrize("plan_id", [1, 2147483647])
def test_workout_can_reference_plan_at_integer_boundaries(
    workout_client: TestClient, postgres_engine: Engine, plan_id: int
) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        writer.add(
            WorkoutPlan(id=plan_id, user_id=DEMO_USER_ID, name="Synthetic boundary plan")
        )

    response = workout_client.post("/workouts", json={"plan_id": plan_id})

    assert response.status_code == 201
    assert response.json()["plan_id"] == plan_id
    with Session(postgres_engine) as reader:
        saved = reader.get(Workout, response.json()["id"])
        assert saved is not None
        assert saved.plan_id == plan_id


def test_workout_owner_comes_from_dependency(
    workout_client: TestClient, postgres_engine: Engine
) -> None:
    owner = uuid4()
    plan_id = make_plan(postgres_engine, owner)
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    try:
        response = workout_client.post("/workouts", json={"plan_id": plan_id})
    finally:
        del app.dependency_overrides[get_demo_user_id]

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == str(owner)
    with Session(postgres_engine) as reader:
        saved = reader.get(Workout, body["id"])
        assert saved is not None
        assert saved.user_id == owner
        assert saved.plan_id == plan_id


def test_plan_deleted_after_validation_returns_404_and_rolls_back(
    workout_client: TestClient, postgres_engine: Engine
) -> None:
    plan_id = make_plan(postgres_engine, DEMO_USER_ID)

    def remove_plan(
        session: Session, _flush_context: object, _instances: object
    ) -> None:
        with postgres_engine.begin() as connection:
            connection.execute(delete(WorkoutPlan).where(WorkoutPlan.id == plan_id))

    event.listen(Session, "before_flush", remove_plan)
    try:
        response = workout_client.post("/workouts", json={"plan_id": plan_id})
    finally:
        event.remove(Session, "before_flush", remove_plan)

    assert response.status_code == 404
    assert response.json() == {"detail": "Workout plan not found"}
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Workout)) == 0
    assert workout_client.post("/workouts", json={}).status_code == 201


@pytest.fixture
def workout_query_log(postgres_engine: Engine) -> Iterator[list[str]]:
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


@pytest.mark.parametrize("with_plan", [False, True])
@pytest.mark.parametrize("with_sets", [False, True])
def test_get_workout_returns_its_ordered_sets_with_two_queries(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    workout_query_log: list[str],
    with_plan: bool,
    with_sets: bool,
) -> None:
    workout_id, exercise_id = set_target
    plan_id = make_plan(postgres_engine, DEMO_USER_ID) if with_plan else None
    started_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    with Session(postgres_engine) as writer, writer.begin():
        workout = writer.get(Workout, workout_id)
        assert workout is not None
        workout.plan_id = plan_id
        workout.started_at = started_at
        performed_sets: list[WorkoutSet] = []
        if with_sets:
            other_exercise = Exercise(name="Synthetic bench press")
            writer.add(other_exercise)
            writer.flush()
            performed_sets = [
                WorkoutSet(
                    workout_id=workout_id,
                    exercise_id=other_exercise.id,
                    set_number=1,
                    reps=5,
                    weight_kg=Decimal("0.00"),
                ),
                WorkoutSet(
                    workout_id=workout_id,
                    exercise_id=exercise_id,
                    set_number=2,
                    reps=8,
                    weight_kg=Decimal("30.25"),
                ),
                WorkoutSet(
                    workout_id=workout_id,
                    exercise_id=exercise_id,
                    set_number=1,
                    reps=10,
                    weight_kg=Decimal("30.25"),
                ),
            ]
            writer.add_all(performed_sets)
        writer.add(
            Workout(
                user_id=DEMO_USER_ID,
                sets=[
                    WorkoutSet(
                        exercise_id=exercise_id,
                        set_number=1,
                        reps=99,
                        weight_kg=Decimal("77.00"),
                    )
                ],
            )
        )
        writer.flush()
        expected_sets = [
            {
                "id": row.id,
                "workout_id": workout_id,
                "exercise_id": row.exercise_id,
                "set_number": row.set_number,
                "reps": row.reps,
                "weight_kg": str(row.weight_kg),
            }
            for row in sorted(
                performed_sets, key=lambda row: (row.exercise_id, row.set_number)
            )
        ]
    workout_query_log.clear()

    response = workout_client.get(f"/workouts/{workout_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "user_id", "plan_id", "started_at", "sets"}
    assert body["id"] == workout_id
    assert body["user_id"] == str(DEMO_USER_ID)
    assert body["plan_id"] == plan_id
    assert datetime.fromisoformat(body["started_at"]) == started_at
    assert body["sets"] == expected_sets
    assert len(workout_query_log) == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in workout_query_log)


@pytest.mark.parametrize("exists", [False, True])
def test_get_missing_or_other_owners_workout_returns_404_with_one_query(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    workout_query_log: list[str],
    exists: bool,
) -> None:
    workout_id = set_target[0] if exists else 999
    if exists:
        with Session(postgres_engine) as writer, writer.begin():
            workout = writer.get(Workout, workout_id)
            assert workout is not None
            workout.user_id = uuid4()
            workout.sets.append(
                WorkoutSet(
                    exercise_id=set_target[1],
                    set_number=1,
                    reps=10,
                    weight_kg=Decimal("30.25"),
                )
            )
    workout_query_log.clear()

    response = workout_client.get(f"/workouts/{workout_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Workout not found"}
    assert len(workout_query_log) == 1
    assert workout_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("workout_id", ["abc", "1.5", "0", "-1", "2147483648"])
def test_get_invalid_workout_id_returns_422_without_querying(
    workout_client: TestClient, workout_query_log: list[str], workout_id: str
) -> None:
    response = workout_client.get(f"/workouts/{workout_id}")

    assert response.status_code == 422
    assert workout_query_log == []


def test_get_workout_uses_owner_dependency(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as writer, writer.begin():
        workout = writer.get(Workout, set_target[0])
        assert workout is not None
        workout.user_id = owner
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    try:
        response = workout_client.get(f"/workouts/{set_target[0]}")
    finally:
        del app.dependency_overrides[get_demo_user_id]

    assert response.status_code == 200
    assert response.json()["user_id"] == str(owner)


@pytest.fixture
def set_target(postgres_engine: Engine) -> tuple[int, int]:
    with Session(postgres_engine) as writer, writer.begin():
        workout = Workout(user_id=DEMO_USER_ID)
        exercise = Exercise(name="Synthetic pull-up")
        writer.add_all([workout, exercise])
        writer.flush()
        target = workout.id, exercise.id
    return target


@pytest.fixture
def set_payload(set_target: tuple[int, int]) -> dict[str, object]:
    return {
        "exercise_id": set_target[1],
        "set_number": 1,
        "reps": 10,
        "weight_kg": "30.25",
    }


@pytest.mark.parametrize("weight", ["30.25", 0, "99999.99"])
def test_create_set_returns_committed_record(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    weight: str | int,
) -> None:
    workout_id, exercise_id = set_target
    response = workout_client.post(
        f"/workouts/{workout_id}/sets", json={**set_payload, "weight_kg": weight}
    )

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body == {
        "id": body["id"],
        "workout_id": workout_id,
        "exercise_id": exercise_id,
        "set_number": 1,
        "reps": 10,
        "weight_kg": format(Decimal(str(weight)), ".2f"),
    }
    with Session(postgres_engine) as reader:
        saved = reader.get(WorkoutSet, body["id"])
        assert saved is not None
        assert saved.workout_id == workout_id
        assert saved.exercise_id == exercise_id
        assert saved.set_number == 1
        assert saved.reps == 10
        assert saved.weight_kg == Decimal(str(weight))
        assert saved.workout.user_id == DEMO_USER_ID


@pytest.mark.parametrize("exists", [False, True])
def test_set_rejects_missing_or_other_owners_workout(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    exists: bool,
) -> None:
    workout_id = set_target[0] if exists else 999
    if exists:
        with Session(postgres_engine) as writer, writer.begin():
            workout = writer.get(Workout, workout_id)
            assert workout is not None
            workout.user_id = uuid4()

    response = workout_client.post(f"/workouts/{workout_id}/sets", json=set_payload)

    assert response.status_code == 404
    assert response.json() == {"detail": "Workout not found"}
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


def test_set_rejects_missing_exercise(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
) -> None:
    response = workout_client.post(
        f"/workouts/{set_target[0]}/sets", json={**set_payload, "exercise_id": 999}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Exercise not found"}
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("field", ["exercise_id", "set_number", "reps"])
@pytest.mark.parametrize("value", [0, -1, True, "1", 1.5, 2147483648, None])
def test_invalid_set_integer_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    field: str,
    value: object,
) -> None:
    response = workout_client.post(
        f"/workouts/{set_target[0]}/sets", json={**set_payload, field: value}
    )

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize(
    "weight", ["-0.01", "30.251", "100000", "NaN", "Infinity", None, True, "abc"]
)
def test_invalid_set_weight_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    weight: object,
) -> None:
    response = workout_client.post(
        f"/workouts/{set_target[0]}/sets", json={**set_payload, "weight_kg": weight}
    )

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("field", ["exercise_id", "set_number", "reps", "weight_kg"])
def test_missing_set_field_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    field: str,
) -> None:
    payload = {key: value for key, value in set_payload.items() if key != field}
    response = workout_client.post(f"/workouts/{set_target[0]}/sets", json=payload)

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("field", ["id", "workout_id", "user_id"])
def test_extra_set_field_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    field: str,
) -> None:
    response = workout_client.post(
        f"/workouts/{set_target[0]}/sets", json={**set_payload, field: 42}
    )

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("workout_id", ["abc", "1.5", "0", "-1", "2147483648"])
def test_invalid_set_workout_path_returns_422_without_writing(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_payload: dict[str, object],
    workout_id: str,
) -> None:
    response = workout_client.post(f"/workouts/{workout_id}/sets", json=set_payload)

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


def test_duplicate_set_returns_409_without_overwriting_and_allows_next_set(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
) -> None:
    url = f"/workouts/{set_target[0]}/sets"
    first = workout_client.post(url, json=set_payload)
    assert first.status_code == 201

    duplicate = workout_client.post(url, json={**set_payload, "reps": 5})

    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "detail": "Set number already exists for this exercise in this workout"
    }
    assert (
        workout_client.post(url, json={**set_payload, "set_number": 2}).status_code
        == 201
    )
    with Session(postgres_engine) as reader:
        saved = reader.get(WorkoutSet, first.json()["id"])
        assert saved is not None
        assert saved.reps == 10
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 2


@pytest.mark.parametrize("different_resource", ["workout", "exercise"])
def test_set_number_can_be_reused_for_another_workout_or_exercise(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    different_resource: str,
) -> None:
    workout_id, exercise_id = set_target
    assert (
        workout_client.post(
            f"/workouts/{workout_id}/sets", json=set_payload
        ).status_code
        == 201
    )
    with Session(postgres_engine) as writer, writer.begin():
        if different_resource == "workout":
            resource = Workout(user_id=DEMO_USER_ID)
        else:
            resource = Exercise(name="Synthetic bench press")
        writer.add(resource)
        writer.flush()
        if different_resource == "workout":
            workout_id = resource.id
        else:
            exercise_id = resource.id

    response = workout_client.post(
        f"/workouts/{workout_id}/sets", json={**set_payload, "exercise_id": exercise_id}
    )

    assert response.status_code == 201
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 2


def test_set_uses_owner_dependency(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as writer, writer.begin():
        workout = writer.get(Workout, set_target[0])
        assert workout is not None
        workout.user_id = owner
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    try:
        response = workout_client.post(
            f"/workouts/{set_target[0]}/sets", json=set_payload
        )
    finally:
        del app.dependency_overrides[get_demo_user_id]

    assert response.status_code == 201


def test_set_exercise_can_be_outside_workout_plan(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
) -> None:
    plan_id = make_plan(postgres_engine, DEMO_USER_ID)
    with Session(postgres_engine) as writer, writer.begin():
        workout = writer.get(Workout, set_target[0])
        assert workout is not None
        workout.plan_id = plan_id

    response = workout_client.post(f"/workouts/{set_target[0]}/sets", json=set_payload)

    assert response.status_code == 201
    with Session(postgres_engine) as reader:
        workout = reader.get(Workout, set_target[0])
        assert workout is not None
        assert workout.plan is not None
        assert workout.plan.exercises[0].exercise_id != set_target[1]
        assert len(workout.sets) == 1


@pytest.mark.parametrize("resource", [Workout, Exercise])
def test_set_reference_deleted_after_validation_returns_404_and_rolls_back(
    workout_client: TestClient,
    postgres_engine: Engine,
    set_target: tuple[int, int],
    set_payload: dict[str, object],
    resource: type[Workout] | type[Exercise],
) -> None:
    resource_id = set_target[0] if resource is Workout else set_target[1]

    def remove_reference(
        session: Session, _flush_context: object, _instances: object
    ) -> None:
        with postgres_engine.begin() as connection:
            connection.execute(delete(resource).where(resource.id == resource_id))

    event.listen(Session, "before_flush", remove_reference)
    try:
        response = workout_client.post(
            f"/workouts/{set_target[0]}/sets", json=set_payload
        )
    finally:
        event.remove(Session, "before_flush", remove_reference)

    assert response.status_code == 404
    detail = "Workout not found" if resource is Workout else "Exercise not found"
    assert response.json() == {"detail": detail}
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert workout_client.post("/workouts", json={}).status_code == 201


@pytest.fixture
def workout_history(postgres_engine: Engine) -> list[dict[str, object]]:
    plan_id = make_plan(postgres_engine, DEMO_USER_ID)
    with Session(postgres_engine) as writer, writer.begin():
        owned = [
            Workout(user_id=DEMO_USER_ID, started_at=datetime(2026, 1, day, tzinfo=UTC))
            for day in [3, 1, 4, 4, 2]
        ]
        owned[0].plan_id = plan_id
        owned[0].sets = [
            WorkoutSet(
                exercise=Exercise(name="Synthetic history exercise"),
                set_number=1,
                reps=10,
                weight_kg=Decimal("30.25"),
            )
        ]
        other_owner = uuid4()
        writer.add_all(
            [
                owned[0],
                Workout(user_id=other_owner, started_at=datetime(2026, 1, 9, tzinfo=UTC)),
                owned[1],
                owned[2],
                Workout(user_id=other_owner, started_at=datetime(2026, 1, 4, tzinfo=UTC)),
                owned[3],
                owned[4],
            ]
        )
        writer.flush()
        return [
            {
                "id": row.id,
                "user_id": str(DEMO_USER_ID),
                "plan_id": row.plan_id,
                "started_at": row.started_at,
            }
            for row in [owned[3], owned[2], owned[0], owned[4], owned[1]]
        ]


@pytest.mark.parametrize("other_history_exists", [False, True])
def test_list_workouts_returns_empty_history_with_one_query(
    workout_client: TestClient,
    postgres_engine: Engine,
    workout_query_log: list[str],
    other_history_exists: bool,
) -> None:
    if other_history_exists:
        with Session(postgres_engine) as writer, writer.begin():
            writer.add(Workout(user_id=uuid4()))
    workout_query_log.clear()

    response = workout_client.get("/workouts")

    assert response.status_code == 200
    assert response.json() == []
    assert len(workout_query_log) == 1
    assert workout_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"limit": 1},
        {"limit": 2, "offset": 1},
        {"limit": 2, "offset": 2},
        {"limit": 2, "offset": 3},
        {"offset": 5},
        {"offset": 2147483647},
        {"limit": 100},
    ],
)
def test_list_workouts_filters_orders_and_paginates_with_one_query(
    workout_client: TestClient,
    workout_history: list[dict[str, object]],
    workout_query_log: list[str],
    params: dict[str, int],
) -> None:
    workout_query_log.clear()

    response = workout_client.get("/workouts", params=params)

    assert response.status_code == 200
    body = response.json()
    for row in body:
        row["started_at"] = datetime.fromisoformat(row["started_at"])
    offset = params.get("offset", 0)
    limit = params.get("limit", 20)
    assert body == workout_history[offset : offset + limit]
    assert len(workout_query_log) == 1
    assert workout_query_log[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("params, expected_count", [({}, 20), ({"limit": 100}, 100)])
def test_list_workouts_applies_default_and_maximum_page_sizes(
    workout_client: TestClient,
    postgres_engine: Engine,
    params: dict[str, int],
    expected_count: int,
) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        workouts = [
            Workout(user_id=DEMO_USER_ID, started_at=datetime(2026, 1, 5, tzinfo=UTC))
            for _ in range(105)
        ]
        writer.add_all(workouts)
        writer.flush()
        expected_ids = [row.id for row in reversed(workouts)][:expected_count]

    response = workout_client.get("/workouts", params=params)

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == expected_ids


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "-1"},
        {"limit": "101"},
        {"limit": "abc"},
        {"limit": "1.5"},
        {"offset": "-1"},
        {"offset": "abc"},
        {"offset": "1.5"},
        {"offset": "2147483648"},
    ],
)
def test_list_invalid_pagination_returns_422_without_querying(
    workout_client: TestClient, workout_query_log: list[str], params: dict[str, str]
) -> None:
    response = workout_client.get("/workouts", params=params)

    assert response.status_code == 422
    assert workout_query_log == []


def test_list_workouts_uses_owner_dependency(
    workout_client: TestClient, postgres_engine: Engine
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as writer, writer.begin():
        workout = Workout(user_id=owner)
        writer.add_all([workout, Workout(user_id=DEMO_USER_ID)])
        writer.flush()
        workout_id = workout.id
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    try:
        response = workout_client.get("/workouts")
    finally:
        del app.dependency_overrides[get_demo_user_id]

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [workout_id]
    assert response.json()[0]["user_id"] == str(owner)


def test_query_parameter_cannot_change_workout_history_owner(
    workout_client: TestClient,
    postgres_engine: Engine,
    workout_history: list[dict[str, object]],
) -> None:
    with Session(postgres_engine) as reader:
        other_owner = reader.scalar(
            select(Workout.user_id).where(Workout.user_id != DEMO_USER_ID).limit(1)
        )
        assert other_owner is not None

    response = workout_client.get("/workouts", params={"user_id": str(other_owner)})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [row["id"] for row in workout_history]
