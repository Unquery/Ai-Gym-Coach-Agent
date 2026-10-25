import json
import logging
import os

import httpx2
import pytest
from openai import AuthenticationError, DefaultHttpx2Client, RateLimitError
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam
from pydantic import ValidationError

from app.llm import create_chat_completion, create_llm_client
from app.llm_config import load_llm_settings, read_required_env
from app.tool_schemas import GetRecentWorkoutsInput


@pytest.fixture
def llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")


@pytest.mark.parametrize("value", [None, "", "   ", " test-model "])
def test_read_required_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("LLM_MODEL", raising=False)
    else:
        monkeypatch.setenv("LLM_MODEL", value)

    if value is None or not value.strip():
        with pytest.raises(
            ValueError, match="^Environment variable LLM_MODEL is required$"
        ):
            read_required_env("LLM_MODEL")
    else:
        assert read_required_env("LLM_MODEL") == "test-model"
        assert os.environ["LLM_MODEL"] == value


def test_settings_default_to_groq_and_hide_the_key(
    llm_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_MODEL", " test-model ")
    monkeypatch.setenv("LLM_API_KEY", " synthetic-test-key ")
    settings = load_llm_settings()

    assert str(settings.base_url) == "https://api.groq.com/openai/v1"
    assert settings.model == "test-model"
    assert settings.api_key.get_secret_value() == "synthetic-test-key"
    assert "synthetic-test-key" not in repr(settings)
    assert "synthetic-test-key" not in settings.model_dump_json()


@pytest.mark.parametrize("name", ["LLM_MODEL", "LLM_API_KEY"])
def test_settings_require_model_and_key(
    llm_environment: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=f"^Environment variable {name} is required$"):
        load_llm_settings()


def test_settings_reject_invalid_base_url_without_exposing_the_key(
    llm_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "not-a-url")
    with pytest.raises(ValidationError) as error:
        load_llm_settings()
    assert "synthetic-test-key" not in str(error.value)


def completion_body(
    message: dict[str, object], *, finish_reason: str = "stop"
) -> dict[str, object]:
    return {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


@pytest.mark.parametrize(
    "base_url, model, key, expected_url",
    [
        (
            None,
            "groq-test-model",
            "synthetic-test-key",
            "https://api.groq.com/openai/v1/chat/completions",
        ),
        (
            "http://localhost:11434/v1/",
            "local-model",
            "ollama",
            "http://localhost:11434/v1/chat/completions",
        ),
        (
            "https://provider.example/api/v1",
            "custom-model",
            "custom-test-key",
            "https://provider.example/api/v1/chat/completions",
        ),
    ],
)
def test_chat_completion_uses_configured_provider(
    llm_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    base_url: str | None,
    model: str,
    key: str,
    expected_url: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if base_url is not None:
        monkeypatch.setenv("LLM_BASE_URL", base_url)
    monkeypatch.setenv("LLM_MODEL", model)
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.example/v1")
    settings = load_llm_settings()
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, json=completion_body({"role": "assistant", "content": "Synthetic reply"})
        )

    transport = DefaultHttpx2Client(transport=httpx2.MockTransport(respond))
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "Hello"}]
    with create_llm_client(settings, http_client=transport) as client:
        assert requests == []
        assert client.max_retries == 0
        assert client.timeout == 30.0
        result = create_chat_completion(client, settings, messages)

    assert client.is_closed()
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == expected_url
    assert requests[0].headers["authorization"] == f"Bearer {key}"
    assert json.loads(requests[0].content) == {"model": model, "messages": messages}
    assert result.choices[0].message.content == "Synthetic reply"
    output = capsys.readouterr()
    assert key not in caplog.text + output.out + output.err


def test_chat_completion_preserves_tool_calls_without_executing_them(
    llm_environment: None,
) -> None:
    settings = load_llm_settings()
    tools: list[ChatCompletionToolUnionParam] = [
        {
            "type": "function",
            "function": {
                "name": "get_recent_workouts",
                "description": "Read recent workouts for the current user.",
                "parameters": GetRecentWorkoutsInput.model_json_schema(),
            },
        }
    ]
    tool_calls = [
        {
            "id": "synthetic-call",
            "type": "function",
            "function": {
                "name": "get_recent_workouts",
                "arguments": '{"limit":3,"offset":0}',
            },
        }
    ]
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json=completion_body(
                {"role": "assistant", "content": None, "tool_calls": tool_calls},
                finish_reason="tool_calls",
            ),
        )

    transport = DefaultHttpx2Client(transport=httpx2.MockTransport(respond))
    with create_llm_client(settings, http_client=transport) as client:
        result = create_chat_completion(
            client,
            settings,
            [{"role": "user", "content": "Show my last three workouts"}],
            tools=tools,
        )

    assert len(requests) == 1
    assert json.loads(requests[0].content)["tools"] == tools
    message = result.choices[0].message
    assert message.content is None
    assert message.tool_calls is not None
    assert [call.model_dump(exclude_none=True) for call in message.tool_calls] == tool_calls


@pytest.mark.parametrize(
    "status, error_type", [(401, AuthenticationError), (429, RateLimitError)]
)
def test_provider_errors_propagate_without_retrying(
    llm_environment: None, status: int, error_type: type[Exception]
) -> None:
    settings = load_llm_settings()
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status, json={"error": {"message": "Synthetic failure"}})

    transport = DefaultHttpx2Client(transport=httpx2.MockTransport(respond))
    with create_llm_client(settings, http_client=transport) as client:
        with pytest.raises(error_type):
            create_chat_completion(
                client, settings, [{"role": "user", "content": "Hello"}]
            )
    assert len(requests) == 1
