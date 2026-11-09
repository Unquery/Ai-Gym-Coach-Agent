import os
import socket
from uuid import uuid4

import pytest

from app.preflight import main


@pytest.fixture
def cloud_profile(monkeypatch):
    for name in (
        "DATABASE_URL", "MIGRATION_DATABASE_URL", "TEST_DATABASE_URL", "POSTGRES_PASSWORD",
        "RUNTIME_DATABASE_PASSWORD", "API_AUTH_TOKEN", "LLM_API_KEY",
        "CONTAINER_MIGRATION_DATABASE_URL", "CONTAINER_DATABASE_URL", "APP_DATABASE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")

    def no_network(*args, **kwargs):
        raise AssertionError("Offline preflight attempted a network operation")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def configure(profile):
        url = (
            "postgresql+psycopg://ROLE:synthetic-secret@ep-synthetic.eu-central-1.aws.neon.tech/gym"
            "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt&channel_binding=require"
        )
        if profile == "migration":
            monkeypatch.setenv("MIGRATION_DATABASE_URL", url.replace("ROLE", "neondb_owner"))
        else:
            for name, value in {
                "DATABASE_URL": url.replace("ROLE", "gym_runtime"), "ENABLE_DEMO_AUTH": "false",
                "API_AUTH_TOKEN": "synthetic-operator-" + "x" * 32, "API_OWNER_ID": str(uuid4()),
                "ALLOWED_HOSTS": "gym-synthetic.onrender.com,localhost,127.0.0.1", "CORS_ORIGINS": "",
                "LLM_MODEL": "openai/gpt-oss-20b", "LLM_API_KEY": "synthetic-provider-key",
                "LLM_BASE_URL": "https://api.groq.com/openai/v1", "CHAT_REQUESTS_PER_MINUTE": "2",
                "API_REQUESTS_PER_MINUTE": "60",
            }.items():
                monkeypatch.setenv(name, value)
    return configure


@pytest.mark.parametrize("profile", ["runtime", "migration"])
def test_safe_profiles_pass_without_network(cloud_profile, capsys, profile):
    cloud_profile(profile)
    assert main([profile]) == 0
    assert "remain unverified" in capsys.readouterr().out


@pytest.mark.parametrize(("profile", "field", "value"), [
    ("runtime", "MIGRATION_DATABASE_URL", "synthetic-sensitive-migrator"),
    ("migration", "LLM_API_KEY", "synthetic-sensitive-provider"),
    ("runtime", "ALLOWED_HOSTS", "REPLACE_WITH_NAME.onrender.com,localhost,127.0.0.1"),
    ("runtime", "LLM_BASE_URL", "https://other.example.com/v1"),
    ("runtime", "CHAT_REQUESTS_PER_MINUTE", "10"),
    ("runtime", "DB_POOL_SIZE", "4"),
    ("runtime", "DB_MAX_OVERFLOW", "3"),
])
def test_wrong_profile_is_rejected_without_exposing_input(cloud_profile, monkeypatch, capsys, profile, field, value):
    cloud_profile(profile)
    monkeypatch.setenv(field, value)
    assert main([profile]) == 1
    output = capsys.readouterr().out
    assert "FAIL offline_" in output and value not in output


@pytest.mark.parametrize("failure", ["pooler", "role", "channel_binding", "placeholder"])
def test_migration_requires_direct_neon_tls_profile(cloud_profile, monkeypatch, failure):
    cloud_profile("migration")
    value = os.environ["MIGRATION_DATABASE_URL"]
    before, after = {
        "pooler": ("ep-synthetic.", "ep-synthetic-pooler."),
        "role": ("neondb_owner", "gym_runtime"),
        "channel_binding": ("channel_binding=require", "channel_binding=prefer"),
        "placeholder": ("synthetic-secret", "REPLACE_WITH_PASSWORD"),
    }[failure]
    monkeypatch.setenv("MIGRATION_DATABASE_URL", value.replace(before, after))
    assert main(["migration"]) == 1
