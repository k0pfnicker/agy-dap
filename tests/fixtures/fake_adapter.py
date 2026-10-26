"""Controllable stdio Fake DAP Adapter for deterministic unit and protocol edge-case testing.

Runs as a lightweight Python subprocess without requiring debugpy, enabling testing of:
- Handshake ordering (launch response after configurationDone)
- Early stopped/terminated events before explicit wait
- Unverified breakpoints
- Reverse request rejection (runInTerminal)
- Adapter crash mid-request or mid-handshake
- Timeout / hang behavior
- Stale frame references across multiple stops
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def send_msg(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def read_msg() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii", errors="replace").strip()
        if ":" in decoded:
            k, v = decoded.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    cl = int(headers.get("content-length", "0"))
    if cl <= 0:
        return None
    body = sys.stdin.buffer.read(cl)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def main() -> None:
    mode = os.environ.get("FAKE_DAP_MODE", "normal")
    seq_counter = 1
    pending_launch_req_seq: int | None = None
    pending_launch_cmd: str = "launch"
    stop_count = 0

    def next_seq() -> int:
        nonlocal seq_counter
        s = seq_counter
        seq_counter += 1
        return s

    while True:
        msg = read_msg()
        if msg is None:
            break

        req_seq = msg.get("seq", 0)
        cmd = msg.get("command", "")
        args = msg.get("arguments") or {}

        if cmd == "initialize":
            if mode == "crash_on_init":
                sys.exit(137)
            if mode == "hang_on_init":
                time.sleep(60)

            caps: dict[str, Any] = {
                "supportsConfigurationDoneRequest": True,
                "supportsConditionalBreakpoints": mode != "no_conditional_bp",
                "supportsEvaluateForHovers": True,
                "supportsTerminateRequest": True,
                "exceptionBreakpointFilters": [
                    {"filter": "uncaught", "label": "Uncaught Exceptions", "default": True},
                    {"filter": "raised", "label": "Raised Exceptions", "default": False},
                ],
            }
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "initialize",
                    "success": True,
                    "body": caps,
                }
            )

        elif cmd in ("launch", "attach"):
            if mode == "fail_launch":
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "response",
                        "request_seq": req_seq,
                        "command": cmd,
                        "success": False,
                        "message": "Simulated launch failure in adapter.",
                    }
                )
                continue

            if mode == "reverse_run_in_terminal":
                # Attempt a reverse request runInTerminal before initialized
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "request",
                        "command": "runInTerminal",
                        "arguments": {"kind": "integrated", "args": ["calc.exe"]},
                    }
                )

            # Hold launch/attach response until configurationDone (like debugpy and netcoredbg)
            pending_launch_req_seq = req_seq
            pending_launch_cmd = cmd
            send_msg({"seq": next_seq(), "type": "event", "event": "initialized", "body": {}})

        elif cmd == "setBreakpoints":
            bps = args.get("breakpoints", [])
            resp_bps = []
            for idx, bp in enumerate(bps, start=1):
                line = bp.get("line", 1)
                if mode == "unverified_bp" or line > 500:
                    resp_bps.append(
                        {
                            "id": idx,
                            "verified": False,
                            "line": line,
                            "message": "Breakpoint in file that does not exist or line beyond EOF.",
                        }
                    )
                else:
                    resp_bps.append({"id": idx, "verified": True, "line": line})
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "setBreakpoints",
                    "success": True,
                    "body": {"breakpoints": resp_bps},
                }
            )

        elif cmd == "setExceptionBreakpoints":
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "setExceptionBreakpoints",
                    "success": True,
                    "body": {},
                }
            )

        elif cmd == "configurationDone":
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "configurationDone",
                    "success": True,
                    "body": {},
                }
            )
            if mode == "early_stop_before_launch_response":
                # Emit stopped event BEFORE the launch response to test race immunity!
                stop_count += 1
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "event",
                        "event": "stopped",
                        "body": {"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True},
                    }
                )

            if pending_launch_req_seq is not None:
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "response",
                        "request_seq": pending_launch_req_seq,
                        "command": pending_launch_cmd,
                        "success": True,
                        "body": {},
                    }
                )
                pending_launch_req_seq = None

            if mode == "normal" or mode == "reverse_run_in_terminal":
                stop_count += 1
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "event",
                        "event": "output",
                        "body": {"category": "stdout", "output": "dup line\ndup line\ndup line\n"},
                    }
                )
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "event",
                        "event": "stopped",
                        "body": {"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True},
                    }
                )
            elif mode == "exit_without_stop":
                send_msg({"seq": next_seq(), "type": "event", "event": "exited", "body": {"exitCode": 0}})
                send_msg({"seq": next_seq(), "type": "event", "event": "terminated", "body": {}})
            elif mode == "exception_stop":
                stop_count += 1
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "event",
                        "event": "stopped",
                        "body": {
                            "reason": "exception",
                            "threadId": 1,
                            "description": "ValueError",
                            "text": "Negative value not allowed",
                            "allThreadsStopped": True,
                        },
                    }
                )
            elif mode == "hang_after_launch":
                pass

        elif cmd == "threads":
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "threads",
                    "success": True,
                    "body": {"threads": [{"id": 1, "name": "MainThread"}]},
                }
            )

        elif cmd == "stackTrace":
            frame_id = 100 + stop_count
            line_no = 38 if stop_count == 1 else 39
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "stackTrace",
                    "success": True,
                    "body": {
                        "stackFrames": [
                            {
                                "id": frame_id,
                                "name": "calculate_total",
                                "line": line_no,
                                "column": 1,
                                "source": {"name": "checkout.py", "path": "src/checkout.py"},
                            }
                        ],
                        "totalFrames": 1,
                    },
                }
            )

        elif cmd == "scopes":
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "scopes",
                    "success": True,
                    "body": {
                        "scopes": [{"name": "Locals", "variablesReference": 200 + stop_count, "expensive": False}]
                    },
                }
            )

        elif cmd == "variables":
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "variables",
                    "success": True,
                    "body": {
                        "variables": [
                            {"name": "subtotal", "value": "100.0", "type": "float", "variablesReference": 0},
                            {"name": "discount", "value": "15.0", "type": "float", "variablesReference": 0},
                            {"name": "tax", "value": "0", "type": "int", "variablesReference": 0},
                            {"name": "is_tax_exempt", "value": "False", "type": "bool", "variablesReference": 0},
                            {"name": "empty_memo", "value": "''", "type": "str", "variablesReference": 0},
                            {"name": "coupon_code", "value": "None", "type": "NoneType", "variablesReference": 0},
                            {
                                "name": "api_token",
                                "value": "'sk-live-secret-1234567890abcdef'",
                                "type": "str",
                                "variablesReference": 0,
                            },
                        ]
                    },
                }
            )

        elif cmd in ("continue", "next", "stepIn", "stepOut"):
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": cmd,
                    "success": True,
                    "body": {"allThreadsContinued": True},
                }
            )
            if cmd == "next" or stop_count < 2:
                stop_count += 1
                send_msg(
                    {
                        "seq": next_seq(),
                        "type": "event",
                        "event": "stopped",
                        "body": {"reason": "step", "threadId": 1, "allThreadsStopped": True},
                    }
                )
            else:
                send_msg({"seq": next_seq(), "type": "event", "event": "exited", "body": {"exitCode": 0}})
                send_msg({"seq": next_seq(), "type": "event", "event": "terminated", "body": {}})

        elif cmd == "evaluate":
            expr = args.get("expression", "")
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": "evaluate",
                    "success": True,
                    "body": {"result": f"eval:{expr}", "type": "str", "variablesReference": 0},
                }
            )

        elif cmd in ("disconnect", "terminate"):
            send_msg(
                {
                    "seq": next_seq(),
                    "type": "response",
                    "request_seq": req_seq,
                    "command": cmd,
                    "success": True,
                    "body": {},
                }
            )
            send_msg({"seq": next_seq(), "type": "event", "event": "terminated", "body": {}})
            break


if __name__ == "__main__":
    main()
