"""Single-operator authentication and bounded HTTP input for the portfolio MVP."""

from hmac import compare_digest
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
            if settings.environment == "production" and request.url.path == "/agent/chat":
                timestamps, lock = request.app.state.chat_rate_limit
                now = monotonic()
                with lock:
                    while timestamps and timestamps[0] <= now - 60:
                        timestamps.popleft()
                    if len(timestamps) >= settings.chat_requests_per_minute:
                        raise HTTPException(429, "Chat rate limit exceeded", headers={"Retry-After": "60"})
                    timestamps.append(now)
            return settings.api_owner_id
    raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


class RequestBodyLimit:
    """Bound actual streamed bytes, including requests without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        sent = False

        async def bounded_receive() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
