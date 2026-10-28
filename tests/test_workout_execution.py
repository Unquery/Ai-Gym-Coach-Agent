from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import database
from app.main import app, get_demo_user_id
from app.models import Exercise, Workout, WorkoutProposalRecord, WorkoutSet
from app.proposal_store import (
    ProposalClosedError,
    ProposalNotFoundError,
    create_workout_proposal,
    decide_workout_proposal,
    get_workout_proposal,
)
from app.tool_dispatch import execute_read_only_tool
from app.tool_schemas import get_read_only_tool_schemas
from app.workout_execution import (
    ProposalCatalogChangedError,
    ProposalNotConfirmedError,
    execute_confirmed_workout,
)
from app.workout_proposals import ProposedWorkoutSet, WorkoutLoggingProposal

pytestmark = pytest.mark.postgres


@pytest.fixture
def pending_proposal(postgres_engine: Engine) -> tuple[UUID, UUID]:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        pull_up = Exercise(name="Synthetic pull-up")
        squat = Exercise(name="Synthetic squat")
        session.add_all([pull_up, squat])
        session.flush()
        proposal = WorkoutLoggingProposal(
            status="awaiting_confirmation",
            issues=[],
            sets=[
                ProposedWorkoutSet(
                    exercise_name=exercise.name,
                    catalog_name=exercise.name,
                    exercise_id=exercise.id,
                    set_number=number,
                    reps=reps,
                    weight_kg=Decimal(weight),
                )
                for exercise, number, reps, weight in [
                    (pull_up, 1, 6, "0.00"),
                    (squat, 1, 8, "20.25"),
                    (pull_up, 2, 5, "2.50"),
                ]
            ],
        )
        stored = create_workout_proposal(proposal, session=session, user_id=owner)
    return stored.id, owner


@pytest.fixture
def confirmed_proposal(
    postgres_engine: Engine,
    pending_proposal: tuple[UUID, UUID],
) -> tuple[UUID, UUID]:
    proposal_id, owner = pending_proposal
    with Session(postgres_engine) as session, session.begin():
        decide_workout_proposal(proposal_id, "confirm", session=session, user_id=owner)
    return pending_proposal


def test_execution_preserves_snapshot_and_replays_without_duplicate_writes(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session, session.begin():
        before = get_workout_proposal(proposal_id, session=session, user_id=owner)
        result = execute_confirmed_workout(proposal_id, session=session, user_id=owner)
        assert result.user_id == owner and result.plan_id is None
        assert result.started_at.utcoffset() is not None
        assert [
            item.model_dump(include={"exercise_id", "set_number", "reps", "weight_kg"})
            for item in result.sets
        ] == [
            item.model_dump(include={"exercise_id", "set_number", "reps", "weight_kg"})
            for item in before.payload.sets
        ]
        assert [item.weight_kg for item in result.sets] == [
            Decimal("0.00"),
            Decimal("20.25"),
            Decimal("2.50"),
        ]

    with Session(postgres_engine) as session, session.begin():
        stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
        assert stored.workout_id == result.id and stored.state == "confirmed"
        assert stored.payload == before.payload
        assert stored.resolved_at == before.resolved_at
        workout = session.get(Workout, result.id)
        assert workout is not None
        assert [item.position for item in workout.sets] == [1, 2, 3]
        assert [item.id for item in workout.sets] == [item.id for item in result.sets]
        # Replays read the saved result even if the catalog was subsequently renamed.
        session.execute(update(Exercise).values(name=Exercise.name + " renamed"))
        assert (
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
            == result
        )
        with pytest.raises(ProposalClosedError):
            decide_workout_proposal(
                proposal_id, "confirm", session=session, user_id=owner
            )
        assert session.scalar(select(func.count()).select_from(Workout)) == 1
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3

    # Existing detail reads must also retain the global interleaved order.
    monkeypatch.setattr(database, "get_engine", lambda: postgres_engine)
    app.dependency_overrides[get_demo_user_id] = lambda: owner
    try:
        with TestClient(app) as client:
            response = client.get(f"/workouts/{result.id}")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["sets"]] == [
            item.id for item in result.sets
        ]
    finally:
        app.dependency_overrides.pop(get_demo_user_id, None)


def test_database_failure_on_second_set_rolls_back_workout_and_all_sets_then_allows_retry(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = confirmed_proposal
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE workout_sets ADD CONSTRAINT test_reject_second_set CHECK (reps <> 8)"
            )
        )
    with pytest.raises(IntegrityError) as error:
        with Session(postgres_engine) as session, session.begin():
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    assert error.value.orig.diag.constraint_name == "test_reject_second_set"
    with Session(postgres_engine) as session, session.begin():
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
        stored = get_workout_proposal(proposal_id, session=session, user_id=owner)
        assert stored.state == "confirmed" and stored.workout_id is None
        session.execute(
            text("ALTER TABLE workout_sets DROP CONSTRAINT test_reject_second_set")
        )
        saved = execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    with Session(postgres_engine) as session, session.begin():
        assert (
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
            == saved
        )
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3


@pytest.mark.parametrize("already_executed", [False, True])
def test_foreign_and_unknown_ids_cannot_execute_or_disclose_saved_results(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
    already_executed: bool,
) -> None:
    proposal_id, owner = confirmed_proposal
    if already_executed:
        with Session(postgres_engine) as session, session.begin():
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    for target in (proposal_id, uuid4()):
        with pytest.raises(ProposalNotFoundError, match="^Proposal not found$"):
            with Session(postgres_engine) as session, session.begin():
                execute_confirmed_workout(target, session=session, user_id=uuid4())
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(Workout)) == int(
            already_executed
        )
        assert (
            session.scalar(select(func.count()).select_from(WorkoutSet))
            == 3 * already_executed
        )


