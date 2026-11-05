"""HTTP entry point for AI Gym Coach."""

from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from threading import Lock
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import APIError, APITimeoutError, RateLimitError
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent.loop import AgentError, run_agent
from app.config import AppSettings, load_settings
from app.api.security import RequestBodyLimit, get_current_user_id
from app.agent.tools import DraftMutationContext
from app.api.schemas import (
    AgentAnswerResponse,
    AgentChatRequest,
    AgentChatResponse,
    AgentProposalResponse,
)
from app.api.idempotency import (
    ChatRequestConflictError,
    ChatRequestError,
    ChatRequestInProgressError,
    chat_request_fingerprint,
    complete_chat_request,
    lock_chat_request,
    register_chat_request,
)
from app.conversations.store import (
    ConversationNotFoundError,
    append_message,
    create_conversation,
    get_recent_conversation_history,
)
from app.db.session import get_session
from app.db.health import database_ready
from app.logging import RequestLogging
from app.llm.client import create_llm_client
from app.llm.config import load_llm_settings
from app.db.models import Conversation, Exercise, Workout, WorkoutPlan, WorkoutSet
from app.workouts.proposal_store import (
    ProposalClosedError,
    ProposalDecision,
    ProposalNotFoundError,
    decide_workout_proposal,
    get_workout_proposal,
)
from app.workouts.queries import get_workout_history
from app.workouts.schemas import (
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
from app.workouts.execution import (
    ProposalCatalogChangedError,
    ProposalNotConfirmedError,
    execute_confirmed_workout,
)
from app.workouts.drafts import WorkoutDraftNotFoundError, get_workout_draft


public_routes = APIRouter()
router = APIRouter(dependencies=[Depends(get_current_user_id)])
# Preserve the existing internal dependency-override hook used by offline evals.
get_demo_user_id = get_current_user_id


@public_routes.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@public_routes.get("/ready")
def ready(request: Request) -> JSONResponse:
    try:
        available = database_ready(production=request.app.state.settings.environment == "production")
    except (SQLAlchemyError, RuntimeError, ValueError):
        available = False
    return JSONResponse(
        {"status": "ready" if available else "not_ready"}, status_code=200 if available else 503,
    )


async def get_chat_idempotency(
    request: Request,
    key: Annotated[UUID | None, Header(alias="Idempotency-Key")] = None,
) -> tuple[UUID, str] | None:
    if key is None:
        return None
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise HTTPException(status_code=422, detail="Supply one Idempotency-Key")
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(status_code=422, detail="Chat request must be a JSON object")
    try:
        # Hash raw parsed JSON, before Pydantic trims message text or fills defaults.
        return key, chat_request_fingerprint(raw_body)
    except (TypeError, ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="Invalid chat request JSON") from None


def _run_chat_turn(
    payload: AgentChatRequest, *, session: Session, user_id: UUID,
    request_id: UUID | None = None,
) -> AgentAnswerResponse | AgentProposalResponse:
    """Run inside the caller's transaction; this helper never commits."""
    try:
        settings = load_llm_settings()
    except ValueError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None

    # Do not insert the configured provider credential even if pasted into chat.
    if settings.api_key.get_secret_value() in payload.message:
        raise HTTPException(status_code=422, detail="Message contains a configured secret")

    conversation_id = payload.conversation_id
    if conversation_id is None:
        conversation_id = create_conversation(session=session, user_id=user_id).id
    # Ownership is checked before opening an LLM client. The parent lock remains
    # held through the whole turn so concurrent pairs stay together.
    current_message = append_message(
        conversation_id, session=session, user_id=user_id,
        role="user", content=payload.message,
    )
    history = get_recent_conversation_history(
        conversation_id, before_position=current_message.position,
        session=session, user_id=user_id,
    )
    draft_context = None
    if request_id is not None:
        try:
            draft = get_workout_draft(conversation_id, session=session, user_id=user_id)
        except WorkoutDraftNotFoundError:
            pass
        else:
            if draft.state == "active":
                draft_context = DraftMutationContext(
                    conversation_id=conversation_id, request_id=request_id, snapshot=draft,
                )
    with create_llm_client(settings) as client:
        result = run_agent(
            payload.message, client=client, settings=settings,
            session=session, user_id=user_id, history=history,
            **({"draft_context": draft_context} if draft_context is not None else {}),
        )
    response: AgentAnswerResponse | AgentProposalResponse
    if isinstance(result, str):
        content = result
        response = AgentAnswerResponse(conversation_id=conversation_id, message=result)
    else:
        content = result.model_dump_json()
        response = AgentProposalResponse(conversation_id=conversation_id, proposal=result)
    if settings.api_key.get_secret_value() in content:
        raise AgentError("The AI response contains a configured secret")
    append_message(
        conversation_id, session=session, user_id=user_id,
        role="assistant", content=content,
    )
    return response


@router.post("/agent/chat", response_model=AgentChatResponse)
def agent_chat(
    payload: AgentChatRequest,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    idempotency: Annotated[tuple[UUID, str] | None, Depends(get_chat_idempotency)],
) -> AgentAnswerResponse | AgentProposalResponse | JSONResponse:
    try:
        request_id = None
        if idempotency is not None:
            key, fingerprint = idempotency
            # Persist binding before execution, including for failed/restarted attempts.
            with session.begin():
                stored = register_chat_request(key, fingerprint, session=session, user_id=user_id)
                request_id = stored.id
        with session.begin():
            stored = lock_chat_request(request_id, session=session, user_id=user_id) if request_id else None
            if stored is not None and stored.state == "completed":
                if stored.response_body is None or stored.response_status is None or stored.conversation_id is None:
                    raise ChatRequestError("Incomplete stored response")
                # Cached output never grants access to a deleted or reassigned conversation.
                with session.no_autoflush:
                    owned_id = session.scalar(select(Conversation.id).where(
                        Conversation.id == stored.conversation_id, Conversation.user_id == user_id,
                    ).with_for_update())
                if owned_id is None:
                    raise ConversationNotFoundError("Conversation not found")
                response: AgentAnswerResponse | AgentProposalResponse | JSONResponse = JSONResponse(
                    status_code=stored.response_status, content=stored.response_body,
                )
            else:
                response = _run_chat_turn(payload, session=session, user_id=user_id, request_id=request_id)
                if stored is not None:
                    # This update and all conversation/draft/proposal writes commit atomically.
                    complete_chat_request(
                        stored.id, conversation_id=response.conversation_id,
                        response_status=200, response_body=response.model_dump(mode="json"),
                        session=session, user_id=user_id,
                    )
        return response
    except ChatRequestConflictError:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"}) from None
    except ChatRequestInProgressError:
        raise HTTPException(
            status_code=409, detail={"code": "request_in_progress"}, headers={"Retry-After": "1"},
        ) from None
    except ChatRequestError:
        raise HTTPException(status_code=503, detail="AI service is unavailable") from None
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


@router.post("/workout-proposals/{proposal_id}/confirm", response_model=ProposalActionResponse)
def confirm_workout_proposal(
    proposal_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    payload: Annotated[ProposalActionRequest, Body()] = ProposalActionRequest(),
) -> ProposalActionResponse:
    return _apply_proposal_decision(
        proposal_id, "confirm", session=session, user_id=user_id,
    )


@router.post("/workout-proposals/{proposal_id}/reject", response_model=ProposalActionResponse)
def reject_workout_proposal(
    proposal_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    payload: Annotated[ProposalActionRequest, Body()] = ProposalActionRequest(),
) -> ProposalActionResponse:
    return _apply_proposal_decision(
        proposal_id, "reject", session=session, user_id=user_id,
    )


@router.get("/exercises", response_model=list[ExerciseRead])
def list_exercises(session: Annotated[Session, Depends(get_session)]) -> list[Exercise]:
    return list(session.scalars(select(Exercise).order_by(Exercise.id)))


@router.get("/exercises/{exercise_id}", response_model=ExerciseRead)
def get_exercise(
    exercise_id: Annotated[int, Path(gt=0, le=2147483647)],
    session: Annotated[Session, Depends(get_session)],
) -> Exercise:
    exercise = session.get(Exercise, exercise_id)
    if exercise is None:
        raise HTTPException(status_code=404, detail="Exercise not found")
    return exercise


@router.post("/exercises", response_model=ExerciseRead, status_code=201)
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


@router.post("/workouts", response_model=WorkoutRead, status_code=201)
def create_workout(
    payload: WorkoutCreate,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
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


@router.post("/workouts/{workout_id}/sets", response_model=WorkoutSetRead, status_code=201)
def create_workout_set(
    workout_id: Annotated[int, Path(gt=0, le=2147483647)],
    payload: WorkoutSetCreate,
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
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


@router.get("/workouts/{workout_id}", response_model=WorkoutDetailRead)
def get_workout(
    workout_id: Annotated[int, Path(gt=0, le=2147483647)],
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
) -> Workout:
    result = session.scalar(
        select(Workout)
        .where(Workout.id == workout_id, Workout.user_id == user_id)
        .options(selectinload(Workout.sets))
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    return result


@router.get("/workouts", response_model=list[WorkoutRead])
def list_workouts(
    session: Annotated[Session, Depends(get_session)],
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=2147483647)] = 0,
) -> list[Workout]:
    return get_workout_history(session, user_id=user_id, limit=limit, offset=offset)


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logging.getLogger("gym.application").info("application_started")
        try:
            yield
        finally:
            logging.getLogger("gym.application").info("application_stopped")

    production = settings.environment == "production"
    application = FastAPI(
        title="AI Gym Coach", lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    application.state.settings = settings
    application.state.chat_rate_limit = (deque(), Lock())
    application.include_router(public_routes)
    application.include_router(router)
    application.add_middleware(RequestBodyLimit, max_bytes=settings.max_request_bytes)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    application.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
    )
    application.add_middleware(RequestLogging, production=production)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        issues = []
        for issue in error.errors()[:5]:
            location = issue["loc"][:-1] if issue["type"] == "extra_forbidden" else issue["loc"]
            message = "Invalid request value"
            if issue["msg"] == "Value error, Name must not contain NUL characters":
                message = issue["msg"]
            issues.append({"loc": list(location), "type": issue["type"], "msg": message})
        return JSONResponse({"detail": issues}, status_code=422)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
        logging.getLogger("gym.application").error("database_error", extra={
            "request_id": getattr(request.state, "request_id", None), "error_type": type(error).__name__,
        })
        return JSONResponse({"detail": "Database service is unavailable"}, status_code=503)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        logging.getLogger("gym.application").error("application_error", extra={
            "request_id": request_id, "error_type": type(error).__name__,
        })
        return JSONResponse({"detail": "Internal server error"}, status_code=500, headers={
            "X-Request-ID": request_id, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    return application


app = create_app()
