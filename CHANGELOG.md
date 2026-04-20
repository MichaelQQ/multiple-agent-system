# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `MAS_OLLAMA_TIMEOUT` environment variable (default: 3600s) for controlling HTTP request timeout to the Ollama API.
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

### Internal

- All parsing functions now use try/except blocks to catch pydantic `ValidationError` and wrap them in custom exception types
