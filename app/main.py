"""HTTP entry point for AI Gym Coach."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Exercise
from app.schemas import ExerciseCreate, ExerciseRead

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


@app.post("/exercises", response_model=ExerciseRead, status_code=201)
def create_exercise(
    payload: ExerciseCreate, session: Annotated[Session, Depends(get_session)]
) -> Exercise:
    exercise = Exercise(name=payload.name)
    session.add(exercise)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == "uq_exercises_name"
        ):
            raise HTTPException(
                status_code=409, detail="Exercise with this name already exists"
            ) from exc
        raise
    session.refresh(exercise)
    return exercise
