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
            "conversations",
            "messages",
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


def test_conversation_migration_preserves_all_existing_tables(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0003")
        owner = uuid4()
        exercise_id = connection.scalar(text(
            "INSERT INTO exercises (name) VALUES ('Synthetic preserved exercise') RETURNING id"
        ))
        plan_id = connection.scalar(text(
            "INSERT INTO workout_plans (user_id, name) "
            "VALUES (:owner, 'Synthetic preserved plan') RETURNING id"
        ), {"owner": owner})
        connection.execute(text(
            "INSERT INTO workout_plan_exercises "
            "(plan_id, exercise_id, position, target_sets, target_reps) "
            "VALUES (:plan, :exercise, 1, 3, 8)"
        ), {"plan": plan_id, "exercise": exercise_id})
        workout_id = connection.scalar(text(
            "INSERT INTO workouts (user_id, plan_id) VALUES (:owner, :plan) RETURNING id"
        ), {"owner": owner, "plan": plan_id})
        connection.execute(text(
            "INSERT INTO workout_sets "
            "(workout_id, exercise_id, set_number, position, reps, weight_kg) "
            "VALUES (:workout, :exercise, 1, 1, 8, 20.25)"
        ), {"workout": workout_id, "exercise": exercise_id})
        connection.execute(text(
            "INSERT INTO workout_proposals "
            "(id, user_id, workout_id, payload, state, created_at, expires_at, resolved_at) "
            "VALUES (:id, :owner, :workout, '{}', 'confirmed', "
            "now(), now() + interval '15 minutes', now())"
        ), {"id": uuid4(), "owner": owner, "workout": workout_id})
        existing_tables = (
            "exercises", "workout_plans", "workout_plan_exercises",
            "workouts", "workout_sets", "workout_proposals",
        )
        # Table names are a fixed allowlist; compare every original column and row.
        before = {
            table: connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all()
            for table in existing_tables
        }
        for target in ("0004", "0005", "0003", "0005"):
            if target == "0003":
                command.downgrade(config, target)
            else:
                command.upgrade(config, target)
            for table, rows in before.items():
                assert connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() == rows


def test_tool_metadata_migration_preserves_existing_conversations(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        command.downgrade(config, "0004")
        conversation_id = uuid4()
        connection.execute(text(
            "INSERT INTO conversations (id, user_id) VALUES (:id, :owner)"
        ), {"id": conversation_id, "owner": uuid4()})
        connection.execute(text(
            "INSERT INTO messages (conversation_id, position, role, content) "
            "VALUES (:id, 1, 'user', 'Synthetic legacy request'), "
            "(:id, 2, 'assistant', 'Synthetic legacy answer')"
        ), {"id": conversation_id})
        conversations = connection.execute(text("SELECT * FROM conversations")).all()
        original_columns = "id, conversation_id, position, role, content, created_at"
        messages = connection.execute(text(
            f"SELECT {original_columns} FROM messages ORDER BY id"
        )).all()
        for target in ("0005", "0004", "0005"):
            if target == "0004":
                command.downgrade(config, target)
            else:
                command.upgrade(config, target)
                assert connection.scalar(text(
                    "SELECT bool_and(tool_calls IS NULL AND tool_call_id IS NULL) FROM messages"
                )) is True
            assert connection.execute(text("SELECT * FROM conversations")).all() == conversations
            assert connection.execute(text(
                f"SELECT {original_columns} FROM messages ORDER BY id"
            )).all() == messages


def test_tool_metadata_downgrade_refuses_data_loss(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        config = migration_config(connection)
        conversation_id = uuid4()
        connection.execute(text(
            "INSERT INTO conversations (id, user_id) VALUES (:id, :owner)"
        ), {"id": conversation_id, "owner": uuid4()})
        # Even assistant text with metadata cannot be downgraded without loss.
        connection.execute(text(
            "INSERT INTO messages (conversation_id, position, role, content, tool_calls) "
            "VALUES (:id, 1, 'assistant', 'Checking history.', "
            "CAST(:calls AS jsonb))"
        ), {"id": conversation_id, "calls": '[{"id":"call_1","type":"function",'
            '"function":{"name":"get_recent_workouts","arguments":"{}"}}]'})
        before = connection.execute(text("SELECT * FROM messages")).all()
        with connection.begin_nested():
            with pytest.raises(RuntimeError, match="Cannot downgrade 0005"):
                command.downgrade(config, "0004")
        assert connection.execute(text("SELECT * FROM messages")).all() == before
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
