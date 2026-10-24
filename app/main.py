"""HTTP entry point for AI Gym Coach."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Exercise
from app.schemas import ExerciseRead

app = FastAPI(title="AI Gym Coach")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/exercises", response_model=list[ExerciseRead])
def list_exercises(session: Annotated[Session, Depends(get_session)]) -> list[Exercise]:
    return list(session.scalars(select(Exercise).order_by(Exercise.id)))


@app.get("/exercises/{exercise_id}", response_model=ExerciseRead)
def get_exercise(
    exercise_id: int, session: Annotated[Session, Depends(get_session)]
) -> Exercise:
    exercise = session.get(Exercise, exercise_id)
    if exercise is None:
        raise HTTPException(status_code=404, detail="Exercise not found")
    return exercise
