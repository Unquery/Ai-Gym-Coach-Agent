from uuid import uuid4

import pytest

from app.config import AppSettings, load_settings


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "APP_ENV": "production", "ENABLE_DEMO_AUTH": "false",
        "API_AUTH_TOKEN": "synthetic-test-token-" + "x" * 32,
        "API_OWNER_ID": str(uuid4()), "ALLOWED_HOSTS": "gym.example.com",
        "DATABASE_URL": "postgresql+psycopg://runtime:synthetic@db.example.com/gym?sslmode=verify-full",
        "LLM_MODEL": "synthetic-model", "LLM_API_KEY": "synthetic-provider-key",
        "LLM_BASE_URL": "https://provider.example.com/v1", "CORS_ORIGINS": "",
    }.items():
        monkeypatch.setenv(name, value)


def test_safe_defaults_and_production_secrets(production_env: None) -> None:
    assert AppSettings().enable_demo_auth is False
    settings = load_settings()
    assert settings.environment == "production"
    assert settings.allowed_hosts == ("gym.example.com",)
    assert "synthetic-test-token" not in repr(settings)


@pytest.mark.parametrize(("name", "value"), [
    ("ENABLE_DEMO_AUTH", "true"), ("API_AUTH_TOKEN", "REPLACE_WITH_SECRET"),
    ("API_OWNER_ID", "00000000-0000-0000-0000-000000000001"),
    ("ALLOWED_HOSTS", "*"), ("CORS_ORIGINS", "*"),
    ("CORS_ORIGINS", "http://frontend.example.com"),
    ("DATABASE_URL", "postgresql+psycopg://runtime:synthetic@db.example.com/gym"),
    ("LLM_BASE_URL", "http://provider.example.com/v1"),
    ("LLM_API_KEY", ""), ("LOG_LEVEL", "DEBUG"),
    ("API_AUTH_TOKEN", " " * 40),
    ("API_AUTH_TOKEN", "synthetic token-" + "x" * 32),
    ("DATABASE_URL", "postgresql+psycopg://runtime:synthetic@db.example.com/gym?sslmode=require"),
    ("DATABASE_URL", "postgresql+psycopg://runtime:synthetic@db.example.com/gym?sslmode=verify-ca"),
    ("LLM_BASE_URL", "https://provider.example.com/v1?secret=synthetic"),
    ("CORS_ORIGINS", "https://frontend.example.com:invalid"),
    ("LLM_MODEL", "REPLACE_WITH_MODEL"),
])
def test_production_rejects_unsafe_configuration(
    production_env: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        load_settings()
    assert "runtime:synthetic" not in str(error.value)


def test_no_implicit_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENABLE_DEMO_AUTH", raising=False)
    assert load_settings().environment == "development"
    assert load_settings().enable_demo_auth is False
