"""Unit tests for workspace confinement, pytest selector validation, and environment sanitization."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.paths import ensure_workspace_path, validate_pytest_selector
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
