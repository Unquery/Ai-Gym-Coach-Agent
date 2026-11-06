import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.application import create_app
from app.api.security import RequestBodyLimit
from app.config import AppSettings
from app.db.models import Workout
from app.db.session import get_session

TOKEN = "synthetic-operator-token-" + "x" * 32


@pytest.fixture
def secured_app():
    return create_app(AppSettings(
        environment="production", api_auth_token=SecretStr(TOKEN), api_owner_id=uuid4(),
        cors_origins=("https://frontend.example.com",), chat_requests_per_minute=2,
    ))


def test_all_business_routes_require_auth_before_database_access(secured_app) -> None:
    def forbidden_database():
        raise AssertionError("Unauthenticated request opened a database session")

    secured_app.dependency_overrides[get_session] = forbidden_database
    with TestClient(secured_app) as client:
        for method, path in (
            ("GET", "/exercises"), ("POST", "/exercises"),
            ("GET", "/workouts"), ("GET", "/workouts/1"),
            ("POST", "/workouts"), ("POST", "/workouts/1/sets"),
            ("POST", "/agent/chat"),
            ("POST", f"/workout-proposals/{uuid4()}/confirm"),
            ("POST", f"/workout-proposals/{uuid4()}/reject"),
        ):
            response = client.request(method, path, json={})
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "Bearer"
        assert client.get("/health").status_code == 200
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN},
    {"Authorization": "Bearer синтетический"},
    [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)],
])
def test_invalid_credentials_are_rejected(secured_app, headers) -> None:
    # Non-ASCII HTTP header values are represented as latin-1 wire bytes.
    if isinstance(headers, dict):
        headers = {key: value.encode("utf-8") for key, value in headers.items()}
    with TestClient(secured_app) as client:
        assert client.get("/workouts", headers=headers).status_code == 401


def test_demo_identity_is_opt_in() -> None:
    with TestClient(create_app(AppSettings())) as client:
        assert client.get("/workouts").status_code == 401


def test_host_cors_body_limits_and_error_sanitization(secured_app) -> None:
    secured_app.dependency_overrides[get_session] = lambda: None
    with TestClient(secured_app) as client:
        assert client.get("/health", headers={"Host": "evil.example.com"}).status_code == 400
        allowed = client.options("/agent/chat", headers={
            "Origin": "https://frontend.example.com", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,idempotency-key,content-type",
        })
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://frontend.example.com"
        denied = client.options("/agent/chat", headers={
            "Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST",
        })
        assert denied.status_code == 400 and "access-control-allow-origin" not in denied.headers
        oversized = client.post("/agent/chat", content=iter([b"x" * 9000, b"y" * 9000]))
        assert oversized.status_code == 413
        invalid = client.post("/agent/chat", headers={"Authorization": "Bearer " + TOKEN},
                              json={"message": {"secret": TOKEN}, TOKEN: "sensitive-value"})
        assert invalid.status_code == 422
        assert TOKEN not in invalid.text and "sensitive-value" not in invalid.text
        assert all(set(issue) == {"loc", "type", "msg"} for issue in invalid.json()["detail"])
        assert client.post("/exercises", headers={"Authorization": "Bearer " + TOKEN},
                           json={"name": "Synthetic exercise", "user_id": str(uuid4())}).status_code == 422


def test_authenticated_chat_is_rate_limited_without_provider_calls(secured_app) -> None:
    secured_app.dependency_overrides[get_session] = lambda: None
    with TestClient(secured_app) as client:
        statuses = [client.post("/agent/chat", json={"message": 123},
                               headers={"Authorization": "Bearer " + TOKEN}).status_code
                    for _ in range(3)]
        assert statuses == [422, 422, 429]


@pytest.mark.parametrize("trickle", [False, True])
def test_incomplete_body_has_total_deadline_before_business_execution(trickle: bool) -> None:
    async def check():
        responses = []

        async def downstream(scope, receive, send):
            raise AssertionError("Incomplete request reached business execution")

        async def receive():
            if trickle:
                await asyncio.sleep(0.005)
                return {"type": "http.request", "body": b"x", "more_body": True}
            await asyncio.Event().wait()

        async def send(message):
            responses.append(message)

        middleware = RequestBodyLimit(downstream, 16384, read_timeout_seconds=0.02)
        await asyncio.wait_for(middleware({"type": "http"}, receive, send), timeout=1)
        assert responses[0]["status"] == 408
        assert (b"connection", b"close") in responses[0]["headers"]

    asyncio.run(check())


@pytest.mark.postgres
def test_token_identity_cannot_read_other_owners_workouts(secured_app, postgres_engine: Engine) -> None:
    owner = secured_app.state.settings.api_owner_id
    foreign_owner = uuid4()
    with Session(postgres_engine) as writer, writer.begin():
        own = Workout(user_id=owner)
        foreign = Workout(user_id=foreign_owner)
        writer.add_all([own, foreign])
        writer.flush()
        own_id, foreign_id = own.id, foreign.id

    def session_override() -> Iterator[Session]:
        with Session(postgres_engine) as session:
            yield session

    secured_app.dependency_overrides[get_session] = session_override
    with TestClient(secured_app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        response = client.get("/workouts")
        assert response.status_code == 200
        assert [workout["id"] for workout in response.json()] == [own_id]
        assert client.get(f"/workouts/{foreign_id}").status_code == 404
        assert client.post(f"/workouts/{foreign_id}/sets", json={
            "exercise_id": 1, "set_number": 1, "reps": 8, "weight_kg": "20",
        }).status_code == 404


@pytest.mark.parametrize(("error", "status"), [
    (SQLAlchemyError("synthetic-sensitive-database-error"), 503),
    (RuntimeError("synthetic-sensitive-runtime-error"), 500),
])
def test_internal_failures_do_not_expose_details(secured_app, error: Exception, status: int) -> None:
    def fail_session():
        raise error

    secured_app.dependency_overrides[get_session] = fail_session
    with TestClient(secured_app, raise_server_exceptions=False) as client:
        response = client.get("/workouts", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == status
        assert "synthetic-sensitive" not in response.text
