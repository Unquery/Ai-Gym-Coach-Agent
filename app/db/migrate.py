"""One explicit, transactional deployment upgrade; never reset or seed a database."""

import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.config import validate_production_database_url
from app.db.session import create_db_engine, postgres_url

# Stable, database-local namespace for this application's deployment lock.
MIGRATION_LOCK = (18417, 8)


def upgrade_database(engine: Engine) -> None:
    with engine.begin() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"), {
            "namespace": MIGRATION_LOCK[0], "key": MIGRATION_LOCK[1],
        })
        if not locked:
            raise RuntimeError("Another deployment migration is running")
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '120s'"))
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        # Alembic reuses this transaction, including its version-table update.
        command.upgrade(config, "head")


def main() -> int:
    engine = None
    try:
        url = os.environ.get("MIGRATION_DATABASE_URL")
        if not url:
            raise ValueError("MIGRATION_DATABASE_URL is required")
        if os.environ.get("APP_ENV", "development") == "production":
            validate_production_database_url(url)
            host = (postgres_url(url).host or "").lower()
            if host.endswith(".neon.tech") and "-pooler." in host:
                raise ValueError("Neon migrations require a direct endpoint")
        engine = create_db_engine(url)
        upgrade_database(engine)
    except Exception:
        # SQL/driver errors can contain passwords, connection strings and row data.
        print("Migration failed; verify the database revision before retrying. Check credentials, "
              "TLS, connectivity, lock availability and schema compatibility.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print("Database upgraded to migration head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
