import pytest
from fastapi.testclient import TestClient

from app.api.application import app
from app.db import session as database
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError


def test_health_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_returns_bad() -> None:
    with TestClient(app) as client:
        response = client.get("/bad")

    assert response.status_code == 404


def test_readiness_failure_is_sanitized_and_liveness_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable():
        raise SQLAlchemyError("synthetic-sensitive-database-url")

    monkeypatch.setattr(database, "get_engine", unavailable)
    with TestClient(app) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert client.get("/health").status_code == 200


@pytest.mark.postgres
def test_readiness_requires_migration_head(postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready"}
        with postgres_engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '0007'"))
        assert client.get("/ready").status_code == 503


@pytest.mark.postgres
def test_production_readiness_rejects_migration_owner(postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.health import database_ready

    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    assert database_ready(production=True) is False
