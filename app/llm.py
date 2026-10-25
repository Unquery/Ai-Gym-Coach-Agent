"""One Chat Completions call through an OpenAI-compatible provider."""

from collections.abc import Sequence

from httpx2 import Client
from openai import OpenAI, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)

from app.llm_config import LLMSettings


def create_llm_client(
    settings: LLMSettings, *, http_client: Client | None = None
) -> OpenAI:
    """Create a client without sending requests; the caller owns its lifetime."""
    return OpenAI(
        base_url=str(settings.base_url),
        api_key=settings.api_key.get_secret_value(),
        timeout=30.0,
        max_retries=0,
        http_client=http_client,
    )


def create_chat_completion(
    client: OpenAI,
    settings: LLMSettings,
    messages: Sequence[ChatCompletionMessageParam],
    *,
    tools: Sequence[ChatCompletionToolUnionParam] | None = None,
) -> ChatCompletion:
    """Return text and tool calls unchanged; do not execute tools or retry."""
    return client.chat.completions.create(
        model=settings.model,
        messages=messages,
        tools=tools if tools is not None else omit,
    )
