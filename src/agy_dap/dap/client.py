"""Asynchronous DAP Client handling request correlation, events, and launch handshake."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from agy_dap.config import PluginConfig
from agy_dap.dap.capabilities import AdapterCapabilities
from agy_dap.dap.transport import DapStdioTransport
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.inspection.output import OutputBuffer
from agy_dap.models import BreakpointRecord, BreakpointSpec, StopEventInfo
from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import ensure_workspace_path, to_workspace_relative

logger = get_logger("agy-dap.client")


class DapClient:
    """Asynchronous Debug Adapter Protocol client over stdio transport.

    Enforces:
    - Request registration BEFORE transport write
    - Sequence-numbered request/response correlation
    - Event processing independent of pending requests
    - Early stop/terminated event buffering (no fixed sleeps, no lost events)
    - Structured rejection of reverse requests (e.g. runInTerminal)
    - Resolution of all pending futures when transport closes or adapter crashes
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: PluginConfig,
        stderr: Optional[asyncio.StreamReader] = None,
        on_event_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
    ) -> None:
        self.config = config
        self._on_event_callback = on_event_callback

        self._seq = 1
        self._pending_requests: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self.capabilities = AdapterCapabilities()
        self.output_buffer = OutputBuffer(
            max_buffer_lines=config.limits.maxOutputBufferLines,
            max_tail_lines=config.limits.maxOutputLines,
            max_line_length=config.limits.maxOutputLineLength,
        )

        # Lifecycle & synchronization primitives (created upfront so early events are never missed)
        self._initialized_event = asyncio.Event()
        self._exited_event = asyncio.Event()
        self._terminated_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._stop_queue: asyncio.Queue[StopEventInfo] = asyncio.Queue()

        self.latest_stop: Optional[StopEventInfo] = None
        self.exit_code: Optional[int] = None
        self.debuggee_pid: Optional[int] = None
        self.close_reason: Optional[str] = None
        self.breakpoints: list[BreakpointRecord] = []

        # Metrics for benchmarking
        self.dap_request_count: int = 0
        self.dap_event_count: int = 0

        self.transport = DapStdioTransport(
            reader=reader,
            writer=writer,
            on_message=self._handle_message,
            on_close=self._handle_transport_close,
            stderr=stderr,
        )

    def start_transport(self) -> None:
        """Start the single reader loop on the underlying transport."""
        self.transport.start()

    @property
    def is_terminated(self) -> bool:
        return self._terminated_event.is_set() or self._closed_event.is_set()

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    async def send_request_nowait(
        self,
        command: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        """Register a pending request BEFORE writing to transport and return (seq, future)."""
        if self._closed_event.is_set():
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message=f"Cannot send DAP request '{command}': adapter connection is closed.",
                retryable=False,
                details={"close_reason": self.close_reason},
            )

        seq = self._next_seq()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()

        # Register BEFORE writing to transport so immediate responses cannot be missed
        self._pending_requests[seq] = (command, fut)
        self.dap_request_count += 1

        payload: dict[str, Any] = {
            "seq": seq,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            payload["arguments"] = arguments

        try:
            await self.transport.send(payload)
        except Exception as e:
            self._pending_requests.pop(seq, None)
            if not fut.done():
                fut.cancel()
            raise e

        return seq, fut

    async def send_request(
        self,
        command: str,
        arguments: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send a DAP request and await its correlated response with bounded timeout."""
        eff_timeout = timeout if timeout is not None else self.config.request_timeout_sec
        seq, fut = await self.send_request_nowait(command, arguments)
        try:
            return await asyncio.wait_for(fut, timeout=eff_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(seq, None)
            raise DapPluginError(
                code=ErrorCode.TIMEOUT,
                message=f"DAP request '{command}' (seq={seq}) timed out after {eff_timeout:.1f}s.",
                retryable=True,
                details={"command": command, "seq": seq, "timeout_seconds": eff_timeout},
            ) from None
        except asyncio.CancelledError:
            self._pending_requests.pop(seq, None)
            raise

    def _handle_message(self, msg: dict[str, Any]) -> None:
        """Dispatch an incoming DAP message (response, event, or reverse request)."""
        msg_type = msg.get("type")

        if msg_type == "response":
            self._handle_response(msg)
        elif msg_type == "event":
            self._handle_event(msg)
        elif msg_type == "request":
            # Reverse request from adapter (e.g., runInTerminal)
            asyncio.create_task(self._reject_reverse_request(msg))
        else:
            logger.warning(f"Received unknown DAP message type: {msg_type!r}")

    def _handle_response(self, msg: dict[str, Any]) -> None:
        req_seq = msg.get("request_seq")
        if not isinstance(req_seq, int):
            logger.warning(f"DAP response missing integer request_seq: {msg}")
            return

        entry = self._pending_requests.pop(req_seq, None)
        if entry is None:
            logger.debug(f"Received response for unknown or timed-out request_seq={req_seq}")
            return

        command, fut = entry
        if fut.done():
            return

        success = bool(msg.get("success", False))
        if not success:
            err_msg = str(msg.get("message") or f"DAP request '{command}' failed.")
            body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
            fut.set_exception(
                DapPluginError(
                    code=ErrorCode.PROTOCOL_ERROR,
                    message=f"DAP '{command}' rejected by adapter: {err_msg}",
                    retryable=False,
                    details={"command": command, "request_seq": req_seq, "adapter_message": err_msg, "body": body},
                )
            )
        else:
            body = msg.get("body")
            fut.set_result(body if isinstance(body, dict) else {})

    def _handle_event(self, msg: dict[str, Any]) -> None:
        self.dap_event_count += 1
        event_name = str(msg.get("event", ""))
        body = msg.get("body") if isinstance(msg.get("body"), dict) else {}

        if event_name == "initialized":
            self._initialized_event.set()

        elif event_name == "stopped":
            hit_ids = body.get("hitBreakpointIds", [])
            if not isinstance(hit_ids, list):
                hit_ids = []
            stop_info = StopEventInfo(
                reason=str(body.get("reason", "unknown")),
                thread_id=body.get("threadId") if isinstance(body.get("threadId"), int) else None,
                description=body.get("description"),
                text=body.get("text"),
                all_threads_stopped=bool(body.get("allThreadsStopped", True)),
                hit_breakpoint_ids=[int(x) for x in hit_ids if isinstance(x, int)],
                raw=body,
            )
            self.latest_stop = stop_info
            self._stop_queue.put_nowait(stop_info)

        elif event_name == "continued":
            self.latest_stop = None

        elif event_name == "output":
            output_text = str(body.get("output", ""))
            category = str(body.get("category", "stdout"))
            self.output_buffer.append_output(output_text, category=category)

        elif event_name == "process":
            sys_pid = body.get("systemProcessId")
            if isinstance(sys_pid, int) and sys_pid > 0:
                self.debuggee_pid = sys_pid

        elif event_name == "breakpoint":
            bp_data = body.get("breakpoint")
            if isinstance(bp_data, dict):
                self._update_breakpoint_from_event(bp_data)

        elif event_name == "exited":
            code = body.get("exitCode")
            if isinstance(code, int):
                self.exit_code = code
            self._exited_event.set()

        elif event_name == "terminated":
            self._terminated_event.set()

        if self._on_event_callback:
            try:
                self._on_event_callback(event_name, body)
            except Exception as e:
                logger.debug(f"Event callback error for {event_name}: {e}")

    def _update_breakpoint_from_event(self, bp_data: dict[str, Any]) -> None:
        bp_id = bp_data.get("id")
        verified = bp_data.get("verified")
        line = bp_data.get("line")
        message = bp_data.get("message")

        for rec in self.breakpoints:
            if (bp_id is not None and rec.breakpoint_id == bp_id) or (
                rec.breakpoint_id is None and line is not None and rec.requested_line == line
            ):
                if isinstance(verified, bool):
                    rec.verified = verified
                if isinstance(line, int):
                    rec.actual_line = line
                    if line <= 0:
                        rec.verified = False
                        if not rec.message:
                            rec.message = "Adapter reported line 0 (no executable statement found)."
                if message:
                    rec.message = str(message)
                if rec.requested_line > getattr(rec, "_file_line_count", 10**9):
                    rec.verified = False

    async def _reject_reverse_request(self, msg: dict[str, Any]) -> None:
        """Explicitly reject unsupported reverse requests such as runInTerminal."""
        req_seq = msg.get("seq", 0)
        command = str(msg.get("command", "unknown"))
        logger.warning(f"Rejecting unsupported reverse request from adapter: {command!r} (seq={req_seq})")
        response = {
            "seq": self._next_seq(),
            "type": "response",
            "request_seq": req_seq,
            "command": command,
            "success": False,
            "message": f"Reverse request '{command}' is not supported and is disabled by agy-dap security policy.",
        }
        try:
            await self.transport.send(response)
        except Exception:
            pass

    def _handle_transport_close(self, reason: Optional[str]) -> None:
        """Fail all pending requests and unblock all waiters when transport closes."""
        self.close_reason = reason or "Adapter transport closed."
        self._closed_event.set()

        for seq, (command, fut) in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(
                    DapPluginError(
                        code=ErrorCode.ADAPTER_CRASHED,
                        message=f"Debug adapter terminated while waiting for '{command}' (seq={seq}): {self.close_reason}",
                        retryable=False,
                        details={
                            "command": command,
                            "seq": seq,
                            "stderr_tail": list(self.transport.stderr_tail[-10:]),
                        },
                    )
                )
        self._pending_requests.clear()

    async def initialize(self, adapter_id: str = "debugpy") -> AdapterCapabilities:
        """Send the DAP 'initialize' request and record adapter capabilities."""
        init_args = {
            "clientID": "agy-dap",
            "clientName": "Antigravity DAP Plugin",
            "adapterID": adapter_id,
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsVariablePaging": False,
            "supportsRunInTerminalRequest": False,
            "locale": "en-US",
        }
        body = await self.send_request(
            "initialize",
            init_args,
            timeout=self.config.startup_timeout_sec,
        )
        self.capabilities = AdapterCapabilities(raw=body)
        return self.capabilities

    async def configure_breakpoints(
        self,
        breakpoint_specs: list[BreakpointSpec],
        workspace_root: str,
    ) -> list[BreakpointRecord]:
        """Validate, send setBreakpoints grouped by source file, and record adapter verification."""
        from pathlib import Path

        grouped: dict[str, list[tuple[str, BreakpointSpec, int]]] = {}
        for spec in breakpoint_specs:
            if spec.line < 1:
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Breakpoint line must be >= 1, got {spec.line}.",
                    retryable=False,
                    details={"file": spec.file, "line": spec.line},
                )
            if spec.condition:
                self.capabilities.require("supportsConditionalBreakpoints", "conditional breakpoints")

            resolved = ensure_workspace_path(
                spec.file,
                workspace_root,
                allow_outside=self.config.allowOutsideWorkspace,
                must_exist=True,
            )
            try:
                file_lines = len(Path(resolved).read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                file_lines = 10**9

            rel = to_workspace_relative(resolved, workspace_root)
            grouped.setdefault(resolved, []).append((rel, spec, file_lines))

        records: list[BreakpointRecord] = []
        for resolved_file, items in grouped.items():
            bp_entries = []
            for _, spec, _ in items:
                entry: dict[str, Any] = {"line": int(spec.line)}
                if spec.condition:
                    entry["condition"] = spec.condition
                bp_entries.append(entry)

            resp = await self.send_request(
                "setBreakpoints",
                {
                    "source": {"path": resolved_file},
                    "breakpoints": bp_entries,
                    "sourceModified": False,
                },
                timeout=self.config.startup_timeout_sec,
            )
            adapter_bps = resp.get("breakpoints", [])
            if not isinstance(adapter_bps, list):
                adapter_bps = []

            for idx, (rel_file, spec, file_lines) in enumerate(items):
                abp = adapter_bps[idx] if idx < len(adapter_bps) and isinstance(adapter_bps[idx], dict) else {}
                verified = bool(abp.get("verified", False))
                actual_line = abp.get("line") if isinstance(abp.get("line"), int) else spec.line
                bp_id = abp.get("id") if isinstance(abp.get("id"), int) else None
                msg = str(abp["message"]) if abp.get("message") else None

                if spec.line > file_lines:
                    verified = False
                    msg = msg or f"Requested line {spec.line} exceeds file length ({file_lines} lines)."
                elif actual_line is not None and actual_line <= 0:
                    verified = False
                    msg = msg or "Adapter could not bind breakpoint to an executable statement."

                rec = BreakpointRecord(
                    requested_file=rel_file,
                    resolved_file=resolved_file,
                    requested_line=spec.line,
                    verified=verified,
                    actual_line=actual_line,
                    breakpoint_id=bp_id,
                    message=msg,
                    requested_symbol=spec.resolved_symbol or spec.symbol,
                    symbol_provider=spec.symbol_provider,
                )
                rec._file_line_count = file_lines
                records.append(rec)

        self.breakpoints = records
        return records

    async def configure_exception_breakpoints(
        self,
        stop_on_uncaught: bool = True,
        stop_on_raised: bool = False,
    ) -> None:
        """Send setExceptionBreakpoints if supported by the adapter."""
        supported = self.capabilities.exception_breakpoint_filters
        if not supported:
            return

        filters: list[str] = []
        if stop_on_uncaught and "uncaught" in supported:
            filters.append("uncaught")
        if stop_on_raised and "raised" in supported:
            filters.append("raised")

        await self.send_request(
            "setExceptionBreakpoints",
            {"filters": filters},
            timeout=self.config.startup_timeout_sec,
        )

    async def launch_and_configure(
        self,
        launch_args: dict[str, Any],
        breakpoint_specs: list[BreakpointSpec],
        workspace_root: str,
        stop_on_uncaught: bool = True,
        stop_on_raised: bool = False,
        on_configuring: Optional[Callable[[], None]] = None,
    ) -> list[BreakpointRecord]:
        """Execute the full DAP launch + configuration handshake without assuming launch returns before initialized.

        Sequence:
        1. Send 'launch' request (registering future before write, NOT awaiting response yet).
        2. Wait for 'initialized' event (or early launch failure / adapter crash).
        3. Configure source breakpoints and exception breakpoints.
        4. Send 'configurationDone' request and await its response.
        5. Await 'launch' response (bounded by startup_timeout_sec).
        """
        startup_timeout = self.config.startup_timeout_sec
        loop = asyncio.get_running_loop()
        deadline = loop.time() + startup_timeout

        req_cmd = "attach" if launch_args.get("request") == "attach" else "launch"
        if isinstance(launch_args.get("processId"), int):
            self.debuggee_pid = int(launch_args["processId"])
        _, launch_fut = await self.send_request_nowait(req_cmd, launch_args)

        # Wait for initialized event OR early launch failure / transport close
        init_wait = asyncio.create_task(self._initialized_event.wait())
        close_wait = asyncio.create_task(self._closed_event.wait())
        try:
            done, _ = await asyncio.wait(
                {init_wait, close_wait, launch_fut},
                timeout=max(0.1, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise DapPluginError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Timed out waiting for DAP 'initialized' event after {startup_timeout:.1f}s.",
                    retryable=True,
                )

            if launch_fut in done and launch_fut.exception() is not None:
                raise launch_fut.exception()  # type: ignore[misc]

            if not self._initialized_event.is_set():
                # Either closed_event fired or launch_fut finished early before initialized
                if self._closed_event.is_set():
                    raise DapPluginError(
                        code=ErrorCode.ADAPTER_CRASHED,
                        message=f"Adapter closed before 'initialized' event: {self.close_reason}",
                        retryable=False,
                        details={"stderr_tail": list(self.transport.stderr_tail[-10:])},
                    )
                # Wait remaining time for initialized
                rem = max(0.1, deadline - loop.time())
                await asyncio.wait_for(self._initialized_event.wait(), timeout=rem)
        except asyncio.TimeoutError:
            raise DapPluginError(
                code=ErrorCode.TIMEOUT,
                message=f"Timed out waiting for DAP 'initialized' event after {startup_timeout:.1f}s.",
                retryable=True,
            ) from None
        finally:
            for t in (init_wait, close_wait):
                if not t.done():
                    t.cancel()

        if on_configuring:
            on_configuring()

        records = await self.configure_breakpoints(breakpoint_specs, workspace_root)
        await self.configure_exception_breakpoints(
            stop_on_uncaught=stop_on_uncaught,
            stop_on_raised=stop_on_raised,
        )

        if self.capabilities.supports_configuration_done:
            await self.send_request("configurationDone", {}, timeout=max(1.0, deadline - loop.time()))

        # Now await the launch response (which debugpy sends after configurationDone)
        try:
            rem = max(1.0, deadline - loop.time())
            await asyncio.wait_for(launch_fut, timeout=rem)
        except asyncio.TimeoutError:
            raise DapPluginError(
                code=ErrorCode.TIMEOUT,
                message=f"Timed out waiting for DAP 'launch' response after {startup_timeout:.1f}s.",
                retryable=True,
            ) from None

        return records

    async def wait_for_stop_or_termination(
        self,
        timeout: float,
    ) -> tuple[str, Optional[StopEventInfo]]:
        """Wait for the next 'stopped' event or session termination without polling or race conditions.

        If a 'stopped' event or 'terminated' event arrived prior to this call, returns immediately.
        Returns ("stopped", StopEventInfo) or ("terminated", None).
        """
        # 1. Immediate check if a stop event was already queued
        if not self._stop_queue.empty():
            stop_info = self._stop_queue.get_nowait()
            self.latest_stop = stop_info
            return "stopped", stop_info

        if self._terminated_event.is_set() or self._closed_event.is_set():
            return "terminated", None

        stop_task = asyncio.create_task(self._stop_queue.get())
        term_task = asyncio.create_task(self._terminated_event.wait())
        close_task = asyncio.create_task(self._closed_event.wait())

        try:
            done, _ = await asyncio.wait(
                {stop_task, term_task, close_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise DapPluginError(
                    code=ErrorCode.TIMEOUT,
                    message=f"Debug session did not reach a breakpoint or terminate within {timeout:.1f}s.",
                    retryable=True,
                    details={"timeout_seconds": timeout},
                )

            if stop_task in done:
                stop_info = stop_task.result()
                self.latest_stop = stop_info
                return "stopped", stop_info

            # Check once more if a stop event arrived in the same frame before termination
            if not self._stop_queue.empty():
                stop_info = self._stop_queue.get_nowait()
                self.latest_stop = stop_info
                return "stopped", stop_info

            if self._closed_event.is_set() and not self._terminated_event.is_set() and not self._exited_event.is_set():
                raise DapPluginError(
                    code=ErrorCode.ADAPTER_CRASHED,
                    message=f"Debug adapter crashed or closed unexpectedly: {self.close_reason}",
                    retryable=False,
                    details={"stderr_tail": list(self.transport.stderr_tail[-10:])},
                )

            return "terminated", None
        finally:
            for t in (stop_task, term_task, close_task):
                if not t.done():
                    t.cancel()

    def drain_stop_queue(self) -> None:
        """Clear any stale stop events before issuing a continue/step command."""
        while not self._stop_queue.empty():
            try:
                self._stop_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.latest_stop = None

    async def disconnect_gracefully(self, timeout: float = 2.0) -> None:
        """Attempt a graceful DAP disconnect/terminate before hard process cleanup."""
        if self._closed_event.is_set():
            return
        try:
            await self.send_request(
                "disconnect",
                {"restart": False, "terminateDebuggee": True},
                timeout=timeout,
            )
        except Exception:
            pass
        finally:
            await self.transport.close()
