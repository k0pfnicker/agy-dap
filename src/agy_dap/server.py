"""Main MCP Server for agy-dap.

Exposes hypothesis-driven Debug Adapter Protocol (DAP) inspection tools over MCP stdio.
"""

from __future__ import annotations

import asyncio
import atexit
import sys
from typing import Any, Callable, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer

from agy_dap.adapters.base import DebugAdapterBase
from agy_dap.config import PluginConfig, load_config
from agy_dap.errors import DapPluginError, ErrorCode, make_error_response
from agy_dap.sessions.manager import SessionManager
from agy_dap.tools import (
    debug_continue_tool,
    debug_doctor_tool,
    debug_evaluate_tool,
    debug_inspect_tool,
    debug_snapshot_tool,
    debug_start_tool,
    debug_stop_tool,
)
from agy_dap.utils.logging import get_logger

logger = get_logger("agy-dap.server")


def create_server(
    config: Optional[PluginConfig] = None,
    adapter_factory: Optional[Callable[[PluginConfig], DebugAdapterBase]] = None,
) -> tuple[MCPServer, SessionManager, PluginConfig]:
    """Create and configure the MCPServer instance and underlying SessionManager."""
    cfg = config or load_config()
    manager = SessionManager(cfg, adapter_factory=adapter_factory)

    app = MCPServer(
        name="agy-dap",
        version="1.0.0",
        description=(
            "Hypothesis-driven Debug Adapter Protocol (DAP) plugin for Antigravity CLI. "
            "Runs local pytest tests under debugpy to capture compact runtime snapshots at breakpoints or exceptions."
        ),
    )

    # 1. debug_doctor
    @app.tool(
        name="debug_doctor",
        description=(
            "Check workspace, selected Python interpreter, debugpy, pytest, profiles, and configuration. "
            "Returns availability and concrete remediation steps without modifying the environment."
        ),
    )
    async def debug_doctor(profile: str = "pytest") -> dict[str, Any]:
        """Verify debugging prerequisites before launching a session."""
        try:
            return await debug_doctor_tool(config=cfg, manager=manager, profile=profile)
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_doctor: {e}")
            return make_error_response(ErrorCode.INVALID_CONFIG, f"Diagnostic check failed: {e}")

    # 2. debug_inspect
    @app.tool(
        name="debug_inspect",
        description=(
            "Primary one-shot debugging investigation tool. Launches a pytest test under debugpy, "
            "configures a line breakpoint, waits for the first stop (breakpoint or exception), "
            "returns a compact runtime snapshot (stack, filtered locals, output tail), and reliably cleans up."
        ),
    )
    async def debug_inspect(
        test_id: str,
        breakpoint: Optional[dict[str, Any]] = None,
        breakpoints: Optional[list[dict[str, Any]]] = None,
        profile: str = "pytest",
        locals: Optional[list[str]] = None,
        timeout_seconds: float = 30.0,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Run a single-shot hypothesis inspection at a breakpoint and clean up automatically."""
        try:
            return await debug_inspect_tool(
                manager=manager,
                test_id=test_id,
                breakpoint=breakpoint,
                breakpoints=breakpoints,
                profile=profile,
                locals=locals,
                timeout_seconds=timeout_seconds,
                include_snippet=include_snippet,
            )
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_inspect: {e}")
            return make_error_response(ErrorCode.ADAPTER_CRASHED, f"Inspection failed: {e}")

    # 3. debug_start
    @app.tool(
        name="debug_start",
        description=(
            "Start a stateful debugging session for multi-step investigation. Launches the test under debugpy, "
            "stops at the first breakpoint or exception, returns the session_id and initial snapshot, "
            "and keeps the session paused for follow-up debug_snapshot, debug_continue, or debug_stop calls."
        ),
    )
    async def debug_start(
        test_id: str,
        breakpoint: Optional[dict[str, Any]] = None,
        breakpoints: Optional[list[dict[str, Any]]] = None,
        profile: str = "pytest",
        locals: Optional[list[str]] = None,
        timeout_seconds: float = 30.0,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Start an interactive debug session and pause at the first stop."""
        try:
            return await debug_start_tool(
                manager=manager,
                test_id=test_id,
                breakpoint=breakpoint,
                breakpoints=breakpoints,
                profile=profile,
                locals=locals,
                timeout_seconds=timeout_seconds,
                include_snippet=include_snippet,
            )
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_start: {e}")
            return make_error_response(ErrorCode.ADAPTER_CRASHED, f"Session start failed: {e}")

    # 4. debug_snapshot
    @app.tool(
        name="debug_snapshot",
        description=(
            "Retrieve a compact stack and variable snapshot for a currently STOPPED session. "
            "Optionally specify thread_id (defaults to the stopped thread), frame_id, or locals filter."
        ),
    )
    async def debug_snapshot(
        session_id: str,
        thread_id: Optional[int] = None,
        frame_id: Optional[int] = None,
        locals: Optional[list[str]] = None,
        snapshot_id: Optional[str] = None,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Inspect stack and local variables in a stopped session."""
        try:
            return await debug_snapshot_tool(
                manager=manager,
                session_id=session_id,
                thread_id=thread_id,
                frame_id=frame_id,
                locals=locals,
                snapshot_id=snapshot_id,
                include_snippet=include_snippet,
            )
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_snapshot: {e}")
            return make_error_response(ErrorCode.PROTOCOL_ERROR, f"Snapshot failed: {e}")

    # 5. debug_continue
    @app.tool(
        name="debug_continue",
        description=(
            "Execute a control-flow action on a STOPPED session: 'continue', 'next' (step over), "
            "'step_in', or 'step_out'. Waits for the next stop or program exit and returns a compact snapshot."
        ),
    )
    async def debug_continue(
        session_id: str,
        action: str = "continue",
        thread_id: Optional[int] = None,
        locals: Optional[list[str]] = None,
        timeout_seconds: Optional[float] = None,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Step or continue a paused session and wait for the next stop."""
        try:
            return await debug_continue_tool(
                manager=manager,
                session_id=session_id,
                action=action,
                thread_id=thread_id,
                locals=locals,
                timeout_seconds=timeout_seconds,
                include_snippet=include_snippet,
            )
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_continue: {e}")
            return make_error_response(ErrorCode.ADAPTER_CRASHED, f"Continue/step failed: {e}")

    # 6. debug_evaluate
    @app.tool(
        name="debug_evaluate",
        description=(
            "Evaluate an expression in the context of a STOPPED frame. "
            "Disabled by default for security; requires server-side 'enableEvaluation: true' in agy-dap.json."
        ),
    )
    async def debug_evaluate(
        session_id: str,
        expression: str,
        frame_id: Optional[int] = None,
        snapshot_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Evaluate an opt-in watch expression in a paused frame."""
        try:
            return await debug_evaluate_tool(
                manager=manager,
                session_id=session_id,
                expression=expression,
                frame_id=frame_id,
                snapshot_id=snapshot_id,
            )
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_evaluate: {e}")
            return make_error_response(ErrorCode.PROTOCOL_ERROR, f"Evaluation failed: {e}")

    # 7. debug_stop
    @app.tool(
        name="debug_stop",
        description=(
            "Idempotently terminate a debugging session and its adapter/debuggee process tree, "
            "returning the verified cleanup state."
        ),
    )
    async def debug_stop(session_id: str) -> dict[str, Any]:
        """Stop a session and clean up all associated processes."""
        try:
            return await debug_stop_tool(manager=manager, session_id=session_id)
        except DapPluginError as e:
            return e.to_dict()
        except Exception as e:
            logger.error(f"Unexpected error in debug_stop: {e}")
            return make_error_response(ErrorCode.CLEANUP_FAILED, f"Stop failed: {e}")

    return app, manager, cfg


async def run_server_async(config: Optional[PluginConfig] = None) -> None:
    """Run the MCP server over stdio and guarantee session cleanup on exit."""
    app, manager, _ = create_server(config)

    def _sync_cleanup() -> None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(manager.shutdown_all())
        except Exception:
            pass

    atexit.register(_sync_cleanup)

    logger.info("Starting agy-dap MCP server over stdio...")
    try:
        await app.run_stdio_async()
    finally:
        await manager.shutdown_all()


def main() -> None:
    """CLI entry point for the agy-dap MCP server."""
    try:
        asyncio.run(run_server_async())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.critical(f"Fatal server failure: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
