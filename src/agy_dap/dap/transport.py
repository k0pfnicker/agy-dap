"""Byte-accurate Content-Length framing and single-reader async stdio transport for DAP."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.logging import get_logger

logger = get_logger("agy-dap.transport")

DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 MiB safety cap against malformed headers


def encode_dap_message(payload: dict[str, Any]) -> bytes:
    """Encode a DAP dictionary into an ASCII Content-Length header + UTF-8 JSON body."""
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header_bytes = f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("ascii")
    return header_bytes + body_bytes


class DapFrameParser:
    """Stateful byte-stream parser for DAP Content-Length framed messages.

    Handles:
    - Multi-byte UTF-8 payloads (byte length != character length)
    - Arbitrary chunk fragmentation (down to 1 byte at a time)
    - Multiple coalesced messages in a single chunk
    - Controlled recovery from malformed headers or invalid JSON payloads
    """

    def __init__(self, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        self._buffer = bytearray()
        self.max_message_bytes = max_message_bytes
        self.bytes_received: int = 0
        self.protocol_errors: list[str] = []

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Feed raw bytes and return all complete, valid DAP message dicts."""
        if not chunk:
            return []

        self.bytes_received += len(chunk)
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []

        while True:
            header_end = self._buffer.find(b"\r\n\r\n")
            if header_end == -1:
                # Guard against unbounded non-header garbage
                if len(self._buffer) > 65536:
                    err = f"Header exceeded 64 KiB without CRLFCRLF separator; dropping {len(self._buffer)} bytes."
                    self.protocol_errors.append(err)
                    logger.warning(err)
                    self._buffer.clear()
                break

            header_bytes = bytes(self._buffer[:header_end])
            content_length: Optional[int] = None
            header_text = header_bytes.decode("ascii", errors="replace")
            for line in header_text.split("\r\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    if key.strip().lower() == "content-length":
                        try:
                            content_length = int(val.strip())
                        except ValueError:
                            content_length = -1

            if content_length is None or content_length < 0 or content_length > self.max_message_bytes:
                err = f"Invalid or missing Content-Length in DAP header: {header_text!r}"
                self.protocol_errors.append(err)
                logger.warning(err)
                # Discard up to end of malformed header so stream can recover if possible
                del self._buffer[: header_end + 4]
                continue

            body_start = header_end + 4
            body_end = body_start + content_length

            if len(self._buffer) < body_end:
                # Wait for remaining body bytes
                break

            body_bytes = bytes(self._buffer[body_start:body_end])
            del self._buffer[:body_end]

            try:
                decoded = body_bytes.decode("utf-8")
                parsed = json.loads(decoded)
                if isinstance(parsed, dict):
                    messages.append(parsed)
                else:
                    err = f"DAP payload root must be a JSON object, got {type(parsed).__name__}"
                    self.protocol_errors.append(err)
                    logger.warning(err)
            except Exception as e:
                err = f"Malformed DAP JSON payload ({len(body_bytes)} bytes): {e}"
                self.protocol_errors.append(err)
                logger.warning(err)

        return messages


class DapStdioTransport:
    """Asynchronous DAP transport with exactly one stdout reader task and serialized writes."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_message: Callable[[dict[str, Any]], None],
        on_close: Callable[[Optional[str]], None],
        stderr: Optional[asyncio.StreamReader] = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._stderr = stderr
        self._on_message = on_message
        self._on_close = on_close
        self.parser = DapFrameParser(max_message_bytes=max_message_bytes)
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self.bytes_sent: int = 0
        self.stderr_tail: list[str] = []

    def start(self) -> None:
        """Start the single reader loop (and optional stderr drain loop)."""
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._read_loop(), name="dap-stdout-reader")
        if self._stderr is not None:
            self._stderr_task = asyncio.create_task(self._read_stderr_loop(), name="dap-stderr-reader")

    async def send(self, payload: dict[str, Any]) -> None:
        """Frame and write a DAP message to stdin."""
        if self._closed:
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message="Cannot send DAP message: transport is closed.",
                retryable=False,
            )
        raw = encode_dap_message(payload)
        async with self._write_lock:
            if self._closed:
                raise DapPluginError(
                    code=ErrorCode.ADAPTER_CRASHED,
                    message="Cannot send DAP message: transport closed during write.",
                    retryable=False,
                )
            try:
                self._writer.write(raw)
                await self._writer.drain()
                self.bytes_sent += len(raw)
            except Exception as e:
                self._closed = True
                self._on_close(f"Transport write failed: {e}")
                raise DapPluginError(
                    code=ErrorCode.ADAPTER_CRASHED,
                    message=f"Adapter transport closed while sending message: {e}",
                    retryable=False,
                ) from e

    async def _read_loop(self) -> None:
        """Single reader loop consuming framed DAP messages from adapter stdout."""
        close_reason: Optional[str] = None
        try:
            while not self._closed:
                chunk = await self._reader.read(65536)
                if not chunk:
                    close_reason = "Adapter stdout reached EOF."
                    break
                messages = self.parser.feed(chunk)
                for msg in messages:
                    try:
                        self._on_message(msg)
                    except Exception as e:
                        logger.error(f"Unhandled error in DAP message callback: {e}")
        except asyncio.CancelledError:
            close_reason = "Transport reader cancelled."
        except Exception as e:
            close_reason = f"Adapter stdout read error: {e}"
            logger.error(close_reason)
        finally:
            self._closed = True
            self._on_close(close_reason)

    async def _read_stderr_loop(self) -> None:
        """Drain adapter stderr into bounded tail and safe stderr logger."""
        assert self._stderr is not None
        try:
            while not self._closed:
                line = await self._stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    if len(self.stderr_tail) >= 50:
                        self.stderr_tail.pop(0)
                    self.stderr_tail.append(text)
                    logger.debug(f"[adapter stderr] {text}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def close(self) -> None:
        """Idempotently close transport streams and cancel reader tasks."""
        self._closed = True
        try:
            if not self._writer.is_closing():
                self._writer.close()
        except Exception:
            pass

        for task in (self._reader_task, self._stderr_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
