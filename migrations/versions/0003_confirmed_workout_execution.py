"""Link confirmed proposals to workouts and preserve global set order."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(op.f("uq_workouts_id"), "workouts", ["id", "user_id"])
    op.add_column("workout_proposals", sa.Column("workout_id", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_workout_proposals_workout_id"), "workout_proposals", ["workout_id"]
    )
    op.create_foreign_key(
        op.f("fk_workout_proposals_workout_id_workouts"),
        "workout_proposals", "workouts", ["workout_id", "user_id"], ["id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_workout_proposals_confirmed_execution"), "workout_proposals",
        "workout_id IS NULL OR state = 'confirmed'",
    )
    op.add_column("workout_sets", sa.Column("position", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        "uq_workout_sets_workout_position", "workout_sets", ["workout_id", "position"]
    )
    op.create_check_constraint(
        op.f("ck_workout_sets_positive_position"), "workout_sets", "position > 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_workout_sets_positive_position"), "workout_sets", type_="check")
    op.drop_constraint("uq_workout_sets_workout_position", "workout_sets", type_="unique")
    op.drop_column("workout_sets", "position")
    op.drop_constraint(
        op.f("ck_workout_proposals_confirmed_execution"), "workout_proposals", type_="check"
    )
    op.drop_constraint(
        op.f("fk_workout_proposals_workout_id_workouts"), "workout_proposals", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("uq_workout_proposals_workout_id"), "workout_proposals", type_="unique"
    )
    op.drop_column("workout_proposals", "workout_id")
    op.drop_constraint(op.f("uq_workouts_id"), "workouts", type_="unique")
