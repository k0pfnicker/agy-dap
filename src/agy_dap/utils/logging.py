"""Stderr-only logging for agy-dap.

MCP uses stdout exclusively for JSON-RPC messages. All internal plugin logs
are routed strictly to sys.stderr and scrubbed for obvious secrets.
"""

from __future__ import annotations

import logging
import sys

from agy_dap.utils.redaction import redact_text


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs sensitive tokens from log records."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_text(formatted)


def get_logger(name: str = "agy-dap") -> logging.Logger:
    """Return a logger configured to emit solely to sys.stderr."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = RedactingFormatter(
            fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
