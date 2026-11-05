"""Read-only database readiness; never migrate, repair, seed or call a provider."""

from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db import session as database


@cache
def migration_heads() -> set[str]:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    return set(ScriptDirectory.from_config(config).get_heads())


def database_ready(*, production: bool) -> bool:
    with database.get_engine().connect() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '2000'"))
        revisions = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
        if revisions != migration_heads():
            return False
        if production:
            unsafe_role = connection.scalar(text("""
                SELECT rolsuper OR rolcreatedb OR rolcreaterole
                    OR has_schema_privilege(current_user, current_schema(), 'CREATE')
                    OR has_database_privilege(current_user, current_database(), 'CREATE')
                    OR EXISTS (
                        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
                            AND pg_has_role(current_user, pg_get_userbyid(c.relowner), 'MEMBER')
                    )
                FROM pg_roles WHERE rolname = current_user
            """))
            if unsafe_role is not False:
                return False
    return True
