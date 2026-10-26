from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Workout, WorkoutProposalRecord, WorkoutSet
from app.proposal_store import (
    PROPOSAL_TTL,
    ProposalClosedError,
    ProposalNotFoundError,
    create_workout_proposal,
    decide_workout_proposal,
    get_workout_proposal,
    next_proposal_state,
)
from app.tool_dispatch import execute_read_only_tool
from app.tool_schemas import get_read_only_tool_schemas
from app.workout_proposals import ProposedWorkoutSet, WorkoutLoggingProposal, WorkoutProposalIssue


@pytest.fixture
def ready_proposal() -> WorkoutLoggingProposal:
    return WorkoutLoggingProposal(
        status="awaiting_confirmation",
        sets=[
            ProposedWorkoutSet(
                exercise_name=name, catalog_name=name, exercise_id=exercise_id,
                set_number=number, reps=reps, weight_kg=Decimal(weight),
            )
            for exercise_id, name, number, reps, weight in [
                (7, "Synthetic pull-up", 1, 6, "0.00"),
                (11, "Synthetic squat", 1, 8, "20.25"),
                (7, "Synthetic pull-up", 2, 5, "0.00"),
            ]
        ],
        issues=[],
    )


@pytest.mark.parametrize(
    "state, decision, seconds_left, expected",
    [
        ("pending", "confirm", 60, "confirmed"),
        ("pending", "reject", 60, "rejected"),
        ("pending", "confirm", 0, "expired"),
        ("pending", "reject", -1, "expired"),
    ],
)
def test_state_transition_respects_decision_and_expiration_boundary(
    state: str, decision: str, seconds_left: int, expected: str,
) -> None:
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert next_proposal_state(
        state, decision, now=now, expires_at=now + timedelta(seconds=seconds_left)
    ) == expected


@pytest.mark.parametrize(
    "state, decision",
    [("confirmed", "confirm"), ("rejected", "confirm"), ("expired", "reject"),
     ("pending", "cancel"), ("unknown", "confirm")],
)
def test_terminal_states_and_unknown_actions_cannot_transition(
    state: str, decision: str,
) -> None:
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_proposal_state(state, decision, now=now, expires_at=now)


@pytest.mark.parametrize(
    "problem", ["clarification", "issues", "empty", "reps", "weight", "id", "order", "flag"]
)
def test_store_revalidates_payload_before_database_access(
    ready_proposal: WorkoutLoggingProposal, problem: str,
) -> None:
    if problem == "clarification":
        ready_proposal.status = "needs_clarification"
    elif problem == "issues":
        ready_proposal.issues = [WorkoutProposalIssue(code="missing_weight", set_index=0)]
    elif problem == "empty":
        ready_proposal.sets = []
    elif problem == "reps":
        ready_proposal.sets[0].reps = None
    elif problem == "weight":
        ready_proposal.sets[0].weight_kg = None
    elif problem == "id":
        ready_proposal.sets[0].exercise_id = 0
    elif problem == "order":
        ready_proposal.sets[0].set_number = 2
    else:
        ready_proposal.requires_confirmation = False
    session = Mock(spec=Session)
    with pytest.raises(ValueError):
        create_workout_proposal(ready_proposal, session=session, user_id=uuid4())
    assert session.mock_calls == []


def test_lifecycle_operations_are_not_available_to_the_llm() -> None:
    names = {tool["function"]["name"] for tool in get_read_only_tool_schemas()}
    session = Mock(spec=Session)
    for name in ("create_workout_proposal", "decide_workout_proposal", "confirm_workout_proposal"):
        assert name not in names
        with pytest.raises(ValueError, match="Unknown read-only tool"):
            execute_read_only_tool(name, "{}", session=session, user_id=uuid4())
    assert session.mock_calls == []


