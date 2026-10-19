"""Session manager handling DAP session creation, step serialization, idle timeouts, and cleanup."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Callable, Optional

from agy_dap.adapters.base import DebugAdapterBase
from agy_dap.adapters.debugpy import DebugpyAdapter
from agy_dap.config import PluginConfig, ProfileConfig
from agy_dap.dap.client import DapClient
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.inspection.snapshots import build_stopped_snapshot
from agy_dap.models import BreakpointSpec
from agy_dap.sessions.lifecycle import LifecycleTracker, SessionState
from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import find_workspace_root
from agy_dap.utils.processes import terminate_adapter_and_debuggee
from agy_dap.utils.redaction import redact_value_if_needed

logger = get_logger("agy-dap.sessions")

STEP_COMMAND_MAP: dict[str, str] = {
    "continue": "continue",
    "next": "next",
    "step_in": "stepIn",
    "step_out": "stepOut",
}


class DebugSession:
    """Encapsulates a single debugging session, its subprocess, DAP client, and lifecycle."""

    def __init__(
        self,
        session_id: str,
        config: PluginConfig,
        profile_name: str,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
        adapter: DebugAdapterBase,
        on_terminated_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.profile_name = profile_name
        self.profile = profile
        self.test_id = test_id
        self.workspace_root = workspace_root
        self.adapter = adapter
        self._on_terminated_callback = on_terminated_callback

        self.lifecycle = LifecycleTracker(session_id)
        self.operation_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()

        self.proc: Optional[asyncio.subprocess.Process] = None
        self.client: Optional[DapClient] = None
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()
        self._idle_task: Optional[asyncio.Task[None]] = None
        self._cleanup_result: Optional[dict[str, Any]] = None
        self.requested_locals: Optional[list[str]] = None

    @property
    def state(self) -> SessionState:
        return self.lifecycle.state

    def touch(self) -> None:
        """Update activity timestamp to prevent idle timeout."""
        self.last_activity = time.monotonic()

    def start_idle_monitor(self) -> None:
        """Start background task to clean up session if idle beyond idleSessionTimeoutSec."""
        if self._idle_task is not None or self.config.timeouts.idleSessionTimeoutSec <= 0:
            return
        self._idle_task = asyncio.create_task(
            self._idle_watchdog_loop(),
            name=f"idle-watchdog-{self.session_id}",
        )

    async def _idle_watchdog_loop(self) -> None:
        timeout = self.config.timeouts.idleSessionTimeoutSec
        check_interval = min(1.0, max(0.05, timeout / 4.0))
        try:
            while self.state not in (SessionState.TERMINATING, SessionState.TERMINATED):
                await asyncio.sleep(check_interval)
                elapsed = time.monotonic() - self.last_activity
                if elapsed >= timeout and not self.operation_lock.locked():
                    logger.warning(f"Session {self.session_id} exceeded idle timeout ({timeout}s); cleaning up.")
                    await self.cleanup(reason="idle_timeout")
                    break
        except asyncio.CancelledError:
            pass

    async def start_and_wait_first_stop(
        self,
        breakpoints: list[BreakpointSpec],
        requested_locals: Optional[list[str]] = None,
        timeout_seconds: Optional[float] = None,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Launch adapter & debuggee, configure breakpoints, and wait for first stop or completion.

        Always cleans up automatically if startup/configuration fails or if the process terminates.
        """
        eff_timeout = self._resolve_timeout(timeout_seconds)
        self.requested_locals = requested_locals
        self.touch()

        async with self.operation_lock:
            try:
                self.lifecycle.transition_to(SessionState.STARTING)

                # Validate launch args before spawning adapter process
                launch_args = self.adapter.build_launch_args(
                    profile=self.profile,
                    test_id=self.test_id,
                    workspace_root=self.workspace_root,
                )

                self.proc = await self.adapter.spawn_adapter_process(self.profile)
                assert self.proc.stdin and self.proc.stdout

                self.client = DapClient(
                    reader=self.proc.stdout,
                    writer=self.proc.stdin,
                    config=self.config,
                    stderr=self.proc.stderr,
                )
                self.client.start_transport()

                await self.client.initialize(adapter_id=self.adapter.adapter_id)

                records = await self.client.launch_and_configure(
                    launch_args=launch_args,
                    breakpoint_specs=breakpoints,
                    workspace_root=self.workspace_root,
                    stop_on_uncaught=self.profile.stopOnUncaughtException,
                    stop_on_raised=self.profile.stopOnRaisedException,
                    on_configuring=lambda: self.lifecycle.transition_to(SessionState.CONFIGURING),
                )

                # Check if any requested breakpoint was immediately rejected as unverified
                unverified = [r for r in records if not r.verified]
                if unverified:
                    bp_details = [r.to_dict() for r in records]
                    first_unv = unverified[0]
                    msg = (
                        f"Breakpoint at {first_unv.requested_file}:{first_unv.requested_line} "
                        f"was not verified by the debug adapter."
                    )
                    if first_unv.message:
                        msg += f" ({first_unv.message})"
                    raise DapPluginError(
                        code=ErrorCode.BREAKPOINT_UNVERIFIED,
                        message=msg,
                        retryable=False,
                        details={"breakpoints": bp_details, "session_id": self.session_id},
                    )

                self.lifecycle.transition_to(SessionState.RUNNING)

                outcome, stop_info = await self.client.wait_for_stop_or_termination(timeout=eff_timeout)
                self.touch()

                if outcome == "stopped" and stop_info is not None:
                    self.lifecycle.transition_to(SessionState.STOPPED)
                    snapshot = await build_stopped_snapshot(
                        client=self.client,
                        config=self.config,
                        session_id=self.session_id,
                        snapshot_id=self.lifecycle.current_snapshot_id or "snap_1",
                        stop_info=stop_info,
                        workspace_root=self.workspace_root,
                        requested_locals=requested_locals,
                        include_snippet=include_snippet,
                    )
                    frame_ids = [f["frame_id"] for f in snapshot.get("stack", []) if "frame_id" in f]
                    self.lifecycle.register_active_frames(frame_ids)
                    snapshot["outcome"] = "exception_stopped" if stop_info.reason == "exception" else "breakpoint_hit"
                    return snapshot

                # Program finished without hitting the breakpoint
                output_tail, out_trunc = self.client.output_buffer.get_tail()
                exit_code = self.client.exit_code
                bp_list = [r.to_dict() for r in self.client.breakpoints]
                unverified_after = [r for r in self.client.breakpoints if not r.verified]
                await self.cleanup(reason="process_exited")

                if unverified_after:
                    first_unv = unverified_after[0]
                    msg = (
                        f"Breakpoint at {first_unv.requested_file}:{first_unv.requested_line} "
                        f"was not verified by the debug adapter."
                    )
                    if first_unv.message:
                        msg += f" ({first_unv.message})"
                    raise DapPluginError(
                        code=ErrorCode.BREAKPOINT_UNVERIFIED,
                        message=msg,
                        retryable=False,
                        details={
                            "session_id": self.session_id,
                            "state": SessionState.TERMINATED.value,
                            "exit_code": exit_code,
                            "breakpoints": bp_list,
                            "output_tail": output_tail,
                        },
                    )

                raise DapPluginError(
                    code=ErrorCode.TARGET_NOT_REACHED,
                    message=(
                        f"Test '{self.test_id}' finished (exit_code={exit_code}) "
                        "without hitting the requested breakpoint."
                    ),
                    retryable=False,
                    details={
                        "session_id": self.session_id,
                        "state": SessionState.TERMINATED.value,
                        "exit_code": exit_code,
                        "breakpoints": bp_list,
                        "output_tail": output_tail,
                        "truncated": out_trunc,
                    },
                )

            except DapPluginError as e:
                # Attach diagnostic tail if available before cleanup
                if self.client is not None and "output_tail" not in e.details:
                    tail, _ = self.client.output_buffer.get_tail()
                    if tail:
                        e.details["output_tail"] = tail
                    if self.client.breakpoints and "breakpoints" not in e.details:
                        e.details["breakpoints"] = [b.to_dict() for b in self.client.breakpoints]
                await self.cleanup(reason="startup_or_wait_error")
                raise
            except Exception as e:
                await self.cleanup(reason="unexpected_error")
                raise DapPluginError(
                    code=ErrorCode.ADAPTER_CRASHED,
                    message=f"Unexpected error during debug session launch: {e}",
                    retryable=False,
                    details={"session_id": self.session_id},
                ) from e

    async def get_snapshot(
        self,
        thread_id: Optional[int] = None,
        frame_id: Optional[int] = None,
        locals_filter: Optional[list[str]] = None,
        snapshot_id: Optional[str] = None,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Capture a snapshot for a currently STOPPED session."""
        self.touch()
        async with self.operation_lock:
            self.lifecycle.validate_references(snapshot_id=snapshot_id, frame_id=frame_id)
            assert self.client is not None and self.client.latest_stop is not None

            eff_locals = locals_filter if locals_filter is not None else self.requested_locals
            snapshot = await build_stopped_snapshot(
                client=self.client,
                config=self.config,
                session_id=self.session_id,
                snapshot_id=self.lifecycle.current_snapshot_id or "snap_1",
                stop_info=self.client.latest_stop,
                workspace_root=self.workspace_root,
                requested_locals=eff_locals,
                requested_thread_id=thread_id,
                requested_frame_id=frame_id,
                include_snippet=include_snippet,
            )
            frame_ids = [f["frame_id"] for f in snapshot.get("stack", []) if "frame_id" in f]
            self.lifecycle.register_active_frames(frame_ids)
            return snapshot

    async def continue_or_step(
        self,
        action: str = "continue",
        thread_id: Optional[int] = None,
        locals_filter: Optional[list[str]] = None,
        timeout_seconds: Optional[float] = None,
        include_snippet: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Execute continue, next, step_in, or step_out and wait for the next stop or termination."""
        if action not in STEP_COMMAND_MAP:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"Unsupported debug action '{action}'. Must be one of {sorted(STEP_COMMAND_MAP)}.",
                retryable=False,
                details={"action": action, "supported_actions": sorted(STEP_COMMAND_MAP)},
            )

        dap_cmd = STEP_COMMAND_MAP[action]
        eff_timeout = self._resolve_timeout(timeout_seconds)
        self.touch()

        async with self.operation_lock:
            self.lifecycle.require_stopped()
            assert self.client is not None

            active_thread_id = thread_id
            if active_thread_id is None:
                if self.client.latest_stop and self.client.latest_stop.thread_id is not None:
                    active_thread_id = self.client.latest_stop.thread_id
                else:
                    active_thread_id = 1

            self.client.drain_stop_queue()
            self.lifecycle.transition_to(SessionState.RUNNING)

            try:
                await self.client.send_request(dap_cmd, {"threadId": active_thread_id})
                outcome, stop_info = await self.client.wait_for_stop_or_termination(timeout=eff_timeout)
                self.touch()

                if outcome == "stopped" and stop_info is not None:
                    self.lifecycle.transition_to(SessionState.STOPPED)
                    eff_locals = locals_filter if locals_filter is not None else self.requested_locals
                    snapshot = await build_stopped_snapshot(
                        client=self.client,
                        config=self.config,
                        session_id=self.session_id,
                        snapshot_id=self.lifecycle.current_snapshot_id or "snap_1",
                        stop_info=stop_info,
                        workspace_root=self.workspace_root,
                        requested_locals=eff_locals,
                        include_snippet=include_snippet,
                    )
                    frame_ids = [f["frame_id"] for f in snapshot.get("stack", []) if "frame_id" in f]
                    self.lifecycle.register_active_frames(frame_ids)
                    snapshot["action"] = action
                    return snapshot

                # Session completed after continue/step
                output_tail, out_trunc = self.client.output_buffer.get_tail()
                exit_code = self.client.exit_code
                cleanup_info = await self.cleanup(reason="completed_after_continue")
                return {
                    "session_id": self.session_id,
                    "state": SessionState.TERMINATED.value,
                    "stop_reason": "terminated",
                    "action": action,
                    "exit_code": exit_code,
                    "output_tail": output_tail,
                    "truncated": out_trunc,
                    "cleanup": cleanup_info,
                }
            except DapPluginError:
                await self.cleanup(reason="continue_error")
                raise
            except Exception as e:
                await self.cleanup(reason="continue_unexpected_error")
                raise DapPluginError(
                    code=ErrorCode.ADAPTER_CRASHED,
                    message=f"Error during '{action}' execution: {e}",
                    retryable=False,
                    details={"session_id": self.session_id, "action": action},
                ) from e

    async def evaluate_expression(
        self,
        expression: str,
        frame_id: Optional[int] = None,
        snapshot_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Evaluate an expression in a stopped frame (only when server-side enableEvaluation=True)."""
        if not self.config.enableEvaluation:
            raise DapPluginError(
                code=ErrorCode.EVALUATION_DISABLED,
                message=(
                    "Expression evaluation ('debug_evaluate') is disabled by default for security. "
                    "Use 'debug_snapshot' / 'locals' filter for safe variable inspection, or explicitly "
                    "enable 'enableEvaluation: true' in agy-dap.json."
                ),
                retryable=False,
                details={"session_id": self.session_id},
            )

        if not expression or not expression.strip():
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Expression must not be empty.",
                retryable=False,
            )

        self.touch()
        async with self.operation_lock:
            self.lifecycle.validate_references(snapshot_id=snapshot_id, frame_id=frame_id)
            assert self.client is not None

            target_frame_id = frame_id
            if target_frame_id is None and self.lifecycle.current_frame_ids:
                target_frame_id = next(iter(self.lifecycle.current_frame_ids))

            args: dict[str, Any] = {
                "expression": expression,
                "context": "watch",
            }
            if target_frame_id is not None:
                args["frameId"] = target_frame_id

            resp = await self.client.send_request("evaluate", args)
            raw_result = resp.get("result")
            var_type = resp.get("type")
            redacted_val, was_redacted = redact_value_if_needed(expression, raw_result)
            val_str = str(redacted_val)
            truncated = False
            if len(val_str) > self.config.limits.maxStringLength:
                val_str = val_str[: self.config.limits.maxStringLength] + "... [truncated]"
                truncated = True

            return {
                "session_id": self.session_id,
                "snapshot_id": self.lifecycle.current_snapshot_id,
                "frame_id": target_frame_id,
                "expression": expression,
                "result": val_str,
                "type": var_type,
                "status": "redacted" if was_redacted else "ok",
                "truncated": truncated,
            }

    async def cleanup(self, reason: str = "explicit_stop") -> dict[str, Any]:
        """Idempotently terminate debuggee and adapter processes and transition to TERMINATED."""
        async with self._cleanup_lock:
            if self._cleanup_result is not None and self.state == SessionState.TERMINATED:
                return self._cleanup_result

            if self._idle_task and not self._idle_task.done() and self._idle_task is not asyncio.current_task():
                self._idle_task.cancel()

            if self.state != SessionState.TERMINATED:
                try:
                    self.lifecycle.transition_to(SessionState.TERMINATING)
                except DapPluginError:
                    self.lifecycle.state = SessionState.TERMINATING

            debuggee_pid = self.client.debuggee_pid if self.client else None

            if self.client is not None:
                try:
                    await self.client.disconnect_gracefully(timeout=1.0)
                except Exception:
                    pass

            term_status = await terminate_adapter_and_debuggee(
                proc=self.proc,
                debuggee_pid=debuggee_pid,
                timeout_sec=self.config.timeouts.cleanupTimeoutSec,
            )

            self.lifecycle.state = SessionState.TERMINATED
            self._cleanup_result = {
                "session_id": self.session_id,
                "state": SessionState.TERMINATED.value,
                "reason": reason,
                **term_status,
            }

            if self._on_terminated_callback:
                try:
                    self._on_terminated_callback(self.session_id)
                except Exception:
                    pass

            if not term_status.get("clean", True):
                raise DapPluginError(
                    code=ErrorCode.CLEANUP_FAILED,
                    message=f"Cleanup for session '{self.session_id}' could not confirm termination of all processes.",
                    retryable=False,
                    details=self._cleanup_result,
                )

            return self._cleanup_result

    def _resolve_timeout(self, requested: Optional[float]) -> float:
        if requested is None:
            return self.config.timeouts.defaultInspectTimeoutSec
        if requested <= 0:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"timeout_seconds must be > 0, got {requested}.",
                retryable=False,
            )
        return min(float(requested), self.config.timeouts.maxInspectTimeoutSec)


class SessionManager:
    """Manages active DebugSessions, enforces concurrency limits, and guarantees cleanup."""

    def __init__(
        self,
        config: PluginConfig,
        adapter_factory: Optional[Callable[[PluginConfig], DebugAdapterBase]] = None,
    ) -> None:
        self.config = config
        self._adapter_factory = adapter_factory or DebugpyAdapter
        self._active_sessions: dict[str, DebugSession] = {}
        self._terminated_cleanup_records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @property
    def active_session_ids(self) -> list[str]:
        return [
            sid
            for sid, sess in self._active_sessions.items()
            if sess.state not in (SessionState.TERMINATING, SessionState.TERMINATED)
        ]

    async def create_session(
        self,
        profile_name: str,
        test_id: str,
    ) -> DebugSession:
        """Validate profile and concurrency limit, then create a new DebugSession."""
        async with self._lock:
            # Prune any sessions that already reached TERMINATED
            for sid, sess in list(self._active_sessions.items()):
                if sess.state == SessionState.TERMINATED:
                    self._active_sessions.pop(sid, None)
                    if sess._cleanup_result:
                        self._terminated_cleanup_records[sid] = sess._cleanup_result

            if len(self._active_sessions) >= self.config.maxConcurrentSessions:
                active_ids = list(self._active_sessions.keys())
                raise DapPluginError(
                    code=ErrorCode.CONCURRENT_SESSION_LIMIT,
                    message=(
                        f"Maximum concurrent debug sessions ({self.config.maxConcurrentSessions}) reached. "
                        f"Active session(s): {active_ids}. Stop the existing session with 'debug_stop' first."
                    ),
                    retryable=True,
                    details={
                        "max_concurrent_sessions": self.config.maxConcurrentSessions,
                        "active_sessions": active_ids,
                    },
                )

            if profile_name not in self.config.profiles:
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Unknown launch profile '{profile_name}'. Available profiles: {sorted(self.config.profiles)}.",
                    retryable=False,
                    details={
                        "requested_profile": profile_name,
                        "available_profiles": sorted(self.config.profiles),
                    },
                )

            profile = self.config.profiles[profile_name]
            ws_root = self.config.workspaceRoot or find_workspace_root()
            session_id = f"sess_{secrets.token_hex(6)}"
            adapter = self._adapter_factory(self.config)

            session = DebugSession(
                session_id=session_id,
                config=self.config,
                profile_name=profile_name,
                profile=profile,
                test_id=test_id,
                workspace_root=ws_root,
                adapter=adapter,
                on_terminated_callback=self._on_session_terminated,
            )
            self._active_sessions[session_id] = session
            return session

    def _on_session_terminated(self, session_id: str) -> None:
        sess = self._active_sessions.pop(session_id, None)
        if sess and sess._cleanup_result:
            self._terminated_cleanup_records[session_id] = sess._cleanup_result

    def get_session(self, session_id: str) -> DebugSession:
        """Retrieve an active session or raise SESSION_NOT_FOUND."""
        sess = self._active_sessions.get(session_id)
        if sess is None or sess.state == SessionState.TERMINATED:
            raise DapPluginError(
                code=ErrorCode.SESSION_NOT_FOUND,
                message=f"Debug session '{session_id}' was not found or has already terminated.",
                retryable=False,
                details={"session_id": session_id},
            )
        return sess

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        """Idempotently stop a session by ID, returning its cleanup report even if already stopped."""
        sess = self._active_sessions.get(session_id)
        if sess is not None:
            res = await sess.cleanup(reason="explicit_stop")
            self._active_sessions.pop(session_id, None)
            self._terminated_cleanup_records[session_id] = res
            return res

        if session_id in self._terminated_cleanup_records:
            prev = dict(self._terminated_cleanup_records[session_id])
            prev["already_terminated"] = True
            return prev

        raise DapPluginError(
            code=ErrorCode.SESSION_NOT_FOUND,
            message=f"Debug session '{session_id}' does not exist.",
            retryable=False,
            details={"session_id": session_id},
        )

    async def shutdown_all(self) -> list[dict[str, Any]]:
        """Clean up all active sessions (called on MCP server exit or client disconnect)."""
        results: list[dict[str, Any]] = []
        for sid, sess in list(self._active_sessions.items()):
            try:
                res = await sess.cleanup(reason="server_shutdown")
                results.append(res)
            except Exception as e:
                logger.warning(f"Error cleaning up session {sid} during shutdown: {e}")
        self._active_sessions.clear()
        return results
