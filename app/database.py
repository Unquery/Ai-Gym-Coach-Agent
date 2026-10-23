"""Database configuration shared by scripts, migrations, and future API handlers."""

import os

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase


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
    return create_engine(
        database_url() if url is None else postgres_url(url),
        pool_pre_ping=True,
        hide_parameters=True,
    )
