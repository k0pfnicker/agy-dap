"""Implementation of the 'debug_doctor' diagnostic MCP tool."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

from agy_dap.adapters.netcoredbg import resolve_dotnet_binary, resolve_netcoredbg_binary
from agy_dap.config import PluginConfig
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.sessions.manager import SessionManager
from agy_dap.utils.paths import find_workspace_root
from agy_dap.utils.processes import build_sanitized_env


async def _probe_python_module(interpreter: str, module_name: str, env: dict[str, str]) -> dict[str, Any]:
    """Check if a module is importable in the target interpreter and retrieve its version."""
    code = f"import {module_name}; print(getattr({module_name}, '__version__', 'installed'))"
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            version = stdout.decode("utf-8", errors="replace").strip() or "installed"
            return {"available": True, "version": version}
        err_msg = stderr.decode("utf-8", errors="replace").strip()
        return {"available": False, "version": None, "error": err_msg}
    except Exception as e:
        return {"available": False, "version": None, "error": str(e)}


async def _probe_python_version(interpreter: str, env: dict[str, str]) -> dict[str, Any]:
    """Check if the target Python interpreter runs and return its version string."""
    if not Path(interpreter).is_file():
        return {
            "available": False,
            "path": interpreter,
            "version": None,
            "error": f"Interpreter file not found at '{interpreter}'.",
        }
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter,
            "-c",
            "import sys; print(sys.version.split()[0])",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            ver = stdout.decode("utf-8", errors="replace").strip()
            return {"available": True, "path": interpreter, "version": ver}
        return {
            "available": False,
            "path": interpreter,
            "version": None,
            "error": stderr.decode("utf-8", errors="replace").strip(),
        }
    except Exception as e:
        return {"available": False, "path": interpreter, "version": None, "error": str(e)}


async def _probe_cli_version(binary_path: Optional[str], args: list[str], env: dict[str, str]) -> dict[str, Any]:
    """Check if a CLI binary runs and return its version string."""
    if not binary_path or not Path(binary_path).is_file():
        return {"available": False, "path": binary_path, "version": None, "error": "Binary not found on PATH"}
    try:
        proc = await asyncio.create_subprocess_exec(
            binary_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0:
            first_line = (stdout.decode("utf-8", errors="replace").strip().splitlines() or ["installed"])[0]
            return {"available": True, "path": binary_path, "version": first_line.strip()}
        return {
            "available": False,
            "path": binary_path,
            "version": None,
            "error": stderr.decode("utf-8", errors="replace").strip() or f"Exit code {proc.returncode}",
        }
    except Exception as e:
        return {"available": False, "path": binary_path, "version": None, "error": str(e)}


async def debug_doctor_tool(
    config: PluginConfig,
    manager: Optional[SessionManager] = None,
    profile: str = "pytest",
) -> dict[str, Any]:
    """Inspect workspace, Python interpreter, debugpy, pytest, dotnet, netcoredbg, profiles, and configuration."""
    remediations: list[str] = []
    config_valid = True
    config_error: Optional[str] = None

    try:
        config.validate()
    except DapPluginError as e:
        config_valid = False
        config_error = e.message
        remediations.append(f"Fix configuration error in agy-dap.json: {e.message}")

    ws_root = config.workspaceRoot or find_workspace_root()
    ws_exists = Path(ws_root).is_dir()
    if not ws_exists:
        remediations.append(f"Configure a valid 'workspaceRoot' directory (current: '{ws_root}').")

    prof_cfg = config.profiles.get(profile)
    if prof_cfg is None and config.profiles:
        prof_cfg = next(iter(config.profiles.values()))

    is_dotnet = prof_cfg is not None and prof_cfg.runner == "dotnet_test"
    interpreter = config.resolve_interpreter(prof_cfg)
    env = build_sanitized_env(extra_allowlist=config.envAllowlist)

    py_info = await _probe_python_version(interpreter, env)
    pytest_required = prof_cfg is None or prof_cfg.runner == "pytest"
    if not py_info["available"]:
        if not is_dotnet:
            remediations.append(
                f"Ensure Python interpreter exists at '{interpreter}' or configure 'pythonPath' in agy-dap.json."
            )
        debugpy_info = {"available": False, "version": None, "error": "Python interpreter unavailable"}
        pytest_info = {"available": False, "version": None, "error": "Python interpreter unavailable"}
    else:
        debugpy_info, pytest_info = await asyncio.gather(
            _probe_python_module(interpreter, "debugpy", env),
            _probe_python_module(interpreter, "pytest", env),
        )
        if not is_dotnet and not debugpy_info["available"]:
            remediations.append(f'Install debugpy in target interpreter: "{interpreter}" -m pip install debugpy')
        if pytest_required and not pytest_info["available"]:
            remediations.append(f'Install pytest in target interpreter: "{interpreter}" -m pip install pytest')

    dotnet_info, netcoredbg_info = await asyncio.gather(
        _probe_cli_version(resolve_dotnet_binary(), ["--version"], env),
        _probe_cli_version(resolve_netcoredbg_binary(config), ["--version"], env),
    )
    if is_dotnet:
        if not dotnet_info["available"]:
            remediations.append("Install the .NET SDK ('dotnet') and ensure it is on PATH: https://dot.net/download")
        if not netcoredbg_info["available"]:
            hint = "scoop install netcoredbg" if os.name == "nt" else "brew install netcoredbg"
            remediations.append(f"Install Samsung NetCoreDbg ('netcoredbg'): `{hint}` or set AGY_DAP_NETCOREDBG_PATH.")

    profiles_summary: dict[str, Any] = {}
    for pname, p in config.profiles.items():
        profiles_summary[pname] = {
            "runner": p.runner,
            "module": p.module,
            "args": p.args,
            "justMyCode": p.justMyCode,
            "stopOnUncaughtException": p.stopOnUncaughtException,
        }

    if is_dotnet:
        healthy = bool(config_valid and ws_exists and dotnet_info["available"] and netcoredbg_info["available"])
    else:
        healthy = bool(
            config_valid
            and ws_exists
            and py_info["available"]
            and debugpy_info["available"]
            and (pytest_info["available"] or not pytest_required)
        )

    result: dict[str, Any] = {
        "healthy": healthy,
        "workspace_root": ws_root,
        "workspace_exists": ws_exists,
        "selected_profile": profile if profile in config.profiles else None,
        "checks": {
            "config": {"valid": config_valid, "schemaVersion": config.schemaVersion, "error": config_error},
            "python": py_info,
            "debugpy": debugpy_info,
            "pytest": pytest_info,
            "dotnet": dotnet_info,
            "netcoredbg": netcoredbg_info,
        },
        "security": {
            "allowOutsideWorkspace": config.allowOutsideWorkspace,
            "enableEvaluation": config.enableEvaluation,
            "maxConcurrentSessions": config.maxConcurrentSessions,
        },
        "profiles": profiles_summary,
        "active_sessions": manager.active_session_ids if manager else [],
        "remediations": remediations,
    }

    if not healthy:
        req_checks = (
            (("dotnet", dotnet_info), ("netcoredbg", netcoredbg_info))
            if is_dotnet
            else (
                (("python", py_info), ("debugpy", debugpy_info), ("pytest", pytest_info))
                if pytest_required
                else (("python", py_info), ("debugpy", debugpy_info))
            )
        )
        missing = [name for name, info in req_checks if not info["available"]]
        if missing:
            result["error"] = {
                "code": ErrorCode.DEPENDENCY_MISSING.value,
                "message": f"Missing required debugging prerequisites: {', '.join(missing)}.",
                "retryable": False,
                "details": {"missing": missing, "remediations": remediations},
            }

    return result
