import pytest
from alembic import command
from sqlalchemy import Engine, text

from app.db import migrate
from tests.helpers import migration_config

pytestmark = pytest.mark.postgres


def test_deployment_upgrade_is_repeatable_and_preserves_data(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        command.downgrade(migration_config(connection), "base")
    migrate.upgrade_database(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.execute(text("INSERT INTO exercises (name) VALUES ('Synthetic retained row')"))
    migrate.upgrade_database(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert connection.scalar(text("SELECT name FROM exercises")) == "Synthetic retained row"


def test_deployment_refuses_concurrent_upgrade(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:namespace, :key)"), {
            "namespace": migrate.MIGRATION_LOCK[0], "key": migrate.MIGRATION_LOCK[1],
        })
        with pytest.raises(RuntimeError, match="Another deployment"):
            migrate.upgrade_database(postgres_engine)


def test_failed_deployment_rolls_back_schema_data_and_version(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_upgrade(config, revision):
        connection = config.attributes["connection"]
        connection.execute(text("CREATE TABLE interrupted_upgrade (id integer)"))
        connection.execute(text("INSERT INTO exercises (name) VALUES ('Synthetic rolled back')"))
        connection.execute(text("UPDATE alembic_version SET version_num = 'failed'"))
        raise RuntimeError("synthetic-sensitive-driver-error")

    monkeypatch.setattr(migrate.command, "upgrade", fail_upgrade)
    with pytest.raises(RuntimeError):
        migrate.upgrade_database(postgres_engine)
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert connection.scalar(text("SELECT count(*) FROM exercises")) == 0
        assert connection.scalar(text("SELECT to_regclass('interrupted_upgrade')")) is None
