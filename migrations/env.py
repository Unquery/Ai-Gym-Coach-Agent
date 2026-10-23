"""Run migrations against DATABASE_URL, or a supplied test connection."""

from alembic import context
from sqlalchemy.engine import Connection

from app import models  # noqa: F401 -- register all mapped tables
from app.database import Base, create_db_engine, database_url


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        run_migrations(supplied_connection)
    else:
        engine = create_db_engine()
        try:
            with engine.connect() as connection:
                run_migrations(connection)
        finally:
            engine.dispose()
