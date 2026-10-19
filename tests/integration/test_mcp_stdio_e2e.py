"""End-to-end MCP stdio server tests verifying tool schemas and stdout log-pollution freedom."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mcp_stdio_server_end_to_end(sample_project_dir: Path) -> None:
    """Launch run_server.py over real stdio and verify MCP protocol purity and tool execution."""
    run_script = Path(__file__).resolve().parents[2] / "run_server.py"
    env = os.environ.copy()
    env["AGY_DAP_WORKSPACE_ROOT"] = str(sample_project_dir)
    env["AGY_DAP_PYTHON_PATH"] = sys.executable
    env["PYTHONUNBUFFERED"] = "1"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(run_script)],
        env=env,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # 1. Verify all 7 tools are registered
            tools_res = await session.list_tools()
            tool_names = {t.name for t in tools_res.tools}
            expected = {
                "debug_doctor",
                "debug_inspect",
                "debug_start",
                "debug_snapshot",
                "debug_continue",
                "debug_evaluate",
                "debug_stop",
            }
            assert expected.issubset(tool_names)

            # 2. Call debug_doctor over stdio
            doc_res = await session.call_tool("debug_doctor", {"profile": "pytest"})
            assert len(doc_res.content) > 0
            doc_payload = json.loads(doc_res.content[0].text)
            assert doc_payload["healthy"] is True

            # 3. Call debug_inspect over stdio
            insp_res = await session.call_tool(
                "debug_inspect",
                {
                    "profile": "pytest",
                    "test_id": "tests/test_checkout.py::test_discount",
                    "breakpoint": {"file": "src/checkout.py", "line": 40},
                    "locals": ["subtotal", "discount", "running_total"],
                    "timeout_seconds": 25.0,
                },
            )
            assert len(insp_res.content) > 0
            insp_payload = json.loads(insp_res.content[0].text)
            assert insp_payload["state"] == "STOPPED"
            assert insp_payload["locals"]["running_total"]["value"] == 115.0
            assert insp_payload["session_terminated"] is True
