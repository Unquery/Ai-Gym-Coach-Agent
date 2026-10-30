"""Owner-scoped draft corrections/removals with versioned, transactional writes."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from functools import partial
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.conversations.store import create_conversation
from app.db.models import Exercise, Workout, WorkoutDraftAppendRecord, WorkoutDraftRecord, WorkoutProposalRecord, WorkoutSet
from app.workouts.drafts import (
    StoredWorkoutDraft,
    WorkoutDraftConflictError,
    WorkoutDraftInactiveError,
    WorkoutDraftNotFoundError,
    WorkoutDraftSetNotFoundError,
    append_workout_draft_set,
    create_workout_draft,
    get_workout_draft,
    remove_set_from_payload,
    remove_workout_draft_set,
    update_workout_draft_set,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def populated_draft(postgres_engine: Engine) -> StoredWorkoutDraft:
    owner = uuid4()
    with Session(postgres_engine) as session, session.begin():
        squat = Exercise(name="Synthetic edit squat")
        pull_up = Exercise(name="Synthetic edit pull-up")
        session.add_all([squat, pull_up])
        session.flush()
        conversation_id = create_conversation(session=session, user_id=owner).id
        create_workout_draft(conversation_id, session=session, user_id=owner)
        for exercise, reps, weight in (
            (squat, 8, Decimal("20.25")), (pull_up, 6, None), (squat, 5, Decimal("0.00")),
        ):
            append_workout_draft_set(
                conversation_id, exercise_id=exercise.id, reps=reps, weight_kg=weight,
                idempotency_key=uuid4(), session=session, user_id=owner,
            )
        return get_workout_draft(conversation_id, session=session, user_id=owner)


@pytest.fixture(params=["update", "remove"])
def change_set(request: pytest.FixtureRequest) -> Callable[..., StoredWorkoutDraft]:
    if request.param == "update":
        return partial(update_workout_draft_set, changes={"reps": 10})
    return remove_workout_draft_set


@pytest.mark.parametrize("changes,expected_reps,expected_weight", [
    ({"reps": 10}, 10, Decimal("20.25")),
    ({"weight_kg": Decimal("12.50")}, 8, Decimal("12.50")),
    ({"weight_kg": Decimal("0")}, 8, Decimal("0")),
    ({"weight_kg": None}, 8, None),
    ({"reps": None}, None, Decimal("20.25")),
    ({"reps": 7, "weight_kg": Decimal("25.25")}, 7, Decimal("25.25")),
])
def test_correction_persists_only_supplied_fields_and_preserves_identity_and_order(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft, changes: dict[str, object],
    expected_reps: int | None, expected_weight: Decimal | None,
) -> None:
    before = populated_draft
    target = before.payload.sets[0]
    with Session(postgres_engine) as session, session.begin():
        saved = update_workout_draft_set(
            before.conversation_id, target.id, changes,
            expected_version=before.version, session=session, user_id=before.user_id,
        )
    with Session(postgres_engine) as session:
        stored = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        assert stored == saved and stored.version == before.version + 1
        assert stored.created_at == before.created_at
        corrected = stored.payload.sets[0]
        assert corrected.id == target.id
        assert corrected.exercise_id == target.exercise_id and corrected.exercise_name == target.exercise_name
        assert corrected.reps == expected_reps and corrected.weight_kg == expected_weight
        assert [item.id for item in stored.payload.sets] == [item.id for item in before.payload.sets]
        assert stored.payload.sets[1:] == before.payload.sets[1:]
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 3
        for model in (Workout, WorkoutSet, WorkoutProposalRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_remove_middle_set_preserves_survivor_order_and_original_payload(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
) -> None:
    before = populated_draft
    first, middle, last = before.payload.sets
    result = remove_set_from_payload(before.payload, middle.id)
    assert result.sets == [first, last]
    assert before.payload.sets == [first, middle, last]
    with Session(postgres_engine) as session, session.begin():
        saved = remove_workout_draft_set(
            before.conversation_id, middle.id,
            expected_version=before.version, session=session, user_id=before.user_id,
        )
    with Session(postgres_engine) as session:
        stored = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        assert stored == saved and stored.version == before.version + 1
        assert stored.payload.sets == [first, last]
        with pytest.raises(WorkoutDraftSetNotFoundError):
            remove_workout_draft_set(
                before.conversation_id, middle.id,
                expected_version=stored.version, session=session, user_id=before.user_id,
            )


def test_removing_all_sets_leaves_a_valid_empty_draft_and_retains_append_receipts(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        current = before
        for item in before.payload.sets:
            current = remove_workout_draft_set(
                before.conversation_id, item.id,
                expected_version=current.version, session=session, user_id=before.user_id,
            )
    with Session(postgres_engine) as session:
        stored = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        assert stored == current and stored.payload.sets == []
        assert stored.state == "active" and stored.version == before.version + 3
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 3


def test_change_requires_owned_existing_active_draft(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        uninitialized = create_conversation(session=session, user_id=before.user_id).id
        for conversation_id, actor in (
            (before.conversation_id, uuid4()), (uuid4(), before.user_id), (uninitialized, before.user_id),
        ):
            with pytest.raises(WorkoutDraftNotFoundError):
                change_set(conversation_id, before.payload.sets[0].id,
                           expected_version=before.version, session=session, user_id=actor)
        session.execute(update(WorkoutDraftRecord).where(
            WorkoutDraftRecord.conversation_id == before.conversation_id,
        ).values(state="discarded"))
    with Session(postgres_engine) as session, session.begin():
        discarded = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        with pytest.raises(WorkoutDraftInactiveError):
            change_set(before.conversation_id, before.payload.sets[0].id,
                       expected_version=before.version, session=session, user_id=before.user_id)
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == discarded


def test_set_must_belong_to_the_specified_draft(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    exercise_id = before.payload.sets[0].exercise_id
    assert exercise_id is not None
    with Session(postgres_engine) as session, session.begin():
        other_id = create_conversation(session=session, user_id=before.user_id).id
        create_workout_draft(other_id, session=session, user_id=before.user_id)
        other = append_workout_draft_set(
            other_id, exercise_id=exercise_id, reps=8, weight_kg=None,
            idempotency_key=uuid4(), session=session, user_id=before.user_id,
        )
    with Session(postgres_engine) as session, session.begin():
        for set_id in (uuid4(), other.payload.sets[0].id):
            with pytest.raises(WorkoutDraftSetNotFoundError):
                change_set(before.conversation_id, set_id,
                           expected_version=before.version, session=session, user_id=before.user_id)
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before
        assert get_workout_draft(other_id, session=session, user_id=before.user_id) == other


@pytest.mark.parametrize("bad_id", [1, "not-a-uuid"])
def test_set_positions_and_invalid_ids_are_rejected(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft], bad_id: Any,
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(ValueError, match="UUID"):
            change_set(before.conversation_id, bad_id,
                       expected_version=before.version, session=session, user_id=before.user_id)
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before


@pytest.mark.parametrize("changes", [
    {}, {"id": uuid4()}, {"exercise_id": 1}, {"user_id": uuid4()},
    {"reps": 0}, {"reps": True}, {"weight_kg": Decimal("-1")}, {"weight_kg": Decimal("0.001")},
])
def test_invalid_or_forbidden_patch_cannot_modify_the_draft(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft, changes: dict[str, object],
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(ValueError):
            update_workout_draft_set(
                before.conversation_id, before.payload.sets[0].id, changes,
                expected_version=before.version, session=session, user_id=before.user_id,
            )
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before


@pytest.mark.parametrize("bad_version", [0, True, None])
def test_expected_version_must_be_an_explicit_positive_integer(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft], bad_version: Any,
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(ValueError, match="positive integer"):
            change_set(before.conversation_id, before.payload.sets[0].id,
                       expected_version=bad_version, session=session, user_id=before.user_id)
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before


def test_retry_with_original_version_conflicts_without_further_changes(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session, session.begin():
        saved = change_set(before.conversation_id, before.payload.sets[0].id,
                           expected_version=before.version, session=session, user_id=before.user_id)
    with Session(postgres_engine) as session, session.begin():
        with pytest.raises(WorkoutDraftConflictError, match="version conflict"):
            change_set(before.conversation_id, before.payload.sets[0].id,
                       expected_version=before.version, session=session, user_id=before.user_id)
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == saved


def test_caller_rollback_restores_snapshot_and_allows_retry(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        with Session(postgres_engine) as session, session.begin():
            change_set(before.conversation_id, before.payload.sets[0].id,
                       expected_version=before.version, session=session, user_id=before.user_id)
            raise RuntimeError("Synthetic failure")
    with Session(postgres_engine) as session, session.begin():
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before
        saved = change_set(before.conversation_id, before.payload.sets[0].id,
                           expected_version=before.version, session=session, user_id=before.user_id)
        assert saved.version == before.version + 1


def test_commit_failure_preserves_the_previous_snapshot(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    with postgres_engine.begin() as connection:
        connection.execute(text("""
            CREATE FUNCTION reject_edit_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic commit failure'; END; $$
        """))
        connection.execute(text("""
            CREATE CONSTRAINT TRIGGER reject_edit_commit AFTER UPDATE ON workout_drafts
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_edit_commit()
        """))
    with pytest.raises(SQLAlchemyError):
        with Session(postgres_engine) as session, session.begin():
            saved = change_set(before.conversation_id, before.payload.sets[0].id,
                               expected_version=before.version, session=session, user_id=before.user_id)
            assert saved.version == before.version + 1
    with Session(postgres_engine) as session:
        assert get_workout_draft(before.conversation_id, session=session, user_id=before.user_id) == before
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 3


def test_changes_never_commit_or_flush_unrelated_objects(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    with Session(postgres_engine) as session:
        unrelated = Exercise(name="")
        session.add(unrelated)
        change_set(before.conversation_id, before.payload.sets[0].id,
                   expected_version=before.version, session=session, user_id=before.user_id)
        assert unrelated in session.new and unrelated.id is None
        with Session(postgres_engine) as observer:
            assert get_workout_draft(before.conversation_id, session=observer, user_id=before.user_id) == before
        session.rollback()


def test_old_append_retry_cannot_restore_removed_sets_or_undo_corrections(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    target = before.payload.sets[0]
    assert target.exercise_id is not None
    with Session(postgres_engine) as session, session.begin():
        key = session.scalar(select(WorkoutDraftAppendRecord.idempotency_key).where(
            WorkoutDraftAppendRecord.conversation_id == before.conversation_id,
            WorkoutDraftAppendRecord.set_payload["id"].astext == str(target.id),
        ))
        assert key is not None
        saved = change_set(before.conversation_id, target.id,
                           expected_version=before.version, session=session, user_id=before.user_id)
    with Session(postgres_engine) as session, session.begin():
        replay = append_workout_draft_set(
            before.conversation_id, exercise_id=target.exercise_id, reps=target.reps, weight_kg=target.weight_kg,
            idempotency_key=key, session=session, user_id=before.user_id,
        )
        assert replay == saved
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 3


def test_concurrent_edits_with_the_same_version_have_one_winner(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    barrier = Barrier(2)

    def worker() -> StoredWorkoutDraft | None:
        with Session(postgres_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            try:
                return change_set(before.conversation_id, before.payload.sets[0].id,
                                  expected_version=before.version, session=session, user_id=before.user_id)
            except WorkoutDraftConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with Session(postgres_engine) as session:
        stored = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        assert stored == winners[0] and stored.version == before.version + 1


def test_concurrent_change_and_append_preserve_the_appended_set(
    postgres_engine: Engine, populated_draft: StoredWorkoutDraft,
    change_set: Callable[..., StoredWorkoutDraft],
) -> None:
    before = populated_draft
    exercise_id = before.payload.sets[0].exercise_id
    assert exercise_id is not None
    barrier = Barrier(2)

    def worker(append: bool) -> StoredWorkoutDraft | None:
        with Session(postgres_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            if append:
                return append_workout_draft_set(
                    before.conversation_id, exercise_id=exercise_id, reps=4, weight_kg=None,
                    idempotency_key=uuid4(), session=session, user_id=before.user_id,
                )
            try:
                return change_set(before.conversation_id, before.payload.sets[0].id,
                                  expected_version=before.version, session=session, user_id=before.user_id)
            except WorkoutDraftConflictError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        changed_future = pool.submit(worker, False)
        appended_future = pool.submit(worker, True)
        changed = changed_future.result(timeout=10)
        appended = appended_future.result(timeout=10)
    assert appended is not None
    with Session(postgres_engine) as session:
        stored = get_workout_draft(before.conversation_id, session=session, user_id=before.user_id)
        survivors = changed.payload.sets if changed is not None else before.payload.sets
        assert stored == appended
        assert stored.payload.sets == [*survivors, appended.payload.sets[-1]]
        assert stored.version == before.version + (2 if changed is not None else 1)
        assert session.scalar(select(func.count()).select_from(WorkoutDraftAppendRecord)) == 4
