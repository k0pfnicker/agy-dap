"""Shared pytest fixtures for unit and integration tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agy_dap.adapters.base import DebugAdapterBase
from agy_dap.config import PluginConfig, ProfileConfig
from agy_dap.utils.paths import normalize_path, validate_pytest_selector


class FakeSubprocessAdapter(DebugAdapterBase):
    """Spawns tests/fixtures/fake_adapter.py as a controlled stdio DAP process."""

    adapter_id = "fake-dap"

    def __init__(self, config: PluginConfig, mode: str = "normal") -> None:
        super().__init__(config)
        self.mode = mode

    async def spawn_adapter_process(self, profile: ProfileConfig) -> asyncio.subprocess.Process:
        fake_script = Path(__file__).parent / "fixtures" / "fake_adapter.py"
        env = os.environ.copy()
        env["FAKE_DAP_MODE"] = self.mode
        env["PYTHONUNBUFFERED"] = "1"
        return await asyncio.create_subprocess_exec(
            sys.executable,
            str(fake_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.workspaceRoot,
            env=env,
        )

    def build_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        canonical_selector, _ = validate_pytest_selector(
            test_id,
            workspace_root,
            allow_outside=self.config.allowOutsideWorkspace,
        )
        return {
            "request": "launch",
            "type": "python",
            "module": "pytest",
            "args": [*profile.args, canonical_selector],
            "cwd": workspace_root,
        }


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_project_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_pytest_project"


@pytest.fixture
def test_config(sample_project_dir: Path) -> PluginConfig:
    cfg = PluginConfig()
    cfg.workspaceRoot = normalize_path(sample_project_dir)
    cfg.pythonPath = sys.executable
    cfg.allowOutsideWorkspace = False
    cfg.enableEvaluation = False
    cfg.maxConcurrentSessions = 1
    return cfg
