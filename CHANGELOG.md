# Changelog

All notable changes to **agy-dap** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
