"""Sample checkout module for deterministic debugging fixture tests."""

from __future__ import annotations

import time
from typing import Optional


def apply_loyalty_bonus(amount: float, tier: Optional[str]) -> float:
    """Helper function to test step_in / step_out and multi-frame stack inspection."""
    bonus = 5.0 if tier == "gold" else 0.0
    adjusted = amount - bonus
    return adjusted


def calculate_total(
    subtotal: float,
    discount_rate: float,
    customer: Optional[str] = None,
    api_token: str = "sk-live-secret-token-1234567890abcdef",
) -> float:
    """Calculate final checkout total.

    Contains an intentional logic bug: adds discount instead of subtracting it.
    Line numbers are kept stable for breakpoint verification tests.
    """
    discount = round(subtotal * discount_rate, 2)
    tax = 0
    is_tax_exempt = False
    empty_memo = ""
    coupon_code = None
    large_description = "X" * 400
    items_summary = {f"item_{i}": i * 10 for i in range(15)}

    for _ in range(4):
        print("checkout-audit-step: validating cart line")

    # Intentional bug on the next line: adds discount instead of subtracting
    running_total = round(subtotal + discount, 2)
    final_total = apply_loyalty_bonus(running_total, customer)
    _ = (tax, is_tax_exempt, empty_memo, coupon_code, large_description, items_summary, api_token)
    return final_total


def UnreachedBranchHelper(flag: bool) -> str:
    """Function with a branch that is not reached when flag is False."""
    if flag:
        secret_branch_value = "reached"
        return secret_branch_value
    return "skipped"


def process_with_early_exception(value: int) -> int:
    """Raises a ValueError before reaching the post-validation calculation."""
    step_marker = "before_validation"
    if value < 0:
        raise ValueError(f"Negative value not allowed: {value} ({step_marker})")
    # Target breakpoint line below is never reached when value < 0
    computed = value * 2
    return computed


def long_running_task(duration_seconds: float = 30.0) -> int:
    """Simulates a hung or slow computation before reaching the final line."""
    ticks = 0
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
        ticks += 1
    return ticks


if __name__ == "__main__":
    calculate_total(subtotal=100.0, discount_rate=0.15, customer="gold")
