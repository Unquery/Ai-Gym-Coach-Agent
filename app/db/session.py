"""Database configuration shared by scripts, migrations, and API handlers."""

import os
from collections.abc import Iterator
from functools import cache

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import load_database_settings


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
        }
    )


def database_url() -> URL:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("Set DATABASE_URL before accessing the database.")
    return postgres_url(value)


def postgres_url(value: str | URL) -> URL:
    url = make_url(value)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Use a postgresql+psycopg database URL.")
    return url


def create_db_engine(url: str | URL | None = None) -> Engine:
    settings = load_database_settings()
    target = database_url() if url is None else postgres_url(url)
    return create_engine(
        target,
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_connect_timeout,
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "options": (
                f"{target.query.get('options', '')} "
                f"-cstatement_timeout={settings.db_statement_timeout_ms}"
            ).strip(),
        },
    )


@cache
def get_engine() -> Engine:
    """Create the shared API engine only when a database request needs it."""
    return create_db_engine()


def dispose_db_engine() -> None:
    """Close an existing API pool after shutdown without opening a new one."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
