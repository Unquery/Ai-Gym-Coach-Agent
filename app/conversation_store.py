"""Owner-scoped conversation storage in the caller's database transaction.

Use a dedicated Session and let failures escape its session.begin() block.
These functions never commit or flush unrelated pending ORM changes. Returned
records are not durably saved until the caller successfully commits.
"""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.models import Conversation, ConversationMessage


class ConversationNotFoundError(LookupError):
    """No conversation exists for the server-selected owner and ID."""


def create_conversation(*, session: Session, user_id: UUID) -> Conversation:
    """Create an empty conversation for the trusted server-selected owner."""
    with session.no_autoflush:
        return session.scalars(
            insert(Conversation).values(user_id=user_id).returning(Conversation)
        ).one()


def _validate_message(
    role: str,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    tool_call_id: str | None,
) -> list[dict[str, Any]] | None:
    if role not in ("user", "assistant", "tool"):
        raise ValueError("Message role is not supported")
    if content is not None and (not isinstance(content, str) or not content.strip()):
        raise ValueError("Message text must be nonblank")
    if content is None and (role != "assistant" or tool_calls is None):
        raise ValueError("Message text is required unless the assistant calls tools")
    if role == "tool":
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise ValueError("Tool results require a nonblank call ID")
    elif tool_call_id is not None:
        raise ValueError("Only tool results may reference a call ID")
    if tool_calls is None:
        return None
    if role != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
        raise ValueError("Only assistant messages may contain a nonempty tool-call list")

    validated: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
            raise ValueError("Tool calls require id, type, and function")
        call_id, function = call["id"], call["function"]
        if call["type"] != "function":
            raise ValueError("Only function tool calls are supported")
        if not isinstance(call_id, str) or not call_id.strip() or call_id in used_ids:
            raise ValueError("Tool call IDs must be nonblank and distinct within a message")
        if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
            raise ValueError("Function calls require name and arguments")
        if not isinstance(function["name"], str) or not function["name"].strip():
            raise ValueError("Function names must be nonblank")
        if not isinstance(function["arguments"], str):
            raise ValueError("Function arguments must be a string")
        used_ids.add(call_id)
        # Copy the JSON structure before waiting for the conversation row lock.
        validated.append({
            "id": call_id, "type": "function",
            "function": {"name": function["name"], "arguments": function["arguments"]},
        })
    return validated


def _check_tool_result(session: Session, conversation_id: UUID, call_id: str) -> None:
    """Match the most recent occurrence of a call ID within this conversation."""
    source_position = session.scalar(
        select(ConversationMessage.position).where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role == "assistant",
            ConversationMessage.tool_calls.contains([{"id": call_id}]),
        ).order_by(ConversationMessage.position.desc()).limit(1)
    )
    if source_position is None:
        raise ValueError("Tool result must reference a call in this conversation")
    duplicate = session.scalar(
        select(ConversationMessage.id).where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role == "tool",
            ConversationMessage.tool_call_id == call_id,
            ConversationMessage.position > source_position,
        ).limit(1)
    )
    if duplicate is not None:
        raise ValueError("A result for this tool call already exists")


def append_message(
    conversation_id: UUID,
    *,
    session: Session,
    user_id: UUID,
    role: Literal["user", "assistant", "tool"],
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
) -> ConversationMessage:
    """Append under the parent's row lock, held until the caller ends the transaction.

    Tool metadata records attempted calls; it never authorizes their execution.
    Arguments remain strings, including malformed JSON from failed tool attempts.
    Full turn/protocol sequencing belongs to the future chat integration.
    """
    calls = _validate_message(role, content, tool_calls, tool_call_id)
    with session.no_autoflush:
        owned_id = session.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id,
            ).with_for_update()
        )
        if owned_id is None:
            raise ConversationNotFoundError("Conversation not found")
        if tool_call_id is not None:
            _check_tool_result(session, conversation_id, tool_call_id)
        last_position = session.execute(
            select(func.coalesce(func.max(ConversationMessage.position), 0)).where(
                ConversationMessage.conversation_id == conversation_id
            )
        ).scalar_one()
        return session.scalars(
            insert(ConversationMessage).values(
                conversation_id=conversation_id, position=last_position + 1,
                role=role, content=content, tool_calls=calls, tool_call_id=tool_call_id,
            ).returning(ConversationMessage)
        ).one()


def get_conversation_messages(
    conversation_id: UUID,
    *,
    session: Session,
    user_id: UUID,
) -> list[ConversationMessage]:
    """Read ordered history; missing, foreign, and empty conversations return []."""
    with session.no_autoflush:
        result = session.scalars(
            select(ConversationMessage)
            .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .order_by(ConversationMessage.position)
        ).all()
        return list(result)
