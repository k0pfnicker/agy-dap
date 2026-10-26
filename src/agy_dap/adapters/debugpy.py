"""debugpy adapter implementation for local Python pytest debugging."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agy_dap.adapters.base import DebugAdapterBase
from agy_dap.config import ProfileConfig
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import (
    validate_pytest_selector,
    validate_python_module_selector,
    validate_unittest_selector,
)
from agy_dap.utils.processes import build_sanitized_env

logger = get_logger("agy-dap.adapters.debugpy")


class DebugpyAdapter(DebugAdapterBase):
    """Spawns 'python -m debugpy.adapter' and constructs safe pytest launch configurations."""

    adapter_id: str = "debugpy"

    async def spawn_adapter_process(self, profile: ProfileConfig) -> asyncio.subprocess.Process:
        interpreter = self.config.resolve_interpreter(profile)
        if not Path(interpreter).is_file():
            raise DapPluginError(
                code=ErrorCode.DEPENDENCY_MISSING,
                message=f"Configured Python interpreter does not exist: '{interpreter}'.",
                retryable=False,
                details={"interpreter": interpreter},
            )

        cwd = self.config.resolve_profile_cwd(profile)
        sanitized_env = build_sanitized_env(
            extra_allowlist=self.config.envAllowlist,
            profile_env=profile.env,
        )

        cmd = [interpreter, "-m", "debugpy.adapter"]
        logger.info(f"Spawning debugpy adapter: {cmd} in {cwd}")

        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=sanitized_env,
                **kwargs,
            )
            return proc
        except FileNotFoundError as e:
            raise DapPluginError(
                code=ErrorCode.DEPENDENCY_MISSING,
                message=f"Failed to launch Python interpreter '{interpreter}': {e}",
                retryable=False,
                details={"interpreter": interpreter},
            ) from e
        except Exception as e:
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message=f"Failed to start debugpy.adapter process: {e}",
                retryable=False,
                details={"interpreter": interpreter},
            ) from e

    def build_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Validate target selector and construct debugpy launch request arguments for pytest, unittest, or python_module."""
        interpreter = self.config.resolve_interpreter(profile)
        cwd = self.config.resolve_profile_cwd(profile)
        sanitized_env = build_sanitized_env(
            extra_allowlist=self.config.envAllowlist,
            profile_env=profile.env,
        )

        # Ensure src/ and workspace root are on PYTHONPATH for local package imports
        existing_py_path = sanitized_env.get("PYTHONPATH", "")
        src_dir = Path(cwd) / "src"
        path_entries = [cwd]
        if src_dir.is_dir():
            path_entries.insert(0, str(src_dir))
        if existing_py_path:
            path_entries.append(existing_py_path)
        sanitized_env["PYTHONPATH"] = os.pathsep.join(path_entries)

        base_launch: dict[str, Any] = {
            "name": f"agy-dap:{profile.runner}",
            "type": "python",
            "request": "launch",
            "python": [interpreter],
            "cwd": cwd,
            "env": sanitized_env,
            "console": "internalConsole",
            "redirectOutput": True,
            "justMyCode": profile.justMyCode,
            "showReturnValue": False,
            "subProcess": False,
        }

        if profile.runner == "pytest":
            canonical_selector, _ = validate_pytest_selector(
                test_id,
                workspace_root,
                allow_outside=self.config.allowOutsideWorkspace,
            )
            base_launch["module"] = profile.module or "pytest"
            base_launch["args"] = [*profile.args, canonical_selector]
            return base_launch

        if profile.runner == "unittest":
            unittest_args, _ = validate_unittest_selector(
                test_id,
                workspace_root,
                allow_outside=self.config.allowOutsideWorkspace,
            )
            base_launch["module"] = profile.module or "unittest"
            base_launch["args"] = [*profile.args, *unittest_args]
            return base_launch

        if profile.runner == "python_module":
            mod_info = validate_python_module_selector(
                test_id,
                workspace_root,
                allow_outside=self.config.allowOutsideWorkspace,
            )
            if mod_info["mode"] == "program":
                base_launch["program"] = mod_info["target"]
            else:
                base_launch["module"] = mod_info["target"]
            base_launch["args"] = list(profile.args)
            return base_launch

        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message=f"Unsupported runner '{profile.runner}' in DebugpyAdapter.",
            retryable=False,
            details={"runner": profile.runner},
        )
