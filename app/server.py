"""Production server entry point; never reload or run migrations on startup."""

import logging
import os

import uvicorn

from app.config import load_settings
from app.db.session import dispose_db_engine
from app.logging import configure_logging


def main() -> int:
    configure_logging()
    try:
        settings = load_settings()
        port = int(os.environ.get("PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port")
    except ValueError:
        # Configuration errors can contain raw URLs or incorrectly placed secrets.
        logging.getLogger("gym.application").error("startup_configuration_error")
        return 1
    logging.getLogger().setLevel(settings.log_level)
    try:
        uvicorn.run(
            "app.main:app", host="0.0.0.0", port=port,
            access_log=False, server_header=False, proxy_headers=False,
            log_config=None,
            workers=1,
            limit_concurrency=16, timeout_graceful_shutdown=100,
        )
    finally:
        # Uvicorn drains requests and completes ASGI shutdown before returning.
        dispose_db_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
