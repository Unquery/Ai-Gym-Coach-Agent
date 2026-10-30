"""Real PostgreSQL tests isolated in disposable, uniquely named schemas."""

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.session import create_db_engine, postgres_url
from tests.helpers import migration_config


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
