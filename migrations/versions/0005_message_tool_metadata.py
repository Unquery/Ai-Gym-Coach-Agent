"""Preserve tool calls and results alongside existing conversation text."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("tool_calls", JSONB(none_as_null=True)))
    op.add_column("messages", sa.Column("tool_call_id", sa.Text()))
    op.alter_column("messages", "content", existing_type=sa.Text(), nullable=True)
    op.drop_constraint(op.f("ck_messages_valid_role"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_content_not_blank"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_valid_role"), "messages",
        "role IN ('user', 'assistant', 'tool')",
    )
    op.create_check_constraint(
        op.f("ck_messages_content_not_blank"), "messages",
        "content IS NULL OR content ~ '[^[:space:]]'",
    )
    op.create_check_constraint(
        op.f("ck_messages_content_or_tool_calls"), "messages",
        "content IS NOT NULL OR (role = 'assistant' AND tool_calls IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_valid_tool_calls"), "messages",
        "tool_calls IS NULL OR (role = 'assistant' AND "
        "CASE WHEN jsonb_typeof(tool_calls) = 'array' "
        "THEN jsonb_array_length(tool_calls) > 0 ELSE false END)",
    )
    op.create_check_constraint(
        op.f("ck_messages_valid_tool_call_id"), "messages",
        "(role = 'tool' AND tool_call_id IS NOT NULL "
        "AND tool_call_id ~ '[^[:space:]]') OR "
        "(role <> 'tool' AND tool_call_id IS NULL)",
    )


def downgrade() -> None:
    # Dropping metadata or coercing tool messages into text would lose history.
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE messages IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM messages WHERE role = 'tool' "
        "OR content IS NULL OR tool_calls IS NOT NULL OR tool_call_id IS NOT NULL)"
    )):
        raise RuntimeError("Cannot downgrade 0005 while messages contain tool metadata.")
    for name in (
        "valid_tool_call_id", "valid_tool_calls", "content_or_tool_calls",
        "content_not_blank", "valid_role",
    ):
        op.drop_constraint(op.f(f"ck_messages_{name}"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_valid_role"), "messages", "role IN ('user', 'assistant')",
    )
    op.create_check_constraint(
        op.f("ck_messages_content_not_blank"), "messages", "content ~ '[^[:space:]]'",
    )
    op.alter_column("messages", "content", existing_type=sa.Text(), nullable=False)
    op.drop_column("messages", "tool_call_id")
    op.drop_column("messages", "tool_calls")
