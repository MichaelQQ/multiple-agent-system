# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **E2E Test Suite**: New end-to-end test infrastructure (`tests/e2e/`) that exercises the full MAS task lifecycle from proposed → doing → done. Tests validate task state transitions, schema validation, transitions.jsonl logging, and Git worktree management.

- **Script Provider Adapter**: New `script` provider adapter (`src/mas/adapters/script_adapter.py`) that executes shell scripts as detached subprocesses. Accepts script path via `--script` extra_args and receives `$MAS_ROLE` and `$MAS_TASK_DIR` environment variables.

### Changed

- RoleConfig schema now accepts `extra_args` field for passing additional arguments to provider adapters.

- Tick loop now automatically promotes proposed tasks to doing when workers are available.