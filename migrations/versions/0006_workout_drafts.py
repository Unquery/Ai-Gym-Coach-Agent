"""Add versioned conversation-owned workout drafts without executing workouts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workout_drafts",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("payload", JSONB(), server_default=sa.text("'{\"sets\": []}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_workout_drafts")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE",
            name=op.f("fk_workout_drafts_conversation_id_conversations"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_workout_drafts_positive_version")),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND payload ? 'sets' "
            "AND jsonb_typeof(payload -> 'sets') = 'array'",
            name=op.f("ck_workout_drafts_sets_payload"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_workout_drafts_valid_update_time"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE workout_drafts IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workout_drafts)")):
        raise RuntimeError("Cannot downgrade 0006 while workout drafts exist.")
    op.drop_table("workout_drafts")
