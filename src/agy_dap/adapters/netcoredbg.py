"""NetCoreDbg adapter implementation for local .NET ('dotnet test') debugging over DAP."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from agy_dap.adapters.base import DebugAdapterBase
from agy_dap.config import PluginConfig, ProfileConfig
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import validate_dotnet_test_selector
from agy_dap.utils.processes import build_sanitized_env, terminate_adapter_and_debuggee

logger = get_logger("agy-dap.adapters.netcoredbg")

_VSTEST_PID_RE = re.compile(r"Process\s+Id\s*:\s*(\d+)", re.IGNORECASE)


def parse_vstest_host_pid(text: str) -> Optional[int]:
    """Extract the suspended testhost.dll process ID emitted by 'dotnet test' with VSTEST_HOST_DEBUG=1."""
    m = _VSTEST_PID_RE.search(text or "")
    if not m:
        return None
    try:
        pid = int(m.group(1))
        return pid if pid > 0 else None
    except ValueError:
        return None


def resolve_dotnet_binary() -> Optional[str]:
    """Locate the 'dotnet' CLI executable on the host system."""
    env_dotnet = os.environ.get("AGY_DAP_DOTNET_PATH")
    if env_dotnet and Path(env_dotnet).is_file():
        return str(Path(env_dotnet).resolve())

    which_dotnet = shutil.which("dotnet")
    if which_dotnet:
        return which_dotnet

    candidates = [
        Path("C:/Program Files/dotnet/dotnet.exe"),
        Path("/usr/share/dotnet/dotnet"),
        Path("/usr/local/share/dotnet/dotnet"),
        Path.home() / ".dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def resolve_netcoredbg_binary(config: Optional[PluginConfig] = None) -> Optional[str]:
    """Locate the 'netcoredbg' DAP adapter executable on the host system."""
    env_bin = os.environ.get("AGY_DAP_NETCOREDBG_PATH")
    if env_bin and Path(env_bin).is_file():
        return str(Path(env_bin).resolve())

    which_bin = shutil.which("netcoredbg")
    if which_bin:
        return which_bin

    exe_name = "netcoredbg.exe" if os.name == "nt" else "netcoredbg"
    candidates = [
        Path.home() / "scoop" / "apps" / "netcoredbg" / "current" / exe_name,
        Path.home() / "scoop" / "shims" / exe_name,
        Path.home() / ".dotnet" / "tools" / exe_name,
        Path("/usr/local/bin/netcoredbg"),
        Path("/usr/bin/netcoredbg"),
        Path("/opt/homebrew/bin/netcoredbg"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


class NetCoreDbgAdapter(DebugAdapterBase):
    """Spawns 'netcoredbg --interpreter=vscode' and attaches to 'dotnet test' via VSTEST_HOST_DEBUG=1."""

    adapter_id: str = "coreclr"

    def __init__(self, config: PluginConfig) -> None:
        super().__init__(config)
        self._vstest_proc: Optional[asyncio.subprocess.Process] = None
        self._testhost_pid: Optional[int] = None

    async def spawn_adapter_process(self, profile: ProfileConfig) -> asyncio.subprocess.Process:
        netcoredbg_bin = resolve_netcoredbg_binary(self.config)
        if not netcoredbg_bin or not Path(netcoredbg_bin).is_file():
            install_hint = (
                "scoop install netcoredbg"
                if os.name == "nt"
                else "brew install netcoredbg (macOS) or download from https://github.com/Samsung/netcoredbg/releases"
            )
            raise DapPluginError(
                code=ErrorCode.DEPENDENCY_MISSING,
                message=(
                    f"NetCoreDbg binary ('netcoredbg') was not found on PATH. "
                    f"Install it via `{install_hint}` or set AGY_DAP_NETCOREDBG_PATH."
                ),
                retryable=False,
                details={"missing": "netcoredbg", "install_hint": install_hint},
            )

        cwd = self.config.resolve_profile_cwd(profile)
        sanitized_env = build_sanitized_env(
            extra_allowlist=self.config.envAllowlist,
            profile_env=profile.env,
        )

        cmd = [netcoredbg_bin, "--interpreter=vscode"]
        logger.info(f"Spawning netcoredbg adapter: {cmd} in {cwd}")

        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True

        try:
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=sanitized_env,
                **kwargs,
            )
        except Exception as e:
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message=f"Failed to start netcoredbg process: {e}",
                retryable=False,
                details={"binary": netcoredbg_bin},
            ) from e

    def build_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Validate dotnet test selector and build the dotnet test argument specification."""
        sel = validate_dotnet_test_selector(
            test_id,
            workspace_root,
            allow_outside=self.config.allowOutsideWorkspace,
        )
        cwd = self.config.resolve_profile_cwd(profile)

        base_args = list(profile.args) if profile.args else ["test"]
        if not base_args or base_args[0] != "test":
            base_args = ["test", *base_args]

        dotnet_args: list[str] = ["test"]
        if sel["project"]:
            dotnet_args.append(sel["project"])
        dotnet_args.extend(base_args[1:])
        if sel["filter"]:
            dotnet_args.extend(["--filter", sel["filter"]])

        return {
            "name": "agy-dap:dotnet_test",
            "type": "coreclr",
            "request": "attach",
            "cwd": cwd,
            "justMyCode": profile.justMyCode,
            "dotnet_test_args": dotnet_args,
            "project": sel["project"],
            "filter": sel["filter"],
        }

    async def prepare_launch_args(
        self,
        profile: ProfileConfig,
        test_id: str,
        workspace_root: str,
    ) -> dict[str, Any]:
        """Spawn 'dotnet test' with VSTEST_HOST_DEBUG=1, await the suspended testhost PID, and return DAP attach args."""
        base = self.build_launch_args(profile, test_id, workspace_root)
        dotnet_bin = resolve_dotnet_binary()
        if not dotnet_bin:
            raise DapPluginError(
                code=ErrorCode.DEPENDENCY_MISSING,
                message="The .NET SDK CLI ('dotnet') was not found on PATH.",
                retryable=False,
                details={"missing": "dotnet"},
            )

        cwd = base["cwd"]
        sanitized_env = build_sanitized_env(
            extra_allowlist=self.config.envAllowlist,
            profile_env={**profile.env, "VSTEST_HOST_DEBUG": "1"},
        )

        cmd = [dotnet_bin, *base["dotnet_test_args"]]
        logger.info(f"Starting VSTest host with VSTEST_HOST_DEBUG=1: {cmd} in {cwd}")

        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True

        try:
            self._vstest_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env=sanitized_env,
                **kwargs,
            )
        except Exception as e:
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message=f"Failed to launch 'dotnet test': {e}",
                retryable=False,
                details={"command": cmd},
            ) from e

        assert self._vstest_proc.stdout is not None
        collected_lines: list[str] = []
        startup_timeout = self.config.startup_timeout_sec
        loop = asyncio.get_running_loop()
        deadline = loop.time() + startup_timeout

        try:
            while True:
                rem = max(0.1, deadline - loop.time())
                raw_line = await asyncio.wait_for(self._vstest_proc.stdout.readline(), timeout=rem)
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    collected_lines.append(line)
                pid = parse_vstest_host_pid(line)
                if pid is not None:
                    self._testhost_pid = pid
                    logger.info(f"Detected suspended VSTest host process ID: {pid}")
                    break
        except asyncio.TimeoutError:
            await self.cleanup_auxiliary_processes()
            raise DapPluginError(
                code=ErrorCode.TIMEOUT,
                message=f"Timed out after {startup_timeout:.1f}s waiting for 'dotnet test' VSTEST_HOST_DEBUG PID.",
                retryable=True,
                details={"output_tail": collected_lines[-20:]},
            ) from None

        if self._testhost_pid is None:
            await self.cleanup_auxiliary_processes()
            raise DapPluginError(
                code=ErrorCode.ADAPTER_CRASHED,
                message=(
                    "'dotnet test' exited without emitting a debuggable testhost Process Id "
                    "(check for build errors or non-matching test filter)."
                ),
                retryable=False,
                details={"output_tail": collected_lines[-25:]},
            )

        return {
            "name": "agy-dap:dotnet_test",
            "type": "coreclr",
            "request": "attach",
            "processId": self._testhost_pid,
            "justMyCode": profile.justMyCode,
            "cwd": cwd,
        }

    async def cleanup_auxiliary_processes(self) -> None:
        """Terminate both the suspended/attached testhost PID and the parent 'dotnet test' runner process."""
        proc = self._vstest_proc
        pid = self._testhost_pid
        self._vstest_proc = None
        self._testhost_pid = None
        if proc is not None or pid is not None:
            try:
                await terminate_adapter_and_debuggee(
                    proc=proc,
                    debuggee_pid=pid,
                    timeout_sec=self.config.timeouts.cleanupTimeoutSec,
                )
            except Exception as e:
                logger.debug(f"Auxiliary dotnet_test cleanup warning: {e}")