@pytest.fixture
def stored_proposal(
    postgres_engine: Engine, ready_proposal: WorkoutLoggingProposal,
) -> tuple[UUID, UUID]:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        result = create_workout_proposal(ready_proposal, session=session, user_id=owner)
    return result.id, owner


@pytest.mark.postgres
def test_snapshot_and_random_id_survive_new_sessions_without_workout_writes(
    postgres_engine: Engine, ready_proposal: WorkoutLoggingProposal,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        first = create_workout_proposal(ready_proposal, session=session, user_id=owner)
        second = create_workout_proposal(ready_proposal, session=session, user_id=owner)
    assert first.id.version == second.id.version == 4
    assert first.id != second.id
    assert first.user_id == owner
    assert first.state == "pending" and first.resolved_at is None
    assert first.expires_at - first.created_at == PROPOSAL_TTL
    snapshot = first.payload.model_dump(mode="json")
    assert [item["exercise_id"] for item in snapshot["sets"]] == [7, 11, 7]
    assert [item["set_number"] for item in snapshot["sets"]] == [1, 1, 2]
    assert snapshot["sets"][1]["weight_kg"] == "20.25"
    ready_proposal.sets[0].reps = 99

    with Session(postgres_engine) as session, session.begin():
        found = get_workout_proposal(first.id, session=session, user_id=owner)
        assert found.payload.model_dump(mode="json") == snapshot
        assert found == first
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["read", "confirm", "reject"])
def test_foreign_and_unknown_proposal_ids_are_indistinguishable(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID], action: str,
) -> None:
    proposal_id, owner = stored_proposal
    for target in (proposal_id, uuid4()):
        with Session(postgres_engine) as session, session.begin():
            with pytest.raises(ProposalNotFoundError, match="^Proposal not found$"):
                if action == "read":
                    get_workout_proposal(target, session=session, user_id=uuid4())
                else:
                    decide_workout_proposal(target, action, session=session, user_id=uuid4())
    with Session(postgres_engine) as session, session.begin():
        assert get_workout_proposal(proposal_id, session=session, user_id=owner).state == "pending"


@pytest.mark.postgres
@pytest.mark.parametrize("decision, expected", [("confirm", "confirmed"), ("reject", "rejected")])
def test_explicit_decision_is_terminal_and_replay_cannot_change_it(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID], decision: str, expected: str,
) -> None:
    proposal_id, owner = stored_proposal
    with Session(postgres_engine) as session, session.begin():
        original = get_workout_proposal(proposal_id, session=session, user_id=owner)
        decided = decide_workout_proposal(proposal_id, decision, session=session, user_id=owner)
        assert decided.state == expected and decided.resolved_at is not None
        assert original.created_at <= decided.resolved_at < original.expires_at
        assert decided.payload == original.payload
    for repeat in ("confirm", "reject"):
        with Session(postgres_engine) as session, session.begin():
            with pytest.raises(ProposalClosedError):
                decide_workout_proposal(proposal_id, repeat, session=session, user_id=owner)
    with Session(postgres_engine) as session, session.begin():
        assert get_workout_proposal(proposal_id, session=session, user_id=owner) == decided


@pytest.mark.postgres
@pytest.mark.parametrize("action", ["read", "confirm", "reject"])
def test_expired_pending_proposal_is_closed_without_confirming_or_rejecting(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID], action: str,
) -> None:
    proposal_id, owner = stored_proposal
    table = WorkoutProposalRecord.__table__
    with Session(postgres_engine) as session, session.begin():
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        session.execute(update(table).where(table.c.id == proposal_id).values(
            created_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=5)
        ))
    with Session(postgres_engine) as session, session.begin():
        if action == "read":
            result = get_workout_proposal(proposal_id, session=session, user_id=owner)
        else:
            result = decide_workout_proposal(proposal_id, action, session=session, user_id=owner)
        assert result.state == "expired"
        assert result.resolved_at >= result.expires_at
    with Session(postgres_engine) as session, session.begin():
        assert get_workout_proposal(proposal_id, session=session, user_id=owner) == result
        with pytest.raises(ProposalClosedError):
            decide_workout_proposal(proposal_id, "confirm", session=session, user_id=owner)


