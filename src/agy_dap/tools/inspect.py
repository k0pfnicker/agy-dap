"""Implementation of the one-shot 'debug_inspect' MCP tool."""

from __future__ import annotations

from typing import Any, Optional

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.models import BreakpointSpec
from agy_dap.sessions.manager import SessionManager


def parse_breakpoint_input(
    breakpoint_arg: Optional[dict[str, Any]] = None,
    breakpoints_arg: Optional[list[dict[str, Any]]] = None,
) -> list[BreakpointSpec]:
    """Validate and normalize single or multiple breakpoint dicts into BreakpointSpec objects."""
    raw_list: list[dict[str, Any]] = []
    if breakpoint_arg is not None:
        if not isinstance(breakpoint_arg, dict):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="'breakpoint' must be an object with 'file' and 'line'.",
                retryable=False,
            )
        raw_list.append(breakpoint_arg)

    if breakpoints_arg is not None:
        if not isinstance(breakpoints_arg, list) or not all(isinstance(x, dict) for x in breakpoints_arg):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="'breakpoints' must be a list of objects with 'file' and 'line'.",
                retryable=False,
            )
        raw_list.extend(breakpoints_arg)

    if not raw_list:
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message="At least one breakpoint ({'file': ..., 'line': ...}) is required.",
            retryable=False,
        )

    specs: list[BreakpointSpec] = []
    for item in raw_list:
        file_val = item.get("file")
        line_val = item.get("line")
        cond_val = item.get("condition")
        if not file_val or not isinstance(file_val, str):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Breakpoint 'file' must be a non-empty string.",
                retryable=False,
                details={"breakpoint": item},
            )
        if not isinstance(line_val, int) or isinstance(line_val, bool) or line_val < 1:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"Breakpoint 'line' must be an integer >= 1, got {line_val!r}.",
                retryable=False,
                details={"breakpoint": item},
            )
        if cond_val is not None and not isinstance(cond_val, str):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Breakpoint 'condition' must be a string if provided.",
                retryable=False,
                details={"breakpoint": item},
            )
        specs.append(BreakpointSpec(file=file_val, line=line_val, condition=cond_val))

    return specs


async def debug_inspect_tool(
    manager: SessionManager,
    test_id: str,
    breakpoint: Optional[dict[str, Any]] = None,
    breakpoints: Optional[list[dict[str, Any]]] = None,
    profile: str = "pytest",
    locals: Optional[list[str]] = None,
    timeout_seconds: Optional[float] = 30.0,
    include_snippet: Optional[bool] = None,
) -> dict[str, Any]:
    """Launch a test under debugpy, wait for the first breakpoint or exception, capture a compact snapshot, and clean up."""
    bp_specs = parse_breakpoint_input(breakpoint_arg=breakpoint, breakpoints_arg=breakpoints)
    if locals is not None and (not isinstance(locals, list) or not all(isinstance(x, str) for x in locals)):
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message="'locals' must be a list of variable name strings.",
            retryable=False,
        )

    session = await manager.create_session(profile_name=profile, test_id=test_id)
    try:
        snapshot = await session.start_and_wait_first_stop(
            breakpoints=bp_specs,
            requested_locals=locals,
            timeout_seconds=timeout_seconds,
            include_snippet=include_snippet,
        )
        cleanup_info = await session.cleanup(reason="inspect_complete")
        snapshot["session_terminated"] = True
        snapshot["cleanup"] = {
            "clean": cleanup_info.get("clean", True),
            "state": cleanup_info.get("state", "TERMINATED"),
        }
        return snapshot
    finally:
        # Ensure session is always cleaned up even if an exception occurred
        if session.state.value != "TERMINATED":
            try:
                await session.cleanup(reason="inspect_finally")
            except Exception:
                pass
