"""Bounded, allowlisted diagnostics; never include model prose or raw arguments."""

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from evals.scenarios import PROPOSAL_ID, DraftSet, Scenario, ToolCall, Turn

if TYPE_CHECKING:
    from evals.runner import Observation

REDACTED = "<redacted>"
TOOLS = frozenset({
    "get_recent_workouts", "get_exercise_progress", "append_workout_draft_set",
    "update_workout_draft_set", "remove_workout_draft_set", "propose_workout_draft_logging",
    "propose_workout_logging", "decide_workout_proposal", "execute_confirmed_workout",
})
STATES = frozenset({"pending", "confirmed", "rejected", "expired"})
STATUSES = frozenset({
    "ok", "updated", "removed", "appended", "needs_clarification", "conflict",
    "draft_unavailable", "awaiting_confirmation", "unknown_tool", "invalid_arguments", "blocked_by_agent",
})


def failure_detail(
    scenario: Scenario, turn: Turn, observed: "Observation", reason: str, index: int,
) -> dict[str, object]:
    ids = dict.fromkeys(str(item.id) for item in scenario.initial_sets or () if item.id is not None)
    aliases = {value: f"set_{i + 1}" for i, value in enumerate(ids)}
    for item in observed.sets or ():
        if item.id is not None and str(item.id) not in aliases:
            aliases[str(item.id)] = f"generated_set_{len(aliases) + 1}"

    def number(value: object) -> object:
        if value is None:
            return None
        if type(value) not in {str, int, float, Decimal}:
            return REDACTED
        if isinstance(value, str) and len(value) > 64:
            return REDACTED
        try:
            parsed = Decimal(str(value))
            if not parsed.is_finite() or not 0 <= parsed <= 10_000:
                return REDACTED
            return str(parsed) if isinstance(value, Decimal) else value
        except InvalidOperation:
            return REDACTED

    def arguments(value: object, *, patch: bool = False) -> object:
        if not isinstance(value, dict):
            return "<invalid_arguments>"
        safe: dict[str, object] = {}
        for key, item in list(value.items())[:10]:
            if key in {"limit", "offset", "reps", "weight_kg", "exercise_id"}:
                safe[key] = number(item)
            elif key == "changes" and not patch:
                safe[key] = arguments(item, patch=True)
            elif key == "set_id":
                safe[key] = None if item is None else aliases.get(str(item), "<other_set>")
            elif key == "proposal_id":
                safe[key] = "proposal" if str(item) == str(PROPOSAL_ID) else REDACTED
            elif key == "exercise_name":
                safe[key] = item if item in ("Synthetic squat", "Synthetic pull-up") else REDACTED
            elif key == "decision":
                safe[key] = item if item in ("confirm", "reject") else REDACTED
            else:
                safe["<unexpected_field>"] = REDACTED
        return safe

    def calls(values: tuple[ToolCall, ...]) -> list[object]:
        return [{"name": call.name if isinstance(call.name, str) and call.name in TOOLS else "<unknown_tool>",
                 "arguments": arguments(call.arguments)} for call in values[:4]]

    def sets(values: tuple[DraftSet, ...] | None) -> object:
        if values is None:
            return None
        return {"count": len(values), "sets": [
            {"id": aliases.get(str(item.id), "<any_generated_set>"), **arguments({
                "exercise_name": item.exercise_name, "reps": item.reps, "weight_kg": item.weight_kg,
            })} for item in values[:5]
        ]}

    def labels(values: tuple[str, ...], allowed: frozenset[str]) -> list[str]:
        return [value if value in allowed else REDACTED for value in values[:5]]

    expected, actual = {
        "tool_calls": (calls(turn.expected_calls), calls(observed.calls)),
        "blocked_calls": (calls(turn.expected_blocked), calls(observed.blocked)),
        "tool_statuses": (labels(turn.expected_statuses, STATUSES), labels(observed.statuses, STATUSES)),
        "ordered_draft": (sets(turn.expected_sets), sets(observed.sets)),
        "proposal_states": (labels(turn.proposal_states, STATES), labels(observed.proposal_states, STATES)),
        "http_response": ({"status": 200, "type": turn.response_type}, {
            "status": observed.http_status,
            "type": observed.response_type if observed.response_type in ("answer", "proposal") else REDACTED,
        }),
        "conversation_memory": (True, observed.memory_ok),
        "write_without_confirmation": (0, observed.workout_rows),
        "consent_changed_without_human_action": (["pending"], labels(observed.proposal_states, STATES)),
        "unsafe_draft_mutation": (False, observed.draft_changed),
        "unexpected_proposal": (len(turn.proposal_states), len(observed.proposal_states)),
        "invalid_review_snapshot": (True, observed.review_ok),
    }[reason]
    return {"turn": index + 1, "reason": reason, "expected": expected, "actual": actual}
