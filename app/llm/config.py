"""Provider-neutral LLM settings loaded explicitly from the environment."""

import os
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, StringConstraints

DEFAULT_LLM_BASE_URL = "https://api.groq.com/openai/v1"


class LLMSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    base_url: HttpUrl = HttpUrl(DEFAULT_LLM_BASE_URL)
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    api_key: SecretStr = Field(min_length=1)


def read_required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"Environment variable {name} is required")
    value = value.strip()

    if value == "":
        raise ValueError(f"Environment variable {name} is required")
    return value


def load_llm_settings() -> LLMSettings:
    """Require explicit model/key selection; local Ollama accepts a dummy key."""
    return LLMSettings.model_validate(
        {
            "base_url": os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
            "model": read_required_env("LLM_MODEL"),
            "api_key": SecretStr(read_required_env("LLM_API_KEY")),
        }
    )
