from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event, func, select
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
    workout_client: TestClient, postgres_engine: Engine, payload: object
) -> None:
    response = workout_client.post("/workouts", json=payload)

    assert response.status_code == 422
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Workout)) == 0


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
    return dict(exercise_id=set_target[1], set_number=1, reps=10, weight_kg="30.25")


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
