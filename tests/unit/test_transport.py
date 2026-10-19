"""Unit tests for byte-accurate DAP Content-Length framing and transport."""

from __future__ import annotations

import asyncio

import pytest

from agy_dap.dap.transport import DapFrameParser, DapStdioTransport, encode_dap_message


def test_utf8_multibyte_framing() -> None:
    """Verify Content-Length uses UTF-8 byte length rather than character count."""
    payload = {
        "seq": 1,
        "type": "event",
        "event": "output",
        "body": {"output": "Ümläute & Emoji 🐛🔍 — Привет"},
    }
    raw = encode_dap_message(payload)
    parser = DapFrameParser()
    msgs = parser.feed(raw)
    assert len(msgs) == 1
    assert msgs[0] == payload
    assert parser.protocol_errors == []


def test_fragmented_single_byte_reads() -> None:
    """Verify parser reconstructs message when fed 1 byte at a time across header and body."""
    payload = {"seq": 42, "type": "response", "request_seq": 7, "command": "initialize", "success": True, "body": {}}
    raw = encode_dap_message(payload)
    parser = DapFrameParser()

    collected = []
    for i in range(len(raw)):
        collected.extend(parser.feed(raw[i : i + 1]))

    assert len(collected) == 1
    assert collected[0] == payload


def test_multiple_coalesced_messages_in_single_read() -> None:
    """Verify parser extracts multiple messages arriving in a single chunk."""
    m1 = {"seq": 1, "type": "event", "event": "initialized", "body": {}}
    m2 = {"seq": 2, "type": "response", "request_seq": 1, "command": "launch", "success": True, "body": {}}
    m3 = {"seq": 3, "type": "event", "event": "stopped", "body": {"reason": "breakpoint", "threadId": 1}}

    combined = encode_dap_message(m1) + encode_dap_message(m2) + encode_dap_message(m3)
    parser = DapFrameParser()
    msgs = parser.feed(combined)
    assert msgs == [m1, m2, m3]


def test_malformed_header_and_invalid_json_recovery() -> None:
    """Verify parser records protocol errors and recovers to parse subsequent valid frames."""
    bad_header = b"Content-Length: not-a-number\r\n\r\n"
    bad_json_body = b"{invalid json"
    bad_json_frame = f"Content-Length: {len(bad_json_body)}\r\n\r\n".encode("ascii") + bad_json_body
    valid_msg = {"seq": 10, "type": "event", "event": "terminated", "body": {}}
    valid_frame = encode_dap_message(valid_msg)

    parser = DapFrameParser()
    msgs = parser.feed(bad_header + bad_json_frame + valid_frame)

    assert len(msgs) == 1
    assert msgs[0] == valid_msg
    assert len(parser.protocol_errors) == 2


@pytest.mark.asyncio
async def test_transport_eof_triggers_on_close() -> None:
    """Verify DapStdioTransport invokes on_close when stdout reaches EOF."""
    reader = asyncio.StreamReader()
    closed_reasons: list[str | None] = []
    received: list[dict] = []

    class DummyWriter:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    transport = DapStdioTransport(
        reader=reader,
        writer=DummyWriter(),  # type: ignore[arg-type]
        on_message=received.append,
        on_close=closed_reasons.append,
    )
    transport.start()

    msg = {"seq": 1, "type": "event", "event": "initialized"}
    reader.feed_data(encode_dap_message(msg))
    reader.feed_eof()

    await asyncio.sleep(0.05)
    assert received == [msg]
    assert len(closed_reasons) == 1
    await transport.close()
