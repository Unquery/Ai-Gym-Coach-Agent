"""HTTP entry point for AI Gym Coach."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from openai import APIError, APITimeoutError, RateLimitError
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.agent import AgentError, run_agent
from app.agent_schemas import (
    AgentAnswerResponse,
    AgentChatRequest,
    AgentChatResponse,
    AgentProposalResponse,
)
from app.database import get_session
from app.llm import create_llm_client
from app.llm_config import load_llm_settings
from app.models import Exercise, Workout, WorkoutPlan, WorkoutSet
from app.queries import get_workout_history
from app.schemas import (
    ExerciseCreate,
    ExerciseRead,
    WorkoutCreate,
    WorkoutDetailRead,
    WorkoutRead,
    WorkoutSetCreate,
    WorkoutSetRead,
)
from app.seed import DEMO_USER_ID

app = FastAPI(title="AI Gym Coach")


def get_demo_user_id() -> UUID:
    """Use the synthetic demo owner until authentication is implemented."""
    return DEMO_USER_ID


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/chat", response_model=AgentChatResponse)
def agent_chat(
    payload: AgentChatRequest,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
) -> AgentAnswerResponse | AgentProposalResponse:
    # Load configuration only after body validation, without affecting app startup.
    try:
        settings = load_llm_settings()
    except ValueError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None

    try:
        with session.begin():
            with create_llm_client(settings) as client:
                result = run_agent(
                    payload.message, client=client, settings=settings,
                    session=session, user_id=user_id,
                )
            response = (
                AgentAnswerResponse(message=result) if isinstance(result, str)
                else AgentProposalResponse(proposal=result)
            )
        # A pending proposal is reported as stored only after commit succeeds.
        return response
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="AI service timed out") from None
    except RateLimitError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None
    except (APIError, AgentError):
        raise HTTPException(status_code=502, detail="AI response could not be produced") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None


@app.get("/exercises", response_model=list[ExerciseRead])
def list_exercises(session: Annotated[Session, Depends(get_session)]) -> list[Exercise]:
    return list(session.scalars(select(Exercise).order_by(Exercise.id)))


@app.get("/exercises/{exercise_id}", response_model=ExerciseRead)
def get_exercise(
    exercise_id: Annotated[int, Path(gt=0, le=2147483647)],
    session: Annotated[Session, Depends(get_session)],
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


@app.post("/workouts", response_model=WorkoutRead, status_code=201)
def create_workout(
    payload: WorkoutCreate,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
) -> Workout:
    if payload.plan_id is not None:
        result = session.scalar(
            select(WorkoutPlan).where(
                WorkoutPlan.id == payload.plan_id, WorkoutPlan.user_id == user_id
            )
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Workout plan not found")

    workout = Workout(user_id=user_id, **payload.model_dump(exclude_none=True))
    session.add(workout)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if (
            isinstance(exc.orig, ForeignKeyViolation)
            and exc.orig.diag.constraint_name == "fk_workouts_plan_id_workout_plans"
        ):
            raise HTTPException(
                status_code=404, detail="Workout plan not found"
            ) from exc
        raise
    session.refresh(workout)
    return workout


@app.post("/workouts/{workout_id}/sets", response_model=WorkoutSetRead, status_code=201)
def create_workout_set(
    workout_id: Annotated[int, Path(gt=0, le=2147483647)],
    payload: WorkoutSetCreate,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
) -> WorkoutSet:
    workout = session.scalar(
        select(Workout).where(Workout.id == workout_id, Workout.user_id == user_id)
    )
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    if session.get(Exercise, payload.exercise_id) is None:
        raise HTTPException(status_code=404, detail="Exercise not found")

    workout_set = WorkoutSet(workout_id=workout_id, **payload.model_dump())
    session.add(workout_set)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if (
            isinstance(exc.orig, UniqueViolation)
            and exc.orig.diag.constraint_name == "uq_workout_sets_workout_id"
        ):
            raise HTTPException(
                status_code=409,
                detail="Set number already exists for this exercise in this workout",
            ) from exc
        if isinstance(exc.orig, ForeignKeyViolation):
            if exc.orig.diag.constraint_name == "fk_workout_sets_workout_id_workouts":
                raise HTTPException(
                    status_code=404, detail="Workout not found"
                ) from exc
            if exc.orig.diag.constraint_name == "fk_workout_sets_exercise_id_exercises":
                raise HTTPException(
                    status_code=404, detail="Exercise not found"
                ) from exc
        raise
    session.refresh(workout_set)
    return workout_set


@app.get("/workouts/{workout_id}", response_model=WorkoutDetailRead)
def get_workout(
    workout_id: Annotated[int, Path(gt=0, le=2147483647)],
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
) -> Workout:
    result = session.scalar(
        select(Workout)
        .where(Workout.id == workout_id, Workout.user_id == user_id)
        .options(selectinload(Workout.sets))
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    return result


@app.get("/workouts", response_model=list[WorkoutRead])
def list_workouts(
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
) -> list[Workout]:
    return get_workout_history(session, user_id=user_id, limit=limit, offset=offset)
