"""Real end-to-end integration tests using debugpy and pytest against the sample fixture project."""

from __future__ import annotations

import importlib.util

import pytest

from agy_dap.config import PluginConfig
from agy_dap.errors import ErrorCode
from agy_dap.server import create_server
from agy_dap.utils.processes import is_pid_alive

pytestmark = pytest.mark.integration


def _require_debugpy() -> None:
    if importlib.util.find_spec("debugpy") is None:
        pytest.skip("debugpy is not installed in the current Python environment")


@pytest.mark.asyncio
async def test_real_debug_doctor(test_config: PluginConfig) -> None:
    """Verify debug_doctor reports healthy status when Python, debugpy, and pytest are present."""
    _require_debugpy()
    app, manager, _ = create_server(test_config)
    from agy_dap.tools.doctor import debug_doctor_tool

    res = await debug_doctor_tool(config=test_config, manager=manager)
    assert res["healthy"] is True
    assert res["checks"]["python"]["available"] is True
    assert res["checks"]["debugpy"]["available"] is True
    assert res["checks"]["pytest"]["available"] is True


@pytest.mark.asyncio
async def test_real_debug_inspect_failing_test_and_variables(test_config: PluginConfig) -> None:
    """Verify real debugpy launch hits confirmed breakpoint, captures exact variables, redacts secrets, and cleans up."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.tools.inspect import debug_inspect_tool

    snap = await debug_inspect_tool(
        manager=manager,
        profile="pytest",
        test_id="tests/test_checkout.py::test_discount",
        breakpoint={"file": "src/checkout.py", "line": 40},
        locals=[
            "subtotal",
            "discount",
            "running_total",
            "tax",
            "is_tax_exempt",
            "empty_memo",
            "coupon_code",
            "api_token",
            "large_description",
            "items_summary",
            "non_existent_var",
        ],
        timeout_seconds=25.0,
    )

    assert snap["state"] == "STOPPED"
    assert snap["stop_reason"] == "breakpoint"
    assert snap["outcome"] == "breakpoint_hit"
    assert snap["location"]["file"] == "src/checkout.py"
    assert snap["location"]["line"] == 40
    assert snap["location"]["function"] == "calculate_total"

    # Breakpoint verification check
    assert len(snap["breakpoints"]) == 1
    assert snap["breakpoints"][0]["verified"] is True
    assert snap["breakpoints"][0]["actual_line"] == 40

    # Variable accuracy
    locals_map = snap["locals"]
    assert locals_map["subtotal"]["value"] == 100.0
    assert locals_map["discount"]["value"] == 15.0
    # Captures the intentional bug (100.0 + 15.0 = 115.0 instead of 85.0)
    assert locals_map["running_total"]["value"] == 115.0

    # Falsy & null values preserved
    assert locals_map["tax"]["value"] == 0
    assert locals_map["tax"]["status"] == "ok"
    assert locals_map["is_tax_exempt"]["value"] is False
    assert locals_map["is_tax_exempt"]["status"] == "ok"
    assert locals_map["empty_memo"]["value"] == ""
    assert locals_map["empty_memo"]["status"] == "ok"
    assert locals_map["coupon_code"]["value"] is None
    assert locals_map["coupon_code"]["status"] == "null"

    # Secret redaction
    assert locals_map["api_token"]["status"] == "redacted"
    assert locals_map["api_token"]["value"] == "[REDACTED]"

    # Truncation limits on strings and containers
    assert locals_map["large_description"].get("truncated") is True
    assert locals_map["items_summary"].get("truncated") is True

    # Missing variable explicitly marked
    assert locals_map["non_existent_var"]["status"] == "missing"

    # Output deduplication
    assert any("checkout-audit-step: validating cart line (repeated 4x)" in line for line in snap["output_tail"])

    # Cleanup verification
    assert snap["session_terminated"] is True
    assert snap["cleanup"]["clean"] is True
    assert manager.active_session_ids == []


@pytest.mark.asyncio
async def test_real_interactive_session_stepping_and_continue(test_config: PluginConfig) -> None:
    """Verify debug_start, step over (next), step_in, debug_snapshot, continue, and debug_stop."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.tools.sessions import (
        debug_continue_tool,
        debug_snapshot_tool,
        debug_start_tool,
        debug_stop_tool,
    )

    # 1. Start at line 39 (running_total = ...)
    s1 = await debug_start_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_passing_zero_discount",
        breakpoint={"file": "src/checkout.py", "line": 39},
        timeout_seconds=25.0,
    )
    sid = s1["session_id"]
    try:
        assert s1["state"] == "STOPPED"
        assert s1["location"]["line"] == 39

        # 2. Step over ('next') to line 40 (final_total = apply_loyalty_bonus(...))
        s2 = await debug_continue_tool(
            manager=manager,
            session_id=sid,
            action="next",
            locals=["running_total"],
            timeout_seconds=15.0,
        )
        assert s2["state"] == "STOPPED"
        assert s2["location"]["line"] == 40
        assert s2["locals"]["running_total"]["value"] == 50.0

        # 3. Step in ('step_in') into apply_loyalty_bonus
        s3 = await debug_continue_tool(
            manager=manager,
            session_id=sid,
            action="step_in",
            timeout_seconds=15.0,
        )
        assert s3["state"] == "STOPPED"
        assert s3["location"]["function"] == "apply_loyalty_bonus"

        # 4. Take an explicit snapshot with locals filter inside apply_loyalty_bonus
        s4 = await debug_snapshot_tool(
            manager=manager,
            session_id=sid,
            locals=["amount", "tier"],
        )
        assert s4["locals"]["amount"]["value"] == 50.0
        assert s4["locals"]["tier"]["value"] == "gold"

        # 5. Continue to completion
        s5 = await debug_continue_tool(
            manager=manager,
            session_id=sid,
            action="continue",
            timeout_seconds=15.0,
        )
        assert s5["state"] == "TERMINATED"
        assert s5["exit_code"] == 0
    finally:
        stop_res = await debug_stop_tool(manager=manager, session_id=sid)
        assert stop_res["state"] == "TERMINATED"


