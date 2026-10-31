"""Typed agent tools; identity and database sessions come from server code."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Exercise
from app.workouts.progress import ExerciseWorkoutProgress, analyze_exercise_progress
from app.workouts.proposal_store import create_workout_proposal
from app.workouts.queries import get_exercise_workouts, get_workout_history
from app.workouts.schemas import WorkoutDetailRead, WorkoutRead
from app.workouts.drafts import (
    PersistentWorkoutDraftSet, StoredWorkoutDraft, WorkoutDraftConflictError, WorkoutDraftExerciseNotFoundError,
    WorkoutDraftInactiveError, WorkoutDraftNotFoundError, append_workout_draft_set,
    WorkoutDraftSetNotFoundError, get_workout_draft, lock_active_workout_draft, remove_workout_draft_set,
    update_workout_draft_set,
)
from app.agent.tool_schemas import (
    DraftSetTargetInput, GetExerciseProgressInput, GetRecentWorkoutsInput, UpdateDraftSetInput,
)
from app.workouts.proposals import (
    WorkoutLoggingDraft, WorkoutLoggingProposal, WorkoutProposalIssue, WorkoutSetDraft,
    resolve_workout_logging,
)


class ExerciseProgressResult(BaseModel):
    exercise_id: int
    status: Literal[
        "exercise_not_found", "no_history", "insufficient_history", "ok"
    ]
    sessions: list[ExerciseWorkoutProgress]


class WorkoutProposalToolResult(WorkoutLoggingProposal):
    proposal_id: UUID | None = None
    expires_at: AwareDatetime | None = None


@dataclass(frozen=True)
class DraftMutationContext:
    """Trusted backend context, never model-selected tool arguments.

    The caller owns the conversation and durable request execution locks, and
    commits the draft change together with its completed HTTP response.
    """

    conversation_id: UUID
    request_id: UUID
    snapshot: StoredWorkoutDraft


class DraftMutationToolResult(BaseModel):
    status: Literal["appended", "updated", "removed", "needs_clarification", "draft_unavailable", "conflict"]
    appended_set: PersistentWorkoutDraftSet | None = None
    updated_set: PersistentWorkoutDraftSet | None = None
    removed_set_id: UUID | None = None
    target_issue: Literal["missing_or_ambiguous_set"] | None = None
    draft_version: int | None = None
    issues: list[WorkoutProposalIssue] = Field(default_factory=list)


def append_set_to_active_draft(
    arguments: WorkoutSetDraft, *, context: DraftMutationContext,
    session: Session, user_id: UUID,
) -> DraftMutationToolResult:
    """One append slot per server request; no model-generated idempotency keys."""
    try:
        draft = get_workout_draft(context.conversation_id, session=session, user_id=user_id)
        if draft.state != "active":
            return DraftMutationToolResult(status="draft_unavailable")
        resolved = resolve_workout_logging(WorkoutLoggingDraft(sets=[arguments]), session=session)
        # Drafts can retain unknown weight, but not a guessed exercise or rep count.
        issues = [issue for issue in resolved.issues if issue.code != "missing_weight"]
        if issues:
            return DraftMutationToolResult(status="needs_clarification", issues=issues)
        candidate = resolved.sets[0]
        with session.no_autoflush:
            name = session.scalar(select(Exercise.name).where(
                Exercise.id == candidate.exercise_id,
            ).with_for_update(read=True))
        if name is None or name != candidate.catalog_name:
            return DraftMutationToolResult(status="needs_clarification", issues=[
                WorkoutProposalIssue(code="unknown_exercise", set_index=0),
            ])
        assert candidate.exercise_id is not None
        saved = append_workout_draft_set(
            context.conversation_id, exercise_id=candidate.exercise_id,
            reps=arguments.reps, weight_kg=arguments.weight_kg,
            idempotency_key=uuid5(context.request_id, "draft-append"),
            session=session, user_id=user_id,
        )
    except (WorkoutDraftNotFoundError, WorkoutDraftInactiveError):
        return DraftMutationToolResult(status="draft_unavailable")
    except WorkoutDraftExerciseNotFoundError:
        return DraftMutationToolResult(status="needs_clarification", issues=[
            WorkoutProposalIssue(code="unknown_exercise", set_index=0),
        ])
    except WorkoutDraftConflictError:
        return DraftMutationToolResult(status="conflict")
    # The execution transaction retains the draft lock; no other writer can
    # change its final set between this append and a matching retry in this turn.
    return DraftMutationToolResult(
        status="appended", appended_set=saved.payload.sets[-1], draft_version=saved.version,
    )


def change_set_in_active_draft(
    arguments: DraftSetTargetInput, *, context: DraftMutationContext,
    session: Session, user_id: UUID,
) -> DraftMutationToolResult:
    """Use the observed snapshot's version; never refresh it to retry a correction."""
    if arguments.set_id is None or not any(
        item.id == arguments.set_id for item in context.snapshot.payload.sets
    ):
        return DraftMutationToolResult(
            status="needs_clarification", target_issue="missing_or_ambiguous_set",
        )
    try:
        if isinstance(arguments, UpdateDraftSetInput):
            saved = update_workout_draft_set(
                context.conversation_id, arguments.set_id, dict(arguments.changes),
                expected_version=context.snapshot.version, session=session, user_id=user_id,
            )
            return DraftMutationToolResult(
                status="updated", draft_version=saved.version,
                updated_set=next(item for item in saved.payload.sets if item.id == arguments.set_id),
            )
        saved = remove_workout_draft_set(
            context.conversation_id, arguments.set_id,
            expected_version=context.snapshot.version, session=session, user_id=user_id,
        )
        return DraftMutationToolResult(
            status="removed", draft_version=saved.version, removed_set_id=arguments.set_id,
        )
    except (WorkoutDraftNotFoundError, WorkoutDraftInactiveError):
        return DraftMutationToolResult(status="draft_unavailable")
    except WorkoutDraftSetNotFoundError:
        return DraftMutationToolResult(
            status="needs_clarification", target_issue="missing_or_ambiguous_set",
        )
    except WorkoutDraftConflictError:
        return DraftMutationToolResult(status="conflict")


