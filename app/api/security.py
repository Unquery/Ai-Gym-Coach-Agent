"""Single-operator authentication and bounded HTTP input for the portfolio MVP."""

from asyncio import timeout
from collections.abc import Iterator
from hmac import compare_digest
from math import ceil
from time import monotonic
from uuid import UUID

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.seed import DEMO_USER_ID


def get_current_user_id(request: Request) -> UUID:
    settings = request.app.state.settings
    if settings.environment == "development" and settings.enable_demo_auth:
        return DEMO_USER_ID
    authorization = request.headers.getlist("authorization")
    expected = settings.api_auth_token
    if len(authorization) == 1 and expected is not None and settings.api_owner_id is not None:
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() == "bearer" and compare_digest(
            token.encode("utf-8"), expected.get_secret_value().encode("utf-8"),
        ):
            if settings.environment == "production":
                buckets, lock = request.app.state.rate_limits
                with lock:
                    now = monotonic()
                    limits = [(buckets["api"], settings.api_requests_per_minute)]
                    if request.url.path == "/agent/chat":
                        limits.append((buckets["chat"], settings.chat_requests_per_minute))
                    for timestamps, limit in limits:
                        while timestamps and timestamps[0] <= now - 60:
                            timestamps.popleft()
                        if len(timestamps) >= limit:
                            raise HTTPException(429, "Request rate limit exceeded", headers={
                                "Retry-After": str(max(1, ceil(timestamps[0] + 60 - now))),
                            })
                    for timestamps, _ in limits:
                        timestamps.append(now)
            return settings.api_owner_id
    raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def admit_chat(request: Request) -> Iterator[None]:
    """Keep provider waits from occupying every HTTP/DB worker; never queue."""
    if request.app.state.settings.environment != "production":
        yield
        return
    slots = request.app.state.chat_slots
    if not slots.acquire(blocking=False):
        raise HTTPException(503, "AI service is busy", headers={"Retry-After": "1"})
    try:
        yield
    finally:
        slots.release()


class RequestBodyLimit:
    """Bound streamed bytes and total body-read time, including chunked uploads."""

    def __init__(self, app: ASGIApp, max_bytes: int, read_timeout_seconds: float = 10) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.read_timeout_seconds = read_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        oversized = False
        try:
            async with timeout(self.read_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        oversized = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse(
                {"detail": "Request body read timed out"}, status_code=408,
                headers={"Connection": "close"},
            )(scope, receive, send)
            return
        if oversized:
            await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            return

        sent = False

        async def bounded_receive() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
