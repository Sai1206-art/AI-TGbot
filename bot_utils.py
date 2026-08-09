from __future__ import annotations

import os
from typing import Any


IMAGE_EDIT_MODE = "image_edit"
VIDEO_MODE = "video"


def mode_cost(mode: str) -> int:
    variable = "VIDEO_COST" if mode == VIDEO_MODE else "IMAGE_EDIT_COST"
    default = "3" if mode == VIDEO_MODE else "1"
    return max(1, int(os.getenv(variable, default)))


def remaining_generations(balance: dict[str, Any]) -> int:
    free_available = balance.get("free_available")
    if free_available is None:
        free_available = not bool(balance.get("free_credit_used", 0))
    return max(0, int(balance.get("balance", 0))) + int(bool(free_available))


def format_balance_summary(balance: dict[str, Any], mode: str | None = None) -> str:
    paid_tokens = max(0, int(balance.get("balance", 0)))
    free_available = balance.get("free_available")
    if free_available is None:
        free_available = not bool(balance.get("free_credit_used", 0))
    free_text = "available" if free_available else "used"
    if mode == VIDEO_MODE:
        cost = mode_cost(mode)
        affordable = paid_tokens // cost
        return f"You have {paid_tokens} paid token(s) remaining ({affordable} video generation(s) at {cost} tokens each)."
    if mode == IMAGE_EDIT_MODE:
        total = paid_tokens + int(bool(free_available))
        return f"You have {total} Image Edit generation(s) remaining ({paid_tokens} paid token(s); free generation {free_text})."
    return f"You have {paid_tokens} paid token(s) remaining; free Image Edit generation {free_text}."


def mode_instruction(mode: str) -> str:
    if mode == "image_edit":
        return f"For Image Edit, write your edit prompt in the photo caption. Cost: {mode_cost(mode)} token."
    return f"For Video, send the photo without a caption; the configured video prompt is used. Cost: {mode_cost(mode)} paid tokens per video."
