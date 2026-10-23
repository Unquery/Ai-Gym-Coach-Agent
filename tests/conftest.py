"""Real PostgreSQL tests isolated in disposable, uniquely named schemas."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import create_db_engine, postgres_url


def migration_config(connection: Connection) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL tests.")
    url = postgres_url(value)
    bootstrap = create_db_engine(url)
    schema = "test_" + uuid4().hex
    with bootstrap.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_db_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        with bootstrap.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        bootstrap.dispose()


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    with Session(postgres_engine) as db_session:
        yield db_session
