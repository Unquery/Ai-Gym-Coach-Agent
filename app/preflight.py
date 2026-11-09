"""Offline M9 configuration checks; never connect, migrate, deploy or call an LLM."""

import argparse
import os
from typing import Literal

from sqlalchemy.engine import make_url

from app.config import load_settings, validate_production_database_url
from app.llm.config import DEFAULT_LLM_BASE_URL, load_llm_settings


def validate_profile(profile: Literal["runtime", "migration"]) -> None:
    if os.environ.get("APP_ENV") != "production":
        raise ValueError("Production mode is required")
    forbidden = (
        ("MIGRATION_DATABASE_URL", "TEST_DATABASE_URL", "POSTGRES_PASSWORD", "RUNTIME_DATABASE_PASSWORD",
         "CONTAINER_MIGRATION_DATABASE_URL", "CONTAINER_DATABASE_URL", "APP_DATABASE_PASSWORD")
        if profile == "runtime" else
        ("DATABASE_URL", "TEST_DATABASE_URL", "API_AUTH_TOKEN", "LLM_API_KEY", "RUNTIME_DATABASE_PASSWORD",
         "CONTAINER_MIGRATION_DATABASE_URL", "CONTAINER_DATABASE_URL", "APP_DATABASE_PASSWORD", "POSTGRES_PASSWORD")
    )
    if any(os.environ.get(name) for name in forbidden):
        raise ValueError("Credentials must be separated by process")

    value = os.environ.get("DATABASE_URL" if profile == "runtime" else "MIGRATION_DATABASE_URL", "")
    validate_production_database_url(value)
    url = make_url(value)
    if (
        "REPLACE_" in value.upper() or not url.host or not url.host.endswith(".neon.tech")
        or "-pooler." in url.host or url.query.get("channel_binding") != "require"
        or not url.query.get("sslrootcert")
        or (url.username != "gym_runtime" if profile == "runtime" else url.username == "gym_runtime")
    ):
        raise ValueError("Use the selected direct Neon endpoint, role and verified TLS settings")

    if profile == "runtime":
        settings = load_settings()
        llm = load_llm_settings()
        if (
            not {"localhost", "127.0.0.1"}.issubset(settings.allowed_hosts)
            or not any(host.endswith(".onrender.com") for host in settings.allowed_hosts)
            or any("REPLACE_" in host.upper() for host in settings.allowed_hosts)
            or str(llm.base_url).rstrip("/") != DEFAULT_LLM_BASE_URL
            or llm.model != "openai/gpt-oss-20b"
            or settings.chat_requests_per_minute > 2 or settings.api_requests_per_minute > 60
            or settings.db_pool_size > 3 or settings.db_max_overflow > 2
        ):
            raise ValueError("Use the reviewed initial Render/Groq demo profile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("runtime", "migration"))
    args = parser.parse_args(argv)
    try:
        validate_profile(args.profile)
    except ValueError:
        # Never format validator errors: they may include a URL, input or secret.
        print(f"FAIL offline_{args.profile}_configuration; verify the M9 profile and credential separation")
        return 1
    print(f"PASS offline_{args.profile}_configuration; account access and connectivity remain unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
