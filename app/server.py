"""Production server entry point; never reload or run migrations on startup."""

import os

import uvicorn

from app.config import load_settings
from app.logging import configure_logging


def main() -> None:
    configure_logging(load_settings().log_level)
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
        access_log=False, server_header=False, proxy_headers=False,
        log_config=None,
        limit_concurrency=16, timeout_graceful_shutdown=100,
    )


if __name__ == "__main__":
    main()
