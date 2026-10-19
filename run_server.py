#!/usr/bin/env python3
"""Runner script for agy-dap MCP Server.

Ensures the plugin's local .venv (if present) is used and src/ is added to sys.path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent
src_dir = root_dir / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

venv_dir = root_dir / ".venv"
if venv_dir.is_dir() and "AGY_DAP_VENV_ACTIVE" not in os.environ:
    venv_py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if venv_py.is_file() and Path(sys.executable).resolve() != venv_py.resolve():
        env = os.environ.copy()
        env["AGY_DAP_VENV_ACTIVE"] = "1"
        try:
            res = subprocess.call([str(venv_py), __file__] + sys.argv[1:], env=env)
            sys.exit(res)
        except Exception:
            pass

from agy_dap.server import main  # noqa: E402

if __name__ == "__main__":
    main()
