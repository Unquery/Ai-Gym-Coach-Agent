from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event, func, select
from sqlalchemy.orm import Session

from app import database
from app.main import app, get_demo_user_id
from app.models import Exercise, Workout, WorkoutPlan, WorkoutPlanExercise
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

    def remove_plan(session: Session, _flush_context: object, _instances: object) -> None:
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
