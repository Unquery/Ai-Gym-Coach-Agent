"""FastAPI entry point for uvicorn app.main:app."""

from app.api.application import app

__all__ = ["app"]
