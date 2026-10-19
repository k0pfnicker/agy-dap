"""Process tree termination and environment variable sanitization utilities."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any, Optional

from agy_dap.utils.logging import get_logger
from agy_dap.utils.redaction import is_sensitive_key

logger = get_logger("agy-dap.processes")

# Safe platform/runtime environment variables allowed into the debuggee by default.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    # Common POSIX / Cross-platform
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TZ",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    # Windows required system variables (needed for winsock/DLL loading/subprocess)
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)


def build_sanitized_env(
    extra_allowlist: Optional[list[str]] = None,
    profile_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Construct a sanitized environment dictionary for adapter and debuggee processes.

    Only copies variables on the allowlist from os.environ (skipping any key that
    matches sensitive secret names unless explicitly provided in profile_env).
    """
    allowed_upper = {k.upper() for k in DEFAULT_ENV_ALLOWLIST}
    if extra_allowlist:
        allowed_upper.update(k.upper() for k in extra_allowlist)

    sanitized: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.upper() in allowed_upper and not is_sensitive_key(k):
            sanitized[k] = v

    # Explicit profile overrides
    if profile_env:
        for k, v in profile_env.items():
            sanitized[str(k)] = str(v)

    sanitized["PYTHONUNBUFFERED"] = "1"
    return sanitized


def is_pid_alive(pid: Optional[int]) -> bool:
    """Return True if a process with the given PID currently exists."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


async def kill_pid_tree(pid: int, timeout_sec: float = 3.0) -> bool:
    """Terminate a specific process tree by PID without affecting unrelated processes.

    Returns True if the process is confirmed dead afterwards.
    """
    if not is_pid_alive(pid):
        return True

    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=timeout_sec)
        except Exception as e:
            logger.debug(f"taskkill for PID {pid} encountered: {e}")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    else:
        # Try process group kill if pid is a process group leader, else direct kill
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        # Wait briefly for graceful exit before SIGKILL
        deadline = asyncio.get_running_loop().time() + min(timeout_sec, 1.0)
        while is_pid_alive(pid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        if is_pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    # Brief wait for OS process table cleanup
    deadline = asyncio.get_running_loop().time() + min(timeout_sec, 1.5)
    while is_pid_alive(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    return not is_pid_alive(pid)


async def terminate_adapter_and_debuggee(
    proc: Optional[asyncio.subprocess.Process],
    debuggee_pid: Optional[int] = None,
    timeout_sec: float = 3.0,
) -> dict[str, Any]:
    """Reliably terminate both debuggee process tree and adapter subprocess."""
    adapter_pid = proc.pid if proc else None
    debuggee_cleaned = True
    adapter_cleaned = True

    # 1. Kill debuggee process tree if known and distinct
    if debuggee_pid and debuggee_pid != adapter_pid:
        debuggee_cleaned = await kill_pid_tree(debuggee_pid, timeout_sec=timeout_sec)

    # 2. Kill adapter process tree
    if proc is not None:
        if proc.returncode is None and adapter_pid:
            await kill_pid_tree(adapter_pid, timeout_sec=timeout_sec)
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        except Exception:
            pass
        adapter_cleaned = proc.returncode is not None and not is_pid_alive(adapter_pid)
    elif adapter_pid:
        adapter_cleaned = await kill_pid_tree(adapter_pid, timeout_sec=timeout_sec)

    if debuggee_pid:
        debuggee_cleaned = not is_pid_alive(debuggee_pid)

    return {
        "adapter_pid": adapter_pid,
        "debuggee_pid": debuggee_pid,
        "adapter_terminated": adapter_cleaned,
        "debuggee_terminated": debuggee_cleaned,
        "clean": bool(adapter_cleaned and debuggee_cleaned),
    }
