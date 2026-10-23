"""Create exercise catalog, workout plans, workouts, and performed sets."""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exercises",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name=op.f("ck_exercises_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exercises")),
        sa.UniqueConstraint("name", name=op.f("uq_exercises_name")),
    )
    op.create_table(
        "workout_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name=op.f("ck_workout_plans_name_not_blank")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workout_plans")),
        sa.UniqueConstraint("id", "user_id", name=op.f("uq_workout_plans_id")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_workout_plans_user_id")),
    )
    op.create_table(
        "workout_plan_exercises",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target_sets", sa.Integer(), nullable=False),
        sa.Column("target_reps", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "position > 0", name=op.f("ck_workout_plan_exercises_positive_position")
        ),
        sa.CheckConstraint(
            "target_sets > 0", name=op.f("ck_workout_plan_exercises_positive_target_sets")
        ),
        sa.CheckConstraint(
            "target_reps > 0", name=op.f("ck_workout_plan_exercises_positive_target_reps")
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["workout_plans.id"], ondelete="CASCADE",
            name=op.f("fk_workout_plan_exercises_plan_id_workout_plans"),
        ),
        sa.ForeignKeyConstraint(
            ["exercise_id"], ["exercises.id"], ondelete="RESTRICT",
            name=op.f("fk_workout_plan_exercises_exercise_id_exercises"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workout_plan_exercises")),
        sa.UniqueConstraint(
            "plan_id", "position", name=op.f("uq_workout_plan_exercises_plan_id")
        ),
    )
    op.create_table(
        "workouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "user_id"], ["workout_plans.id", "workout_plans.user_id"],
            ondelete="RESTRICT", name=op.f("fk_workouts_plan_id_workout_plans"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workouts")),
    )
    op.create_index("ix_workouts_user_id_started_at", "workouts", ["user_id", "started_at"])
    op.create_table(
        "workout_sets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("set_number", sa.Integer(), nullable=False),
        sa.Column("reps", sa.Integer(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(7, 2), nullable=False),
        sa.CheckConstraint("set_number > 0", name=op.f("ck_workout_sets_positive_set_number")),
        sa.CheckConstraint("reps > 0", name=op.f("ck_workout_sets_positive_reps")),
        sa.CheckConstraint(
            "weight_kg >= 0 AND weight_kg <= 99999.99", name=op.f("ck_workout_sets_valid_weight_kg")
        ),
        sa.ForeignKeyConstraint(
            ["workout_id"], ["workouts.id"], ondelete="CASCADE",
            name=op.f("fk_workout_sets_workout_id_workouts"),
        ),
        sa.ForeignKeyConstraint(
            ["exercise_id"], ["exercises.id"], ondelete="RESTRICT",
            name=op.f("fk_workout_sets_exercise_id_exercises"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workout_sets")),
        sa.UniqueConstraint(
            "workout_id", "exercise_id", "set_number", name=op.f("uq_workout_sets_workout_id")
        ),
    )
    op.create_index("ix_workout_sets_exercise_id", "workout_sets", ["exercise_id"])


def downgrade() -> None:
    op.drop_index("ix_workout_sets_exercise_id", table_name="workout_sets")
    op.drop_table("workout_sets")
    op.drop_index("ix_workouts_user_id_started_at", table_name="workouts")
    op.drop_table("workouts")
    op.drop_table("workout_plan_exercises")
    op.drop_table("workout_plans")
    op.drop_table("exercises")
