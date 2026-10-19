# agy-dap — Hypothesis-Driven Debug Adapter Protocol (DAP) Plugin for Antigravity CLI

[![Tests](https://img.shields.io/badge/tests-34%20passed-brightgreen)](#12-tested-versions-platforms--ci)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](#12-tested-versions-platforms--ci)
[![MCP](https://img.shields.io/badge/MCP-compatible-orange)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*[English](README.md) | [Deutsch](README.de.md)*

**agy-dap** is a local, agent-friendly debugging plugin for the **Google Antigravity CLI (`agy`)**. It connects AI coding agents to Python's **`debugpy`** debugger over the **Debug Adapter Protocol (DAP)** and exposes compact, deterministic runtime inspection tools via the **Model Context Protocol (MCP)** over `stdio`.

Rather than guessing runtime behavior solely from static source code and test logs, an agent can formulate a concrete hypothesis for a failing `pytest` test, place a verified breakpoint, and inspect actual stack frames and local variables in a single bounded call.

---

## Table of Contents

1. [Purpose & Non-Goals](#1-purpose--non-goals)
2. [Architecture & Session Lifecycle](#2-architecture--session-lifecycle)
3. [Security & Trust Model](#3-security--trust-model)
4. [Installation & Setup](#4-installation--setup)
   - [Windows (PowerShell)](#windows-powershell)
   - [Linux / macOS (Bash)](#linux--macos-bash)
5. [Antigravity CLI & MCP Integration](#5-antigravity-cli--mcp-integration)
6. [Configuration (`agy-dap.json`)](#6-configuration-agy-dapjson)
7. [MCP Tools Reference](#7-mcp-tools-reference)
8. [Reproducible Walkthrough on the Fixture Project](#8-reproducible-walkthrough-on-the-fixture-project)
9. [Collaboration with `agy-lsp`](#9-collaboration-with-agy-lsp)
10. [Error Contract & Troubleshooting](#10-error-contract--troubleshooting)
11. [Measurements & Evaluation Methodology](#11-measurements--evaluation-methodology)
12. [Tested Versions, Platforms & CI](#12-tested-versions-platforms--ci)
13. [Known Limitations & Future Extensions](#13-known-limitations--future-extensions)
14. [Official References](#14-official-references)

---

## 1. Purpose & Non-Goals

### Hypothesis-Driven Workflow
1. The agent identifies a reproducible failing test and states a specific runtime hypothesis.
2. The agent selects a relevant source file/line breakpoint and the exact local variable names needed to test that hypothesis.
3. `agy-dap` launches the test under `debugpy.adapter` using an explicit project profile.
4. Execution pauses at the verified breakpoint (or at an unhandled/raised exception before the target).
5. `agy-dap` returns a compact, structured snapshot (`stack`, filtered `locals`, folded `output_tail`, and `breakpoints`).
6. The agent decides on the fix based on observed values.
7. After applying the fix, the agent runs `pytest` normally outside the debugger to verify the repair.

`agy-dap` provides **runtime observations**, not fabricated root-cause explanations or automatic code repairs.

### Explicit Non-Goals (MVP)
- No remote debugging or attaching to arbitrary external PIDs.
- No production system, container orchestration, or browser debugging.
- Python (`debugpy` + `pytest`) is the sole target runtime in the MVP.
- No arbitrary shell commands or free-form executables via tool arguments.
- No automatic code modifications or write-back debugger mutations (`setVariable`, `setExpression`).
- No reverse debugging / time-travel or unbounded object graph dumps.

---

## 2. Architecture & Session Lifecycle

```text
┌───────────────────────────┐
│   Antigravity Agent / CLI │
└─────────────┬─────────────┘
              │ MCP (stdio)
┌─────────────▼─────────────┐
│    agy-dap MCP Server     │
│  ┌─────────────────────┐  │
│  │   SessionManager    │  │
│  │ (Lifecycle, Locks,  │  │
│  │  Idle Watchdog)     │  │
│  └──────────┬──────────┘  │
│  ┌──────────▼──────────┐  │
│  │     DapClient       │  │
│  │ (Framing, Handshake,│  │
│  │  Snapshot Builder)  │  │
│  └──────────┬──────────┘  │
└─────────────┼─────────────┘
              │ DAP (Content-Length stdio)
┌─────────────▼─────────────┐
│      debugpy.adapter      │
│             │             │
│             ▼             │
│   pytest Debuggee Process │
└───────────────────────────┘
```

### Package Layout

```text
src/agy_dap/
  server.py          # MCP stdio server & tool registration
  config.py          # Versioned schema (1.0), profiles, limits, timeouts
  models.py          # Breakpoint, StackFrame, Variable, and StopEvent models
  errors.py          # Machine-readable error contract (17 error codes)
  symbols.py         # Optional SymbolResolver protocol for future LSP integration
  dap/
    transport.py     # Byte-exact Content-Length framing & single-reader stdio transport
    client.py        # Request correlation, async event queues, launch handshake
    capabilities.py  # Adapter capability inspection & enforcement
  adapters/
    base.py          # Minimal adapter launcher interface
    debugpy.py       # debugpy.adapter subprocess spawner & pytest launch builder
  sessions/
    lifecycle.py     # State machine & stop-generation stale-reference guard
    manager.py       # Session concurrency, per-session lock, idle watchdog, cleanup
  inspection/
    snapshots.py     # Compact snapshot builder with byte budget & optional code snippets
    variables.py     # Scopes/variables traversal, falsy preservation, redaction
    output.py        # Bounded output ring buffer with repeat-line folding
  tools/
    doctor.py        # debug_doctor prerequisite & environment checker
    inspect.py       # debug_inspect one-shot investigation tool
    sessions.py      # Stateful tools (debug_start, debug_snapshot, debug_continue, debug_evaluate, debug_stop)
  utils/
    paths.py         # Workspace confinement & pytest selector validation
    processes.py     # Sanitized env allowlist & cross-platform process-tree cleanup
    redaction.py     # Best-effort secret redaction for keys and inline tokens
    logging.py       # Stderr-only redacting logger
```

### Session State Machine
```text
CREATED → STARTING → CONFIGURING → RUNNING ⇄ STOPPED
                                      ↓
                         TERMINATING → TERMINATED
```
- **Stop-Generation Tracking**: Every transition into `STOPPED` increments a monotonic stop generation (`snapshot_id = snap_<id>_<gen>`). Frame IDs and snapshot IDs from prior stops are invalidated upon `continue` / step and rejected with `STALE_SNAPSHOT`.
- **DAP Handshake Correctness**: `DapClient` registers pending requests before writing to `stdin`, buffers early `initialized` / `stopped` / `terminated` events without fixed sleeps, and does not assume the `launch` response arrives before `initialized` + `setBreakpoints` + `configurationDone`.

---

## 3. Security & Trust Model

### Local Code Execution
Debugging executes project code and test fixtures. **Only debug trusted local projects.** Workspace path checks and disabled `evaluate` do not turn the Python interpreter into a sandbox.

- **Named Launch Profiles**: Launches use named profiles (`"pytest"`) from configuration. No MCP tool accepts shell strings or arbitrary executables.
- **Subprocess Isolation**: Adapter processes are started via `asyncio.create_subprocess_exec` (`shell=False`). Reverse requests such as `runInTerminal` are explicitly rejected.
- **Pytest Selector Validation**: Test selectors (`tests/test_file.py::test_name`) are validated structurally. Leading dashes (`-o`, `--rootdir`), control characters, non-`.py` files, and paths resolving outside the workspace (including via symlinks or `..`) are rejected with `INVALID_TEST_SELECTOR` or `PATH_OUTSIDE_WORKSPACE`.

### Variable Inspection & Expression Evaluation
- **Safe Default Inspection**: Snapshots use DAP `scopes` and `variables` requests, never auto-generated `evaluate` expressions. The `locals` parameter in `debug_inspect` / `debug_snapshot` is a **name filter** over existing frame locals, not an expression list.
- **`debug_evaluate` Disabled by Default**: Free-form expression evaluation (`debug_evaluate`) can execute arbitrary Python code (even under DAP context `"watch"`) and is blocked server-side (`EVALUATION_DISABLED`) unless `"enableEvaluation": true` is explicitly configured in `agy-dap.json`.
- **Adapter-Side Formatting Caveat**: Even without `evaluate`, Python's `debugpy` invokes `__repr__` when formatting custom objects during `variables` requests. `agy-dap` sets `justMyCode: true` and `showReturnValue: false` by default, but custom `__repr__` methods with side effects in project classes can still execute when their frame is inspected.

### Confidential Data & Environment Hygiene
- **Environment Allowlist**: `agy-dap` does not copy the full host `os.environ` into debuggee processes. Only essential platform/runtime variables (`PATH`, `SYSTEMROOT`, `HOME`, `VIRTUAL_ENV`, etc.) plus explicit `envAllowlist` keys and profile `env` entries are passed, and any host variable matching secret key patterns is excluded.
- **Best-Effort Redaction**: Variables whose names match conservative secret patterns (`password`, `secret`, `token`, `api_key`, `authorization`, `client_secret`, etc.) and strings containing inline bearer/API tokens (`sk-...`, `ghp_...`, `Bearer ...`) are replaced with `"[REDACTED]"` (`status: "redacted"`). Redaction is best-effort and not a guarantee of complete secret detection.
- **No Persistent Disk Dumps**: Snapshots and variable values are held only in memory for the MCP response and are never written to disk by default.

---

## 4. Installation & Setup

### Windows (PowerShell)

```powershell
cd D:\Projekte\Plugins\agy-dap

# 1. Create a virtual environment
python -m venv .venv

# 2. Install agy-dap with dev/test dependencies
.\.venv\Scripts\pip.exe install -e ".[dev]"

# 3. Verify installation and run tests
.\.venv\Scripts\pytest.exe -v
```

### Linux / macOS (Bash)

```bash
cd /path/to/agy-dap

# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install agy-dap with dev/test dependencies
pip install -e ".[dev]"

# 3. Verify installation and run tests
pytest -v
```

---

## 5. Antigravity CLI & MCP Integration

### Option A: As a Discovered Antigravity Plugin
Copy or symlink `agy-dap` into an Antigravity plugin discovery path:
- **Project-scoped**: `.agents/plugins/agy-dap`
- **User-global**: `~/.gemini/config/plugins/agy-dap`

Validate the plugin manifest using the Antigravity CLI:
```powershell
agy plugin validate .
```
Verified output:
```text
Validating agy-dap (D:\Projekte\Plugins\agy-dap\plugin.json)

  skills 1 · agents 0 · rules 1 · hooks 0 · mcpServers 1 · sidecars 0

✔ Validation passed
```

### Option B: Direct MCP Server Registration
```powershell
agy mcp add agy-dap -- command="python" args=["/path/to/agy-dap/run_server.py"]
```
`run_server.py` automatically re-executes into the plugin's local `.venv` if present and routes all logs strictly to `stderr`.

---

## 6. Configuration (`agy-dap.json`)

Place an optional `agy-dap.json` in your workspace root (or `.gemini/agy-dap.json`) to customize profiles, limits, or timeouts. See [`config.default.json`](config.default.json) for a complete template without local user paths:

```json
{
  "schemaVersion": "1.0",
  "workspaceRoot": null,
  "pythonPath": null,
  "allowOutsideWorkspace": false,
  "enableEvaluation": false,
  "includeCodeSnippets": false,
  "snippetContextLines": 3,
  "maxConcurrentSessions": 1,
  "envAllowlist": [],
  "limits": {
    "maxStackFrames": 8,
    "maxVariablesPerScope": 25,
    "maxVariableDepth": 2,
    "maxContainerItems": 10,
    "maxStringLength": 240,
    "maxOutputLines": 40,
    "maxOutputLineLength": 300,
    "maxOutputBufferLines": 500,
    "maxSnapshotBytes": 16384
  },
  "timeouts": {
    "startupTimeoutMs": 15000,
    "requestTimeoutMs": 10000,
    "defaultInspectTimeoutSec": 30.0,
    "maxInspectTimeoutSec": 120.0,
    "idleSessionTimeoutSec": 120.0,
    "cleanupTimeoutSec": 5.0
  },
  "profiles": {
    "pytest": {
      "runner": "pytest",
      "pythonPath": null,
      "module": "pytest",
      "cwd": null,
      "args": ["-q", "-s", "--no-header", "--tb=short"],
      "env": {},
      "justMyCode": true,
      "stopOnUncaughtException": true,
      "stopOnRaisedException": false
    }
  }
}
```

---

## 7. MCP Tools Reference

| Tool | Purpose | Key Arguments | Behavior |
| :--- | :--- | :--- | :--- |
| `debug_doctor` | Check workspace, Python interpreter, `debugpy`, `pytest`, and config | `profile="pytest"` | Read-only check returning `healthy`, component versions, and `remediations`. |
| `debug_inspect` | **Primary one-shot investigation** | `test_id`, `breakpoint={"file": ..., "line": ...}`, `profile="pytest"`, `locals=[...]`, `timeout_seconds=30`, `include_snippet=None` | Launches test, stops at breakpoint or exception, captures compact snapshot (with optional highlighted source snippet), and **always cleans up** before returning. |
| `debug_start` | Start interactive debugging session | `test_id`, `breakpoint`, `profile="pytest"`, `locals=[...]`, `timeout_seconds=30`, `include_snippet=None` | Stops at first breakpoint/exception, returns `session_id` + snapshot, and keeps session alive (with idle timeout). |
| `debug_snapshot` | Inspect stopped session | `session_id`, `thread_id=None`, `frame_id=None`, `locals=[...]`, `snapshot_id=None`, `include_snippet=None` | Captures stack & variables for a `STOPPED` session. Defaults to the stopped thread. Rejects stale `frame_id` / `snapshot_id`. |
| `debug_continue` | Step or continue execution | `session_id`, `action="continue" \| "next" \| "step_in" \| "step_out"`, `locals=[...]`, `timeout_seconds=None`, `include_snippet=None` | Advances execution, waits for the next stop or termination, and returns a new snapshot (with incremented `snapshot_id`). |
| `debug_evaluate` | Opt-in watch expression evaluation | `session_id`, `expression`, `frame_id=None`, `snapshot_id=None` | **Disabled by default** (`EVALUATION_DISABLED`). Requires `"enableEvaluation": true` in server config. |
| `debug_stop` | Terminate session idempotently | `session_id` | Terminates debuggee and adapter process trees and returns verified cleanup status. |

---

## 8. Reproducible Walkthrough on the Fixture Project

The repository includes a self-contained fixture project at [`tests/fixtures/sample_pytest_project`](tests/fixtures/sample_pytest_project) with a deterministic failing test `tests/test_checkout.py::test_discount`.

### Step 1: Call `debug_inspect`
```json
{
  "profile": "pytest",
  "test_id": "tests/test_checkout.py::test_discount",
  "breakpoint": { "file": "src/checkout.py", "line": 40 },
  "locals": ["subtotal", "discount", "running_total", "tax", "is_tax_exempt", "empty_memo", "coupon_code", "api_token"],
  "timeout_seconds": 30
}
```

### Step 2: Returned Compact Snapshot
```json
{
  "session_id": "sess_a1b2c3d4e5f6",
  "snapshot_id": "snap_c3d4e5f6_1",
  "state": "STOPPED",
  "stop_reason": "breakpoint",
  "outcome": "breakpoint_hit",
  "thread_id": 1,
  "frame_id": 2,
  "location": {
    "file": "src/checkout.py",
    "line": 40,
    "function": "calculate_total"
  },
  "stack": [
    { "frame_id": 2, "function": "calculate_total", "file": "src/checkout.py", "line": 40, "column": 1 },
    { "frame_id": 3, "function": "test_discount", "file": "tests/test_checkout.py", "line": 14, "column": 1 }
  ],
  "locals": {
    "subtotal": { "value": 100.0, "type": "float", "status": "ok" },
    "discount": { "value": 15.0, "type": "float", "status": "ok" },
    "running_total": { "value": 115.0, "type": "float", "status": "ok" },
    "tax": { "value": 0, "type": "int", "status": "ok" },
    "is_tax_exempt": { "value": false, "type": "bool", "status": "ok" },
    "empty_memo": { "value": "", "type": "str", "status": "ok" },
    "coupon_code": { "value": null, "type": "NoneType", "status": "null" },
    "api_token": { "value": "[REDACTED]", "type": "str", "status": "redacted" }
  },
  "breakpoints": [
    { "requested_file": "src/checkout.py", "requested_line": 40, "verified": true, "actual_line": 40, "id": 0 }
  ],
  "output_tail": [
    "checkout-audit-step: validating cart line (repeated 4x)"
  ],
  "truncated": true,
  "session_terminated": true,
  "cleanup": { "clean": true, "state": "TERMINATED" }
}
```
The snapshot immediately shows `subtotal = 100.0`, `discount = 15.0`, and `running_total = 115.0` at line 40, confirming the hypothesis that line 39 (`running_total = round(subtotal + discount, 2)`) added the discount instead of subtracting it.

---

## 9. Collaboration with `agy-lsp`

- `agy-dap` has **no runtime dependency** on a running `agy-lsp` server; file and line breakpoints work standalone.
- When an agent wants to place a breakpoint by symbol name, the recommended workflow is:
  1. Query `go_to_definition` or `document_symbols` in `agy-lsp` to find the file and declaration line.
  2. Choose an executable line inside the function body and pass `{ "file": ..., "line": ... }` to `agy-dap`.
  3. Inspect `breakpoints[0].actual_line` and `verified` in the `agy-dap` response, as a function's `def` header line is distinct from the lines where local variables are assigned.
- Programmatic extension point: [`src/agy_dap/symbols.py`](src/agy_dap/symbols.py) defines the `SymbolResolver` protocol for optional future symbol resolution without regex heuristics.

---

## 10. Error Contract & Troubleshooting

All errors return a deterministic structure without leaking plugin stacktraces:

```json
{
  "error": {
    "code": "BREAKPOINT_UNVERIFIED",
    "message": "Breakpoint at src/checkout.py:9999 was not verified by the debug adapter.",
    "retryable": false,
    "details": {}
  }
}
```

| Error Code | Cause & Remediation |
| :--- | :--- |
| `DEPENDENCY_MISSING` | Target Python interpreter, `debugpy`, or `pytest` is missing. Run `debug_doctor` for exact install commands. |
| `INVALID_CONFIG` | Invalid `agy-dap.json`, unknown profile name, or invalid tool arguments. |
| `PATH_OUTSIDE_WORKSPACE` | Breakpoint file, test file, or profile `cwd` resolves outside `workspaceRoot`. |
| `INVALID_TEST_SELECTOR` | Selector starts with `-` (CLI flag injection), is not a `.py` file, or has invalid node syntax. |
| `CAPABILITY_UNSUPPORTED` | Requested feature (e.g., conditional breakpoint) is not supported by the adapter. |
| `BREAKPOINT_UNVERIFIED` | Adapter could not verify the breakpoint (e.g., line beyond EOF or non-executable block). |
| `TARGET_NOT_REACHED` | Test finished (`exit_code` included in `details`) without executing the breakpoint line. |
| `SESSION_NOT_FOUND` | Unknown or already terminated `session_id`. |
| `SESSION_NOT_STOPPED` | `debug_snapshot`, `debug_continue`, or `debug_evaluate` called while session is not `STOPPED`. |
| `STALE_SNAPSHOT` | `snapshot_id` or `frame_id` belongs to an earlier stop generation before a `continue` / step. |
| `EVALUATION_DISABLED` | `debug_evaluate` called while `enableEvaluation` is `false` (default). |
| `CONCURRENT_SESSION_LIMIT` | An interactive session is already active (`maxConcurrentSessions: 1`). Call `debug_stop` first. |
| `TIMEOUT` | Adapter request or debuggee execution exceeded the configured timeout; processes were cleaned up. |
| `ADAPTER_CRASHED` | Adapter process exited unexpectedly or stdio stream closed mid-request. |
| `PROTOCOL_ERROR` | Adapter rejected a DAP request or emitted malformed protocol frames. |
| `CLEANUP_FAILED` | Process tree termination could not confirm all processes exited within `cleanupTimeoutSec`. |

---

## 11. Measurements & Evaluation Methodology

Run the reproducible benchmark script against the fixture project:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py
```

### Measured Local Results (Windows 11, Python 3.14.8, debugpy 1.8.22, pytest 9.1.1)
- **Launch-to-Snapshot Latency**: ~4,680 ms
- **MCP Tool Calls**: 1 (`debug_inspect`)
- **DAP Requests Sent**: 9 (`initialize`, `launch`, `setBreakpoints`, `setExceptionBreakpoints`, `configurationDone`, `threads`, `stackTrace`, `scopes`, `variables`)
- **Compact Filtered Snapshot Payload**: `1,013` UTF-8 bytes (`254` estimated tokens via `ceil(chars / 4)`)
- **Untruncated Raw Capture at Same Stop**: `12,047` UTF-8 bytes (`3,012` estimated tokens via `ceil(chars / 4)`)
- **Single-Snapshot Payload Ratio**: `0.0841` (compact snapshot is ~8.4% of the untruncated capture size at the same stop point)

*Important Methodological Note:* Token counts above are character-based estimates (`ceil(chars / 4)`), not exact model tokenizer outputs. Furthermore, single-snapshot payload compactness does not automatically prove net token savings or reduced hallucinations across a full multi-turn agent session. To evaluate end-to-end agent effectiveness rigorously, run paired A/B trials on a fixed benchmark suite of buggy repositories (with and without `agy-dap`), measuring task pass rate, wall-clock time to green tests, total number of agent turns, and total input/output tokens recorded by the LLM API.

---

## 12. Tested Versions, Platforms & CI

### Locally Verified Environment
- **OS**: Windows (win32, AMD64)
- **Python**: `3.14.8`
- **debugpy**: `1.8.22`
- **pytest**: `9.1.1` (`pytest-asyncio 1.4.0`)
- **MCP Python SDK**: `2.3.0`
- **Ruff**: `0.17.1`
- **Antigravity CLI**: `agy` (`agy plugin validate` passed)

### Configured CI Matrix
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is configured for `ubuntu-latest` and `windows-latest` across Python `3.11` and `3.12` (running Ruff, unit tests, real `debugpy` E2E integration tests, and `scripts/benchmark.py`). Note: CI jobs are configured in the repository workflow file; only the Windows local test suite was executed directly in this environment.

---

## 13. Known Limitations & Future Extensions

- **Python / pytest Only in MVP**: Only `debugpy` and the `pytest` runner profile are enabled in the MVP.
- **Single Active Session Default**: `maxConcurrentSessions` defaults to `1` to prevent resource exhaustion from orphaned debuggers.
- **Subprocess Debugging (`subProcess: false`)**: The MVP launches `pytest` in a single debuggee process (`subProcess: false`) for deterministic lifecycle management and process-tree cleanup.
- **Future Extensions**: Additional runner profiles (e.g., `unittest` or module script profiles), optional multi-process attach, and wiring an optional `SymbolResolver` implementation to `agy-lsp`.

---

## 14. Official References

- [Debug Adapter Protocol (DAP) Overview](https://microsoft.github.io/debug-adapter-protocol/overview.html)
- [Debug Adapter Protocol (DAP) Specification](https://microsoft.github.io/debug-adapter-protocol/specification)
- [microsoft/debugpy](https://github.com/microsoft/debugpy)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- [k0pfnicker/agy-lsp (Integration Reference)](https://github.com/k0pfnicker/agy-lsp)
- [microsoft/DebugMCP (Reference)](https://github.com/microsoft/DebugMCP)
