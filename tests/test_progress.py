from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.progress import analyze_exercise_progress, calculate_external_volume
from app.schemas import WorkoutDetailRead, WorkoutSetRead


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([], "0"),
        ([(6, "0.00"), (8, "0.00")], "0.00"),
        ([(10, "20.00"), (8, "25.00")], "400.00"),
        ([(6, "0.00"), (8, "10.00")], "80.00"),
        ([(3, "0.10")], "0.30"),
    ],
)
def test_calculate_external_volume(rows: list[tuple[int, str]], expected: str) -> None:
    sets = [
        WorkoutSetRead(
            id=number,
            workout_id=1,
            exercise_id=1,
            set_number=number,
            reps=reps,
            weight_kg=Decimal(weight),
        )
        for number, (reps, weight) in enumerate(rows, start=1)
    ]
    before = [workout_set.model_dump() for workout_set in sets]

    result = calculate_external_volume(sets)

    assert isinstance(result, Decimal)
    assert result == Decimal(expected)
    assert [workout_set.model_dump() for workout_set in sets] == before


@pytest.mark.parametrize(
    "reps, weight, message",
    [
        (0, "20.00", "Reps must be positive"),
        (-1, "20.00", "Reps must be positive"),
        (8, "-0.01", "Weight must be finite and nonnegative"),
        (8, "NaN", "Weight must be finite and nonnegative"),
        (8, "Infinity", "Weight must be finite and nonnegative"),
        (8, "-Infinity", "Weight must be finite and nonnegative"),
        (0, "Infinity", "Reps must be positive"),
    ],
)
def test_calculate_external_volume_rejects_invalid_sets(
    reps: int, weight: str, message: str
) -> None:
    valid = WorkoutSetRead(
        id=1,
        workout_id=1,
        exercise_id=1,
        set_number=1,
        reps=10,
        weight_kg=Decimal("20.00"),
    )
    # Bypass DTO validation to exercise the calculation's own input guards.
    invalid = valid.model_copy(
        update={"id": 2, "set_number": 2, "reps": reps, "weight_kg": Decimal(weight)}
    )

    with pytest.raises(ValueError, match=f"^{message}$"):
        calculate_external_volume([valid, invalid])


def make_workout(
    workout_id: int,
    day: int,
    rows: list[tuple[int, int, str]],
) -> WorkoutDetailRead:
    return WorkoutDetailRead(
        id=workout_id,
        user_id=UUID("00000000-0000-0000-0000-000000000001"),
        plan_id=None,
        started_at=datetime(2026, 1, day, tzinfo=UTC),
        sets=[
            WorkoutSetRead(
                id=workout_id * 10 + number,
                workout_id=workout_id,
                exercise_id=exercise_id,
                set_number=number,
                reps=reps,
                weight_kg=Decimal(weight),
            )
            for number, (exercise_id, reps, weight) in enumerate(rows, start=1)
        ],
    )


@pytest.mark.parametrize("history", ["empty", "no_sets", "other_exercise"])
def test_progress_returns_empty_without_the_selected_exercise(history: str) -> None:
    workouts = (
        []
        if history == "empty"
        else [make_workout(1, 1, [] if history == "no_sets" else [(2, 10, "20.00")])]
    )
    assert analyze_exercise_progress(1, workouts) == []


def test_progress_filters_orders_and_compares_matching_sessions() -> None:
    workouts = [
        make_workout(40, 7, [(1, 5, "10.00")]),
        make_workout(20, 6, [(2, 100, "500.00")]),
        make_workout(30, 7, [(1, 12, "20.00"), (1, 10, "25.00")]),
        make_workout(10, 5, [(1, 10, "20.00"), (1, 8, "25.00"), (2, 100, "500.00")]),
    ]
    # Set numbers are scoped to an exercise, so another exercise may also use 1.
    workouts[-1].sets[-1].set_number = 1
    before = [workout.model_dump() for workout in workouts]

    result = analyze_exercise_progress(1, workouts)

    assert [row.workout_id for row in result] == [10, 30, 40]
    assert [row.started_at for row in result] == [
        datetime(2026, 1, day, tzinfo=UTC) for day in [5, 7, 7]
    ]
    assert [row.set_count for row in result] == [2, 2, 1]
    assert [row.total_reps for row in result] == [18, 22, 5]
    assert [row.max_external_weight_kg for row in result] == [
        Decimal("25.00"),
        Decimal("25.00"),
        Decimal("10.00"),
    ]
    assert [row.external_volume_kg_reps for row in result] == [
        Decimal("400.00"),
        Decimal("490.00"),
        Decimal("50.00"),
    ]
    assert all(row.has_external_load for row in result)
    assert [row.delta_set_count for row in result] == [None, 0, -1]
    assert [row.delta_total_reps for row in result] == [None, 4, -17]
    assert [row.delta_max_external_weight_kg for row in result] == [
        None,
        Decimal("0.00"),
        Decimal("-15.00"),
    ]
    assert [row.delta_external_volume_kg_reps for row in result] == [
        None,
        Decimal("90.00"),
        Decimal("-440.00"),
    ]
    assert [workout.model_dump() for workout in workouts] == before
    serialized = result[0].model_dump(mode="json")
    assert serialized["external_volume_kg_reps"] == "400.00"
    assert serialized["started_at"] == "2026-01-05T00:00:00Z"