@pytest.mark.parametrize("state", ["pending", "rejected", "expired"])
def test_only_stored_human_confirmation_authorizes_execution(
    postgres_engine: Engine,
    pending_proposal: tuple[UUID, UUID],
    state: str,
) -> None:
    proposal_id, owner = pending_proposal
    with Session(postgres_engine) as session, session.begin():
        if state == "rejected":
            decide_workout_proposal(
                proposal_id, "reject", session=session, user_id=owner
            )
        elif state == "expired":
            now = session.scalar(select(func.clock_timestamp()))
            assert now is not None
            session.execute(
                update(WorkoutProposalRecord).values(
                    created_at=now - timedelta(minutes=20),
                    expires_at=now - timedelta(minutes=5),
                )
            )
            assert (
                decide_workout_proposal(
                    proposal_id, "confirm", session=session, user_id=owner
                ).state
                == "expired"
            )
    with pytest.raises(ProposalNotConfirmedError):
        with Session(postgres_engine) as session, session.begin():
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


@pytest.mark.parametrize("change", ["delete", "rename", "invalid_payload"])
def test_execution_revalidates_stored_snapshot_and_catalog(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
    change: str,
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session, session.begin():
        if change == "delete":
            session.execute(delete(Exercise))
        elif change == "rename":
            session.execute(update(Exercise).values(name=Exercise.name + " renamed"))
        else:
            session.execute(update(WorkoutProposalRecord).values(payload={"sets": []}))
    expected = (
        ValueError if change == "invalid_payload" else ProposalCatalogChangedError
    )
    with pytest.raises(expected):
        with Session(postgres_engine) as session, session.begin():
            execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0


def test_concurrent_execution_returns_the_same_single_workout(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = confirmed_proposal
    barrier = Barrier(2)

    def execute() -> int:
        with Session(postgres_engine) as session, session.begin():
            session.execute(select(1))
            barrier.wait(timeout=5)
            return execute_confirmed_workout(
                proposal_id, session=session, user_id=owner
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(Workout)) == 1
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 3


def test_expiration_limits_confirmation_time_not_execution_of_existing_consent(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session, session.begin():
        now = session.scalar(select(func.clock_timestamp()))
        assert now is not None
        session.execute(
            update(WorkoutProposalRecord).values(
                created_at=now - timedelta(minutes=20),
                expires_at=now - timedelta(minutes=5),
                resolved_at=now - timedelta(minutes=10),
            )
        )
    with Session(postgres_engine) as session, session.begin():
        result = execute_confirmed_workout(proposal_id, session=session, user_id=owner)
        assert len(result.sets) == 3


def test_executor_does_not_commit_or_flush_unrelated_orm_changes(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="Unrelated pending exercise")
        session.add(unrelated)
        execute_confirmed_workout(proposal_id, session=session, user_id=owner)
        assert unrelated in session.new and unrelated.id is None
        assert session.in_transaction()
        session.rollback()
    with Session(postgres_engine) as session, session.begin():
        assert session.scalar(select(func.count()).select_from(Workout)) == 0
        assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
        assert (
            get_workout_proposal(proposal_id, session=session, user_id=owner).workout_id
            is None
        )


@pytest.mark.parametrize(
    "problem", ["owner", "duplicate_link", "unconfirmed", "delete_workout"]
)
def test_database_protects_execution_links(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
    problem: str,
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session, session.begin():
        result = execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    with pytest.raises(IntegrityError):
        with Session(postgres_engine) as session, session.begin():
            if problem == "owner":
                session.execute(update(WorkoutProposalRecord).values(user_id=uuid4()))
            elif problem == "duplicate_link":
                stored = get_workout_proposal(
                    proposal_id, session=session, user_id=owner
                )
                table = WorkoutProposalRecord.__table__
                record = session.execute(select(table)).mappings().one()
                session.execute(
                    table.insert().values(
                        **{
                            **record,
                            "id": uuid4(),
                            "workout_id": stored.workout_id,
                        }
                    )
                )
            elif problem == "unconfirmed":
                session.execute(
                    update(WorkoutProposalRecord).values(
                        state="pending", resolved_at=None
                    )
                )
            else:
                session.execute(delete(Workout).where(Workout.id == result.id))


@pytest.mark.parametrize("position", [0, 1])
def test_database_rejects_nonpositive_or_duplicate_global_positions(
    postgres_engine: Engine,
    confirmed_proposal: tuple[UUID, UUID],
    position: int,
) -> None:
    proposal_id, owner = confirmed_proposal
    with Session(postgres_engine) as session, session.begin():
        result = execute_confirmed_workout(proposal_id, session=session, user_id=owner)
    with pytest.raises(IntegrityError):
        with Session(postgres_engine) as session, session.begin():
            session.execute(
                update(WorkoutSet)
                .where(
                    WorkoutSet.id == result.sets[1].id,
                )
                .values(position=position)
            )


@pytest.mark.parametrize(
    "tool_name", ["execute_confirmed_workout", "build_workout_set_values"]
)
def test_executor_is_not_an_llm_tool(tool_name: str) -> None:
    assert tool_name not in {
        item["function"]["name"] for item in get_read_only_tool_schemas()
    }
    session = Mock(spec=Session)
    with pytest.raises(ValueError, match="Unknown read-only tool"):
        execute_read_only_tool(tool_name, "{}", session=session, user_id=uuid4())
    assert session.mock_calls == []