def create_workout_logging_proposal(
    arguments: WorkoutLoggingDraft, *, session: Session, user_id: UUID,
) -> WorkoutProposalToolResult:
    """Resolve a draft and store complete pending consent; the caller must commit.

    No workout/set writes, human decisions, LLM requests, or transaction management.
    Incomplete/ambiguous drafts return clarification with no stored proposal ID.
    """
    proposal = resolve_workout_logging(arguments, session=session)
    if proposal.status == "needs_clarification":
        return WorkoutProposalToolResult(**proposal.model_dump())
    stored = create_workout_proposal(proposal, session=session, user_id=user_id)
    return WorkoutProposalToolResult(
        **proposal.model_dump(), proposal_id=stored.id, expires_at=stored.expires_at,
    )


def propose_active_workout_draft(
    *, context: DraftMutationContext, session: Session, user_id: UUID,
) -> WorkoutProposalToolResult | DraftMutationToolResult:
    """Freeze a validated saved snapshot as pending consent in the chat transaction."""
    with session.no_autoflush:
        try:
            draft = lock_active_workout_draft(session, context.conversation_id, user_id)
        except (WorkoutDraftNotFoundError, WorkoutDraftInactiveError):
            return DraftMutationToolResult(status="draft_unavailable")
        if draft.version != context.snapshot.version:
            return DraftMutationToolResult(status="conflict")
        if not draft.payload.sets:
            return WorkoutProposalToolResult(
                status="needs_clarification", sets=[],
                issues=[WorkoutProposalIssue(code="empty_draft")],
            )
        arguments = WorkoutLoggingDraft(sets=[
            WorkoutSetDraft.model_validate(item.model_dump(include={"exercise_name", "reps", "weight_kg"}))
            for item in draft.payload.sets
        ])
        proposal = resolve_workout_logging(arguments, session=session)
        exercise_ids = sorted({item.exercise_id for item in proposal.sets if item.exercise_id is not None})
        catalog = dict(session.execute(
            select(Exercise.id, Exercise.name).where(Exercise.id.in_(exercise_ids))
            .order_by(Exercise.id).with_for_update(read=True)
        ).all())
        for index, (source, candidate) in enumerate(zip(draft.payload.sets, proposal.sets, strict=True)):
            if candidate.exercise_id is not None and (
                catalog.get(candidate.exercise_id) != candidate.catalog_name
                or (source.exercise_id is not None and source.exercise_id != candidate.exercise_id)
            ):
                # A catalog rename/replacement must not silently rebind a saved set.
                candidate.exercise_id = candidate.catalog_name = candidate.set_number = None
                proposal.issues.append(WorkoutProposalIssue(code="unknown_exercise", set_index=index))
        if proposal.issues:
            proposal.status = "needs_clarification"
            return WorkoutProposalToolResult(**proposal.model_dump())
        stored = create_workout_proposal(proposal, session=session, user_id=user_id)
        return WorkoutProposalToolResult(
            **proposal.model_dump(), proposal_id=stored.id, expires_at=stored.expires_at,
        )


def get_recent_workouts(
    arguments: GetRecentWorkoutsInput, *, session: Session, user_id: UUID
) -> list[WorkoutRead]:
    """Return recent workouts for the server-selected owner without writing data."""
    workouts = get_workout_history(
        session, user_id=user_id, limit=arguments.limit, offset=arguments.offset
    )
    return [WorkoutRead.model_validate(workout) for workout in workouts]


def get_exercise_progress(
    arguments: GetExerciseProgressInput, *, session: Session, user_id: UUID
) -> ExerciseProgressResult:
    """Read one owner's exercise statistics without writing or calling an LLM."""
    with session.no_autoflush:
        exercise_id = session.scalar(
            select(Exercise.id).where(Exercise.id == arguments.exercise_id)
        )
        if exercise_id is None:
            return ExerciseProgressResult(
                exercise_id=arguments.exercise_id,
                status="exercise_not_found",
                sessions=[],
            )
        workouts = get_exercise_workouts(
            session,
            user_id=user_id,
            exercise_id=exercise_id,
            start_at=arguments.start_at,
            end_at=arguments.end_at,
        )
        progress = analyze_exercise_progress(
            exercise_id, [WorkoutDetailRead.model_validate(row) for row in workouts]
        )
        if not progress:
            return ExerciseProgressResult(
                exercise_id=exercise_id, status="no_history", sessions=[]
            )
        return ExerciseProgressResult(
            exercise_id=exercise_id,
            status="insufficient_history" if len(progress) == 1 else "ok",
            sessions=progress,
        )
