"""Explicit, repeatable synthetic fixtures for a local demonstration."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import create_db_engine
from app.models import Exercise, Workout, WorkoutPlan, WorkoutPlanExercise, WorkoutSet

DEMO_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
DEMO_PLAN_NAME = "Synthetic full-body demo"


def seed_demo(session: Session) -> None:
    """Add synthetic data once; the caller controls the transaction."""
    existing = session.scalar(
        select(WorkoutPlan.id).where(
            WorkoutPlan.user_id == DEMO_USER_ID, WorkoutPlan.name == DEMO_PLAN_NAME
        )
    )
    if existing is not None:
        return

    exercises = []
    for name in ("Demo squat", "Demo pull-up"):
        exercise = session.scalar(select(Exercise).where(Exercise.name == name))
        if exercise is None:
            exercise = Exercise(name=name)
            session.add(exercise)
        exercises.append(exercise)

    plan = WorkoutPlan(user_id=DEMO_USER_ID, name=DEMO_PLAN_NAME)
    plan.exercises = [
        WorkoutPlanExercise(
            exercise=exercise, position=position, target_sets=2, target_reps=8
        )
        for position, exercise in enumerate(exercises, start=1)
    ]
    workout = Workout(
        user_id=DEMO_USER_ID,
        plan=plan,
        started_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        sets=[
            WorkoutSet(
                exercise=exercises[0], set_number=1, reps=8, weight_kg=Decimal("20.00")
            ),
            WorkoutSet(
                exercise=exercises[0], set_number=2, reps=8, weight_kg=Decimal("20.00")
            ),
            WorkoutSet(
                exercise=exercises[1], set_number=1, reps=6, weight_kg=Decimal("0.00")
            ),
        ],
    )
    session.add(workout)


def main() -> None:
    engine = create_db_engine()
    try:
        with Session(engine) as session, session.begin():
            seed_demo(session)
        print("Synthetic demo data is stored (existing demo data is left unchanged).")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
