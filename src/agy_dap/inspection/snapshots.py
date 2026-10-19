"""Compact snapshot builder for stopped debug sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agy_dap.config import PluginConfig
from agy_dap.dap.client import DapClient
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.inspection.variables import fetch_scope_variables
from agy_dap.models import StackFrameRecord, StopEventInfo
from agy_dap.utils.paths import is_safe_workspace_path, to_workspace_relative
from agy_dap.utils.redaction import redact_text


def extract_code_snippet(
    file_path: str | Path,
    line_1idx: int,
    context_lines: int = 3,
    max_line_length: int = 240,
) -> Optional[str]:
    """Read a compact, redacted slice of source code around line_1idx (1-indexed)."""
    try:
        p = Path(file_path)
        if not p.is_file():
            return None
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    total_lines = len(lines)
    if total_lines == 0 or line_1idx < 1 or line_1idx > total_lines:
        return None

    from_line = max(1, line_1idx - context_lines)
    to_line = min(total_lines, line_1idx + context_lines)

    snippet_lines: list[str] = []
    for line_num in range(from_line, to_line + 1):
        raw_line = redact_text(lines[line_num - 1])
        if len(raw_line) > max_line_length:
            raw_line = raw_line[:max_line_length] + "... [truncated]"
        prefix = "> " if line_num == line_1idx else "  "
        snippet_lines.append(f"{prefix}{line_num:4d} | {raw_line}")

    return "\n".join(snippet_lines)


async def build_stopped_snapshot(
    client: DapClient,
    config: PluginConfig,
    session_id: str,
    snapshot_id: str,
    stop_info: StopEventInfo,
    workspace_root: str,
    requested_locals: Optional[list[str]] = None,
    requested_thread_id: Optional[int] = None,
    requested_frame_id: Optional[int] = None,
    include_snippet: Optional[bool] = None,
) -> dict[str, Any]:
    """Build a compact, deterministic snapshot of a stopped session without calling evaluate."""
    limits = config.limits
    hints: list[str] = []
    any_truncated = False

    # 1. Resolve thread_id: prefer explicit requested_thread_id -> stop_info.thread_id -> first thread from threads request
    threads_resp = await client.send_request("threads", {})
    raw_threads = threads_resp.get("threads", [])
    if not isinstance(raw_threads, list):
        raw_threads = []

    available_thread_ids = [int(t["id"]) for t in raw_threads if isinstance(t, dict) and isinstance(t.get("id"), int)]
    if requested_thread_id is not None:
        if available_thread_ids and requested_thread_id not in available_thread_ids:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"Requested thread_id={requested_thread_id} does not exist. Available threads: {available_thread_ids}",
                retryable=False,
                details={"requested_thread_id": requested_thread_id, "available_threads": available_thread_ids},
            )
        active_thread_id = requested_thread_id
    elif stop_info.thread_id is not None:
        active_thread_id = stop_info.thread_id
    elif available_thread_ids:
        active_thread_id = available_thread_ids[0]
    else:
        active_thread_id = 1

    # 2. Fetch bounded stackTrace
    stack_resp = await client.send_request(
        "stackTrace",
        {
            "threadId": active_thread_id,
            "startFrame": 0,
            "levels": limits.maxStackFrames + 5,
        },
    )
    raw_frames = stack_resp.get("stackFrames", [])
    total_frames = stack_resp.get("totalFrames")
    if not isinstance(raw_frames, list):
        raw_frames = []

    stack_records: list[StackFrameRecord] = []
    for rf in raw_frames:
        if not isinstance(rf, dict):
            continue
        fid = int(rf.get("id", 0))
        func_name = str(rf.get("name", "<unknown>"))
        line_no = int(rf.get("line", 0))
        col_no = int(rf["column"]) if isinstance(rf.get("column"), int) else None
        source = rf.get("source") if isinstance(rf.get("source"), dict) else {}
        src_path = source.get("path") or source.get("name")
        rel_path: Optional[str] = None
        if src_path:
            if is_safe_workspace_path(src_path, workspace_root, allow_outside=True):
                rel_path = to_workspace_relative(src_path, workspace_root)
            else:
                rel_path = str(src_path)

        stack_records.append(
            StackFrameRecord(
                frame_id=fid,
                function=func_name,
                file=rel_path,
                line=line_no,
                column=col_no,
            )
        )

    if len(stack_records) > limits.maxStackFrames or (
        isinstance(total_frames, int) and total_frames > limits.maxStackFrames
    ):
        any_truncated = True
        total_known = total_frames if isinstance(total_frames, int) else len(stack_records)
        stack_records = stack_records[: limits.maxStackFrames]
        hints.append(
            f"Stack trace truncated to top {limits.maxStackFrames} of {total_known} frames. "
            "Use debug_snapshot(frame_id=...) to inspect a specific frame."
        )

    # 3. Select target frame (prefer requested_frame_id -> top workspace frame or top frame)
    target_frame: Optional[StackFrameRecord] = None
    if requested_frame_id is not None:
        for fr in stack_records:
            if fr.frame_id == requested_frame_id:
                target_frame = fr
                break
        if target_frame is None:
            # Check full raw_frames if not in top slice
            for rf in raw_frames:
                if isinstance(rf, dict) and int(rf.get("id", -1)) == requested_frame_id:
                    src = rf.get("source") if isinstance(rf.get("source"), dict) else {}
                    sp = src.get("path") or src.get("name")
                    target_frame = StackFrameRecord(
                        frame_id=int(rf["id"]),
                        function=str(rf.get("name", "<unknown>")),
                        file=to_workspace_relative(sp, workspace_root) if sp else None,
                        line=int(rf.get("line", 0)),
                    )
                    break
        if target_frame is None:
            raise DapPluginError(
                code=ErrorCode.STALE_SNAPSHOT,
                message=f"Requested frame_id={requested_frame_id} is not valid for the current stopped state (snapshot_id={snapshot_id}).",
                retryable=False,
                details={
                    "requested_frame_id": requested_frame_id,
                    "available_frame_ids": [f.frame_id for f in stack_records],
                    "snapshot_id": snapshot_id,
                },
            )
    elif stack_records:
        target_frame = stack_records[0]

    # 4. Fetch scopes & local variables for target_frame
    locals_dict: dict[str, dict[str, Any]] = {}
    if target_frame is not None:
        scopes_resp = await client.send_request("scopes", {"frameId": target_frame.frame_id})
        raw_scopes = scopes_resp.get("scopes", [])
        if not isinstance(raw_scopes, list):
            raw_scopes = []

        locals_scope_ref = 0
        for sc in raw_scopes:
            if not isinstance(sc, dict):
                continue
            sc_name = str(sc.get("name", "")).lower()
            if "local" in sc_name or locals_scope_ref == 0:
                locals_scope_ref = int(sc.get("variablesReference", 0) or 0)
                if "local" in sc_name:
                    break

        if locals_scope_ref > 0:
            locals_dict, vars_truncated, var_hints = await fetch_scope_variables(
                client=client,
                variables_reference=locals_scope_ref,
                limits=limits,
                requested_names=requested_locals,
            )
            if vars_truncated:
                any_truncated = True
            hints.extend(var_hints)

    # 5. Exception details if stopped on exception
    exception_details: Optional[dict[str, Any]] = None
    if stop_info.reason == "exception":
        exception_details = {
            "description": redact_text(str(stop_info.description or "")),
            "text": redact_text(str(stop_info.text or "")),
        }
        if client.capabilities.raw.get("supportsExceptionInfoRequest"):
            try:
                exc_resp = await client.send_request("exceptionInfo", {"threadId": active_thread_id})
                if isinstance(exc_resp, dict):
                    exception_details["exception_id"] = exc_resp.get("exceptionId")
                    if exc_resp.get("description"):
                        exception_details["description"] = redact_text(str(exc_resp["description"]))
                    details_obj = exc_resp.get("details")
                    if isinstance(details_obj, dict) and details_obj.get("message"):
                        exception_details["message"] = redact_text(str(details_obj["message"]))
            except Exception:
                pass

    # 6. Output tail
    output_tail, out_truncated = client.output_buffer.get_tail(limit=limits.maxOutputLines)
    if out_truncated:
        any_truncated = True
        hints.append("Program stdout/stderr output was truncated or deduplicated to fit output_tail limits.")

    want_snippet = include_snippet if include_snippet is not None else config.includeCodeSnippets
    location_obj: Optional[dict[str, Any]] = None
    if target_frame is not None:
        location_obj = {
            "file": target_frame.file,
            "line": target_frame.line,
            "function": target_frame.function,
        }
        if want_snippet and target_frame.file and target_frame.line >= 1:
            cand_path = Path(workspace_root) / target_frame.file
            if is_safe_workspace_path(cand_path, workspace_root, allow_outside=config.allowOutsideWorkspace):
                snippet = extract_code_snippet(
                    cand_path,
                    line_1idx=target_frame.line,
                    context_lines=config.snippetContextLines,
                    max_line_length=limits.maxStringLength,
                )
                if snippet:
                    location_obj["snippet"] = snippet

    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "snapshot_id": snapshot_id,
        "state": "STOPPED",
        "stop_reason": stop_info.reason,
        "thread_id": active_thread_id,
        "frame_id": target_frame.frame_id if target_frame else None,
        "location": location_obj,
        "stack": [f.to_dict() for f in stack_records],
        "locals": locals_dict,
        "breakpoints": [bp.to_dict() for bp in client.breakpoints],
        "output_tail": output_tail,
        "truncated": any_truncated,
    }
    if exception_details is not None:
        snapshot["exception"] = exception_details
    if hints:
        snapshot["truncation_hints"] = hints

    # 7. Enforce total byte budget (maxSnapshotBytes)
    serialized = json.dumps(snapshot, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > limits.maxSnapshotBytes:
        snapshot["truncated"] = True
        # Trim output_tail first
        if len(snapshot["output_tail"]) > 5:
            snapshot["output_tail"] = snapshot["output_tail"][-5:]
        # Trim children of locals if still over budget
        serialized = json.dumps(snapshot, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > limits.maxSnapshotBytes:
            for v in snapshot["locals"].values():
                if isinstance(v, dict) and "children" in v:
                    del v["children"]
                    v["status"] = "not_loaded"
                    v["truncated"] = True
            snapshot.setdefault("truncation_hints", []).append(
                f"Snapshot exceeded maxSnapshotBytes ({limits.maxSnapshotBytes} B); nested variable children were trimmed."
            )

    return snapshot
