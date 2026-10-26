"""Implementation of the one-shot 'debug_inspect' MCP tool and breakpoint parsing/resolution."""

from __future__ import annotations

from typing import Any, Optional

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.models import BreakpointSpec
from agy_dap.sessions.manager import SessionManager
from agy_dap.symbols import CompositeSymbolResolver, SymbolResolver
from agy_dap.utils.paths import find_workspace_root


def parse_breakpoint_input(
    breakpoint_arg: Optional[dict[str, Any]] = None,
    breakpoints_arg: Optional[list[dict[str, Any]]] = None,
) -> list[BreakpointSpec]:
    """Validate and normalize single or multiple breakpoint dicts into BreakpointSpec objects.

    Each breakpoint may specify either:
    - `{"file": "src/checkout.py", "line": 40}`
    - `{"file": "src/checkout.py", "symbol": "calculate_total", "line_offset": 0}`
    """
    raw_list: list[dict[str, Any]] = []
    if breakpoint_arg is not None:
        if not isinstance(breakpoint_arg, dict):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="'breakpoint' must be an object with 'file' and 'line' (or 'symbol').",
                retryable=False,
            )
        raw_list.append(breakpoint_arg)

    if breakpoints_arg is not None:
        if not isinstance(breakpoints_arg, list) or not all(isinstance(x, dict) for x in breakpoints_arg):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="'breakpoints' must be a list of objects with 'file' and 'line' (or 'symbol').",
                retryable=False,
            )
        raw_list.extend(breakpoints_arg)

    if not raw_list:
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message="At least one breakpoint ({'file': ..., 'line': ...} or {'file': ..., 'symbol': ...}) is required.",
            retryable=False,
        )

    specs: list[BreakpointSpec] = []
    for item in raw_list:
        file_val = item.get("file")
        line_val = item.get("line")
        sym_val = item.get("symbol")
        offset_val = item.get("line_offset", 0)
        cond_val = item.get("condition")

        if (not file_val or not isinstance(file_val, str)) and not (sym_val and isinstance(sym_val, str)):
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Breakpoint 'file' must be a non-empty string.",
                retryable=False,
                details={"breakpoint": item},
            )

        if sym_val is not None:
            if not isinstance(sym_val, str) or not sym_val.strip():
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message="Breakpoint 'symbol' must be a non-empty string if provided.",
                    retryable=False,
                    details={"breakpoint": item},
                )
            if not isinstance(offset_val, int) or isinstance(offset_val, bool) or offset_val < 0:
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Breakpoint 'line_offset' must be an integer >= 0, got {offset_val!r}.",
                    retryable=False,
                    details={"breakpoint": item},
                )

        if line_val is None and not sym_val:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Breakpoint must specify either 'line' (integer >= 1) or 'symbol' (string).",
                retryable=False,
                details={"breakpoint": item},
            )

        if line_val is not None and (not isinstance(line_val, int) or isinstance(line_val, bool) or line_val < 1):
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

        specs.append(
            BreakpointSpec(
                file=str(file_val or ""),
                line=int(line_val) if line_val is not None else 0,
                condition=cond_val,
                symbol=sym_val.strip() if isinstance(sym_val, str) else None,
                line_offset=int(offset_val) if sym_val is not None else 0,
            )
        )

    return specs


async def resolve_breakpoint_specs(
    specs: list[BreakpointSpec],
    resolver: SymbolResolver,
    workspace_root: str,
) -> list[BreakpointSpec]:
    """Resolve any symbol-based BreakpointSpec entries into concrete file + 1-indexed line numbers."""
    resolved_list: list[BreakpointSpec] = []
    for spec in specs:
        if spec.line >= 1 and not spec.symbol:
            resolved_list.append(spec)
            continue

        assert spec.symbol is not None
        loc = await resolver.resolve_symbol(spec.symbol, context_file=spec.file or None)
        if loc is None:
            raise DapPluginError(
                code=ErrorCode.BREAKPOINT_UNVERIFIED,
                message=(
                    f"Could not resolve symbol '{spec.symbol}'"
                    + (f" in '{spec.file}'." if spec.file else " in workspace.")
                ),
                retryable=False,
                details={"symbol": spec.symbol, "file": spec.file, "workspace_root": workspace_root},
            )

        target_line = loc.line + spec.line_offset
        resolved_list.append(
            BreakpointSpec(
                file=spec.file or loc.file,
                line=target_line,
                condition=spec.condition,
                symbol=spec.symbol,
                line_offset=spec.line_offset,
                resolved_symbol=loc.symbol,
                symbol_provider=loc.provider,
            )
        )
    return resolved_list


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
