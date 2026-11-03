"""All live-runner checks use an injected HTTP mock; never contact Groq."""

import json
from unittest.mock import Mock

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from app.llm.config import LLMSettings
from evals.live import GroqBudget, disposable_engine, main
from evals.runner import Observation, _completion, run_scenario, score_scenario
from evals.scenarios import SCENARIOS


def settings() -> LLMSettings:
    return LLMSettings(model="synthetic-model", api_key=SecretStr("synthetic-key"))


def test_preview_never_loads_credentials_or_database(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("Preview must be offline"))
    monkeypatch.setattr("evals.live.load_llm_settings", forbidden)
    monkeypatch.setattr("evals.live.disposable_engine", forbidden)
    assert main([]) == 0
    forbidden.assert_not_called()


def test_safe_clarification_does_not_require_a_forced_tool_attempt() -> None:
    scenario = SCENARIOS[3]
    observed = Observation((), (), (), scenario.initial_sets, (), 200, "answer", True, False, 0)
    result = score_scenario(scenario, [observed])
    assert result.task_success and result.tool_slots == 0


@pytest.mark.parametrize("failure", ["budget", "rate_limit", "network", "prompt_size"])
def test_live_budget_stops_without_retrying(failure: str) -> None:
    sent: list[dict] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        if failure == "network":
            raise httpx2.ReadTimeout("Synthetic timeout", request=request)
        return httpx2.Response(429 if failure == "rate_limit" else 200, json={})

    budget = GroqBudget(settings(), max_requests=1, transport=httpx2.MockTransport(respond))
    request = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions",
                             json={"messages": "x" * (32_001 if failure == "prompt_size" else 1)})
    try:
        if failure in {"network", "prompt_size"}:
            with pytest.raises(httpx2.RequestError):
                budget.send(request)
        else:
            budget.send(request)
        with pytest.raises(httpx2.RequestError):
            budget.send(request)
        assert len(sent) == (0 if failure == "prompt_size" else 1)
        if sent:
            assert sent[0]["max_completion_tokens"] == 512 and sent[0]["temperature"] == 0
    finally:
        budget.close()


@pytest.mark.postgres
def test_live_path_uses_real_workflow_with_mocked_network(postgres_engine: Engine) -> None:
    turn = SCENARIOS[0].turns[0]
    requests = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        response = _completion(turn, requests)
        requests += 1
        payload = response.json()
        payload["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        return httpx2.Response(200, json=payload)

    budget = GroqBudget(settings(), transport=httpx2.MockTransport(respond))
    try:
        result = run_scenario(SCENARIOS[0], engine=postgres_engine, provider=budget)
        assert result.task_success, result
        assert budget.requests == budget.usage_responses == 2
        assert budget.usage == {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240}
    finally:
        budget.close()


@pytest.mark.postgres
def test_disposable_schema_is_removed_after_failure(postgres_engine: Engine) -> None:
    from sqlalchemy import text

    url = postgres_engine.url.difference_update_query(["options"])
    schema = None
    with pytest.raises(RuntimeError, match="synthetic"):
        with disposable_engine(url.render_as_string(hide_password=False)) as engine:
            with engine.connect() as connection:
                schema = connection.scalar(text("SELECT current_schema()"))
            raise RuntimeError("synthetic")
    with postgres_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname=:name"), {"name": schema}) == 0
