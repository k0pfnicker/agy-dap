"""Machine-readable error contract and custom exception classes for agy-dap.

Ensures plugin stacktraces never leak raw into MCP responses and all failure modes
map to deterministic error codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Standardized error codes for agy-dap responses."""

    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    INVALID_CONFIG = "INVALID_CONFIG"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    INVALID_TEST_SELECTOR = "INVALID_TEST_SELECTOR"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    BREAKPOINT_UNVERIFIED = "BREAKPOINT_UNVERIFIED"
    TARGET_NOT_REACHED = "TARGET_NOT_REACHED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_NOT_STOPPED = "SESSION_NOT_STOPPED"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    EVALUATION_DISABLED = "EVALUATION_DISABLED"
    TIMEOUT = "TIMEOUT"
    ADAPTER_CRASHED = "ADAPTER_CRASHED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CONCURRENT_SESSION_LIMIT = "CONCURRENT_SESSION_LIMIT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"


@dataclass
class DapPluginError(Exception):
    """Structured exception carrying machine-readable error metadata."""

    code: ErrorCode | str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        code_val = self.code.value if isinstance(self.code, ErrorCode) else str(self.code)
        return f"[{code_val}] {self.message}"

    def to_dict(self, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Serialize into the canonical error response contract."""
        code_val = self.code.value if isinstance(self.code, ErrorCode) else str(self.code)
        payload: dict[str, Any] = {
            "error": {
                "code": code_val,
                "message": self.message,
                "retryable": self.retryable,
                "details": dict(self.details),
            }
        }
        if extra:
            payload.update(extra)
        return payload


def make_error_response(
    code: ErrorCode | str,
    message: str,
    retryable: bool = False,
    details: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a canonical error response dictionary without raising."""
    err = DapPluginError(
        code=code,
        message=message,
        retryable=retryable,
        details=details or {},
    )
    return err.to_dict(extra=extra)
