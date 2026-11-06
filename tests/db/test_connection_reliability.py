"""Real connection loss affects only sessions inside a disposable test schema."""
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.db.session import create_db_engine

pytestmark = pytest.mark.postgres


def test_pool_replaces_a_stale_connection(postgres_engine: Engine, monkeypatch) -> None:
    monkeypatch.setenv("DB_POOL_SIZE", "1")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    engine = create_db_engine(postgres_engine.url)
    try:
        with engine.connect() as connection:
            old_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        with postgres_engine.connect() as control:
            assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": old_pid})
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) != old_pid
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_disconnect_aborts_write_without_replaying_transaction(postgres_engine: Engine) -> None:
    engine = create_db_engine(postgres_engine.url)
    try:
        with pytest.raises(DBAPIError) as error:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO exercises (name) VALUES ('Synthetic interrupted write')"))
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                with postgres_engine.connect() as control:
                    assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                connection.execute(text("SELECT 1"))
        assert error.value.connection_invalidated
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM exercises")) == 0
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
    finally:
        engine.dispose()


def test_statement_timeout_cancels_query_and_pool_stays_usable(postgres_engine: Engine, monkeypatch) -> None:
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "1000")
    engine = create_db_engine(postgres_engine.url)
    try:
        with pytest.raises(DBAPIError) as error:
            with engine.begin() as connection:
                connection.execute(text("SELECT pg_sleep(2)"))
        assert error.value.orig.sqlstate == "57014"
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()
