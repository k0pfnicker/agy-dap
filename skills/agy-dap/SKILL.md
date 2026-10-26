---
name: agy-dap
description: >-
  Provides hypothesis-driven runtime debugging workflows using the Debug Adapter Protocol (DAP)
  via debugpy and pytest. Activate this skill when investigating reproducible test failures
  or runtime state questions that cannot be answered reliably from static source code alone.
---

# agy-dap: Hypothesis-Driven Debugging Workflow

This skill guides the agent in using `agy-dap` MCP tools to inspect real runtime state during reproducible test failures.

---

## 1. When to Use (and When NOT to Use) Debugging

- **Use `agy-lsp` or normal test runs first** for syntax errors, import errors, type mismatches, or obvious assertion typos.
- **Use `agy-dap`** when a test fails reproducibly at runtime and you need to verify a concrete hypothesis about control flow, intermediate calculations, or variable values.
- `agy-dap` provides **runtime observations**, not automated root-cause explanations.

---

## 2. The 7-Step Hypothesis-Driven Debugging Loop

1. **State the Failing Test & Hypothesis**:
   Name the exact pytest selector (e.g., `tests/test_checkout.py::test_discount`) and state what you expect vs. suspect at runtime (e.g., *"Hypothesis: `running_total` adds `discount` instead of subtracting it at line 40"*).
2. **Select the Breakpoint & Minimal Locals**:
   - Choose an executable line inside the workspace where the relevant variables are in scope (`{"file": "src/checkout.py", "line": 40}`), **or** specify a function/method symbol directly with an optional statement offset (`{"file": "src/checkout.py", "symbol": "calculate_total", "line_offset": 13}`).
   - When using `"symbol"`, `agy-dap` resolves the symbol via Python AST or companion plugin `agy-lsp` directly to the **first executable statement** inside the function body (skipping docstrings), and adds `line_offset` if provided.
   - Always check `breakpoints[0].actual_line` and `verified` in the returned snapshot.
   - Request **only the specific variable names** needed to test your hypothesis via `locals=["subtotal", "discount", "running_total"]`.
3. **Prefer One-Shot `debug_inspect` Over Multi-Step Stepping**:
   Call `debug_inspect` first (supports `profile="pytest"`, `"unittest"`, or `"python_module"`):
   ```json
   {
     "profile": "pytest",
     "test_id": "tests/test_checkout.py::test_discount",
     "breakpoint": {"file": "src/checkout.py", "line": 40},
     "locals": ["subtotal", "discount", "running_total"],
     "timeout_seconds": 30
   }
   ```
   `debug_inspect` starts the session, captures the snapshot at the first hit, and cleans up all processes automatically.
4. **Use Interactive Sessions (`debug_start` / `debug_continue` / `debug_stop`) Sparingly**:
   - Only use `debug_start` when a single snapshot is insufficient and you must step (`next`, `step_in`, `step_out`, `continue`) across multiple execution points.
   - Always call `debug_stop(session_id=...)` as soon as the interactive investigation is done.
   - Never reuse a `frame_id` or `snapshot_id` from before a `debug_continue` call (`STALE_SNAPSHOT`).
5. **Interpret Variable Statuses Accurately**:
   - `status: "ok"` — loaded runtime value (`false`, `0`, and `""` are preserved faithfully).
   - `status: "null"` — variable value is `None`.
   - `status: "missing"` — variable name is not defined in the current frame's local scope.
   - `status: "not_loaded"` — nested container depth limit reached.
   - `status: "redacted"` — value matched secret redaction rules.
6. **Do Not Over-Generalize from a Single Run**:
   Observing expected values in one test input does not prove general correctness across all inputs.
7. **Verify the Fix Outside the Debugger**:
   After editing the code to fix the bug, run the test suite normally (outside `agy-dap`) and check `agy-lsp` diagnostics to confirm the fix.
