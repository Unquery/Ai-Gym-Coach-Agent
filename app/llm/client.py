"""One Chat Completions call through an OpenAI-compatible provider."""

from collections.abc import Sequence
import json
from time import monotonic

from httpx2 import Client, HTTPError, Response, TimeoutException
from openai import (
    APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError,
    BadRequestError, ConflictError, DefaultHttpx2Client, InternalServerError,
    NotFoundError, OpenAI, PermissionDeniedError, RateLimitError,
    UnprocessableEntityError, omit,
)
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)

from app.llm.config import LLMSettings

MAX_PROMPT_BYTES = 65536
MAX_RESPONSE_BYTES = 131072
MAX_COMPLETION_TOKENS = 1024


class LLMResponseLimitError(ValueError):
    """Provider input/output exceeds the backend's bounded processing budget."""


def _reject_provider_error(response: Response) -> None:
    """The SDK otherwise reads error bodies eagerly, outside our bounded reader."""
    if response.status_code < 300:
        return
    response.close()
    errors = {
        400: BadRequestError, 401: AuthenticationError, 403: PermissionDeniedError,
        404: NotFoundError, 409: ConflictError, 422: UnprocessableEntityError, 429: RateLimitError,
    }
    error = errors.get(response.status_code, InternalServerError if response.status_code >= 500 else APIStatusError)
    raise error("LLM provider request failed", response=response, body=None)


def create_llm_client(
    settings: LLMSettings, *, http_client: Client | None = None
) -> OpenAI:
    """Create a client without sending requests; the caller owns its lifetime."""
    if http_client is None:
        http_client = DefaultHttpx2Client(follow_redirects=False, trust_env=False)
    if _reject_provider_error not in http_client.event_hooks["response"]:
        http_client.event_hooks["response"].append(_reject_provider_error)
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
    deadline: float | None = None,
) -> ChatCompletion:
    """Bound prompt, completion and body reads; close failed responses, never retry.

    The deadline is checked between synchronous I/O operations. Socket phase
    timeouts bound a blocked read; this is not forcible thread cancellation.
    """
    if len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8")) > MAX_PROMPT_BYTES:
        raise LLMResponseLimitError("LLM prompt is too large")
    deadline = deadline if deadline is not None else monotonic() + 30
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("LLM deadline exceeded")
    try:
        with client.chat.completions.with_streaming_response.create(
            model=settings.model, messages=messages,
            tools=tools if tools is not None else omit,
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            timeout=min(30.0, remaining),
        ) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                if monotonic() >= deadline:
                    raise TimeoutError("LLM deadline exceeded")
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise LLMResponseLimitError("LLM response is too large")
                body.extend(chunk)
            return ChatCompletion.model_validate_json(body)
    except TimeoutException as error:
        raise APITimeoutError(request=error.request) from None
    except HTTPError as error:
        raise APIConnectionError(request=error.request) from None