@pytest.mark.asyncio
async def test_real_target_not_reached(test_config: PluginConfig) -> None:
    """Verify TARGET_NOT_REACHED when test completes without executing the breakpoint line."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.errors import DapPluginError
    from agy_dap.tools.inspect import debug_inspect_tool

    with pytest.raises(DapPluginError) as exc_info:
        await debug_inspect_tool(
            manager=manager,
            test_id="tests/test_checkout.py::test_unreached_breakpoint",
            breakpoint={"file": "src/checkout.py", "line": 48},
            timeout_seconds=20.0,
        )
    assert exc_info.value.code == ErrorCode.TARGET_NOT_REACHED
    assert exc_info.value.details.get("exit_code") == 0
    assert manager.active_session_ids == []


@pytest.mark.asyncio
async def test_real_exception_stop_before_target(test_config: PluginConfig) -> None:
    """Verify exception raised before target breakpoint stops at exception when stopOnRaisedException is enabled."""
    _require_debugpy()
    test_config.profiles["pytest"].stopOnRaisedException = True
    _, manager, _ = create_server(test_config)
    from agy_dap.tools.inspect import debug_inspect_tool

    snap = await debug_inspect_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_exception_before_target",
        breakpoint={"file": "src/checkout.py", "line": 59},
        locals=["value", "step_marker"],
        timeout_seconds=25.0,
    )
    assert snap["state"] == "STOPPED"
    assert snap["stop_reason"] == "exception"
    assert snap["outcome"] == "exception_stopped"
    assert snap["location"]["line"] == 57
    assert snap["locals"]["value"]["value"] == -5
    assert snap["locals"]["step_marker"]["value"] == "before_validation"
    assert snap["session_terminated"] is True


@pytest.mark.asyncio
async def test_real_timeout_and_zero_leftover_processes(test_config: PluginConfig) -> None:
    """Verify a hung test times out cleanly and kills both adapter and debuggee processes."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.errors import DapPluginError
    from agy_dap.models import BreakpointSpec

    session = await manager.create_session("pytest", "tests/test_checkout.py::test_long_running_timeout")
    with pytest.raises(DapPluginError) as exc_info:
        await session.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=70)],
            timeout_seconds=2.0,
        )

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert session.state.value == "TERMINATED"
    assert session._cleanup_result is not None
    adapter_pid = session._cleanup_result.get("adapter_pid")
    debuggee_pid = session._cleanup_result.get("debuggee_pid")
    assert not is_pid_alive(adapter_pid)
    assert not is_pid_alive(debuggee_pid)
    assert manager.active_session_ids == []


@pytest.mark.asyncio
async def test_real_unverified_breakpoint_beyond_eof(test_config: PluginConfig) -> None:
    """Verify debugpy rejects a breakpoint placed beyond the end of file with BREAKPOINT_UNVERIFIED."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.errors import DapPluginError
    from agy_dap.tools.inspect import debug_inspect_tool

    with pytest.raises(DapPluginError) as exc_info:
        await debug_inspect_tool(
            manager=manager,
            test_id="tests/test_checkout.py::test_discount",
            breakpoint={"file": "src/checkout.py", "line": 9999},
            timeout_seconds=20.0,
        )
    assert exc_info.value.code == ErrorCode.BREAKPOINT_UNVERIFIED
    assert manager.active_session_ids == []


@pytest.mark.asyncio
async def test_real_breakpoint_shift_from_comment_line_is_visible(test_config: PluginConfig) -> None:
    """Verify requesting a breakpoint on a comment line (38) shifts visibly to the executable line (39)."""
    _require_debugpy()
    _, manager, _ = create_server(test_config)
    from agy_dap.tools.inspect import debug_inspect_tool

    snap = await debug_inspect_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_discount",
        breakpoint={"file": "src/checkout.py", "line": 38},
        locals=["subtotal", "discount"],
        timeout_seconds=25.0,
    )
    assert snap["state"] == "STOPPED"
    bp = snap["breakpoints"][0]
    assert bp["requested_line"] == 38
    assert bp["actual_line"] != 38
    assert bp["verified"] is True
    assert snap["location"]["line"] == bp["actual_line"]
