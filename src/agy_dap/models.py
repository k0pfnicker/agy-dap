"""Data models for breakpoints, stack frames, variable records, and snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BreakpointSpec:
    """User-requested breakpoint position (1-indexed line)."""

    file: str
    line: int
    condition: Optional[str] = None


@dataclass
class BreakpointRecord:
    """Tracks both the requested breakpoint and the adapter's verification response."""

    requested_file: str
    resolved_file: str
    requested_line: int
    verified: bool = False
    actual_line: Optional[int] = None
    breakpoint_id: Optional[int] = None
    message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested_file": self.requested_file,
            "requested_line": self.requested_line,
            "verified": self.verified,
            "actual_line": self.actual_line if self.actual_line is not None else self.requested_line,
        }
        if self.breakpoint_id is not None:
            payload["id"] = self.breakpoint_id
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass
class StopEventInfo:
    """Captured metadata from a DAP 'stopped' event."""

    reason: str
    thread_id: Optional[int] = None
    description: Optional[str] = None
    text: Optional[str] = None
    all_threads_stopped: bool = True
    hit_breakpoint_ids: list[int] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StackFrameRecord:
    """Compact representation of a single stack frame."""

    frame_id: int
    function: str
    file: Optional[str]
    line: int
    column: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "frame_id": self.frame_id,
            "function": self.function,
            "file": self.file,
            "line": self.line,
        }
        if self.column is not None:
            d["column"] = self.column
        return d


@dataclass
class VariableEntry:
    """Structured variable entry distinguishing loaded, null, not_loaded, redacted, and missing."""

    name: str
    value: Any
    var_type: Optional[str] = None
    status: str = "ok"  # "ok" | "null" | "redacted" | "not_loaded" | "missing"
    truncated: bool = False
    children: Optional[dict[str, Any] | list[Any]] = None
    hint: Optional[str] = None

    def to_compact_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "value": self.value,
            "type": self.var_type,
            "status": self.status,
        }
        if self.truncated:
            d["truncated"] = True
        if self.children is not None:
            d["children"] = self.children
        if self.hint:
            d["hint"] = self.hint
        return d
