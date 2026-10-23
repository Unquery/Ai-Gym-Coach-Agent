from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Exercise, Workout, WorkoutPlan, WorkoutPlanExercise, WorkoutSet
from app.seed import DEMO_USER_ID, seed_demo

pytestmark = pytest.mark.postgres


def make_workout(session: Session) -> Workout:
    squat = Exercise(name="Synthetic squat")
    plan = WorkoutPlan(user_id=uuid4(), name="Synthetic plan")
    plan.exercises = [
        WorkoutPlanExercise(exercise=squat, position=1, target_sets=3, target_reps=10)
    ]
    workout = Workout(
        user_id=plan.user_id,
        plan=plan,
        started_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        sets=[WorkoutSet(exercise=squat, set_number=1, reps=10, weight_kg=Decimal("30.25"))],
    )
    session.add(workout)
    session.flush()
    return workout


def test_committed_graph_survives_new_session(postgres_engine: Engine) -> None:
    with Session(postgres_engine) as writer, writer.begin():
        workout = make_workout(writer)
        workout_id = workout.id
        user_id = workout.user_id

    with Session(postgres_engine) as reader:
        saved = reader.scalar(
            select(Workout).where(Workout.id == workout_id, Workout.user_id == user_id)
        )
        assert saved is not None
        assert saved.started_at == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
        assert saved.plan is not None
        assert saved.plan.workouts == [saved]
        assert saved.plan.exercises[0].exercise == saved.sets[0].exercise
        assert saved.sets[0].weight_kg == Decimal("30.25")
        assert saved.sets[0].reps == 10
        assert saved.sets[0].workout == saved


def test_workout_can_exist_without_plan(session: Session) -> None:
    workout = Workout(user_id=uuid4())
    session.add(workout)
    session.commit()
    session.refresh(workout)
    assert workout.plan is None
    assert workout.sets == []
    assert workout.started_at.utcoffset() is not None


@pytest.mark.parametrize("assign_relationship", [False, True])
def test_workout_cannot_use_another_users_plan(
    session: Session, assign_relationship: bool
) -> None:
    plan = WorkoutPlan(user_id=uuid4(), name="Other user's synthetic plan")
    session.add(plan)
    session.flush()
    workout = Workout(user_id=uuid4())
    if assign_relationship:
        workout.plan = plan
    else:
        workout.plan_id = plan.id
    session.add(workout)
    with pytest.raises(IntegrityError):
        session.flush()


def test_history_query_filters_by_owner(session: Session) -> None:
    workout = make_workout(session)
    other = Workout(user_id=uuid4())
    session.add(other)
    session.flush()
    found = list(session.scalars(select(Workout).where(Workout.user_id == workout.user_id)))
    assert found == [workout]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reps", 0),
        ("reps", -1),
        ("set_number", 0),
        ("weight_kg", Decimal("-0.01")),
        ("weight_kg", Decimal("NaN")),
    ],
)
def test_invalid_set_values_are_rejected(session: Session, field: str, value) -> None:
    workout = make_workout(session)
    setattr(workout.sets[0], field, value)
    with pytest.raises(IntegrityError):
        session.flush()


def test_zero_external_load_is_valid(session: Session) -> None:
    workout = make_workout(session)
    workout.sets[0].weight_kg = Decimal("0.00")
    session.commit()
    assert workout.sets[0].weight_kg == Decimal("0.00")


