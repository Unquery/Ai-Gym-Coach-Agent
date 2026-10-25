from collections.abc import Iterator
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app import database
from app.database import get_session
from app.main import app
from app.models import Exercise


@pytest.fixture
def exercise_client() -> Iterator[tuple[TestClient, Mock]]:
    session = Mock(spec=Session)

    def override_session() -> Mock:
        return session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            yield client, session
    finally:
        del app.dependency_overrides[get_session]


@pytest.fixture
def postgres_client(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "rows",
    [[], [{"id": 1, "name": "Synthetic squat"}, {"id": 2, "name": "Synthetic pull-up"}]],
)
def test_exercises_response_contract(
    exercise_client: tuple[TestClient, Mock], rows: list[dict[str, int | str]]
) -> None:
    client, session = exercise_client
    session.scalars.return_value = [Exercise(**row) for row in rows]

    response = client.get("/exercises")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == rows


def test_existing_exercise_returns_object(
    exercise_client: tuple[TestClient, Mock],
) -> None:
    client, session = exercise_client
    session.get.return_value = Exercise(id=7, name="Synthetic squat")

    response = client.get("/exercises/7")

    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "Synthetic squat"}
    session.get.assert_called_once_with(Exercise, 7)


def test_missing_exercise_returns_404(exercise_client: tuple[TestClient, Mock]) -> None:
    client, session = exercise_client
    session.get.return_value = None

    response = client.get("/exercises/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Exercise not found"}


@pytest.mark.parametrize(
    "exercise_id, error_type",
    [
        ("abc", "int_parsing"),
        ("0", "greater_than"),
        ("-1", "greater_than"),
        ("2147483648", "less_than_equal"),
        (str(10**40), "less_than_equal"),
    ],
)
def test_invalid_exercise_id_returns_422(
    exercise_client: tuple[TestClient, Mock], exercise_id: str, error_type: str
) -> None:
    client, session = exercise_client

    response = client.get(f"/exercises/{exercise_id}")

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["path", "exercise_id"]
    assert error["type"] == error_type
    session.get.assert_not_called()


@pytest.mark.postgres
@pytest.mark.parametrize("populated", [False, True])
def test_exercises_reads_postgres_catalog(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, populated: bool
) -> None:
    expected = []
    if populated:
        with Session(postgres_engine) as writer, writer.begin():
            writer.add_all(
                [
                    Exercise(id=20, name="Synthetic pull-up"),
                    Exercise(id=10, name="Synthetic squat"),
                ]
            )
        expected = [
            {"id": 10, "name": "Synthetic squat"},
            {"id": 20, "name": "Synthetic pull-up"},
        ]

    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    with TestClient(app) as client:
        response = client.get("/exercises")

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.postgres
@pytest.mark.parametrize("exercise_id", [10, 999])
def test_exercise_reads_postgres_by_id(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, exercise_id: int
) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        writer.add_all(
            [
                Exercise(id=20, name="Synthetic pull-up"),
                Exercise(id=10, name="Synthetic squat"),
            ]
        )

    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    with TestClient(app) as client:
        response = client.get(f"/exercises/{exercise_id}")

    if exercise_id == 10:
        assert response.status_code == 200
        assert response.json() == {"id": 10, "name": "Synthetic squat"}
    else:
        assert response.status_code == 404
        assert response.json() == {"detail": "Exercise not found"}


@pytest.mark.postgres
@pytest.mark.parametrize(
    "name",
    [
        "Synthetic squat",
        "  Synthetic squat  ",
        "x" * 120,
        "  " + "x" * 120 + "  ",
        "Приседания 🏋",
        r"Synthetic\u0000squat",
    ],
)
def test_create_exercise_persists_and_returns_object(
    postgres_client: TestClient, postgres_engine: Engine, name: str
) -> None:
    response = postgres_client.post("/exercises", json={"name": name})

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    assert body["id"] > 0
    assert body == {"id": body["id"], "name": name.strip()}
    with Session(postgres_engine) as reader:
        saved = reader.get(Exercise, body["id"])
        assert saved is not None
        assert saved.name == name.strip()
        assert reader.scalar(select(func.count()).select_from(Exercise)) == 1
    detail = postgres_client.get(f"/exercises/{body['id']}")
    assert detail.status_code == 200
    assert detail.json() == body


@pytest.mark.postgres
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None},
        {"name": 123},
        {"name": []},
        {"name": ""},
        {"name": " \t\n "},
        {"name": "x" * 121},
    ],
)
def test_create_exercise_rejects_invalid_name_without_writing(
    postgres_client: TestClient, postgres_engine: Engine, payload: dict[str, object]
) -> None:
    response = postgres_client.post("/exercises", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Exercise)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("duplicate_name", ["Synthetic squat", "  Synthetic squat  "])
def test_create_duplicate_exercise_returns_409_without_changing_catalog(
    postgres_client: TestClient, postgres_engine: Engine, duplicate_name: str
) -> None:
    created = postgres_client.post("/exercises", json={"name": "Synthetic squat"})
    assert created.status_code == 201

    duplicate = postgres_client.post("/exercises", json={"name": duplicate_name})

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Exercise with this name already exists"}
    with Session(postgres_engine) as reader:
        rows = reader.scalars(select(Exercise)).all()
        assert [(row.id, row.name) for row in rows] == [
            (created.json()["id"], "Synthetic squat")
        ]

    next_response = postgres_client.post("/exercises", json={"name": "Synthetic pull-up"})
    assert next_response.status_code == 201
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Exercise)) == 2


@pytest.mark.postgres
@pytest.mark.parametrize("exercise_id", [1, 2147483647])
def test_exercise_integer_boundaries_can_be_looked_up(
    postgres_client: TestClient, postgres_engine: Engine, exercise_id: int
) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        writer.add(Exercise(id=exercise_id, name="Synthetic boundary exercise"))

    response = postgres_client.get(f"/exercises/{exercise_id}")

    assert response.status_code == 200
    assert response.json() == {"id": exercise_id, "name": "Synthetic boundary exercise"}


@pytest.mark.postgres
@pytest.mark.parametrize("exercise_id", [0, -1, 2147483648, 10**40])
def test_out_of_range_exercise_id_is_rejected_before_database_access(
    postgres_client: TestClient, postgres_engine: Engine, exercise_id: int
) -> None:
    with patch.object(postgres_engine, "connect", wraps=postgres_engine.connect) as connect:
        response = postgres_client.get(f"/exercises/{exercise_id}")

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["path", "exercise_id"]
        connect.assert_not_called()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "name",
    [
        "\x00Synthetic squat",
        "Synthetic\x00squat",
        "Synthetic squat\x00",
        "\x00",
        "x" * 119 + "\x00",
    ],
)
def test_exercise_nul_is_rejected_before_database_access(
    postgres_client: TestClient, postgres_engine: Engine, name: str
) -> None:
    with patch.object(postgres_engine, "connect", wraps=postgres_engine.connect) as connect:
        response = postgres_client.post("/exercises", json={"name": name})

        assert response.status_code == 422
        error = response.json()["detail"][0]
        assert error["loc"] == ["body", "name"]
        assert error["type"] == "value_error"
        assert "Name must not contain NUL characters" in error["msg"]
        connect.assert_not_called()
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Exercise)) == 0
