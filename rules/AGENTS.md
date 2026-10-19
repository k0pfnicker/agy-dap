# Agent Guidelines for Hypothesis-Driven Debugging (`agy-dap`)

When investigating test failures with `agy-dap`:

1. **Formulate a Hypothesis First**: Before calling `debug_inspect` or `debug_start`, state which test failed, which line you are targeting, and which local variables will confirm or refute your hypothesis.
2. **Prefer `debug_inspect` (One-Shot)**: Use `debug_inspect` with an explicit `locals` list rather than stepping line-by-line through an interactive session.
3. **Avoid `debug_evaluate`**: Expression evaluation executes code inside the debuggee and is disabled by default (`EVALUATION_DISABLED`). Rely on `locals` filtering in `debug_inspect` / `debug_snapshot`.
4. **Always Clean Up Interactive Sessions**: If you start an interactive session with `debug_start`, always terminate it with `debug_stop` when finished.
5. **Verify Fixes via Regular Test Execution**: After applying a code fix, re-run `pytest` normally outside the debugger to confirm the test passes.
