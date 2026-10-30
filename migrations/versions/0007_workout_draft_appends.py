"""Track draft state and durable idempotency receipts for set appends."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workout_drafts", sa.Column(
        "state", sa.String(9), server_default="active", nullable=False,
    ))
    op.create_check_constraint(
        op.f("ck_workout_drafts_valid_state"), "workout_drafts",
        "state IN ('active', 'discarded')",
    )
    op.create_table(
        "workout_draft_appends",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("set_payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "conversation_id", "idempotency_key", name=op.f("pk_workout_draft_appends"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["workout_drafts.conversation_id"], ondelete="CASCADE",
            name=op.f("fk_workout_draft_appends_conversation_id_workout_drafts"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(set_payload) = 'object'",
            name=op.f("ck_workout_draft_appends_set_payload_object"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE workout_drafts IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM workout_drafts)")):
        raise RuntimeError("Cannot downgrade 0007 while workout drafts exist.")
    op.drop_table("workout_draft_appends")
    op.drop_constraint(op.f("ck_workout_drafts_valid_state"), "workout_drafts", type_="check")
    op.drop_column("workout_drafts", "state")
