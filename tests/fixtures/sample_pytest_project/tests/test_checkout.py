"""Fixture test suite covering failing logic, passing test, unreached breakpoint, early exception, and timeout."""

from __future__ import annotations

from checkout import (
    UnreachedBranchHelper,
    calculate_total,
    long_running_task,
    process_with_early_exception,
)


def test_discount() -> None:
    """Deterministic failing logic test with known local variables."""
    total = calculate_total(subtotal=100.0, discount_rate=0.15, customer=None)
    assert total == 85.0


def test_passing_zero_discount() -> None:
    """Normal passing test."""
    total = calculate_total(subtotal=50.0, discount_rate=0.0, customer="gold")
    assert total == 45.0


def test_unreached_breakpoint() -> None:
    """Test that finishes cleanly without executing the True branch in UnreachedBranchHelper."""
    res = UnreachedBranchHelper(False)
    assert res == "skipped"


def test_exception_before_target() -> None:
    """Test that raises an unhandled ValueError before reaching the target line."""
    res = process_with_early_exception(-5)
    assert res == 0


def test_long_running_timeout() -> None:
    """Long-running test used to verify bounded timeout and process-tree cleanup."""
    ticks = long_running_task(30.0)
    assert ticks > 0
