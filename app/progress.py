"""Deterministic exercise metrics and changes between workout sessions."""

from collections.abc import Sequence
from datetime import UTC
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel

from app.schemas import WorkoutDetailRead, WorkoutSetRead


class ExerciseWorkoutProgress(BaseModel):
    workout_id: int
    started_at: AwareDatetime
    set_count: int
    total_reps: int
    max_external_weight_kg: Decimal
    external_volume_kg_reps: Decimal
    has_external_load: bool
    delta_set_count: int | None = None
    delta_total_reps: int | None = None
    delta_max_external_weight_kg: Decimal | None = None
    delta_external_volume_kg_reps: Decimal | None = None


def calculate_external_volume(
    sets: Sequence[WorkoutSetRead],
) -> Decimal:
    """Sum external kg * reps; zero external load does not mean zero effort."""
    volume: Decimal = Decimal("0")
    for workout_set in sets:
        if workout_set.reps <= 0:
            raise ValueError("Reps must be positive")
        weight = workout_set.weight_kg
        if not weight.is_finite() or weight < 0:
            raise ValueError("Weight must be finite and nonnegative")
        volume += workout_set.reps * weight
    return volume


def analyze_exercise_progress(
    exercise_id: int,
    workouts: Sequence[WorkoutDetailRead],
) -> list[ExerciseWorkoutProgress]:
    """Compare one exercise across supplied sessions belonging to one owner.

    The caller must obtain owner-scoped records. This calculation does not
    authenticate users, query the database, or measure bodyweight effort.
    """
    if (
        isinstance(exercise_id, bool)
        or not isinstance(exercise_id, int)
        or not 1 <= exercise_id <= 2147483647
    ):
        raise ValueError("Exercise ID must be an integer from 1 to 2147483647")
    if len({workout.user_id for workout in workouts}) > 1:
        raise ValueError("Workouts must belong to one user")
    if len({workout.id for workout in workouts}) != len(workouts):
        raise ValueError("Workout IDs must be unique")

    progress: list[ExerciseWorkoutProgress] = []
    for workout in sorted(
        workouts, key=lambda row: (row.started_at.astimezone(UTC), row.id)
    ):
        selected_sets = [
            workout_set
            for workout_set in workout.sets
            if workout_set.exercise_id == exercise_id
        ]
        if not selected_sets:
            continue
        if any(workout_set.workout_id != workout.id for workout_set in selected_sets):
            raise ValueError("Sets must belong to their containing workout")
        if len({row.set_number for row in selected_sets}) != len(selected_sets):
            raise ValueError("Set numbers must be unique for the selected exercise")

        # Validate all selected reps/weights before max, sums, or comparisons.
        volume = calculate_external_volume(selected_sets)
        maximum_weight = max(workout_set.weight_kg for workout_set in selected_sets)
        current = ExerciseWorkoutProgress(
            workout_id=workout.id,
            started_at=workout.started_at,
            set_count=len(selected_sets),
            total_reps=sum(workout_set.reps for workout_set in selected_sets),
            max_external_weight_kg=maximum_weight,
            external_volume_kg_reps=volume,
            has_external_load=maximum_weight > 0,
        )
        if progress:
            previous = progress[-1]
            current.delta_set_count = current.set_count - previous.set_count
            current.delta_total_reps = current.total_reps - previous.total_reps
            current.delta_max_external_weight_kg = (
                current.max_external_weight_kg - previous.max_external_weight_kg
            )
            current.delta_external_volume_kg_reps = (
                current.external_volume_kg_reps - previous.external_volume_kg_reps
            )
        progress.append(current)
    return progress
