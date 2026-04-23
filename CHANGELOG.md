# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `mas upgrade` now prints a unified diff for each changed template file and prompts for confirmation before writing. New `-y/--yes` flag skips the prompt.
- `mas upgrade` detects a running daemon and offers to restart it so the new templates take effect. The previous tick interval is restored from a new `.mas/daemon.interval` sidecar written by `mas daemon start`.
- `mas.daemon.read_interval(mas)` helper returning the last-started interval (defaults to 300s when missing or corrupt).
- `MAS_OLLAMA_TIMEOUT` environment variable (default: 3600s) for controlling HTTP request timeout to the Ollama API.
- E2E test suite (`tests/e2e/test_lifecycle.py`) covering full lifecycle scenarios, revision cycles, failure recovery, worktree lifecycle, and prior_results propagation. Run with `pytest tests/e2e/ -q`.
- Script-adapter-driven E2E tests (`tests/e2e/test_lifecycle_script.py` plus `tests/e2e/conftest.py` and `tests/e2e/scripts/`) that exercise the full MAS task lifecycle from proposed → doing → done using real subprocesses, validating state transitions, schema compliance, transitions.jsonl logging, and Git worktree management.
- **Script Provider Adapter**: New `script` provider adapter (`src/mas/adapters/script_adapter.py`) that executes shell scripts as detached subprocesses. Accepts script path via `--script` extra_args and receives `$MAS_ROLE` and `$MAS_TASK_DIR` environment variables.
- `ProposalHandoff` model in `src/mas/schemas.py` for typed proposer handoffs
- `board.read_plan()` helper to read and validate `plan.json` files
- `Task.id` field validation against pattern `{yyyymmdd}-{slug}-{hash4}`
- `Result.duration_s` validator rejecting negative values
- Added a formal testing strategy document (`TESTING_STRATEGY.md`) defining test layers
  (Unit/Integration/E2E), component mapping, mocking guidance, and organization conventions.
- Custom exception types for robust error handling in `src/mas/errors.py`:
  - `PlanParseError` - raised when parsing a malformed or invalid `plan.json`
  - `TaskReadError` - raised when reading a malformed or invalid `task.json`
  - `ResultReadError` - raised when reading a malformed or invalid `result.json`
- All custom exceptions include context (file path, raw content snippet, and root cause) for clearer debugging

### Changed

- `board.read_task()` now uses `model_validate_json()` (strict validation)
- All schemas use `extra="forbid"` to reject unknown fields
- `parse_plan()` in `roles.py` now wraps JSON parsing and validation errors with `PlanParseError`, including file path and content snippet
- `read_task()` in `board.py` now wraps JSON parsing and validation errors with `TaskReadError`, including file path and content snippet
- `read_result()` in `board.py` now wraps JSON parsing and validation errors with `ResultReadError`, including file path and content snippet
- Error messages now include the file path, a snippet of the problematic content, and the original exception type and message
- RoleConfig schema now accepts `extra_args` field for passing additional arguments to provider adapters.

### Fixed

- OllamaAdapter now properly categorizes failure messages by exception type:
  - Connection errors (URLError) produce "connection error" message
  - Timeout errors (socket.timeout) produce "timeout after Xs" message
  - HTTP errors (4xx/5xx responses) produce "HTTP {code}: {reason}" message
  - JSON decode errors produce "invalid JSON response" message
- Failure result objects now properly populate all required schema fields (`task_id`, `status`, `summary`, `artifacts`, `handoff`, `verdict`, `feedback`, `tokens_in`, `tokens_out`, `duration_s`, `cost_usd`).
- Robust parsing of `plan.json`, `task.json`, and `result.json` files that were previously failing due to malformed JSON
- Clearer error messages that help identify which file caused the issue and include context for debugging

### Breaking Changes

- **Unknown fields in JSON files cause validation errors.** Manually-crafted
  `task.json`, `result.json`, `plan.json`, or `config.yaml` files with
  extra fields will fail to load. Remove unknown fields before loading.

- **Startup validation** — `mas validate` CLI command and programmatic API for validating
  configuration at startup.

  - `validate_config(cfg: MasConfig, mas_dir: Path) -> list[ValidationIssue]` — validates
    a loaded config object. Checks for empty/missing config, provider CLI
    availability (via `shutil.which`), and role prompt template existence.

  - `validate_environment(mas_dir: Path) -> list[ValidationIssue]` — higher-level
    that loads and validates config in one call.

  - `mas validate` — CLI command that runs validation and exits:
    - 0 if all checks pass
    - 1 if validation fails (prints errors to stderr)

  - **tick integration** — `run_tick()` now validates config before
    executing. Raises `ValueError` if validation fails, preventing
    orphaned tasks.

  - **daemon integration** — `daemon.start()` now validates config
    before starting. Raises `DaemonError` if validation fails.

### Internal

- All parsing functions now use try/except blocks to catch pydantic `ValidationError` and wrap them in custom exception types

## [1.0.0] - 2025-04-16

### Added

- Initial release of mas (Multi-Agents Orchestration System)
- `mas init` — Initialize project with default config, roles, prompts
- `mas tick` — Run one pass of the orchestrator
- `mas show` — Print the board
- `mas promote <id>` — Move proposal from proposed/ to doing/
- `mas retry <id>` — Retry a failed task
- `mas logs <id>` — Show task logs
- `mas cron` — System cron scheduling
- `mas daemon` — Detached daemon process
