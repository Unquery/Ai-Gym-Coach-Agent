from uuid import uuid4

import pytest
from alembic import command
from conftest import migration_config
from sqlalchemy import Engine, inspect, text

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


def test_execution_migration_preserves_existing_workouts_and_sets(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0002")
        exercise_id = connection.scalar(text(
            "INSERT INTO exercises (name) VALUES ('Synthetic legacy exercise') RETURNING id"
        ))
        workout_id = connection.scalar(text(
            "INSERT INTO workouts (user_id) VALUES (:owner) RETURNING id"
        ), {"owner": uuid4()})
        connection.execute(text(
            "INSERT INTO workout_sets (workout_id, exercise_id, set_number, reps, weight_kg) "
            "VALUES (:workout, :exercise, 1, 8, 20.25)"
        ), {"workout": workout_id, "exercise": exercise_id})
        command.upgrade(config, "head")
        assert connection.scalar(text("SELECT count(*) FROM workouts")) == 1
        saved = connection.execute(text(
            "SELECT set_number, reps, weight_kg, position FROM workout_sets"
        )).mappings().one()
        assert saved["set_number"] == 1 and saved["reps"] == 8
        assert str(saved["weight_kg"]) == "20.25" and saved["position"] is None
