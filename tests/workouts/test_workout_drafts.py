"""Structured draft state, ownership, and compare-and-swap on isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.conversations.store import ConversationNotFoundError, create_conversation
from app.db.models import (
    Conversation,
    Exercise,
    Workout,
    WorkoutDraftAppendRecord,
    WorkoutDraftRecord,
    WorkoutProposalRecord,
    WorkoutSet,
)
from app.workouts.drafts import (
    PersistentWorkoutDraftSet,
    StoredWorkoutDraft,
    WorkoutDraftConflictError,
    WorkoutDraftExerciseNotFoundError,
    WorkoutDraftInactiveError,
    WorkoutDraftNotFoundError,
    WorkoutDraftPayload,
    append_workout_draft_set,
    create_workout_draft,
    get_workout_draft,
    replace_workout_draft,
)

pytestmark = pytest.mark.postgres


def snapshot() -> WorkoutDraftPayload:
    return WorkoutDraftPayload(
        sets=[
            PersistentWorkoutDraftSet(
                id=uuid4(), exercise_name=name, reps=reps, weight_kg=weight
            )
            for name, reps, weight in (
                ("Synthetic pull-up", 6, Decimal("0.00")),
                ("Synthetic squat", None, Decimal("20.25")),
                ("Synthetic pull-up", 5, None),
            )
        ]
    )


@pytest.fixture
def empty_draft(postgres_engine: Engine) -> tuple[UUID, UUID]:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
        create_workout_draft(conversation_id, session=session, user_id=owner)
    return conversation_id, owner


def test_draft_snapshot_survives_sessions_with_order_unknown_and_zero_weights(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
) -> None:
    conversation_id, owner = empty_draft
    payload = snapshot()
    with Session(postgres_engine) as session, session.begin():
        initial = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert initial.version == 1 and initial.payload.sets == []
        saved = replace_workout_draft(
            conversation_id,
            payload,
            expected_version=1,
            session=session,
            user_id=owner,
        )
        assert saved.version == 2 and saved.user_id == owner
        assert (
            saved.created_at == initial.created_at
            and saved.updated_at >= saved.created_at
        )
        assert saved.created_at.utcoffset() is not None
    with Session(postgres_engine) as session:
        loaded = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert loaded == saved
        assert [item.id for item in loaded.payload.sets] == [
            item.id for item in payload.sets
        ]
        assert [item.exercise_name for item in loaded.payload.sets] == [
            "Synthetic pull-up",
            "Synthetic squat",
            "Synthetic pull-up",
        ]
        assert loaded.payload.sets[0].weight_kg == Decimal(0)
        assert loaded.payload.sets[1].reps is None
        assert loaded.payload.sets[2].weight_kg is None
        for model in (Workout, WorkoutSet, WorkoutProposalRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_repeated_creation_and_stale_update_do_not_reset_or_duplicate_sets(
    session: Session,
) -> None:
    owner = uuid4()
    conversation_id = create_conversation(session=session, user_id=owner).id
    create_workout_draft(conversation_id, session=session, user_id=owner)
    payload = snapshot()
    saved = replace_workout_draft(
        conversation_id,
        payload,
        expected_version=1,
        session=session,
        user_id=owner,
    )
    session.commit()
    assert (
        create_workout_draft(conversation_id, session=session, user_id=owner) == saved
    )
    with pytest.raises(WorkoutDraftConflictError):
        replace_workout_draft(
            conversation_id,
            payload,
            expected_version=1,
            session=session,
            user_id=owner,
        )
    assert get_workout_draft(conversation_id, session=session, user_id=owner) == saved
    assert len(saved.payload.sets) == 3


def test_reordering_and_corrections_keep_set_ids_and_identical_sets_can_be_distinct(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
) -> None:
    conversation_id, owner = empty_draft
    first_id, second_id = uuid4(), uuid4()
    payload = WorkoutDraftPayload(
        sets=[
            PersistentWorkoutDraftSet(
                id=item_id, exercise_name="Synthetic squat", reps=8, weight_kg=None
            )
            for item_id in (first_id, second_id)
        ]
    )
    with Session(postgres_engine) as session, session.begin():
        replace_workout_draft(
            conversation_id, payload, expected_version=1, session=session, user_id=owner
        )
        payload.sets.reverse()
        payload.sets[0].weight_kg = Decimal("0.00")
        updated = replace_workout_draft(
            conversation_id,
            payload,
            expected_version=2,
            session=session,
            user_id=owner,
        )
        assert [item.id for item in updated.payload.sets] == [second_id, first_id]
        assert updated.payload.sets[0].weight_kg == Decimal(0)
        assert updated.payload.sets[1].weight_kg is None
        assert updated.version == 3


def test_foreign_or_missing_drafts_cannot_be_read_or_modified(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
) -> None:
    conversation_id, owner = empty_draft
    with Session(postgres_engine) as session:
        original = get_workout_draft(conversation_id, session=session, user_id=owner)
        for target, reader in ((conversation_id, uuid4()), (uuid4(), owner)):
            with pytest.raises(ConversationNotFoundError):
                create_workout_draft(target, session=session, user_id=reader)
            with pytest.raises(WorkoutDraftNotFoundError):
                get_workout_draft(target, session=session, user_id=reader)
            with pytest.raises(WorkoutDraftNotFoundError):
                replace_workout_draft(
                    target,
                    snapshot(),
                    expected_version=1,
                    session=session,
                    user_id=reader,
                )
        own_without_draft = create_conversation(session=session, user_id=owner).id
        with pytest.raises(WorkoutDraftNotFoundError):
            get_workout_draft(own_without_draft, session=session, user_id=owner)
        with pytest.raises(WorkoutDraftNotFoundError):
            replace_workout_draft(
                own_without_draft,
                snapshot(),
                expected_version=1,
                session=session,
                user_id=owner,
            )
        assert (
            get_workout_draft(conversation_id, session=session, user_id=owner)
            == original
        )
        session.rollback()


@pytest.mark.parametrize(
    "problem",
    ["duplicate_id", "blank_name", "reps", "bool_reps", "negative", "nan", "precision"],
)
def test_service_revalidates_modified_models_without_writing_partial_state(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    problem: str,
) -> None:
    conversation_id, owner = empty_draft
    payload = snapshot()
    if problem == "duplicate_id":
        payload.sets[1].id = payload.sets[0].id
    elif problem == "blank_name":
        payload.sets[0].exercise_name = " \t"
    elif problem in ("reps", "bool_reps"):
        payload.sets[0].reps = 0 if problem == "reps" else True
    else:
        payload.sets[0].weight_kg = {
            "negative": Decimal(-1),
            "nan": Decimal("NaN"),
            "precision": Decimal("1.001"),
        }[problem]
    with Session(postgres_engine) as session:
        with pytest.raises(ValidationError):
            replace_workout_draft(
                conversation_id,
                payload,
                expected_version=1,
                session=session,
                user_id=owner,
            )
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored.version == 1 and stored.payload.sets == []


@pytest.mark.parametrize("version", [0, -1, True])
def test_expected_version_must_be_a_positive_integer(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    version: Any,
) -> None:
    conversation_id, owner = empty_draft
    with Session(postgres_engine) as session:
        with pytest.raises(ValueError, match="positive integer"):
            replace_workout_draft(
                conversation_id,
                snapshot(),
                expected_version=version,
                session=session,
                user_id=owner,
            )
        assert (
            get_workout_draft(conversation_id, session=session, user_id=owner).version
            == 1
        )


@pytest.mark.parametrize(
    "values,sqlstate",
    [
        ({"version": 0}, "23514"),
        ({"version": None}, "23502"),
        ({"state": "pending"}, "23514"),
        ({"state": None}, "23502"),
        ({"payload": {}}, "23514"),
        ({"payload": {"sets": None}}, "23514"),
        ({"payload": {"sets": {}}}, "23514"),
        ({"payload": []}, "23514"),
        ({"updated_at": datetime(2000, 1, 1, tzinfo=UTC)}, "23514"),
    ],
)
def test_postgres_enforces_draft_constraints(
    session: Session, values: dict[str, Any], sqlstate: str
) -> None:
    owner = uuid4()
    conversation_id = create_conversation(session=session, user_id=owner).id
    session.commit()
    with pytest.raises(IntegrityError) as error:
        session.execute(
            insert(WorkoutDraftRecord).values(conversation_id=conversation_id, **values)
        )
    assert getattr(error.value.orig, "sqlstate", None) == sqlstate
    session.rollback()


def test_postgres_enforces_one_draft_per_existing_conversation(
    session: Session,
) -> None:
    owner = uuid4()
    conversation_id = create_conversation(session=session, user_id=owner).id
    create_workout_draft(conversation_id, session=session, user_id=owner)
    session.commit()
    for target, sqlstate in ((conversation_id, "23505"), (uuid4(), "23503")):
        with pytest.raises(IntegrityError) as error:
            session.execute(insert(WorkoutDraftRecord).values(conversation_id=target))
        assert getattr(error.value.orig, "sqlstate", None) == sqlstate
        session.rollback()


def test_services_do_not_commit_or_flush_unrelated_orm_changes(
    postgres_engine: Engine,
) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        conversation_id = create_conversation(session=session, user_id=owner).id
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        create_workout_draft(conversation_id, session=session, user_id=owner)
        saved = replace_workout_draft(
            conversation_id,
            snapshot(),
            expected_version=1,
            session=session,
            user_id=owner,
        )
        assert (
            get_workout_draft(conversation_id, session=session, user_id=owner) == saved
        )
        assert unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            with pytest.raises(WorkoutDraftNotFoundError):
                get_workout_draft(conversation_id, session=observer, user_id=owner)
        session.rollback()
    with Session(postgres_engine) as observer:
        with pytest.raises(WorkoutDraftNotFoundError):
            get_workout_draft(conversation_id, session=observer, user_id=owner)


def test_deleting_one_conversation_cascades_only_its_draft(session: Session) -> None:
    owner = uuid4()
    first = create_conversation(session=session, user_id=owner).id
    second = create_conversation(session=session, user_id=owner).id
    for conversation_id in (first, second):
        create_workout_draft(conversation_id, session=session, user_id=owner)
    session.commit()
    session.execute(delete(Conversation).where(Conversation.id == first))
    session.commit()
    with pytest.raises(WorkoutDraftNotFoundError):
        get_workout_draft(first, session=session, user_id=owner)
    assert get_workout_draft(second, session=session, user_id=owner).version == 1


def test_commit_failure_preserves_the_previous_snapshot(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
) -> None:
    conversation_id, owner = empty_draft
    with postgres_engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION reject_draft_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic commit failure'; END; $$
        """)
        )
        connection.execute(
            text("""
            CREATE CONSTRAINT TRIGGER reject_draft_commit AFTER UPDATE ON workout_drafts
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_draft_commit()
        """)
        )
    with Session(postgres_engine) as session:
        original = get_workout_draft(conversation_id, session=session, user_id=owner)
        session.rollback()
        with pytest.raises(SQLAlchemyError), session.begin():
            saved = replace_workout_draft(
                conversation_id,
                snapshot(),
                expected_version=1,
                session=session,
                user_id=owner,
            )
            assert saved.version == 2
    with Session(postgres_engine) as session:
        assert (
            get_workout_draft(conversation_id, session=session, user_id=owner)
            == original
        )


