"""Shared migration configuration for isolated PostgreSQL tests."""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import Connection


def migration_config(connection: Connection) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    return config
