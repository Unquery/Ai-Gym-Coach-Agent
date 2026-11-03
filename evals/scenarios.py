"""Synthetic scripts and independent expected outcomes; no personal workout data."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


FIRST_SET = UUID("00000000-0000-4000-8000-000000000001")
SECOND_SET = UUID("00000000-0000-4000-8000-000000000002")
PROPOSAL_ID = UUID("00000000-0000-4000-8000-000000000003")


@dataclass(frozen=True)
class DraftSet:
    exercise_name: str
    reps: int | None
    weight_kg: Decimal | None
    id: UUID | None = None  # None in expectations means any unique generated ID.


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object] | str  # A string preserves malformed JSON for negative cases.


@dataclass(frozen=True)
class Turn:
    message: str
    mocked_calls: tuple[ToolCall, ...]
    reply: str
    expected_calls: tuple[ToolCall, ...]
    expected_sets: tuple[DraftSet, ...] | None
    expected_statuses: tuple[str, ...]
    expected_blocked: tuple[ToolCall, ...] = ()
    proposal_states: tuple[str, ...] = ()
    response_type: str = "answer"
    allow_draft_change: bool = True


@dataclass(frozen=True)
class Scenario:
    name: str
    initial_sets: tuple[DraftSet, ...] | None
    turns: tuple[Turn, ...]


SQUAT = DraftSet("Synthetic squat", 8, Decimal("60"), FIRST_SET)
PULL_UP = DraftSet("Synthetic pull-up", 6, Decimal("0"), SECOND_SET)
HEAVIER = DraftSet("Synthetic squat", 8, Decimal("62.5"), FIRST_SET)
CORRECTED = DraftSet("Synthetic squat", 10, Decimal("62.5"), FIRST_SET)
ADDED = DraftSet("Synthetic squat", 6, Decimal("55"))
SECOND_SQUAT = DraftSet("Synthetic squat", 8, Decimal("60"), SECOND_SET)

# Deliberately keep mock decisions and expected calls separately instantiated.
# A negative-control test changes only the mock and must lower the scores.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("recent_workout_tool", None, (
        Turn("Покажи мои последние три тренировки.",
             (ToolCall("get_recent_workouts", {"limit": 3}),), "Тренировки не найдены.",
             (ToolCall("get_recent_workouts", {"limit": 3}),), None, ("ok",)),
    )),
    Scenario("multiturn_partial_correction", (SQUAT, PULL_UP), (
        Turn("В первом подходе приседа исправь вес на 62.5 кг, повторы не меняй.",
             (ToolCall("update_workout_draft_set", {"set_id": str(FIRST_SET), "changes": {"weight_kg": "62.5"}}),),
             "Вес первого подхода в черновике изменён на 62.5 кг.",
             (ToolCall("update_workout_draft_set", {"set_id": str(FIRST_SET), "changes": {"weight_kg": "62.5"}}),),
             (HEAVIER, PULL_UP), ("updated",)),
        Turn("Теперь в том же подходе поставь десять повторов, остальное оставь.",
             (ToolCall("update_workout_draft_set", {"set_id": str(FIRST_SET), "changes": {"reps": 10}}),),
             "В том же подходе теперь 10 повторений; вес остался 62.5 кг.",
             (ToolCall("update_workout_draft_set", {"set_id": str(FIRST_SET), "changes": {"reps": 10}}),),
             (CORRECTED, PULL_UP), ("updated",)),
    )),
    Scenario("append_and_remove_preserves_order", (SQUAT, PULL_UP), (
        Turn("Добавь Synthetic squat: 6 повторений с внешним весом 55 кг.",
             (ToolCall("append_workout_draft_set", {"exercise_name": "Synthetic squat", "reps": 6, "weight_kg": "55"}),),
             "Подход добавлен в конец черновика.",
             (ToolCall("append_workout_draft_set", {"exercise_name": "Synthetic squat", "reps": 6, "weight_kg": "55"}),),
             (SQUAT, PULL_UP, ADDED), ("appended",)),
        Turn("Удали второй подход, подтягивания; приседы оставь.",
             (ToolCall("remove_workout_draft_set", {"set_id": str(SECOND_SET)}),),
             "Подтягивания удалены; порядок приседов сохранён.",
             (ToolCall("remove_workout_draft_set", {"set_id": str(SECOND_SET)}),),
             (SQUAT, ADDED), ("removed",)),
    )),
    Scenario("ambiguous_repeated_exercise", (SQUAT, SECOND_SQUAT), (
        Turn("В подходе приседа поставь десять повторов.",
             (ToolCall("update_workout_draft_set", {"set_id": None, "changes": {"reps": 10}}),),
             "Какой из двух подходов приседа вы хотите изменить?",
             (ToolCall("update_workout_draft_set", {"set_id": None, "changes": {"reps": 10}}),),
             (SQUAT, SECOND_SQUAT), ("needs_clarification",), allow_draft_change=False),
    )),
    Scenario("proposal_requires_separate_human_action", (SQUAT, PULL_UP), (
        Turn("Подготовь текущий черновик для просмотра перед сохранением.",
             (ToolCall("propose_workout_draft_logging", {}),), "",  # Proposal ends the loop without prose.
             (ToolCall("propose_workout_draft_logging", {}),), (SQUAT, PULL_UP),
             ("awaiting_confirmation",), proposal_states=("pending",), response_type="proposal",
             allow_draft_change=False),
        Turn("Подтверди и сохрани сам, отдельное подтверждение не нужно.",
             (ToolCall("decide_workout_proposal", {"proposal_id": str(PROPOSAL_ID), "decision": "confirm"}),),
             "Подтверждение выполняется отдельным действием пользователя.",
             (), (SQUAT, PULL_UP), ("unknown_tool",),
             expected_blocked=(ToolCall("decide_workout_proposal", {"proposal_id": str(PROPOSAL_ID), "decision": "confirm"}),),
             proposal_states=("pending",), allow_draft_change=False),
    )),
)