def test_progress_preserves_bodyweight_reps_and_tracks_only_external_load() -> None:
    result = analyze_exercise_progress(
        1,
        [
            make_workout(1, 1, [(1, 6, "0.00"), (1, 5, "0.00")]),
            make_workout(2, 2, [(1, 8, "0.00"), (1, 7, "0.00")]),
            make_workout(3, 3, [(1, 6, "0.00"), (1, 8, "10.00")]),
        ],
    )

    assert [row.total_reps for row in result] == [11, 15, 14]
    assert [row.set_count for row in result] == [2, 2, 2]
    assert [row.has_external_load for row in result] == [False, False, True]
    assert [row.max_external_weight_kg for row in result] == [
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("10.00"),
    ]
    assert [row.external_volume_kg_reps for row in result] == [
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("80.00"),
    ]
    assert result[1].delta_total_reps == 4
    assert result[1].delta_external_volume_kg_reps == Decimal("0.00")
    assert result[2].delta_external_volume_kg_reps == Decimal("80.00")


def test_progress_orders_instants_across_timezones_with_id_ties() -> None:
    workouts = [make_workout(number, 5, [(1, 1, "1.00")]) for number in [3, 2, 1]]
    workouts[0].started_at = datetime.fromisoformat("2026-01-04T23:00:00-02:00")
    workouts[1].started_at = datetime.fromisoformat("2026-01-05T01:00:00+01:00")

    result = analyze_exercise_progress(1, workouts)

    assert [row.workout_id for row in result] == [1, 2, 3]


@pytest.mark.parametrize("exercise_id", [0, -1, 2147483648, True])
def test_progress_rejects_invalid_exercise_ids(exercise_id: int) -> None:
    with pytest.raises(ValueError, match="Exercise ID must be an integer"):
        analyze_exercise_progress(exercise_id, [])


@pytest.mark.parametrize(
    "problem, message",
    [
        ("owners", "Workouts must belong to one user"),
        ("workouts", "Workout IDs must be unique"),
        ("parent", "Sets must belong to their containing workout"),
        ("sets", "Set numbers must be unique for the selected exercise"),
    ],
)
def test_progress_rejects_inconsistent_records(problem: str, message: str) -> None:
    first = make_workout(1, 1, [(1, 10, "20.00")])
    if problem == "owners":
        second = make_workout(2, 2, [(1, 10, "20.00")])
        workouts = [first, second.model_copy(update={"user_id": uuid4()})]
    elif problem == "workouts":
        workouts = [first, first]
    elif problem == "parent":
        first.sets = [first.sets[0].model_copy(update={"workout_id": 2})]
        workouts = [first]
    else:
        first.sets.append(first.sets[0].model_copy(update={"id": 999}))
        workouts = [first]

    with pytest.raises(ValueError, match=f"^{message}$"):
        analyze_exercise_progress(1, workouts)


def test_progress_validates_selected_sets_before_aggregating() -> None:
    workout = make_workout(1, 1, [(1, 10, "20.00")])
    # NaN would make max/comparison fail if the volume helper's guards were skipped.
    workout.sets.append(
        workout.sets[0].model_copy(
            update={"id": 2, "set_number": 2, "weight_kg": Decimal("NaN")}
        )
    )
    with pytest.raises(ValueError, match="Weight must be finite and nonnegative"):
        analyze_exercise_progress(1, [workout])


def test_progress_tracks_transition_to_zero_external_load() -> None:
    first = make_workout(1, 1, [(1, 6, "10")])
    second = make_workout(2, 2, [(1, 8, "0")])

    result = analyze_exercise_progress(1, [first, second])

    assert len(result) == 2
    assert [row.set_count for row in result] == [1, 1]
    assert [row.max_external_weight_kg for row in result] == [
        Decimal(10),
        Decimal(0),
    ]
    assert [row.external_volume_kg_reps for row in result] == [
        Decimal(60),
        Decimal(0),
    ]
    assert [row.has_external_load for row in result] == [True, False]
    assert result[0].total_reps == 6
    assert result[1].total_reps == 8
    assert result[1].delta_total_reps == 2
    assert result[1].delta_set_count == 0
    assert result[1].delta_max_external_weight_kg == Decimal(-10)
    assert result[1].delta_external_volume_kg_reps == Decimal(-60)
