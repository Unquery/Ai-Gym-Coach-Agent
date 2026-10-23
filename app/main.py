"""HTTP entry point for AI Gym Coach."""

from fastapi import FastAPI

app = FastAPI(title="AI Gym Coach")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
