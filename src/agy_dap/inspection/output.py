"""Bounded output buffer with consecutive line deduplication and secret redaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agy_dap.utils.redaction import redact_text


@dataclass
class _OutputLineEntry:
    text: str
    category: str
    count: int = 1
    line_truncated: bool = False


class OutputBuffer:
    """Collects debuggee output events into a bounded buffer with repeat folding."""

    def __init__(
        self,
        max_buffer_lines: int = 500,
        max_tail_lines: int = 40,
        max_line_length: int = 300,
    ) -> None:
        self.max_buffer_lines = max(10, max_buffer_lines)
        self.max_tail_lines = max(1, max_tail_lines)
        self.max_line_length = max(32, max_line_length)
        self._entries: list[_OutputLineEntry] = []
        self._partial_line: str = ""
        self.total_raw_chars: int = 0
        self.total_lines_seen: int = 0
        self.dropped_entries: int = 0

    def append_output(self, text: str, category: str = "stdout") -> None:
        """Append raw output text from a DAP 'output' event."""
        if not text or category == "telemetry":
            return

        self.total_raw_chars += len(text)
        combined = self._partial_line + text
        lines = combined.splitlines(keepends=True)
        self._partial_line = ""

        for raw_line in lines:
            if not (raw_line.endswith("\n") or raw_line.endswith("\r")):
                # Keep trailing partial line until newline or flush
                if len(raw_line) <= self.max_line_length * 2:
                    self._partial_line = raw_line
                    continue
            clean_line = raw_line.rstrip("\r\n")
            self._add_line(clean_line, category)

    def _add_line(self, line: str, category: str) -> None:
        self.total_lines_seen += 1
        redacted = redact_text(line)
        line_truncated = False
        if len(redacted) > self.max_line_length:
            redacted = redacted[: self.max_line_length] + "... [truncated]"
            line_truncated = True

        if self._entries and self._entries[-1].text == redacted and self._entries[-1].category == category:
            self._entries[-1].count += 1
            return

        if len(self._entries) >= self.max_buffer_lines:
            self._entries.pop(0)
            self.dropped_entries += 1

        self._entries.append(
            _OutputLineEntry(
                text=redacted,
                category=category,
                count=1,
                line_truncated=line_truncated,
            )
        )

    def flush_partial(self) -> None:
        """Flush any buffered partial line without trailing newline."""
        if self._partial_line:
            line = self._partial_line
            self._partial_line = ""
            self._add_line(line, "stdout")

    def get_tail(self, limit: Optional[int] = None) -> tuple[list[str], bool]:
        """Return (formatted_tail_lines, was_truncated)."""
        self.flush_partial()
        cap = limit if limit is not None else self.max_tail_lines
        total_entries = len(self._entries)
        sliced = self._entries[-cap:] if cap > 0 else []

        was_truncated = (
            self.dropped_entries > 0
            or total_entries > len(sliced)
            or any(e.line_truncated or e.count > 1 for e in sliced)
        )

        formatted: list[str] = []
        for entry in sliced:
            if entry.count > 1:
                formatted.append(f"{entry.text} (repeated {entry.count}x)")
            else:
                formatted.append(entry.text)
        return formatted, was_truncated