def test_new_conversation_and_draft_roll_back_together(postgres_engine: Engine) -> None:
    owner = uuid4()
    with Session(postgres_engine) as session:
        with pytest.raises(RuntimeError, match="Synthetic failure"):
            with session.begin():
                conversation_id = create_conversation(session=session, user_id=owner).id
                create_workout_draft(conversation_id, session=session, user_id=owner)
                replace_workout_draft(
                    conversation_id,
                    snapshot(),
                    expected_version=1,
                    session=session,
                    user_id=owner,
                )
                raise RuntimeError("Synthetic failure")
    with Session(postgres_engine) as session:
        assert session.get(Conversation, conversation_id) is None
        assert session.scalar(select(func.count()).select_from(WorkoutDraftRecord)) == 0


@pytest.mark.parametrize("operation", ["create", "replace"])
def test_concurrent_requests_do_not_duplicate_or_overwrite_sets(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    operation: str,
) -> None:
    conversation_id, owner = empty_draft
    base = snapshot()
    with Session(postgres_engine) as session, session.begin():
        if operation == "create":
            session.execute(delete(WorkoutDraftRecord))
        else:
            replace_workout_draft(
                conversation_id,
                base,
                expected_version=1,
                session=session,
                user_id=owner,
            )
    barrier = Barrier(2)
    candidates = [snapshot(), snapshot()]
    for candidate in candidates:
        candidate.sets = [*base.sets, *candidate.sets]

    def worker(index: int) -> StoredWorkoutDraft | None:
        with Session(postgres_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            if operation == "create":
                return create_workout_draft(
                    conversation_id, session=session, user_id=owner
                )
            try:
                return replace_workout_draft(
                    conversation_id,
                    candidates[index],
                    expected_version=2,
                    session=session,
                    user_id=owner,
                )
            except WorkoutDraftConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert session.scalar(select(func.count()).select_from(WorkoutDraftRecord)) == 1
        if operation == "create":
            assert results == [stored, stored] and stored.version == 1
        else:
            winners = [result for result in results if result is not None]
            assert len(winners) == 1 and winners[0] == stored and stored.version == 3
            assert [item.id for item in stored.payload.sets[:3]] == [
                item.id for item in base.sets
            ]
            assert len(stored.payload.sets) == 6
            assert stored.payload in candidates


def test_append_retry_after_commit_does_not_duplicate_set(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
) -> None:
    conversation_id, owner = empty_draft
    operation_key = uuid4()

    with Session(postgres_engine) as session, session.begin():
        exercise = Exercise(name="Synthetic retry squat")
        session.add(exercise)
        session.flush()
        exercise_id = exercise.id

    with Session(postgres_engine) as session, session.begin():
        first = append_workout_draft_set(
            conversation_id,
            exercise_id=exercise_id,
            reps=8,
            weight_kg=None,
            idempotency_key=operation_key,
            session=session,
            user_id=owner,
        )

    with Session(postgres_engine) as session, session.begin():
        second = append_workout_draft_set(
            conversation_id,
            exercise_id=exercise_id,
            reps=8,
            weight_kg=None,
            idempotency_key=operation_key,
            session=session,
            user_id=owner,
        )

    assert len(second.payload.sets) == 1
    assert second.payload.sets[0].id == first.payload.sets[0].id
    assert second.version == first.version == 2

    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored == second
        assert stored.payload.sets[0].reps == 8
        assert stored.payload.sets[0].weight_kg is None


@pytest.fixture
def catalog_exercises(postgres_engine: Engine) -> tuple[int, int]:
    with Session(postgres_engine) as session, session.begin():
        squat = Exercise(name="Synthetic catalog squat")
        pull_up = Exercise(name="Synthetic catalog pull-up")
        session.add_all([squat, pull_up])
        session.flush()
        return squat.id, pull_up.id


def test_appends_reuse_catalog_exercises_and_preserve_global_order(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    squat, pull_up = catalog_exercises
    inputs: list[tuple[int, int | None, Decimal | None]] = [
        (squat, 8, None), (pull_up, None, Decimal("20.25")),
        (squat, 6, Decimal("0.00")), (squat, 8, None),
    ]
    for exercise_id, reps, weight in inputs:
        with Session(postgres_engine) as session, session.begin():
            append_workout_draft_set(
                conversation_id, exercise_id=exercise_id, reps=reps, weight_kg=weight,
                idempotency_key=uuid4(), session=session, user_id=owner,
            )
    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored.state == "active" and stored.version == 5
        assert [(item.exercise_id, item.reps, item.weight_kg) for item in stored.payload.sets] == inputs
        assert [item.exercise_name for item in stored.payload.sets] == [
            "Synthetic catalog squat", "Synthetic catalog pull-up",
            "Synthetic catalog squat", "Synthetic catalog squat",
        ]
        assert len({item.id for item in stored.payload.sets}) == 4
        assert session.scalar(select(func.count()).select_from(Exercise)) == 2
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 4
        for model in (Workout, WorkoutSet, WorkoutProposalRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_append_preserves_legacy_sets_and_retry_survives_replacement_and_catalog_deletion(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    exercise_id = catalog_exercises[0]
    key = uuid4()
    legacy = snapshot()
    with Session(postgres_engine) as session, session.begin():
        replace_workout_draft(conversation_id, legacy, expected_version=1, session=session, user_id=owner)
        appended = append_workout_draft_set(
            conversation_id, exercise_id=exercise_id, reps=8, weight_kg=Decimal("20.0"),
            idempotency_key=key, session=session, user_id=owner,
        )
        assert appended.payload.sets[:-1] == legacy.sets
        # An edit can remove a set, but must not erase the operation's receipt.
        edited = replace_workout_draft(
            conversation_id, legacy, expected_version=3, session=session, user_id=owner,
        )
        session.execute(delete(Exercise).where(Exercise.id == exercise_id))
    with Session(postgres_engine) as session, session.begin():
        replay = append_workout_draft_set(
            conversation_id, exercise_id=exercise_id, reps=8, weight_kg=Decimal("20.00"),
            idempotency_key=key, session=session, user_id=owner,
        )
        assert replay == edited and replay.version == 4
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 1


@pytest.mark.parametrize("changed", ["exercise_id", "reps", "weight_kg"])
def test_reused_append_key_with_different_arguments_conflicts_without_writes(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
    changed: str,
) -> None:
    conversation_id, owner = empty_draft
    arguments: dict[str, Any] = dict(
        exercise_id=catalog_exercises[0], reps=8, weight_kg=None, idempotency_key=uuid4(),
    )
    with Session(postgres_engine) as session, session.begin():
        original = append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
    arguments[changed] = {"exercise_id": catalog_exercises[1], "reps": 7, "weight_kg": Decimal("0")}[changed]
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(WorkoutDraftConflictError, match="different set arguments"):
            append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
        assert get_workout_draft(conversation_id, session=session, user_id=owner) == original
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 1


def test_append_rejects_foreign_missing_and_uninitialized_drafts(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    key = uuid4()
    with Session(postgres_engine) as session, session.begin():
        uninitialized = create_conversation(session=session, user_id=owner).id
        original = append_workout_draft_set(
            conversation_id, exercise_id=catalog_exercises[0], reps=8, weight_kg=None,
            idempotency_key=key, session=session, user_id=owner,
        )
    with Session(postgres_engine) as session, session.begin():
        for target, actor in ((conversation_id, uuid4()), (uuid4(), owner), (uninitialized, owner)):
            with pytest.raises(WorkoutDraftNotFoundError):
                append_workout_draft_set(
                    target, exercise_id=catalog_exercises[0], reps=8, weight_kg=None,
                    idempotency_key=key, session=session, user_id=actor,
                )
        assert get_workout_draft(conversation_id, session=session, user_id=owner) == original
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 1


def test_discarded_draft_rejects_new_appends_retries_and_replacements(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    key = uuid4()
    with Session(postgres_engine) as session, session.begin():
        append_workout_draft_set(
            conversation_id, exercise_id=catalog_exercises[0], reps=8, weight_kg=None,
            idempotency_key=key, session=session, user_id=owner,
        )
        session.execute(update(WorkoutDraftRecord).values(state="discarded"))
    with Session(postgres_engine) as session, session.begin():
        original = get_workout_draft(conversation_id, session=session, user_id=owner)
        for operation_key in (key, uuid4()):
            with pytest.raises(WorkoutDraftInactiveError):
                append_workout_draft_set(
                    conversation_id, exercise_id=catalog_exercises[0], reps=8, weight_kg=None,
                    idempotency_key=operation_key, session=session, user_id=owner,
                )
        with pytest.raises(WorkoutDraftInactiveError):
            replace_workout_draft(conversation_id, snapshot(), expected_version=2, session=session, user_id=owner)
        assert create_workout_draft(conversation_id, session=session, user_id=owner) == original
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 1


@pytest.mark.parametrize("changes,error", [
    ({"exercise_id": 2147483647}, WorkoutDraftExerciseNotFoundError),
    ({"exercise_id": True}, ValueError),
    ({"idempotency_key": "not-a-uuid"}, ValueError),
    ({"reps": 0}, ValidationError),
    ({"weight_kg": Decimal("-1")}, ValidationError),
])
def test_invalid_append_leaves_no_snapshot_changes_or_receipt(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
    changes: dict[str, Any],
    error: type[Exception],
) -> None:
    conversation_id, owner = empty_draft
    arguments: dict[str, Any] = dict(
        exercise_id=catalog_exercises[0], reps=8, weight_kg=None, idempotency_key=uuid4(),
    )
    arguments.update(changes)
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(error):
            append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored.version == 1 and stored.payload.sets == []
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 0


def test_append_never_commits_or_flushes_unrelated_changes_and_key_can_retry_after_rollback(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    arguments: dict[str, Any] = dict(exercise_id=catalog_exercises[0], reps=8, weight_kg=None, idempotency_key=uuid4())
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        saved = append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
        assert saved.version == 2 and unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            stored = get_workout_draft(conversation_id, session=observer, user_id=owner)
            assert stored.version == 1 and stored.payload.sets == []
            assert observer.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 0
        session.rollback()
    with Session(postgres_engine) as session, session.begin():
        retried = append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
        assert retried.version == 2 and len(retried.payload.sets) == 1


@pytest.mark.parametrize("deferred", [False, True])
def test_receipt_insert_or_commit_failure_rolls_back_snapshot_and_allows_retry(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
    deferred: bool,
) -> None:
    conversation_id, owner = empty_draft
    with postgres_engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION reject_append() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic append failure'; END; $$
        """))
        trigger = (
            "CREATE CONSTRAINT TRIGGER reject_append AFTER INSERT ON workout_draft_appends "
            "DEFERRABLE INITIALLY DEFERRED" if deferred else
            "CREATE TRIGGER reject_append BEFORE INSERT ON workout_draft_appends"
        )
        connection.execute(text(trigger + " FOR EACH ROW EXECUTE FUNCTION reject_append()"))
    arguments: dict[str, Any] = dict(exercise_id=catalog_exercises[0], reps=8, weight_kg=None, idempotency_key=uuid4())
    with pytest.raises(SQLAlchemyError):
        with Session(postgres_engine) as session, session.begin():
            saved = append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
            assert deferred and saved.version == 2
    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored.version == 1 and stored.payload.sets == []
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 0
    with postgres_engine.begin() as connection:
        connection.execute(text("DROP TRIGGER reject_append ON workout_draft_appends"))
    with Session(postgres_engine) as session, session.begin():
        saved = append_workout_draft_set(conversation_id, **arguments, session=session, user_id=owner)
        assert saved.version == 2 and len(saved.payload.sets) == 1


@pytest.mark.parametrize("mode", ["distinct_keys", "same_key", "conflicting_key"])
def test_concurrent_appends_serialize_without_lost_sets_or_duplicate_operations(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
    mode: str,
) -> None:
    conversation_id, owner = empty_draft
    keys = [uuid4(), uuid4()]
    if mode != "distinct_keys":
        keys[1] = keys[0]
    barrier = Barrier(2)

    def worker(index: int) -> StoredWorkoutDraft | None:
        with Session(postgres_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            try:
                return append_workout_draft_set(
                    conversation_id, exercise_id=catalog_exercises[0],
                    reps=8 if mode == "same_key" else 8 - index, weight_kg=None,
                    idempotency_key=keys[index], session=session, user_id=owner,
                )
            except WorkoutDraftConflictError:
                assert mode == "conflicting_key"
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        expected_count = 2 if mode == "distinct_keys" else 1
        assert stored.version == expected_count + 1 and len(stored.payload.sets) == expected_count
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == expected_count
        winners = [result for result in results if result is not None]
        assert len(winners) == (1 if mode == "conflicting_key" else 2)
        if mode == "same_key":
            assert results == [stored, stored]
        else:
            ordered = sorted(winners, key=lambda draft: draft.version)
            assert [item.id for item in stored.payload.sets] == [draft.payload.sets[-1].id for draft in ordered]


def test_concurrent_append_and_snapshot_replacement_do_not_lose_sets(
    postgres_engine: Engine,
    empty_draft: tuple[UUID, UUID],
    catalog_exercises: tuple[int, int],
) -> None:
    conversation_id, owner = empty_draft
    barrier = Barrier(2)
    replacement = snapshot()

    def worker(append: bool) -> StoredWorkoutDraft | None:
        with Session(postgres_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            if append:
                return append_workout_draft_set(
                    conversation_id, exercise_id=catalog_exercises[0], reps=8, weight_kg=None,
                    idempotency_key=uuid4(), session=session, user_id=owner,
                )
            try:
                return replace_workout_draft(
                    conversation_id, replacement, expected_version=1, session=session, user_id=owner,
                )
            except WorkoutDraftConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        appended_future = pool.submit(worker, True)
        replaced_future = pool.submit(worker, False)
        appended = appended_future.result(timeout=10)
        replaced = replaced_future.result(timeout=10)
    with Session(postgres_engine) as session:
        stored = get_workout_draft(conversation_id, session=session, user_id=owner)
        assert stored == appended
        assert stored.payload.sets[-1].exercise_id == catalog_exercises[0]
        if replaced is None:
            assert stored.version == 2 and len(stored.payload.sets) == 1
        else:
            assert stored.version == 3 and stored.payload.sets[:-1] == replacement.sets


def test_postgres_enforces_append_receipt_identity_and_cascades_on_conversation_deletion(
    session: Session,
) -> None:
    owner, key = uuid4(), uuid4()
    conversation_id = create_conversation(session=session, user_id=owner).id
    create_workout_draft(conversation_id, session=session, user_id=owner)
    receipt: dict[str, Any] = dict(conversation_id=conversation_id, idempotency_key=key, set_payload={})
    session.execute(insert(WorkoutDraftAppendRecord).values(**receipt))
    for changes, sqlstate in (({}, "23505"), ({"conversation_id": uuid4()}, "23503"),
                              ({"idempotency_key": uuid4(), "set_payload": []}, "23514")):
        with pytest.raises(IntegrityError) as error:
            with session.begin_nested():
                session.execute(insert(WorkoutDraftAppendRecord).values(**{**receipt, **changes}))
        assert getattr(error.value.orig, "sqlstate", None) == sqlstate
    session.execute(delete(Conversation).where(Conversation.id == conversation_id))
    assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 0
