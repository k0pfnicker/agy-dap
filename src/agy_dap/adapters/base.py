"""Minimal adapter abstraction separating debugpy specifics from DAP session management."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from agy_dap.config import PluginConfig, ProfileConfig


class DebugAdapterBase(ABC):
    """Minimal base interface for spawning a stdio DAP adapter and building launch parameters."""

    adapter_id: str = "generic"

    def __init__(self, config: PluginConfig) -> None:
        self.config = config

    @abstractmethod
    async def spawn_adapter_process(self, profile: ProfileConfig) -> asyncio.subprocess.Process:
        """Spawn the debug adapter subprocess over stdio without a shell."""

    @abstractmethod
    def build_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Build the adapter-specific DAP 'launch' (or 'attach') request arguments."""

    async def prepare_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Prepare and return DAP launch/attach arguments (e.g. spawning VSTest host for attach adapters)."""
        return self.build_launch_args(profile, test_id, workspace_root)

    async def cleanup_auxiliary_processes(self) -> None:
        """Clean up any auxiliary processes spawned during prepare_launch_args (e.g. dotnet test host)."""
        return None
