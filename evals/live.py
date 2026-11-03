"""Opt-in Groq evaluation with a shared request budget and no automatic retries.

Preview: uv run --env-file .env --locked python -m evals.live
Run: add --live --scenario recent_workout_tool (never run without permission).
Uses TEST_DATABASE_URL and disposable schemas, never DATABASE_URL/demo tables.
"""

import argparse
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx2
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.session import create_db_engine, postgres_url
from app.llm.config import LLMSettings, load_llm_settings
from evals.runner import run_scenario, summarize
from evals.scenarios import SCENARIOS


class GroqBudget:
    """One budget across all turns/scenarios; transport injection keeps tests offline."""

    def __init__(
        self, settings: LLMSettings, *, max_requests: int = 6,
        max_completion_tokens: int = 512, transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if str(settings.base_url).rstrip("/") != "https://api.groq.com/openai/v1":
            raise ValueError("Live evaluations require the standard HTTPS Groq endpoint")
        if not 1 <= max_requests <= 24 or not 64 <= max_completion_tokens <= 1024:
            raise ValueError("Use 1..24 requests and 64..1024 completion tokens")
        self.settings = settings
        self.max_requests = max_requests
        self.max_completion_tokens = max_completion_tokens
        self.requests = 0
        self.stop_reason: str | None = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.usage_responses = 0
        self.client = httpx2.Client(
            transport=transport or httpx2.HTTPTransport(retries=0),
            timeout=30.0, follow_redirects=False, trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def send(self, request: httpx2.Request) -> httpx2.Response:
        if self.stop_reason or self.requests >= self.max_requests:
            self.stop_reason = self.stop_reason or "request_budget_exhausted"
            raise httpx2.RequestError("Evaluation request budget stopped the run", request=request)
        if str(request.url) != "https://api.groq.com/openai/v1/chat/completions":
            raise ValueError("Unexpected evaluation endpoint")
        if len(request.content) > 32_000:
            self.stop_reason = "prompt_size_limit"
            raise httpx2.RequestError("Evaluation prompt exceeds 32000 bytes", request=request)
        payload = json.loads(request.content)
        payload.update(max_completion_tokens=self.max_completion_tokens, temperature=0)
        self.requests += 1  # Count attempts, including network failures and rate limits.
        try:
            response = self.client.post(
                str(request.url), json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
            )
        except httpx2.RequestError:
            self.stop_reason = "network_error"
            raise
        if not response.is_success:
            self.stop_reason = f"provider_http_{response.status_code}"
        try:
            usage = response.json().get("usage", {})
            values = [usage.get(key) for key in self.usage]
            if all(type(value) is int and value >= 0 for value in values):
                self.usage_responses += 1
                for key, value in zip(self.usage, values):
                    self.usage[key] += value
        except (ValueError, AttributeError):
            pass  # Missing usage is unknown, never inferred from text length.
        return response


@contextmanager
def disposable_engine(url: str) -> Iterator[Engine]:
    """Migrate/drop only the randomly generated evaluation schema."""
    parsed = postgres_url(url)
    bootstrap = create_db_engine(parsed)
    schema = "eval_" + uuid4().hex
    engine = create_db_engine(parsed.update_query_dict({"options": f"-csearch_path={schema}"}))
    created = False
    try:
        with bootstrap.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        try:
            if created:
                with bootstrap.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            bootstrap.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable paid Groq requests")
    parser.add_argument("--scenario", action="append", choices=[s.name for s in SCENARIOS])
    parser.add_argument("--max-requests", type=int, choices=range(1, 25), default=6)
    parser.add_argument("--max-completion-tokens", type=int, choices=range(64, 1025), default=512)
    args = parser.parse_args(argv)
    names = args.scenario or [SCENARIOS[0].name]
    if len(names) != len(set(names)) or len(names) > 3:
        parser.error("Select at most three distinct scenarios per run")
    selected = [next(s for s in SCENARIOS if s.name == name) for name in names]
    if not args.live:
        print(json.dumps({"mode": "preview", "scenarios": names, "max_requests": args.max_requests,
                          "max_completion_tokens": args.max_completion_tokens}))
        return 0
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        parser.error("TEST_DATABASE_URL is required; DATABASE_URL is never used")
    results = []
    budget = GroqBudget(load_llm_settings(), max_requests=args.max_requests,
                        max_completion_tokens=args.max_completion_tokens)
    try:
        for scenario in selected:
            with disposable_engine(url) as engine:
                results.append(run_scenario(scenario, engine=engine, provider=budget))
            if budget.stop_reason or budget.requests >= budget.max_requests:
                break
    finally:
        budget.close()
    report = summarize(results, expected_scenarios=len(selected))
    report.update(mode="live_groq_http", model=budget.settings.model, provider_requests=budget.requests,
                  token_usage=budget.usage, usage_responses=budget.usage_responses,
                  stop_reason=budget.stop_reason)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["complete"] and all(r.task_success for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
