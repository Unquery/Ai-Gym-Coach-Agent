"""Allowlisted JSON logs: no raw messages, exception text, SQL or request payloads."""

from datetime import UTC, datetime
import json
import logging
import sys
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

EVENTS = {"http_request", "application_started", "application_stopped", "application_error", "database_error",
          "startup_configuration_error"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.msg if isinstance(record.msg, str) and record.msg in EVENTS else "runtime_event",
        }
        # These fields are supplied only by the HTTP middleware/handlers below.
        if record.name.startswith("gym."):
            for name in ("request_id", "method", "route", "status", "duration_ms", "error_type"):
                if hasattr(record, name):
                    payload[name] = getattr(record, name)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Uvicorn must not retain an independent plaintext/traceback handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.WARNING)


class RequestLogging:
    def __init__(self, app: ASGIApp, production: bool) -> None:
        self.app = app
        self.production = production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-request-id", request_id.encode("ascii")),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ])
                if self.production:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        finally:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            # Successful probes are frequent and carry no useful diagnostic data.
            if status != 200 or route not in {"/health", "/ready"}:
                method = scope["method"]
                logging.getLogger("gym.http").log(
                    logging.ERROR if status >= 500 else logging.INFO, "http_request",
                    extra={
                        "request_id": request_id,
                        "method": method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "OTHER",
                        "route": route, "status": status,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    },
                )
