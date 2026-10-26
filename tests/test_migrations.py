import pytest
from alembic import command
from conftest import migration_config
from sqlalchemy import Engine, inspect

pytestmark = pytest.mark.postgres


def test_migration_matches_models(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        command.check(migration_config(connection))


def test_migration_can_downgrade_and_upgrade(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "exercises",
            "workout_plans",
            "workout_plan_exercises",
            "workouts",
            "workout_sets",
            "workout_proposals",
        }
