import json
import logging

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.config import AppSettings
from app.logging import JsonFormatter


def test_formatter_omits_arbitrary_messages_args_and_exception_details() -> None:
    try:
        raise ValueError("synthetic-sensitive-exception")
    except ValueError:
        import sys
        exception = sys.exc_info()
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                               "synthetic-sensitive-message %s", ("synthetic-sensitive-argument",), exception)
    output = JsonFormatter().format(record)
    assert "synthetic-sensitive" not in output
    assert json.loads(output)["event"] == "runtime_event"


def test_request_logs_use_templates_and_generated_ids(caplog) -> None:
    application = create_app(AppSettings())
    with caplog.at_level(logging.INFO, logger="gym.http"), TestClient(application) as client:
        response = client.get("/workouts/123456?secret=synthetic-sensitive-query", headers={
            "Authorization": "Bearer synthetic-sensitive-token", "X-Request-ID": "synthetic-sensitive-id",
        })
        assert response.status_code == 401
        request_id = response.headers["x-request-id"]
        assert len(request_id) == 32
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        client.get("/synthetic-sensitive-path")
    logs = [json.loads(JsonFormatter().format(record)) for record in caplog.records if record.name == "gym.http"]
    assert len(logs) == 2
    assert logs[0]["route"] == "/workouts/{workout_id}"
    assert logs[0]["request_id"] == request_id and logs[0]["status"] == 401
    assert logs[1]["route"] == "<unmatched>"
    assert "synthetic-sensitive" not in json.dumps(logs) and "123456" not in json.dumps(logs)


def test_readiness_transitions_are_correlated_without_repeating_probe_events(monkeypatch, caplog) -> None:
    from app.api import application as module

    application = create_app(AppSettings())
    state = [False]
    monkeypatch.setattr(module, "database_ready", lambda **kwargs: state[0])
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        failed = client.get("/ready")
        client.get("/ready")
        state[0] = True
        recovered = client.get("/ready")
        client.get("/ready")
    transitions = [record for record in caplog.records if record.msg == "readiness_changed"]
    assert [record.status for record in transitions] == [503, 200]
    assert [record.request_id for record in transitions] == [
        failed.headers["x-request-id"], recovered.headers["x-request-id"],
    ]
