"""Best-effort secret redaction for variable values, environment maps, and logs."""

from __future__ import annotations

import re
from typing import Any

REDACTED_PLACEHOLDER = "[REDACTED]"

# Conservative key pattern matching secret/credential identifiers
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_.\-\s])("
    r"password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret|auth[_-]?token|authorization|"
    r"credential|credentials|session[_-]?key|cookie|jwt|bearer"
    r")(?:$|[_.\-\s])"
)

# Value patterns that strongly indicate inline credentials/tokens
_SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.~+/]+=*"),
    re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]{8,}"),
    re.compile(r"(?i)\b((?:api[_-]?key|password|passwd|secret|token|authorization)\s*[:=]\s*)(['\"]?)[^\s,'\";]+(\2)"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{16,})\b"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})\b"),
]


def is_sensitive_key(name: str) -> bool:
    """Return True if a variable or key name matches conservative secret rules."""
    if not name:
        return False
    return bool(_SENSITIVE_KEY_RE.search(name))


def redact_text(text: str) -> str:
    """Redact inline credential patterns from free-form text or log lines."""
    if not text:
        return text
    result = text
    result = _SENSITIVE_VALUE_PATTERNS[0].sub(rf"\1{REDACTED_PLACEHOLDER}", result)
    result = _SENSITIVE_VALUE_PATTERNS[1].sub(rf"\1{REDACTED_PLACEHOLDER}", result)
    result = _SENSITIVE_VALUE_PATTERNS[2].sub(rf"\1\2{REDACTED_PLACEHOLDER}\3", result)
    result = _SENSITIVE_VALUE_PATTERNS[3].sub(REDACTED_PLACEHOLDER, result)
    result = _SENSITIVE_VALUE_PATTERNS[4].sub(REDACTED_PLACEHOLDER, result)
    return result


def redact_value_if_needed(name: str, value: Any) -> tuple[Any, bool]:
    """Check whether a variable name or string value requires redaction.

    Returns (possibly_redacted_value, was_redacted).
    """
    if is_sensitive_key(name):
        return REDACTED_PLACEHOLDER, True

    if isinstance(value, str):
        redacted = redact_text(value)
        if redacted != value:
            return redacted, True

    return value, False
