"""HTTP entry point for AI Gym Coach."""

from typing import Annotated
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query
from openai import APIError, APITimeoutError, RateLimitError
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import ValidationError
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
from app.conversation_store import (
    ConversationNotFoundError,
    append_message,
    create_conversation,
    get_recent_conversation_history,
)
from app.database import get_session
from app.llm import create_llm_client
from app.llm_config import load_llm_settings
from app.models import Exercise, Workout, WorkoutPlan, WorkoutSet
from app.proposal_store import (
    ProposalClosedError,
    ProposalDecision,
    ProposalNotFoundError,
    decide_workout_proposal,
    get_workout_proposal,
)
from app.queries import get_workout_history
from app.schemas import (
    ExerciseCreate,
    ExerciseRead,
    ProposalActionRequest,
    ProposalActionResponse,
    WorkoutCreate,
    WorkoutDetailRead,
    WorkoutRead,
    WorkoutSetCreate,
    WorkoutSetRead,
)
from app.seed import DEMO_USER_ID
from app.workout_execution import (
    ProposalCatalogChangedError,
    ProposalNotConfirmedError,
    execute_confirmed_workout,
)

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

    # Do not insert the configured provider credential even if pasted into chat.
    if settings.api_key.get_secret_value() in payload.message:
        raise HTTPException(status_code=422, detail="Message contains a configured secret")

    try:
        with session.begin():
            conversation_id = payload.conversation_id
            if conversation_id is None:
                conversation_id = create_conversation(session=session, user_id=user_id).id
            # Ownership is checked before opening an LLM client. The parent lock
            # remains held through the whole turn so concurrent pairs stay together.
            current_message = append_message(
                conversation_id, session=session, user_id=user_id,
                role="user", content=payload.message,
            )
            history = get_recent_conversation_history(
                conversation_id, before_position=current_message.position,
                session=session, user_id=user_id,
            )
            with create_llm_client(settings) as client:
                result = run_agent(
                    payload.message, client=client, settings=settings,
                    session=session, user_id=user_id, history=history,
                )
            response: AgentAnswerResponse | AgentProposalResponse
            if isinstance(result, str):
                content = result
                response = AgentAnswerResponse(conversation_id=conversation_id, message=result)
            else:
                # Store the validated proposal, never discarded model prose or settings.
                content = result.model_dump_json()
                response = AgentProposalResponse(conversation_id=conversation_id, proposal=result)
            if settings.api_key.get_secret_value() in content:
                raise AgentError("The AI response contains a configured secret")
            append_message(
                conversation_id, session=session, user_id=user_id,
                role="assistant", content=content,
            )
        # Report the conversation, messages, and any proposal only after commit.
        return response
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
    except APITimeoutError:
        raise HTTPException(status_code=504, detail="AI service timed out") from None
    except RateLimitError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None
    except (APIError, AgentError):
        raise HTTPException(status_code=502, detail="AI response could not be produced") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None


def _apply_proposal_decision(
    proposal_id: UUID,
    decision: ProposalDecision,
    *,
    session: Session,
    user_id: UUID,
) -> ProposalActionResponse:
    """Apply a human HTTP action and its writes in one request-owned transaction."""
    try:
        with session.begin():
            # Keep the owner-scoped row lock through the decision and execution.
            proposal = get_workout_proposal(proposal_id, session=session, user_id=user_id)
            if proposal.state == "pending":
                proposal = decide_workout_proposal(
                    proposal_id, decision, session=session, user_id=user_id,
                )

            response = None
            if proposal.state != "expired":
                expected_state = "confirmed" if decision == "confirm" else "rejected"
                if proposal.state != expected_state:
                    raise HTTPException(
                        status_code=409, detail="Proposal cannot accept this decision",
                    )
                workout_id = (
                    execute_confirmed_workout(
                        proposal_id, session=session, user_id=user_id,
                    ).id
                    if decision == "confirm" else None
                )
                response = ProposalActionResponse(
                    proposal_id=proposal_id, state=expected_state, workout_id=workout_id,
                )
    except ProposalNotFoundError:
        raise HTTPException(status_code=404, detail="Proposal not found") from None
    except ProposalCatalogChangedError:
        raise HTTPException(
            status_code=409, detail="Proposal exercise catalog has changed",
        ) from None
    except (ProposalClosedError, ProposalNotConfirmedError):
        raise HTTPException(
            status_code=409, detail="Proposal cannot accept this decision",
        ) from None
    except (SQLAlchemyError, ValidationError):
        raise HTTPException(status_code=503, detail="Workout service is unavailable") from None

    # Expiration is a persisted transition: commit it before raising an HTTP error.
    if response is None:
        raise HTTPException(status_code=410, detail="Proposal has expired")
    return response


@app.post("/workout-proposals/{proposal_id}/confirm", response_model=ProposalActionResponse)
def confirm_workout_proposal(
    proposal_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
    payload: Annotated[ProposalActionRequest, Body()] = ProposalActionRequest(),
) -> ProposalActionResponse:
    return _apply_proposal_decision(
        proposal_id, "confirm", session=session, user_id=user_id,
    )


@app.post("/workout-proposals/{proposal_id}/reject", response_model=ProposalActionResponse)
def reject_workout_proposal(
    proposal_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_demo_user_id)],
    payload: Annotated[ProposalActionRequest, Body()] = ProposalActionRequest(),
) -> ProposalActionResponse:
    return _apply_proposal_decision(
        proposal_id, "reject", session=session, user_id=user_id,
    )


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
