"""Persist owner-scoped workout proposals without executing workout writes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workout_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(10), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'confirmed', 'rejected', 'expired')",
            name=op.f("ck_workout_proposals_valid_state"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_workout_proposals_valid_expiration")
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND resolved_at IS NULL) OR "
            "(state <> 'pending' AND resolved_at IS NOT NULL)",
            name=op.f("ck_workout_proposals_valid_resolution"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_workout_proposals_object_payload"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workout_proposals")),
    )


def downgrade() -> None:
    op.drop_table("workout_proposals")
