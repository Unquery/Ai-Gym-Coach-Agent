"""Production server entry point; never reload or run migrations on startup."""

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
        access_log=False, server_header=False, proxy_headers=False,
        limit_concurrency=16, timeout_graceful_shutdown=100,
    )


if __name__ == "__main__":
    main()
