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
