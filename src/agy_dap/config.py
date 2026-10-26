"""Versioned, validated configuration schema for agy-dap.

Supports safe defaults, explicit named launch profiles (starting with pytest),
timeout bounds, output truncation limits, and environment variable allowlists.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.utils.logging import get_logger
from agy_dap.utils.paths import ensure_workspace_path, find_workspace_root, normalize_path

logger = get_logger("agy-dap.config")

SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1"}


@dataclass
class ProfileConfig:
    """Explicit launch profile configuration.

    Defines runner type, interpreter, module/program, working directory,
    fixed arguments, and profile-specific environment variables.
    """

    runner: str = "pytest"
    pythonPath: Optional[str] = None
    module: str = "pytest"
    cwd: Optional[str] = None
    args: list[str] = field(default_factory=lambda: ["-q", "-s", "--no-header", "--tb=short"])
    env: dict[str, str] = field(default_factory=dict)
    justMyCode: bool = True
    stopOnUncaughtException: bool = True
    stopOnRaisedException: bool = False


@dataclass
class LimitsConfig:
    """Output and variable inspection limits to prevent context bloat."""

    maxStackFrames: int = 8
    maxVariablesPerScope: int = 25
    maxVariableDepth: int = 2
    maxContainerItems: int = 10
    maxStringLength: int = 240
    maxOutputLines: int = 40
    maxOutputLineLength: int = 300
    maxOutputBufferLines: int = 500
    maxSnapshotBytes: int = 16384


@dataclass
class TimeoutsConfig:
    """Timeouts for adapter initialization, individual requests, and sessions."""

    startupTimeoutMs: int = 15000
    requestTimeoutMs: int = 10000
    defaultInspectTimeoutSec: float = 30.0
    maxInspectTimeoutSec: float = 120.0
    idleSessionTimeoutSec: float = 120.0
    cleanupTimeoutSec: float = 5.0


@dataclass
class PluginConfig:
    """Master configuration for the agy-dap plugin."""

    schemaVersion: str = "1.0"
    workspaceRoot: Optional[str] = None
    pythonPath: Optional[str] = None
    allowOutsideWorkspace: bool = False
    enableEvaluation: bool = False
    includeCodeSnippets: bool = False
    snippetContextLines: int = 3
    maxConcurrentSessions: int = 1
    envAllowlist: list[str] = field(default_factory=list)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    profiles: dict[str, ProfileConfig] = field(
        default_factory=lambda: {
            "pytest": ProfileConfig(
                runner="pytest",
                module="pytest",
                args=["-q", "-s", "--no-header", "--tb=short"],
                justMyCode=True,
                stopOnUncaughtException=True,
                stopOnRaisedException=False,
            ),
            "unittest": ProfileConfig(
                runner="unittest",
                module="unittest",
                args=["-v"],
                justMyCode=True,
                stopOnUncaughtException=True,
                stopOnRaisedException=False,
            ),
            "python_module": ProfileConfig(
                runner="python_module",
                module="",
                args=[],
                justMyCode=True,
                stopOnUncaughtException=True,
                stopOnRaisedException=False,
            ),
            "dotnet_test": ProfileConfig(
                runner="dotnet_test",
                module="dotnet",
                args=["test"],
                justMyCode=True,
                stopOnUncaughtException=True,
                stopOnRaisedException=False,
            ),
        }
    )

    @property
    def startup_timeout_sec(self) -> float:
        return max(1.0, self.timeouts.startupTimeoutMs / 1000.0)

    @property
    def request_timeout_sec(self) -> float:
        return max(1.0, self.timeouts.requestTimeoutMs / 1000.0)

    def resolve_interpreter(self, profile: Optional[ProfileConfig] = None) -> str:
        """Resolve the Python interpreter to use for adapter/debuggee."""
        if profile and profile.pythonPath:
            cand = Path(profile.pythonPath)
            if not cand.is_absolute() and self.workspaceRoot:
                cand = Path(self.workspaceRoot) / cand
            return str(cand)

        if self.pythonPath:
            cand = Path(self.pythonPath)
            if not cand.is_absolute() and self.workspaceRoot:
                cand = Path(self.workspaceRoot) / cand
            return str(cand)

        # Check workspace .venv, active VIRTUAL_ENV, and plugin .venv
        venv_candidates: list[Path] = []
        if self.workspaceRoot:
            venv_candidates.append(Path(self.workspaceRoot) / ".venv")
        env_venv = os.environ.get("VIRTUAL_ENV")
        if env_venv:
            venv_candidates.append(Path(env_venv))
        venv_candidates.append(Path(__file__).resolve().parents[2] / ".venv")

        bin_folder = "Scripts" if os.name == "nt" else "bin"
        exe_name = "python.exe" if os.name == "nt" else "python"
        for venv_dir in venv_candidates:
            venv_py = venv_dir / bin_folder / exe_name
            if venv_py.is_file():
                return str(venv_py)

        return sys.executable

    def resolve_profile_cwd(self, profile: ProfileConfig) -> str:
        """Resolve and validate the working directory for a launch profile."""
        ws = self.workspaceRoot or find_workspace_root()
        if not profile.cwd:
            return normalize_path(ws)
        return ensure_workspace_path(
            profile.cwd,
            ws,
            allow_outside=self.allowOutsideWorkspace,
            must_exist=True,
        )

    def validate(self) -> None:
        """Validate configuration invariants and raise DapPluginError on invalid values."""
        if str(self.schemaVersion) not in SUPPORTED_SCHEMA_VERSIONS:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"Unsupported config schemaVersion '{self.schemaVersion}'. Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}.",
                retryable=False,
                details={"schemaVersion": self.schemaVersion},
            )

        if self.maxConcurrentSessions < 1:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="maxConcurrentSessions must be >= 1.",
                retryable=False,
            )

        if self.limits.maxStackFrames < 1 or self.limits.maxVariablesPerScope < 1:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="limits.maxStackFrames and limits.maxVariablesPerScope must be >= 1.",
                retryable=False,
            )

        if self.limits.maxVariableDepth < 0 or self.limits.maxContainerItems < 1 or self.limits.maxStringLength < 16:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Invalid variable truncation limits in configuration.",
                retryable=False,
            )

        if self.timeouts.startupTimeoutMs <= 0 or self.timeouts.requestTimeoutMs <= 0:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Timeouts must be positive integers.",
                retryable=False,
            )

        if self.timeouts.defaultInspectTimeoutSec <= 0 or self.timeouts.maxInspectTimeoutSec <= 0:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Inspection timeouts must be > 0.",
                retryable=False,
            )

        if not self.profiles:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message="Configuration must define at least one launch profile.",
                retryable=False,
            )

        supported_runners = {"pytest", "unittest", "python_module", "dotnet_test"}
        for name, prof in self.profiles.items():
            if prof.runner not in supported_runners:
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=(
                        f"Profile '{name}' uses unsupported runner '{prof.runner}'. "
                        f"Supported runners: {sorted(supported_runners)}."
                    ),
                    retryable=False,
                    details={"profile": name, "runner": prof.runner, "module": prof.module},
                )
            if prof.runner == "pytest" and prof.module != "pytest":
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Profile '{name}' with runner 'pytest' must use module 'pytest' (got '{prof.module}').",
                    retryable=False,
                    details={"profile": name, "runner": prof.runner, "module": prof.module},
                )
            if prof.runner == "unittest" and prof.module != "unittest":
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Profile '{name}' with runner 'unittest' must use module 'unittest' (got '{prof.module}').",
                    retryable=False,
                    details={"profile": name, "runner": prof.runner, "module": prof.module},
                )
            if not isinstance(prof.args, list) or not all(isinstance(a, str) for a in prof.args):
                raise DapPluginError(
                    code=ErrorCode.INVALID_CONFIG,
                    message=f"Profile '{name}' args must be a list of strings.",
                    retryable=False,
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(
    custom_path: Optional[str | Path] = None,
    workspace_dir: Optional[str | Path] = None,
    strict: bool = False,
) -> PluginConfig:
    """Load and merge configuration from defaults, files, and environment overrides."""
    cfg = PluginConfig()

    root = find_workspace_root(workspace_dir or os.getcwd())
    cfg.workspaceRoot = root

    candidates: list[tuple[Path, bool]] = []
    env_cfg = os.environ.get("AGY_DAP_CONFIG")
    if custom_path:
        candidates.append((Path(custom_path), True))
    elif env_cfg:
        candidates.append((Path(env_cfg), True))

    candidates.append((Path(root) / "agy-dap.json", strict))
    candidates.append((Path(root) / ".gemini" / "agy-dap.json", strict))
    candidates.append((Path.home() / ".gemini" / "config" / "agy-dap.json", False))

    for cand, is_explicit in candidates:
        if cand.is_file():
            try:
                raw_text = cand.read_text(encoding="utf-8")
                data = json.loads(raw_text)
                if not isinstance(data, dict):
                    raise ValueError("Root of config file must be a JSON object.")
                _apply_dict_to_config(cfg, data)
                logger.info(f"Loaded configuration from {cand}")
                break
            except DapPluginError:
                raise
            except Exception as e:
                if is_explicit or strict:
                    raise DapPluginError(
                        code=ErrorCode.INVALID_CONFIG,
                        message=f"Failed to parse configuration file '{cand}': {e}",
                        retryable=False,
                        details={"config_file": str(cand)},
                    ) from e
                logger.warning(f"Failed to parse config file {cand}: {e}")
        elif is_explicit and custom_path:
            raise DapPluginError(
                code=ErrorCode.INVALID_CONFIG,
                message=f"Explicit configuration file does not exist: '{cand}'.",
                retryable=False,
                details={"config_file": str(cand)},
            )

    # Environment overrides
    if os.environ.get("AGY_DAP_ENABLE_EVALUATE", "").lower() in ("1", "true", "yes"):
        cfg.enableEvaluation = True
    if os.environ.get("AGY_DAP_ALLOW_OUTSIDE", "").lower() in ("1", "true", "yes"):
        cfg.allowOutsideWorkspace = True
    if os.environ.get("AGY_DAP_WORKSPACE_ROOT"):
        cfg.workspaceRoot = os.environ["AGY_DAP_WORKSPACE_ROOT"]
    if os.environ.get("AGY_DAP_PYTHON_PATH"):
        cfg.pythonPath = os.environ["AGY_DAP_PYTHON_PATH"]

    if cfg.workspaceRoot:
        cfg.workspaceRoot = normalize_path(cfg.workspaceRoot)

    cfg.validate()
    return cfg


def _apply_dict_to_config(cfg: PluginConfig, data: dict[str, Any]) -> None:
    """Apply dictionary values to a PluginConfig instance with type validation."""
    try:
        if "schemaVersion" in data:
            cfg.schemaVersion = str(data["schemaVersion"])
        if "workspaceRoot" in data and data["workspaceRoot"] is not None:
            cfg.workspaceRoot = str(data["workspaceRoot"])
        if "pythonPath" in data and data["pythonPath"] is not None:
            cfg.pythonPath = str(data["pythonPath"])
        if "allowOutsideWorkspace" in data:
            if not isinstance(data["allowOutsideWorkspace"], bool):
                raise ValueError("allowOutsideWorkspace must be a boolean")
            cfg.allowOutsideWorkspace = data["allowOutsideWorkspace"]
        if "enableEvaluation" in data:
            if not isinstance(data["enableEvaluation"], bool):
                raise ValueError("enableEvaluation must be a boolean")
            cfg.enableEvaluation = data["enableEvaluation"]
        if "includeCodeSnippets" in data:
            if not isinstance(data["includeCodeSnippets"], bool):
                raise ValueError("includeCodeSnippets must be a boolean")
            cfg.includeCodeSnippets = data["includeCodeSnippets"]
        if "snippetContextLines" in data:
            cfg.snippetContextLines = max(0, int(data["snippetContextLines"]))
        if "maxConcurrentSessions" in data:
            cfg.maxConcurrentSessions = int(data["maxConcurrentSessions"])
        if "envAllowlist" in data:
            if not isinstance(data["envAllowlist"], list) or not all(isinstance(x, str) for x in data["envAllowlist"]):
                raise ValueError("envAllowlist must be a list of strings")
            cfg.envAllowlist = list(data["envAllowlist"])

        if "limits" in data:
            if not isinstance(data["limits"], dict):
                raise ValueError("limits must be an object")
            lim = data["limits"]
            if "maxStackFrames" in lim:
                cfg.limits.maxStackFrames = int(lim["maxStackFrames"])
            if "maxVariablesPerScope" in lim:
                cfg.limits.maxVariablesPerScope = int(lim["maxVariablesPerScope"])
            if "maxVariableDepth" in lim:
                cfg.limits.maxVariableDepth = int(lim["maxVariableDepth"])
            if "maxContainerItems" in lim:
                cfg.limits.maxContainerItems = int(lim["maxContainerItems"])
            if "maxStringLength" in lim:
                cfg.limits.maxStringLength = int(lim["maxStringLength"])
            if "maxOutputLines" in lim:
                cfg.limits.maxOutputLines = int(lim["maxOutputLines"])
            if "maxOutputLineLength" in lim:
                cfg.limits.maxOutputLineLength = int(lim["maxOutputLineLength"])
            if "maxOutputBufferLines" in lim:
                cfg.limits.maxOutputBufferLines = int(lim["maxOutputBufferLines"])
            if "maxSnapshotBytes" in lim:
                cfg.limits.maxSnapshotBytes = int(lim["maxSnapshotBytes"])

        if "timeouts" in data:
            if not isinstance(data["timeouts"], dict):
                raise ValueError("timeouts must be an object")
            t = data["timeouts"]
            if "startupTimeoutMs" in t:
                cfg.timeouts.startupTimeoutMs = int(t["startupTimeoutMs"])
            if "requestTimeoutMs" in t:
                cfg.timeouts.requestTimeoutMs = int(t["requestTimeoutMs"])
            if "defaultInspectTimeoutSec" in t:
                cfg.timeouts.defaultInspectTimeoutSec = float(t["defaultInspectTimeoutSec"])
            if "maxInspectTimeoutSec" in t:
                cfg.timeouts.maxInspectTimeoutSec = float(t["maxInspectTimeoutSec"])
            if "idleSessionTimeoutSec" in t:
                cfg.timeouts.idleSessionTimeoutSec = float(t["idleSessionTimeoutSec"])
            if "cleanupTimeoutSec" in t:
                cfg.timeouts.cleanupTimeoutSec = float(t["cleanupTimeoutSec"])

        if "profiles" in data:
            if not isinstance(data["profiles"], dict):
                raise ValueError("profiles must be an object")
            new_profiles: dict[str, ProfileConfig] = {}
            for name, pdata in data["profiles"].items():
                if not isinstance(pdata, dict):
                    raise ValueError(f"Profile '{name}' must be an object")
                runner_val = str(pdata.get("runner", "pytest"))
                default_mod = (
                    "unittest"
                    if runner_val == "unittest"
                    else (
                        "" if runner_val == "python_module" else ("dotnet" if runner_val == "dotnet_test" else "pytest")
                    )
                )
                default_args = (
                    ["-v"]
                    if runner_val == "unittest"
                    else (
                        []
                        if runner_val == "python_module"
                        else (["test"] if runner_val == "dotnet_test" else ["-q", "-s", "--no-header", "--tb=short"])
                    )
                )
                args_val = pdata.get("args", default_args)
                if not isinstance(args_val, list) or not all(isinstance(x, str) for x in args_val):
                    raise ValueError(f"Profile '{name}' args must be a list of strings")
                env_val = pdata.get("env", {})
                if not isinstance(env_val, dict):
                    raise ValueError(f"Profile '{name}' env must be an object")
                new_profiles[name] = ProfileConfig(
                    runner=runner_val,
                    pythonPath=pdata.get("pythonPath"),
                    module=str(pdata.get("module", default_mod)),
                    cwd=pdata.get("cwd"),
                    args=list(args_val),
                    env={str(k): str(v) for k, v in env_val.items()},
                    justMyCode=bool(pdata.get("justMyCode", True)),
                    stopOnUncaughtException=bool(pdata.get("stopOnUncaughtException", True)),
                    stopOnRaisedException=bool(pdata.get("stopOnRaisedException", False)),
                )
            cfg.profiles = new_profiles
    except DapPluginError:
        raise
    except Exception as e:
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message=f"Invalid configuration field: {e}",
            retryable=False,
        ) from e
