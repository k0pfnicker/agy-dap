# Changelog

All notable changes to **agy-dap** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-10-26

### Added
- **`.NET` (`dotnet test`) Debugging via `NetCoreDbgAdapter` (`profile="dotnet_test"`):** Added `NetCoreDbgAdapter` (`src/agy_dap/adapters/netcoredbg.py`), `validate_dotnet_test_selector` (`src/agy_dap/utils/paths.py`), and DAP `attach` handshake support in `DapClient`. Automatically launches `dotnet test --filter <TestFilter>` with `VSTEST_HOST_DEBUG=1`, parses the suspended `testhost.dll` process ID (`Process Id: <PID>`), attaches `netcoredbg --interpreter=vscode` over DAP stdio, and cleans up both the `testhost` and `dotnet test` process trees on completion.
- **C# Symbol Breakpoint Resolution (`CSharpSymbolResolver`):** Extended `CompositeSymbolResolver` (`src/agy_dap/symbols.py`) with `CSharpSymbolResolver` so symbol breakpoints in `.cs` files (`{"file": "CheckoutService.cs", "symbol": "CheckoutService.CalculateTotal"}`) resolve directly to the first executable statement inside the method body even when no C# LSP server is running.
- **Extended `debug_doctor` for `.NET` & `netcoredbg`:** `debug_doctor(profile="dotnet_test")` probes `dotnet --version` and `netcoredbg --version` and returns OS-specific installation instructions when missing.

## [1.2.0] - 2026-10-26

### Added
- **Built-in `unittest` and `python_module` Launch Profiles:** Added first-class support for standard library `unittest` (`profile="unittest"`, accepting both dotted `tests.test_mod.TestClass.test_method` and `pytest`-style `tests/test_mod.py::TestClass::test_method` selectors) and standalone Python modules/scripts (`profile="python_module"`, accepting workspace `.py` scripts or dotted module names).
- **Direct Symbol Breakpoints (`symbol` & `line_offset`) with `agy-lsp` Integration:** Implemented `PythonAstSymbolResolver`, `AgyLspSymbolResolver` (connecting directly to companion plugin [`agy-lsp`](https://github.com/k0pfnicker/agy-lsp)), and `CompositeSymbolResolver` in `src/agy_dap/symbols.py`. Breakpoints in `debug_inspect` and `debug_start` can now be specified by function/method symbol name (e.g. `{"file": "src/checkout.py", "symbol": "calculate_total", "line_offset": 13}`), automatically resolving to the first executable statement inside the function body (skipping docstrings).
- **Expanded Verification Suite:** Added unit and end-to-end `debugpy` tests covering symbol breakpoints, `unittest` profile execution, and `python_module` execution (40 automated tests total).

## [1.1.0] - 2026-10-26

### Changed
- Aligned release versioning (`1.1.0`) and repository metadata (`authors`, `keywords`, `classifiers`, `project.urls`) with companion plugin [`agy-lsp`](https://github.com/k0pfnicker/agy-lsp).
- Added `CHANGELOG.md` tracking release history and architectural milestones.

## [1.0.0] - 2026-10-19

### Added
- Initial release of `agy-dap` — Hypothesis-Driven Debug Adapter Protocol (DAP) Plugin for Google Antigravity CLI (`agy`).
- Exposed 7 MCP tools: `debug_doctor`, `debug_inspect` (one-shot), `debug_start`, `debug_snapshot`, `debug_continue`, `debug_evaluate`, and `debug_stop`.
- Deterministic byte-exact DAP `Content-Length` stdio transport and handshake state machine without race-prone sleeps.
- Process-tree cleanup (`taskkill /T /F` on Windows, process groups on POSIX) and idle session watchdog.
- Secret redaction, workspace path confinement, CLI flag injection protection, and server-side evaluation guard (`enableEvaluation: false` by default).
- Reproducible benchmark script (`scripts/benchmark.py`) and GitHub Actions CI matrix.
