import httpx2
import pytest

from app.smoke import main, run_smoke, validate_target

TOKEN = "synthetic-smoke-token"


@pytest.mark.parametrize("failure", [None, "database", "auth", "redirect", "network", "malformed", "oversized", "validation"])
def test_smoke_checks_are_read_only_and_fail_closed(failure, capsys) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path != "/agent/chat"
        if failure == "network":
            raise httpx2.ConnectError(TOKEN, request=request)
        if request.url.path == "/health":
            return httpx2.Response(200, json={"status": "ok"})
        if request.url.path == "/ready":
            if failure in {"malformed", "oversized"}:
                return httpx2.Response(200, content=b"x" * (16385 if failure == "oversized" else 1))
            return httpx2.Response(503 if failure == "database" else 200, json={"status": "ready"})
        if request.url.path == "/openapi.json":
            return httpx2.Response(404)
        if failure == "redirect":
            return httpx2.Response(307, headers={"Location": "https://foreign.example.com"})
        if request.url.params.get("limit") == "0":
            issue = {"loc": ["query", "limit"], "type": "greater_than_equal", "msg": "Invalid request value"}
            if failure == "validation":
                issue["input"] = TOKEN
            return httpx2.Response(422, json={"detail": [issue]})
        if request.headers.get("authorization") == "Bearer " + TOKEN:
            return httpx2.Response(200, json=[])
        return httpx2.Response(200 if failure == "auth" else 401, headers={"WWW-Authenticate": "Bearer"})

    assert run_smoke("https://api.example.com", TOKEN, transport=httpx2.MockTransport(respond)) == (failure is None)
    assert len(requests) == 7
    assert all(request.url.host == "api.example.com" for request in requests)
    output = capsys.readouterr().out
    assert TOKEN not in output and "foreign.example.com" not in output


@pytest.mark.parametrize("target", [
    "http://api.example.com", "https://user:secret@api.example.com", "https://api.example.com?secret=x",
    "https://api.example.com:invalid", "https://api.example.com:65536", "https://api.example.com\n",
])
def test_unsafe_smoke_targets_are_rejected(target) -> None:
    with pytest.raises(ValueError):
        validate_target(target, TOKEN)


@pytest.mark.parametrize(("target", "token"), [
    ("https://api.example.com", ""),
    ("https://api.example.com:invalid", TOKEN),
    ("https://api.example.com", TOKEN + "\x7f"),
])
def test_smoke_cli_requires_safe_configuration_without_printing_input(monkeypatch, capsys, target, token) -> None:
    monkeypatch.setenv("SMOKE_API_AUTH_TOKEN", token)
    assert main([target]) == 2
    assert "api.example.com" not in capsys.readouterr().out
