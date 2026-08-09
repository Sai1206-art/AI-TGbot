from __future__ import annotations

from typing import Any


def remaining_generations(balance: dict[str, Any]) -> int:
    free_available = balance.get("free_available")
    if free_available is None:
        free_available = not bool(balance.get("free_credit_used", 0))
    return max(0, int(balance.get("balance", 0))) + int(bool(free_available))


def format_balance_summary(balance: dict[str, Any]) -> str:
    paid_tokens = max(0, int(balance.get("balance", 0)))
    free_available = balance.get("free_available")
    if free_available is None:
        free_available = not bool(balance.get("free_credit_used", 0))
    free_text = "available" if free_available else "used"
    total = remaining_generations(balance)
    return (
        f"You have {total} generation(s) remaining "
        f"({paid_tokens} paid token(s); free generation {free_text})."
    )


def mode_instruction(mode: str) -> str:
    if mode == "image_edit":
        return "For Image Edit, write your edit prompt in the photo caption."
    return "For Video, send the photo without a caption; the configured video prompt is used."
