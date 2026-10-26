"""Stateful interactive session tools: debug_start, debug_snapshot, debug_continue, debug_evaluate, debug_stop."""

from __future__ import annotations

from typing import Any, Optional

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.sessions.manager import SessionManager
from agy_dap.symbols import CompositeSymbolResolver
from agy_dap.tools.inspect import parse_breakpoint_input, resolve_breakpoint_specs
from agy_dap.utils.paths import find_workspace_root


async def debug_start_tool(
    manager: SessionManager,
    test_id: str,
    breakpoint: Optional[dict[str, Any]] = None,
    breakpoints: Optional[list[dict[str, Any]]] = None,
    profile: str = "pytest",
    locals: Optional[list[str]] = None,
    timeout_seconds: Optional[float] = 30.0,
    include_snippet: Optional[bool] = None,
) -> dict[str, Any]:
    """Start an interactive debugging session, wait for the first stop, and keep the session open for follow-up calls."""
    raw_specs = parse_breakpoint_input(breakpoint_arg=breakpoint, breakpoints_arg=breakpoints)
    if locals is not None and (not isinstance(locals, list) or not all(isinstance(x, str) for x in locals)):
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message="'locals' must be a list of variable name strings.",
            retryable=False,
        )

    ws_root = manager.config.workspaceRoot or find_workspace_root()
    resolver = CompositeSymbolResolver(
        workspace_root=ws_root,
        allow_outside=manager.config.allowOutsideWorkspace,
    )
    bp_specs = await resolve_breakpoint_specs(raw_specs, resolver=resolver, workspace_root=ws_root)

    session = await manager.create_session(profile_name=profile, test_id=test_id)
    try:
        snapshot = await session.start_and_wait_first_stop(
            breakpoints=bp_specs,
            requested_locals=locals,
            timeout_seconds=timeout_seconds,
            include_snippet=include_snippet,
        )
        session.start_idle_monitor()
        snapshot["session_terminated"] = False
        return snapshot
    except Exception:
        if session.state.value != "TERMINATED":
            try:
                await session.cleanup(reason="start_failed")
            except Exception:
                pass
        raise


async def debug_snapshot_tool(
    manager: SessionManager,
    session_id: str,
    thread_id: Optional[int] = None,
    frame_id: Optional[int] = None,
    locals: Optional[list[str]] = None,
    snapshot_id: Optional[str] = None,
    include_snippet: Optional[bool] = None,
) -> dict[str, Any]:
    """Retrieve a snapshot for a stopped session, optionally selecting thread_id, frame_id, or locals filter."""
    if locals is not None and (not isinstance(locals, list) or not all(isinstance(x, str) for x in locals)):
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message="'locals' must be a list of variable name strings.",
            retryable=False,
        )
    session = manager.get_session(session_id)
    return await session.get_snapshot(
        thread_id=thread_id,
        frame_id=frame_id,
        locals_filter=locals,
        snapshot_id=snapshot_id,
        include_snippet=include_snippet,
    )


async def debug_continue_tool(
    manager: SessionManager,
    session_id: str,
    action: str = "continue",
    thread_id: Optional[int] = None,
    locals: Optional[list[str]] = None,
    timeout_seconds: Optional[float] = None,
    include_snippet: Optional[bool] = None,
) -> dict[str, Any]:
    """Resume execution with 'continue', 'next', 'step_in', or 'step_out' and return the next stop snapshot or termination state."""
    session = manager.get_session(session_id)
    return await session.continue_or_step(
        action=action,
        thread_id=thread_id,
        locals_filter=locals,
        timeout_seconds=timeout_seconds,
        include_snippet=include_snippet,
    )


async def debug_evaluate_tool(
    manager: SessionManager,
    session_id: str,
    expression: str,
    frame_id: Optional[int] = None,
    snapshot_id: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate a watch expression in a stopped frame (disabled by default; enforced server-side)."""
    # Enforce server-side evaluation check even before looking up the session
    if not manager.config.enableEvaluation:
        raise DapPluginError(
            code=ErrorCode.EVALUATION_DISABLED,
            message=(
                "Expression evaluation ('debug_evaluate') is disabled by default for security. "
                "Use 'debug_snapshot' / 'locals' filter for safe variable inspection, or explicitly "
                "enable 'enableEvaluation: true' in agy-dap.json."
            ),
            retryable=False,
            details={"session_id": session_id},
        )
    session = manager.get_session(session_id)
    return await session.evaluate_expression(
        expression=expression,
        frame_id=frame_id,
        snapshot_id=snapshot_id,
    )


async def debug_stop_tool(
    manager: SessionManager,
    session_id: str,
) -> dict[str, Any]:
    """Idempotently stop a debugging session and report process cleanup status."""
    return await manager.stop_session(session_id)
