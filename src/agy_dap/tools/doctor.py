"""Implementation of the 'debug_doctor' diagnostic MCP tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

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


async def debug_doctor_tool(
    config: PluginConfig,
    manager: Optional[SessionManager] = None,
    profile: str = "pytest",
) -> dict[str, Any]:
    """Inspect workspace, Python interpreter, debugpy, pytest, profiles, and configuration."""
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

    interpreter = config.resolve_interpreter(prof_cfg)
    env = build_sanitized_env(extra_allowlist=config.envAllowlist)

    py_info = await _probe_python_version(interpreter, env)
    if not py_info["available"]:
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
        if not debugpy_info["available"]:
            remediations.append(f'Install debugpy in target interpreter: "{interpreter}" -m pip install debugpy')
        if not pytest_info["available"]:
            remediations.append(f'Install pytest in target interpreter: "{interpreter}" -m pip install pytest')

    profiles_summary: dict[str, Any] = {}
    for pname, p in config.profiles.items():
        profiles_summary[pname] = {
            "runner": p.runner,
            "module": p.module,
            "args": p.args,
            "justMyCode": p.justMyCode,
            "stopOnUncaughtException": p.stopOnUncaughtException,
        }

    healthy = bool(
        config_valid and ws_exists and py_info["available"] and debugpy_info["available"] and pytest_info["available"]
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

    if not healthy and not (py_info["available"] and debugpy_info["available"] and pytest_info["available"]):
        missing = [
            name
            for name, info in (("python", py_info), ("debugpy", debugpy_info), ("pytest", pytest_info))
            if not info["available"]
        ]
        result["error"] = {
            "code": ErrorCode.DEPENDENCY_MISSING.value,
            "message": f"Missing required debugging prerequisites: {', '.join(missing)}.",
            "retryable": False,
            "details": {"missing": missing, "remediations": remediations},
        }

    return result