def test_set_numbers_are_unique_per_workout_and_exercise(session: Session) -> None:
    workout = make_workout(session)
    workout.sets.append(
        WorkoutSet(
            exercise=workout.sets[0].exercise,
            set_number=1,
            reps=8,
            weight_kg=Decimal("30.00"),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_two_exercises_can_have_a_first_set(session: Session) -> None:
    workout = make_workout(session)
    workout.sets.append(
        WorkoutSet(
            exercise=Exercise(name="Synthetic pull-up"),
            set_number=1,
            reps=5,
            weight_kg=Decimal("0.00"),
        )
    )
    session.commit()
    assert len(workout.sets) == 2


@pytest.mark.parametrize("field", ["workout_id", "exercise_id"])
def test_set_cannot_reference_missing_parent(session: Session, field: str) -> None:
    workout = make_workout(session)
    invalid_set = WorkoutSet(
        workout_id=workout.id,
        exercise_id=workout.sets[0].exercise_id,
        set_number=2,
        reps=5,
        weight_kg=Decimal("0.00"),
    )
    setattr(invalid_set, field, 999999)
    session.add(invalid_set)
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("field", ["position", "target_sets", "target_reps"])
def test_plan_targets_must_be_positive(session: Session, field: str) -> None:
    workout = make_workout(session)
    setattr(workout.plan.exercises[0], field, 0)
    with pytest.raises(IntegrityError):
        session.flush()


def test_plan_exercise_order_round_trips(session: Session) -> None:
    workout = make_workout(session)
    plan = workout.plan
    plan.exercises.insert(
        0,
        WorkoutPlanExercise(
            exercise=Exercise(name="Synthetic second exercise"),
            position=2,
            target_sets=2,
            target_reps=8,
        ),
    )
    session.commit()
    session.expire_all()
    assert [entry.position for entry in plan.exercises] == [1, 2]


def test_plan_positions_cannot_repeat(session: Session) -> None:
    workout = make_workout(session)
    workout.plan.exercises.append(
        WorkoutPlanExercise(
            exercise=Exercise(name="Synthetic duplicate position"),
            position=1,
            target_sets=2,
            target_reps=8,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("model", [Exercise, WorkoutPlan])
def test_names_cannot_be_blank(
    session: Session, model: type[Exercise] | type[WorkoutPlan]
) -> None:
    row = Exercise(name="   ") if model is Exercise else WorkoutPlan(user_id=uuid4(), name="   ")
    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()


def test_exercise_catalog_names_are_unique(session: Session) -> None:
    session.add_all([Exercise(name="Synthetic squat"), Exercise(name="Synthetic squat")])
    with pytest.raises(IntegrityError):
        session.flush()


def test_plan_names_are_unique_per_owner(session: Session) -> None:
    owner = uuid4()
    session.add_all(
        [WorkoutPlan(user_id=owner, name="Synthetic A"), WorkoutPlan(user_id=owner, name="Synthetic A")]
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_different_owners_can_use_the_same_plan_name(session: Session) -> None:
    session.add_all(
        [WorkoutPlan(user_id=uuid4(), name="Synthetic A"), WorkoutPlan(user_id=uuid4(), name="Synthetic A")]
    )
    session.commit()
    assert session.scalar(select(func.count()).select_from(WorkoutPlan)) == 2


def test_editing_plan_does_not_change_performed_sets(session: Session) -> None:
    workout = make_workout(session)
    workout.plan.exercises[0].target_reps = 12
    session.commit()
    assert workout.sets[0].reps == 10
    assert workout.sets[0].weight_kg == Decimal("30.25")


def test_deleting_workout_cascades_only_to_its_sets(session: Session) -> None:
    workout = make_workout(session)
    session.execute(delete(Workout).where(Workout.id == workout.id))
    session.commit()
    assert session.scalar(select(func.count()).select_from(WorkoutSet)) == 0
    assert session.scalar(select(func.count()).select_from(WorkoutPlan)) == 1
    assert session.scalar(select(func.count()).select_from(Exercise)) == 1


@pytest.mark.parametrize("delete_model", [Exercise, WorkoutPlan])
def test_referenced_catalog_and_plan_cannot_be_deleted(
    session: Session, delete_model: type[Exercise] | type[WorkoutPlan]
) -> None:
    make_workout(session)
    with pytest.raises(IntegrityError):
        session.execute(delete(delete_model))


def test_orm_plan_deletion_cannot_silently_detach_history(session: Session) -> None:
    workout = make_workout(session)
    assert workout.plan.workouts == [workout]
    session.delete(workout.plan)
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_unused_plan_cascades_to_its_entries(session: Session) -> None:
    plan = WorkoutPlan(
        user_id=uuid4(),
        name="Synthetic unused plan",
        exercises=[
            WorkoutPlanExercise(
                exercise=Exercise(name="Synthetic squat"), position=1, target_sets=2, target_reps=8
            )
        ],
    )
    session.add(plan)
    session.flush()
    session.execute(delete(WorkoutPlan).where(WorkoutPlan.id == plan.id))
    session.commit()
    assert session.scalar(select(func.count()).select_from(WorkoutPlanExercise)) == 0
    assert session.scalar(select(func.count()).select_from(Exercise)) == 1


def test_rollback_discards_partial_workout(postgres_engine: Engine) -> None:
    with Session(postgres_engine) as writer:
        make_workout(writer)
        writer.rollback()
    with Session(postgres_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(Workout)) == 0
        assert reader.scalar(select(func.count()).select_from(WorkoutSet)) == 0


def test_demo_seed_is_repeatable_and_synthetic(session: Session) -> None:
    seed_demo(session)
    session.commit()
    counts_before = [
        session.scalar(select(func.count()).select_from(model))
        for model in (Exercise, WorkoutPlan, WorkoutPlanExercise, Workout, WorkoutSet)
    ]
    seed_demo(session)
    session.commit()
    counts_after = [
        session.scalar(select(func.count()).select_from(model))
        for model in (Exercise, WorkoutPlan, WorkoutPlanExercise, Workout, WorkoutSet)
    ]
    assert counts_before == counts_after == [2, 1, 2, 1, 3]
    assert session.scalar(select(Workout.user_id)) == DEMO_USER_ID