@pytest.mark.postgres
def test_two_sessions_cannot_both_confirm_the_same_proposal(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = stored_proposal
    barrier = Barrier(2)

    def confirm() -> str:
        with Session(postgres_engine) as session, session.begin():
            session.execute(select(1))  # Connect both workers before the race.
            barrier.wait(timeout=5)
            try:
                return decide_workout_proposal(
                    proposal_id, "confirm", session=session, user_id=owner
                ).state
            except ProposalClosedError:
                return "closed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == ["closed", "confirmed"]
    with Session(postgres_engine) as session, session.begin():
        assert get_workout_proposal(proposal_id, session=session, user_id=owner).state == "confirmed"


@pytest.mark.postgres
def test_expiration_uses_database_wall_clock_after_waiting_for_a_row_lock(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = stored_proposal
    started = Event()
    times: dict[str, datetime] = {}
    table = WorkoutProposalRecord.__table__

    def confirm() -> str:
        with Session(postgres_engine) as session, session.begin():
            times["transaction_start"] = session.execute(select(func.now())).scalar_one()
            started.set()
            return decide_workout_proposal(
                proposal_id, "confirm", session=session, user_id=owner
            ).state

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(postgres_engine) as holder, holder.begin():
            holder.execute(select(table).where(table.c.id == proposal_id).with_for_update())
            future = pool.submit(confirm)
            assert started.wait(timeout=5)
            holder.execute(select(func.pg_sleep(0.02)))
            # The deadline is after the waiting transaction began, but before
            # it can acquire this lock. Transaction-start now() would be stale.
            instant = times["transaction_start"]
            holder.execute(update(table).where(table.c.id == proposal_id).values(
                created_at=instant - PROPOSAL_TTL,
                expires_at=instant + timedelta(microseconds=1),
            ))
        assert future.result(timeout=10) == "expired"


@pytest.mark.postgres
def test_store_never_flushes_unrelated_workouts_or_commits_the_callers_transaction(
    postgres_engine: Engine, ready_proposal: WorkoutLoggingProposal,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session:
        pending = Workout(user_id=owner, sets=[WorkoutSet(
            exercise_id=7, set_number=1, reps=6, weight_kg=Decimal(0)
        )])
        session.add(pending)
        result = create_workout_proposal(ready_proposal, session=session, user_id=owner)
        result = decide_workout_proposal(result.id, "confirm", session=session, user_id=owner)
        assert get_workout_proposal(result.id, session=session, user_id=owner) == result
        assert pending in session.new and pending.id is None
        assert pending.sets[0] in session.new and pending.sets[0].id is None
        assert session.in_transaction()
        with session.no_autoflush:
            assert session.scalar(select(func.count()).select_from(Workout)) == 0
            assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
        session.rollback()
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkoutProposalRecord)) == 0


@pytest.mark.postgres
@pytest.mark.parametrize("problem", ["state", "expiration", "resolution", "payload"])
def test_database_constraints_reject_invalid_proposal_records(
    postgres_engine: Engine, stored_proposal: tuple[UUID, UUID], problem: str,
) -> None:
    proposal_id, _ = stored_proposal
    table = WorkoutProposalRecord.__table__
    with Session(postgres_engine) as session:
        record = session.execute(select(table).where(table.c.id == proposal_id)).mappings().one()
        values = {
            "state": {"state": "unknown"},
            "expiration": {"expires_at": record["created_at"]},
            "resolution": {"resolved_at": record["created_at"]},
            "payload": {"payload": []},
        }[problem]
        with pytest.raises(IntegrityError):
            session.execute(update(table).where(table.c.id == proposal_id).values(**values))
        session.rollback()
