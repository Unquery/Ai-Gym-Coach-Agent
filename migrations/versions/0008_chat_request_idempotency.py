"""Persist HTTP chat retry bindings and transactionally completed responses."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), server_default="processing", nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", JSONB(none_as_null=True), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_requests")),
        sa.UniqueConstraint("user_id", "endpoint", "idempotency_key", name=op.f("uq_chat_requests_user_id")),
        sa.CheckConstraint("length(trim(endpoint)) > 0", name=op.f("ck_chat_requests_endpoint_not_blank")),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_chat_requests_valid_fingerprint")),
        sa.CheckConstraint(
            "(state = 'processing' AND response_status IS NULL AND response_body IS NULL "
            "AND conversation_id IS NULL) OR (state = 'completed' "
            "AND response_status IS NOT NULL AND response_status BETWEEN 100 AND 599 "
            "AND response_body IS NOT NULL AND jsonb_typeof(response_body) = 'object' "
            "AND conversation_id IS NOT NULL AND response_body ? 'conversation_id' "
            "AND response_body ->> 'conversation_id' IS NOT NULL "
            "AND response_body ->> 'conversation_id' = conversation_id::text)",
            name=op.f("ck_chat_requests_valid_response_state"),
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE chat_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM chat_requests)")):
        raise RuntimeError("Cannot downgrade 0008 while chat request bindings exist.")
    op.drop_table("chat_requests")
