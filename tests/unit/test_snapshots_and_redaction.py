"""Unit tests for variable inspection, falsy value preservation, redaction, and MCP tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_dap.config import PluginConfig, load_config
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.server import create_server
from tests.conftest import FakeSubprocessAdapter


@pytest.mark.asyncio
async def test_falsy_values_missing_and_redaction_in_snapshot(test_config: PluginConfig) -> None:
    """Verify 0, False, '', None, missing variables, and secrets are distinguished accurately."""
    app, manager, _ = create_server(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    from agy_dap.tools.inspect import debug_inspect_tool

    snap = await debug_inspect_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_discount",
        breakpoint={"file": "src/checkout.py", "line": 39},
        locals=[
            "subtotal",
            "discount",
            "tax",
            "is_tax_exempt",
            "empty_memo",
            "coupon_code",
            "api_token",
            "missing_var",
        ],
    )

    locals_map = snap["locals"]
    assert locals_map["subtotal"] == {"value": 100.0, "type": "float", "status": "ok"}
    assert locals_map["discount"] == {"value": 15.0, "type": "float", "status": "ok"}
    assert locals_map["tax"] == {"value": 0, "type": "int", "status": "ok"}
    assert locals_map["is_tax_exempt"] == {"value": False, "type": "bool", "status": "ok"}
    assert locals_map["empty_memo"] == {"value": "", "type": "str", "status": "ok"}
    assert locals_map["coupon_code"] == {"value": None, "type": "NoneType", "status": "null"}
    assert locals_map["api_token"]["status"] == "redacted"
    assert locals_map["api_token"]["value"] == "[REDACTED]"
    assert locals_map["missing_var"]["status"] == "missing"
    assert locals_map["missing_var"]["value"] is None


@pytest.mark.asyncio
async def test_evaluate_disabled_by_default_server_side(test_config: PluginConfig) -> None:
    """Verify debug_evaluate returns EVALUATION_DISABLED unless explicitly enabled in config."""
    _, manager, _ = create_server(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    from agy_dap.tools.sessions import debug_evaluate_tool, debug_start_tool, debug_stop_tool

    start_snap = await debug_start_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_discount",
        breakpoint={"file": "src/checkout.py", "line": 39},
    )
    sid = start_snap["session_id"]
    try:
        with pytest.raises(DapPluginError) as exc_info:
            await debug_evaluate_tool(manager=manager, session_id=sid, expression="1 + 1")
        assert exc_info.value.code == ErrorCode.EVALUATION_DISABLED
    finally:
        await debug_stop_tool(manager=manager, session_id=sid)


def test_config_validation_rejects_invalid_schema(tmp_path: Path) -> None:
    """Verify load_config raises INVALID_CONFIG on unsupported schemaVersion or bad runner."""
    bad_cfg = tmp_path / "agy-dap.json"
    bad_cfg.write_text('{"schemaVersion": "99.0"}', encoding="utf-8")

    with pytest.raises(DapPluginError) as exc_info:
        load_config(custom_path=bad_cfg, workspace_dir=tmp_path, strict=True)
    assert exc_info.value.code == ErrorCode.INVALID_CONFIG


@pytest.mark.asyncio
async def test_mcp_server_canonical_error_responses_without_stacktraces(test_config: PluginConfig) -> None:
    """Verify MCP server tools return structured error objects without leaking raw Python tracebacks."""
    import json

    app, manager, _ = create_server(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )

    # 1. Non-existent session -> SESSION_NOT_FOUND
    res = await app.call_tool("debug_snapshot", {"session_id": "sess_doesnotexist"})
    payload = json.loads(res.content[0].text)
    assert payload["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value
    assert "Traceback" not in json.dumps(payload)

    # 2. Path outside workspace -> PATH_OUTSIDE_WORKSPACE
    res2 = await app.call_tool(
        "debug_inspect",
        {
            "test_id": "tests/test_checkout.py::test_discount",
            "breakpoint": {"file": "../../outside.py", "line": 10},
        },
    )
    payload2 = json.loads(res2.content[0].text)
    assert payload2["error"]["code"] == ErrorCode.PATH_OUTSIDE_WORKSPACE.value

    # 3. Invalid test selector -> INVALID_TEST_SELECTOR
    res3 = await app.call_tool(
        "debug_inspect",
        {
            "test_id": "--rootdir=/evil",
            "breakpoint": {"file": "src/checkout.py", "line": 39},
        },
    )
    payload3 = json.loads(res3.content[0].text)
    assert payload3["error"]["code"] == ErrorCode.INVALID_TEST_SELECTOR.value

    # 4. debug_doctor with missing interpreter -> DEPENDENCY_MISSING
    bad_cfg = PluginConfig(
        workspaceRoot=test_config.workspaceRoot,
        pythonPath="C:/nonexistent_dir_xyz/python.exe",
    )
    from agy_dap.tools.doctor import debug_doctor_tool

    doc = await debug_doctor_tool(config=bad_cfg)
    assert doc["healthy"] is False
    assert doc["error"]["code"] == ErrorCode.DEPENDENCY_MISSING.value


@pytest.mark.asyncio
async def test_include_snippet_in_snapshot(test_config: PluginConfig) -> None:
    """Verify include_snippet=True attaches a compact, highlighted source snippet to location."""
    _, manager, _ = create_server(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    from agy_dap.tools.inspect import debug_inspect_tool

    snap = await debug_inspect_tool(
        manager=manager,
        test_id="tests/test_checkout.py::test_discount",
        breakpoint={"file": "src/checkout.py", "line": 39},
        locals=["subtotal"],
        include_snippet=True,
    )
    assert "snippet" in snap["location"]
    assert f">   {snap['location']['line']} |" in snap["location"]["snippet"]
    assert "running_total = round(subtotal + discount, 2)" in snap["location"]["snippet"]
