from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
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
