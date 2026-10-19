"""Unit tests for DAP protocol client correlation, race conditions, crashes, and reverse requests."""

from __future__ import annotations

import pytest

from agy_dap.config import PluginConfig
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.models import BreakpointSpec
from agy_dap.sessions.manager import SessionManager
from tests.conftest import FakeSubprocessAdapter


@pytest.mark.asyncio
async def test_early_stop_event_before_launch_response_not_lost(test_config: PluginConfig) -> None:
    """Verify a stopped event arriving before the launch response is captured without deadlock."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="early_stop_before_launch_response"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    try:
        snap = await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=5.0,
        )
        assert snap["state"] == "STOPPED"
        assert snap["stop_reason"] == "breakpoint"
    finally:
        await sess.cleanup()


@pytest.mark.asyncio
async def test_reverse_request_run_in_terminal_is_rejected_and_session_proceeds(test_config: PluginConfig) -> None:
    """Verify adapter reverse requests (runInTerminal) are rejected safely without executing commands."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="reverse_run_in_terminal"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    try:
        snap = await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=5.0,
        )
        assert snap["state"] == "STOPPED"
        # Also verify duplicate output lines were folded
        assert any("repeated 3x" in line for line in snap["output_tail"])
    finally:
        await sess.cleanup()


@pytest.mark.asyncio
async def test_adapter_crash_during_initialize_resolves_pending_request(test_config: PluginConfig) -> None:
    """Verify adapter crashing mid-request raises ADAPTER_CRASHED and cleans up."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="crash_on_init"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    with pytest.raises(DapPluginError) as exc_info:
        await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=5.0,
        )
    assert exc_info.value.code == ErrorCode.ADAPTER_CRASHED
    assert sess.state.value == "TERMINATED"


@pytest.mark.asyncio
async def test_unverified_breakpoint_raises_breakpoint_unverified(test_config: PluginConfig) -> None:
    """Verify unverified breakpoints raise BREAKPOINT_UNVERIFIED with adapter details."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="unverified_bp"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    with pytest.raises(DapPluginError) as exc_info:
        await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=5.0,
        )
    assert exc_info.value.code == ErrorCode.BREAKPOINT_UNVERIFIED
    assert sess.state.value == "TERMINATED"


@pytest.mark.asyncio
async def test_unsupported_conditional_breakpoint_capability(test_config: PluginConfig) -> None:
    """Verify requesting a conditional breakpoint when adapter lacks capability raises CAPABILITY_UNSUPPORTED."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="no_conditional_bp"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    with pytest.raises(DapPluginError) as exc_info:
        await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39, condition="subtotal > 50")],
            timeout_seconds=5.0,
        )
    assert exc_info.value.code == ErrorCode.CAPABILITY_UNSUPPORTED
    assert sess.state.value == "TERMINATED"


@pytest.mark.asyncio
async def test_timeout_waiting_for_stop_cleans_up(test_config: PluginConfig) -> None:
    """Verify timeout waiting for stop raises TIMEOUT and terminates adapter process."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="hang_after_launch"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    with pytest.raises(DapPluginError) as exc_info:
        await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=0.4,
        )
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert sess.state.value == "TERMINATED"


@pytest.mark.asyncio
async def test_partial_startup_failure_on_launch_cleans_up_adapter_process(test_config: PluginConfig) -> None:
    """Verify cleanup is executed when launch fails after adapter process has already started."""
    from agy_dap.utils.processes import is_pid_alive

    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="fail_launch"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    with pytest.raises(DapPluginError) as exc_info:
        await sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=5.0,
        )
    assert exc_info.value.code == ErrorCode.PROTOCOL_ERROR
    assert sess.state.value == "TERMINATED"
    assert sess._cleanup_result is not None
    assert not is_pid_alive(sess._cleanup_result.get("adapter_pid"))


@pytest.mark.asyncio
async def test_request_cancellation_cleans_up_pending_map(test_config: PluginConfig) -> None:
    """Verify cancelling an in-flight DAP request removes its pending future cleanly."""
    import asyncio

    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="hang_on_init"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    task = asyncio.create_task(
        sess.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=39)],
            timeout_seconds=10.0,
        )
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sess.cleanup(reason="cancelled_test")
    assert sess.state.value == "TERMINATED"
