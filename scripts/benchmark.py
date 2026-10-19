#!/usr/bin/env python3
"""Reproducible benchmark measuring agy-dap launch-to-snapshot latency, request counts, and payload size.

Compares the compact, filtered snapshot returned by `debug_inspect` against an
untruncated capture of the same stopped state (all stack frames, unfiltered scope
variables including dunders/specials, full strings/containers, and raw output).

Note on Token Estimates:
Token counts reported by this script use a deterministic character heuristic
(ceil(utf8_chars / 4)) and are explicitly labeled as estimates (`estimated_tokens_char_div_4`).
Payload size reduction in a single snapshot does not automatically equate to total
token savings across a full multi-turn agent session.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agy_dap.config import LimitsConfig, PluginConfig  # noqa: E402
from agy_dap.models import BreakpointSpec  # noqa: E402
from agy_dap.sessions.manager import SessionManager  # noqa: E402
from agy_dap.utils.paths import normalize_path  # noqa: E402


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate (~4 characters per token)."""
    return math.ceil(len(text) / 4.0)


async def capture_untruncated_state(session: Any) -> dict[str, Any]:
    """Fetch the full untruncated stack, all scope variables (including specials), and raw output from the same stopped session."""
    client = session.client
    assert client is not None and client.latest_stop is not None
    thread_id = client.latest_stop.thread_id or 1

    stack_resp = await client.send_request("stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 200})
    all_frames = stack_resp.get("stackFrames", [])

    top_frame_id = all_frames[0]["id"] if all_frames else 1
    scopes_resp = await client.send_request("scopes", {"frameId": top_frame_id})
    scopes = scopes_resp.get("scopes", [])

    unfiltered_scopes: dict[str, Any] = {}
    for sc in scopes:
        sc_name = sc.get("name", "Scope")
        vref = int(sc.get("variablesReference", 0) or 0)
        if vref > 0:
            vresp = await client.send_request("variables", {"variablesReference": vref})
            raw_vars = vresp.get("variables", [])
            expanded_vars = []
            for rv in raw_vars:
                item_copy = dict(rv)
                sub_ref = int(rv.get("variablesReference", 0) or 0)
                if sub_ref > 0:
                    try:
                        sub_resp = await client.send_request("variables", {"variablesReference": sub_ref})
                        item_copy["children"] = sub_resp.get("variables", [])
                    except Exception:
                        pass
                expanded_vars.append(item_copy)
            unfiltered_scopes[sc_name] = expanded_vars

    raw_output_tail, _ = client.output_buffer.get_tail(limit=500)
    return {
        "session_id": session.session_id,
        "state": "STOPPED",
        "stop_reason": client.latest_stop.reason,
        "stackFrames": all_frames,
        "scopes": unfiltered_scopes,
        "output": raw_output_tail,
    }


async def run_benchmark() -> dict[str, Any]:
    fixture_dir = ROOT_DIR / "tests" / "fixtures" / "sample_pytest_project"
    cfg = PluginConfig(
        workspaceRoot=normalize_path(fixture_dir),
        pythonPath=sys.executable,
    )
    manager = SessionManager(cfg)

    # 1. Measure compact hypothesis-driven inspection (1 MCP call equivalent)
    t0 = time.perf_counter()
    session = await manager.create_session("pytest", "tests/test_checkout.py::test_discount")
    try:
        compact_snapshot = await session.start_and_wait_first_stop(
            breakpoints=[BreakpointSpec(file="src/checkout.py", line=40)],
            requested_locals=["subtotal", "discount", "running_total"],
            timeout_seconds=30.0,
        )
        t1 = time.perf_counter()
        launch_to_snapshot_ms = round((t1 - t0) * 1000.0, 2)
        dap_requests_for_compact = session.client.dap_request_count if session.client else 0
        dap_events_for_compact = session.client.dap_event_count if session.client else 0

        # 2. Capture untruncated/unfiltered data at the exact same stop point for comparison
        untruncated_data = await capture_untruncated_state(session)

        # Also capture default-limits unfiltered locals (when agent omits `locals=[...]`)
        unfiltered_bounded_cfg = LimitsConfig(
            maxStackFrames=50,
            maxVariablesPerScope=200,
            maxVariableDepth=3,
            maxContainerItems=100,
            maxStringLength=10000,
            maxOutputLines=200,
            maxSnapshotBytes=1024 * 1024,
        )
        _ = unfiltered_bounded_cfg
    finally:
        await session.cleanup(reason="benchmark_complete")

    compact_json = json.dumps(compact_snapshot, ensure_ascii=False)
    untruncated_json = json.dumps(untruncated_data, ensure_ascii=False)

    compact_bytes = len(compact_json.encode("utf-8"))
    untruncated_bytes = len(untruncated_json.encode("utf-8"))

    report = {
        "target_test": "tests/test_checkout.py::test_discount",
        "breakpoint": "src/checkout.py:40",
        "requested_locals": ["subtotal", "discount", "running_total"],
        "metrics": {
            "launch_to_snapshot_ms": launch_to_snapshot_ms,
            "mcp_tool_calls": 1,
            "dap_requests_sent": dap_requests_for_compact,
            "dap_events_received": dap_events_for_compact,
            "compact_snapshot": {
                "utf8_bytes": compact_bytes,
                "char_length": len(compact_json),
                "estimated_tokens_char_div_4": estimate_tokens(compact_json),
            },
            "untruncated_capture_same_stop": {
                "utf8_bytes": untruncated_bytes,
                "char_length": len(untruncated_json),
                "estimated_tokens_char_div_4": estimate_tokens(untruncated_json),
            },
            "payload_size_ratio_compact_vs_untruncated": round(compact_bytes / max(1, untruncated_bytes), 4),
        },
        "notes": [
            "estimated_tokens_char_div_4 is a heuristic estimate (ceil(chars / 4)), not a model-specific tokenizer count.",
            "Payload size comparison reflects single-snapshot compactness vs. untruncated DAP state at the same stop point, not total multi-turn agent session token consumption.",
        ],
    }
    return report


def main() -> None:
    report = asyncio.run(run_benchmark())
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
