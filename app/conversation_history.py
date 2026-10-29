"""Select complete text turns without rebuilding historical tool protocols."""

from collections.abc import Sequence
from typing import Literal

MAX_HISTORY_MESSAGE_CHARS = 4_000
MAX_HISTORY_ROWS = 20
MAX_HISTORY_TURNS = 5
MAX_HISTORY_CHARS = 20_000


def select_recent_text_turns(
    rows: Sequence[tuple[int, str, str | None]],
    *,
    max_turns: int = MAX_HISTORY_TURNS,
    max_chars: int = MAX_HISTORY_CHARS,
) -> list[tuple[Literal["user", "assistant"], str]]:
    """Keep the newest fitting valid pairs and return them in chronological order.

    Rows are ascending (position, role, content) tuples. SQL marks unusable text
    as None. Invalid pairs are skipped; a valid pair exceeding the remaining
    budget stops selection. No message is truncated or joined across a gap.
    """
    if max_turns <= 0 or max_chars <= 0:
        return []

    previous_position = 0
    for row in rows:
        if (
            not isinstance(row, tuple) or len(row) != 3
            or type(row[0]) is not int or row[0] <= previous_position
        ):
            return []
        previous_position = row[0]

    selected_pairs: list[tuple[str, str]] = []
    chars = 0
    index = len(rows) - 1
    while index > 0 and len(selected_pairs) < max_turns:
        user_row, assistant_row = rows[index - 1], rows[index]
        user_text, assistant_text = user_row[2], assistant_row[2]
        if (
            user_row[1] != "user" or assistant_row[1] != "assistant"
            or assistant_row[0] != user_row[0] + 1
            or not isinstance(user_text, str) or not user_text.strip()
            or not isinstance(assistant_text, str) or not assistant_text.strip()
            or len(user_text) > MAX_HISTORY_MESSAGE_CHARS
            or len(assistant_text) > MAX_HISTORY_MESSAGE_CHARS
        ):
            index -= 1
            continue

        pair_chars = len(user_text) + len(assistant_text)
        if chars + pair_chars > max_chars:
            break
        selected_pairs.append((user_text, assistant_text))
        chars += pair_chars
        index -= 2

    result: list[tuple[Literal["user", "assistant"], str]] = []
    for user_text, assistant_text in reversed(selected_pairs):
        result.extend([("user", user_text), ("assistant", assistant_text)])
    return result
