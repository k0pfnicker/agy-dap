"""Unit tests for workspace confinement, pytest selector validation, and environment sanitization."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.paths import (
    ensure_workspace_path,
    validate_pytest_selector,
    validate_python_module_selector,
    validate_unittest_selector,
)
from agy_dap.utils.processes import build_sanitized_env


def test_workspace_confinement_blocks_traversal(sample_project_dir: Path) -> None:
    """Verify paths outside workspace root raise PATH_OUTSIDE_WORKSPACE."""
    ws = str(sample_project_dir)

    # Inside workspace succeeds
    inside = ensure_workspace_path("src/checkout.py", ws, must_exist=True)
    assert inside.endswith("checkout.py")

    # Relative parent traversal is blocked
    with pytest.raises(DapPluginError) as exc_info:
        ensure_workspace_path("../../outside.py", ws)
    assert exc_info.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_pytest_selector_blocks_flag_injection_and_outside_paths(sample_project_dir: Path) -> None:
    """Verify validate_pytest_selector rejects CLI options, outside files, and invalid nodes."""
    ws = str(sample_project_dir)

    # Valid selector with node ID
    sel, resolved = validate_pytest_selector("tests/test_checkout.py::test_discount", ws)
    assert sel.endswith("test_checkout.py::test_discount")
    assert Path(resolved).is_file()

    # Flag injection blocked
    for bad in ("--rootdir=/tmp", "-o", "-k test_discount", "tests/test_checkout.py::-- evil"):
        with pytest.raises(DapPluginError) as exc_info:
            validate_pytest_selector(bad, ws)
        assert exc_info.value.code == ErrorCode.INVALID_TEST_SELECTOR

    # Outside workspace blocked
    with pytest.raises(DapPluginError) as exc_info2:
        validate_pytest_selector("../fake_adapter.py::test_foo", ws)
    assert exc_info2.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_unittest_and_python_module_selectors_validation(sample_project_dir: Path) -> None:
    """Verify unittest and python_module selector validation and normalization."""
    ws = str(sample_project_dir)

    # unittest accepts pytest-style 'tests/test_checkout.py::test_discount' or dotted 'tests.test_checkout.test_discount'
    u_args1, _ = validate_unittest_selector("tests/test_checkout.py::test_discount", ws)
    assert u_args1 == ["tests.test_checkout.test_discount"]

    u_args2, _ = validate_unittest_selector("tests.test_checkout.TestCheckout.test_discount", ws)
    assert u_args2 == ["tests.test_checkout.TestCheckout.test_discount"]

    # python_module accepts a .py file inside workspace or a dotted module name
    mod_info1 = validate_python_module_selector("src/checkout.py", ws)
    assert mod_info1["mode"] == "program"
    assert mod_info1["target"].endswith("checkout.py")

    mod_info2 = validate_python_module_selector("checkout", ws)
    assert mod_info2["mode"] == "module"
    assert mod_info2["target"] == "checkout"

    # CLI flags blocked in both
    for bad in ("-v", "--help", "-m evil"):
        with pytest.raises(DapPluginError) as exc1:
            validate_unittest_selector(bad, ws)
        assert exc1.value.code == ErrorCode.INVALID_TEST_SELECTOR
        with pytest.raises(DapPluginError) as exc2:
            validate_python_module_selector(bad, ws)
        assert exc2.value.code == ErrorCode.INVALID_TEST_SELECTOR


@pytest.mark.asyncio
async def test_symbol_resolver_and_symbol_breakpoints(sample_project_dir: Path) -> None:
    """Verify PythonAstSymbolResolver, AgyLspSymbolResolver, and symbol breakpoints in parse_breakpoint_input."""
    from agy_dap.symbols import CompositeSymbolResolver, PythonAstSymbolResolver
    from agy_dap.tools.inspect import parse_breakpoint_input, resolve_breakpoint_specs

    ws = str(sample_project_dir)
    ast_resolver = PythonAstSymbolResolver(workspace_root=ws)

    # calculate_total is defined on line 16, docstring ends on line 26, first statement 'discount = ...' is line 27
    loc = await ast_resolver.resolve_symbol("calculate_total", context_file="src/checkout.py")
    assert loc is not None
    assert loc.declaration_line == 16
    assert loc.line == 27
    assert loc.provider == "python-ast"

    # parse_breakpoint_input accepts {'file': 'src/checkout.py', 'symbol': 'calculate_total', 'line_offset': 13}
    specs = parse_breakpoint_input(
        breakpoint_arg={"file": "src/checkout.py", "symbol": "calculate_total", "line_offset": 13}
    )
    assert len(specs) == 1
    assert specs[0].symbol == "calculate_total"
    assert specs[0].line_offset == 13

    resolver = CompositeSymbolResolver(workspace_root=ws)
    resolved_specs = await resolve_breakpoint_specs(specs, resolver=resolver, workspace_root=ws)
    assert len(resolved_specs) == 1
    # declaration_line (16) + line_offset (13) or body_line when line_offset is 0:
    # With line_offset=13 from body_line (27): 27 + 13 = 40!
    assert resolved_specs[0].line == 40
    assert resolved_specs[0].resolved_symbol == "calculate_total"

    # Also verify AgyLspSymbolResolver integration with agy_lsp.tools.symbols.resolve_symbol_target
    from agy_dap.symbols import AgyLspSymbolResolver, _ensure_agy_lsp_importable

    if _ensure_agy_lsp_importable():
        lsp_resolver = AgyLspSymbolResolver(workspace_root=ws)
        with patch(
            "agy_lsp.tools.symbols.resolve_symbol_target",
            return_value={
                "name": "calculate_total",
                "file": str(sample_project_dir / "src" / "checkout.py"),
                "line": 16,
                "body_line": 27,
                "column": 1,
                "end_line": 42,
            },
        ):
            lsp_loc = await lsp_resolver.resolve_symbol("calculate_total", context_file="src/checkout.py")
            assert lsp_loc is not None
            assert lsp_loc.provider == "agy-lsp"
            assert lsp_loc.declaration_line == 16
            assert lsp_loc.line == 27


def test_default_profiles_and_debugpy_build_launch_args(test_config) -> None:
    """Verify default profiles include pytest, unittest, and python_module and build_launch_args handles each."""
    from agy_dap.adapters.debugpy import DebugpyAdapter

    test_config.validate()
    assert "pytest" in test_config.profiles
    assert "unittest" in test_config.profiles
    assert "python_module" in test_config.profiles

    adapter = DebugpyAdapter(test_config)
    ws = test_config.workspaceRoot

    # 1. unittest profile
    u_launch = adapter.build_launch_args(
        test_config.profiles["unittest"],
        "tests/test_checkout.py::test_discount",
        ws,
    )
    assert u_launch["module"] == "unittest"
    assert "tests.test_checkout.test_discount" in u_launch["args"]

    # 2. python_module profile with .py script
    p_launch = adapter.build_launch_args(
        test_config.profiles["python_module"],
        "src/checkout.py",
        ws,
    )
    assert "program" in p_launch
    assert p_launch["program"].endswith("checkout.py")
    assert "module" not in p_launch

    # 3. python_module profile with dotted module name
    m_launch = adapter.build_launch_args(
        test_config.profiles["python_module"],
        "checkout",
        ws,
    )
    assert m_launch["module"] == "checkout"
    assert "program" not in m_launch


@pytest.mark.asyncio
async def test_dotnet_test_selector_and_netcoredbg_adapter(tmp_path: Path) -> None:
    """Verify validate_dotnet_test_selector, CSharpSymbolResolver, and NetCoreDbgAdapter VSTEST_HOST_DEBUG handshake."""
    from agy_dap.adapters.netcoredbg import NetCoreDbgAdapter, parse_vstest_host_pid
    from agy_dap.config import PluginConfig
    from agy_dap.symbols import CompositeSymbolResolver
    from agy_dap.utils.paths import validate_dotnet_test_selector

    # Create a dummy C# test project structure inside tmp_path
    cs_dir = tmp_path / "SampleDotnetTests"
    cs_dir.mkdir()
    csproj = cs_dir / "SampleDotnetTests.csproj"
    csproj.write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>', encoding="utf-8")
    cs_file = cs_dir / "CheckoutService.cs"
    cs_file.write_text(
        "namespace SampleDotnetTests;\n"
        "\n"
        "public class CheckoutService\n"
        "{\n"
        "    public decimal CalculateTotal(decimal subtotal, decimal discountRate)\n"
        "    {\n"
        "        // Compute discount\n"
        "        decimal discount = subtotal * discountRate;\n"
        "        decimal runningTotal = subtotal + discount;\n"
        "        return runningTotal;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    ws = str(tmp_path)

    # 1. Selector with .csproj and filter
    sel1 = validate_dotnet_test_selector(
        "SampleDotnetTests/SampleDotnetTests.csproj::CheckoutTests.TestDiscount",
        ws,
    )
    assert sel1["project"] is not None and sel1["project"].endswith("SampleDotnetTests.csproj")
    assert sel1["filter"] == "CheckoutTests.TestDiscount"

    # 2. Standalone filter selector
    sel2 = validate_dotnet_test_selector("FullyQualifiedName~CheckoutTests.TestDiscount", ws)
    assert sel2["project"] is None
    assert sel2["filter"] == "FullyQualifiedName~CheckoutTests.TestDiscount"

    # 3. CLI flag injection and path traversal blocked
    for bad in ("--logger:trx", "-v", "SampleDotnetTests/SampleDotnetTests.csproj::--evil"):
        with pytest.raises(DapPluginError) as exc_info:
            validate_dotnet_test_selector(bad, ws)
        assert exc_info.value.code == ErrorCode.INVALID_TEST_SELECTOR

    with pytest.raises(DapPluginError) as exc_out:
        validate_dotnet_test_selector("../Outside.csproj::TestFoo", ws)
    assert exc_out.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE

    # 4. CSharpSymbolResolver resolves CalculateTotal to line 8 (first executable line after '{' and comment)
    resolver = CompositeSymbolResolver(workspace_root=ws)
    cs_loc = await resolver.resolve_symbol(
        "CheckoutService.CalculateTotal",
        context_file="SampleDotnetTests/CheckoutService.cs",
    )
    assert cs_loc is not None
    assert cs_loc.declaration_line == 5
    assert cs_loc.line == 8

    # 5. VSTest output PID parser and NetCoreDbgAdapter build_launch_args
    sample_stdout = (
        "Microsoft (R) Test Execution Command Line Tool Version 17.11.0\n"
        "Host debugging is enabled. Please attach debugger to testhost process to continue.\n"
        "Process Id: 42424, Name: dotnet\n"
    )
    assert parse_vstest_host_pid(sample_stdout) == 42424

    cfg = PluginConfig(workspaceRoot=ws)
    cfg.validate()
    assert "dotnet_test" in cfg.profiles
    net_adapter = NetCoreDbgAdapter(cfg)
    launch_cfg = net_adapter.build_launch_args(
        cfg.profiles["dotnet_test"],
        "SampleDotnetTests/SampleDotnetTests.csproj::CheckoutTests.TestDiscount",
        ws,
    )
    assert launch_cfg["type"] == "coreclr"
    assert "--filter" in launch_cfg["dotnet_test_args"]

    # 6. Simulate VSTEST_HOST_DEBUG=1 subprocess in prepare_launch_args and full DAP attach inspection
    import sys

    fake_dotnet_script = tmp_path / "fake_dotnet.py"
    fake_dotnet_script.write_text(
        "import sys, time\n"
        "print('Host debugging is enabled. Please attach debugger to testhost process to continue.', flush=True)\n"
        "print('Process Id: 49999, Name: dotnet', flush=True)\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )

    class SimulatedNetCoreDbgAdapter(NetCoreDbgAdapter):
        async def spawn_adapter_process(self, profile):
            import asyncio

            fake_script = Path(__file__).parents[1] / "fixtures" / "fake_adapter.py"
            env = os.environ.copy()
            env["FAKE_DAP_MODE"] = "normal"
            return await asyncio.create_subprocess_exec(
                sys.executable,
                str(fake_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ws,
                env=env,
            )

        async def prepare_launch_args(self, profile, test_id, workspace_root):
            # Spawn our fake_dotnet_script via python and parse its emitted Process Id
            import asyncio

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(fake_dotnet_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=ws,
            )
            self._vstest_proc = proc
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                pid = parse_vstest_host_pid(raw.decode("utf-8", errors="replace"))
                if pid is not None:
                    self._testhost_pid = pid
                    break
            return {
                "name": "agy-dap:dotnet_test",
                "type": "coreclr",
                "request": "attach",
                "processId": self._testhost_pid,
                "justMyCode": True,
                "cwd": ws,
            }

    from agy_dap.server import create_server
    from agy_dap.tools.doctor import debug_doctor_tool
    from agy_dap.tools.inspect import debug_inspect_tool

    _, manager, _ = create_server(cfg, adapter_factory=lambda c: SimulatedNetCoreDbgAdapter(c))
    snap = await debug_inspect_tool(
        manager=manager,
        profile="dotnet_test",
        test_id="SampleDotnetTests/SampleDotnetTests.csproj::CheckoutTests.TestDiscount",
        breakpoint={"file": "SampleDotnetTests/CheckoutService.cs", "symbol": "CheckoutService.CalculateTotal"},
        locals=["subtotal", "discount"],
    )
    assert snap["state"] == "STOPPED"
    assert snap["breakpoints"][0]["requested_line"] == 8
    assert snap["breakpoints"][0]["requested_symbol"] == "CheckoutService.CalculateTotal"
    assert snap["breakpoints"][0]["symbol_provider"] == "csharp-ast"
    assert snap["session_terminated"] is True

    # 7. Verify debug_doctor reports dotnet_test checks
    doc = await debug_doctor_tool(config=cfg, profile="dotnet_test")
    assert "dotnet" in doc["checks"]
    assert "netcoredbg" in doc["checks"]


def test_sanitized_env_filters_host_secrets() -> None:
    """Verify build_sanitized_env excludes arbitrary host secrets and keeps allowlisted platform vars."""
    mock_env = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:\\Windows",
        "OPENAI_API_KEY": "sk-super-secret-host-key-1234567890",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "GITHUB_TOKEN": "ghp_1234567890abcdefghijklmnop",
        "UNLISTED_RANDOM_VAR": "should_not_be_copied",
    }
    with patch.dict(os.environ, mock_env, clear=True):
        sanitized = build_sanitized_env(profile_env={"CUSTOM_SAFE_FLAG": "1"})
        assert "PATH" in sanitized
        assert "OPENAI_API_KEY" not in sanitized
        assert "AWS_SECRET_ACCESS_KEY" not in sanitized
        assert "GITHUB_TOKEN" not in sanitized
        assert "UNLISTED_RANDOM_VAR" not in sanitized
        assert sanitized["CUSTOM_SAFE_FLAG"] == "1"
        assert sanitized["PYTHONUNBUFFERED"] == "1"
