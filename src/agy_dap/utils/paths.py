"""Workspace path resolution, confinement checks, and pytest selector validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from agy_dap.errors import DapPluginError, ErrorCode

# Safe pytest node ID part: identifier or parametrized bracket expression without shell/control chars
_NODE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\x00-\x1f\r\n]*\])?$")


def normalize_path(path: str | Path) -> str:
    """Resolve a path (following symlinks) to an canonical absolute string."""
    p = Path(path).expanduser().resolve()
    path_str = str(p)
    if os.name == "nt":
        # Strip extended-length prefix if resolve() added it
        if path_str.startswith("\\\\?\\"):
            path_str = path_str[4:]
        if len(path_str) >= 2 and path_str[1] == ":":
            path_str = path_str[0].upper() + path_str[1:]
    return path_str


def find_workspace_root(start_path: Optional[str | Path] = None) -> str:
    """Traverse upwards from start_path to locate a workspace root."""
    current = Path(normalize_path(start_path or Path.cwd()))
    markers = [".git", "pyproject.toml", "setup.py", "agy-dap.json"]

    check_dir = current
    while True:
        for marker in markers:
            if (check_dir / marker).exists():
                return normalize_path(check_dir)
        parent = check_dir.parent
        if parent == check_dir:
            break
        check_dir = parent

    return normalize_path(current)


def is_safe_workspace_path(
    target_path: str | Path,
    workspace_root: str | Path,
    allow_outside: bool = False,
) -> bool:
    """Return True if target_path resolves inside workspace_root (accounting for symlinks and OS case)."""
    if allow_outside:
        return True
    try:
        ws_norm = normalize_path(workspace_root)
        cand = Path(target_path)
        if not cand.is_absolute():
            cand = Path(ws_norm) / cand
        target_norm = normalize_path(cand)

        if os.name == "nt":
            ws_cmp = Path(os.path.normcase(ws_norm))
            target_cmp = Path(os.path.normcase(target_norm))
            target_cmp.relative_to(ws_cmp)
        else:
            Path(target_norm).relative_to(Path(ws_norm))
        return True
    except (ValueError, OSError):
        return False


def ensure_workspace_path(
    target_path: str | Path,
    workspace_root: str | Path,
    allow_outside: bool = False,
    must_exist: bool = False,
) -> str:
    """Resolve target_path relative to workspace_root and enforce confinement."""
    if not target_path or not str(target_path).strip():
        raise DapPluginError(
            code=ErrorCode.PATH_OUTSIDE_WORKSPACE,
            message="Path must not be empty.",
            retryable=False,
        )

    ws_norm = normalize_path(workspace_root)
    raw = Path(target_path)
    joined = raw if raw.is_absolute() else (Path(ws_norm) / raw)

    if not is_safe_workspace_path(joined, ws_norm, allow_outside=allow_outside):
        raise DapPluginError(
            code=ErrorCode.PATH_OUTSIDE_WORKSPACE,
            message=f"Path '{target_path}' resolves outside the workspace root '{ws_norm}'.",
            retryable=False,
            details={"requested_path": str(target_path), "workspace_root": ws_norm},
        )

    resolved = normalize_path(joined)
    if must_exist and not Path(resolved).exists():
        raise DapPluginError(
            code=ErrorCode.INVALID_CONFIG,
            message=f"File does not exist: '{target_path}'.",
            retryable=False,
            details={"requested_path": str(target_path), "resolved_path": resolved},
        )
    return resolved


def to_workspace_relative(path: str | Path, workspace_root: str | Path) -> str:
    """Convert path to a clean POSIX relative path inside workspace_root when possible."""
    try:
        ws_norm = normalize_path(workspace_root)
        target_norm = normalize_path(path if Path(path).is_absolute() else Path(ws_norm) / path)
        if os.name == "nt":
            # Compare normcase for drive/folder casing, but preserve actual relative casing from Path
            ws_parts = Path(ws_norm).parts
            tgt_parts = Path(target_norm).parts
            if len(tgt_parts) >= len(ws_parts) and all(
                os.path.normcase(a) == os.path.normcase(b)
                for a, b in zip(ws_parts, tgt_parts[: len(ws_parts)], strict=False)
            ):
                rel_parts = tgt_parts[len(ws_parts) :]
                return "/".join(rel_parts) if rel_parts else "."
        else:
            rel = Path(target_norm).relative_to(Path(ws_norm))
            return rel.as_posix()
    except Exception:
        pass
    return str(path).replace("\\", "/")


def paths_refer_to_same_file(
    path_a: str | Path, path_b: str | Path, workspace_root: Optional[str | Path] = None
) -> bool:
    """Compare two file paths canonically across OS path conventions."""
    try:
        pa = Path(path_a)
        pb = Path(path_b)
        if workspace_root:
            ws = Path(normalize_path(workspace_root))
            if not pa.is_absolute():
                pa = ws / pa
            if not pb.is_absolute():
                pb = ws / pb
        na = normalize_path(pa)
        nb = normalize_path(pb)
        if os.name == "nt":
            return os.path.normcase(na) == os.path.normcase(nb)
        return na == nb
    except Exception:
        return False


def validate_pytest_selector(
    selector: str,
    workspace_root: str | Path,
    allow_outside: bool = False,
) -> tuple[str, str]:
    """Validate a pytest node selector and return (canonical_selector_arg, resolved_test_file).

    Prevents CLI flag injection (leading '-'), path traversal outside workspace,
    control characters, and non-Python targets.
    """
    if not selector or not isinstance(selector, str):
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message="Test selector must be a non-empty string.",
            retryable=False,
        )

    cleaned = selector.strip()
    if not cleaned:
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message="Test selector must not be whitespace only.",
            retryable=False,
        )

    if cleaned.startswith("-"):
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message=f"Invalid test selector '{selector}': CLI options (leading '-') are forbidden.",
            retryable=False,
            details={"selector": selector},
        )

    if any(ch in cleaned for ch in ("\x00", "\r", "\n", "\t")):
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message="Test selector contains forbidden control characters.",
            retryable=False,
            details={"selector": selector},
        )

    parts = cleaned.split("::")
    file_part = parts[0]
    node_parts = parts[1:]

    if not file_part:
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message=f"Test selector '{selector}' is missing a file path before '::'.",
            retryable=False,
            details={"selector": selector},
        )

    # Check workspace confinement first
    resolved_file = ensure_workspace_path(file_part, workspace_root, allow_outside=allow_outside, must_exist=False)
    resolved_path = Path(resolved_file)

    if resolved_path.suffix.lower() != ".py":
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message=f"Test selector file '{file_part}' must be a Python (.py) file.",
            retryable=False,
            details={"selector": selector, "file": file_part},
        )

    if not resolved_path.is_file():
        raise DapPluginError(
            code=ErrorCode.INVALID_TEST_SELECTOR,
            message=f"Test file '{file_part}' does not exist inside workspace.",
            retryable=False,
            details={"selector": selector, "resolved_file": resolved_file},
        )

    for node in node_parts:
        if not node or not _NODE_PART_RE.match(node):
            raise DapPluginError(
                code=ErrorCode.INVALID_TEST_SELECTOR,
                message=f"Invalid pytest node identifier '{node}' in selector '{selector}'.",
                retryable=False,
                details={"selector": selector, "invalid_node": node},
            )

    rel_file = to_workspace_relative(resolved_file, workspace_root)
    if node_parts:
        canonical_selector = "::".join([resolved_file, *node_parts])
    else:
        canonical_selector = resolved_file
    _ = rel_file
    return canonical_selector, resolved_file
