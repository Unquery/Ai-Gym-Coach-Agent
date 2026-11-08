from functools import cache
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import server
from app.db import session as database


@pytest.mark.parametrize("port", ["0", "65536", "synthetic-sensitive-port"])
def test_invalid_startup_exits_safely_before_starting_server(monkeypatch, capsys, port) -> None:
    monkeypatch.setenv("PORT", port)
    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    run = Mock()
    monkeypatch.setattr(server.uvicorn, "run", run)
    assert server.main() == 1
    run.assert_not_called()
    output = capsys.readouterr()
    assert json.loads(output.out)["event"] == "startup_configuration_error"
    assert "synthetic-sensitive" not in output.out + output.err


def test_configuration_failure_never_prints_exception(monkeypatch, capsys) -> None:
    def invalid():
        raise ValueError("synthetic-sensitive-configuration")

    monkeypatch.setattr(server, "load_settings", invalid)
    assert server.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["event"] == "startup_configuration_error"
    assert "synthetic-sensitive" not in output.out + output.err


def test_server_closes_pool_after_graceful_run(monkeypatch) -> None:
    events = []
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(log_level="INFO"))

    def run(application, **options):
        assert application == "app.main:app"
        assert options["port"] == 10000 and options["host"] == "0.0.0.0"
        assert options["timeout_graceful_shutdown"] == 100
        assert options["workers"] == 1
        assert not options.get("reload") and not options.get("factory")
        events.append("drained")

    monkeypatch.setattr(server.uvicorn, "run", run)
    monkeypatch.setattr(server, "dispose_db_engine", lambda: events.append("disposed"))
    assert server.main() == 0
    assert events == ["drained", "disposed"]


def test_shutdown_does_not_create_unused_engine_and_closes_used_pool(monkeypatch) -> None:
    engine = Mock()

    @cache
    def factory():
        return engine

    monkeypatch.setattr(database, "get_engine", factory)
    database.dispose_db_engine()
    engine.dispose.assert_not_called()
    factory()
    database.dispose_db_engine()
    engine.dispose.assert_called_once()
    assert factory.cache_info().currsize == 0
