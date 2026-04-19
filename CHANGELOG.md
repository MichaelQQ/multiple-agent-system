# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Custom exception types for robust error handling in `src/mas/errors.py`:
  - `PlanParseError` - raised when parsing a malformed or invalid `plan.json`
  - `TaskReadError` - raised when reading a malformed or invalid `task.json`
  - `ResultReadError` - raised when reading a malformed or invalid `result.json`
- All custom exceptions include context (file path, raw content snippet, and root cause) for clearer debugging

### Changed

- `parse_plan()` in `roles.py` now wraps JSON parsing and validation errors with `PlanParseError`, including file path and content snippet
- `read_task()` in `board.py` now wraps JSON parsing and validation errors with `TaskReadError`, including file path and content snippet
- `read_result()` in `board.py` now wraps JSON parsing and validation errors with `ResultReadError`, including file path and content snippet
- Extra fields in JSON files are now stripped before validation, allowing graceful degradation when agents add new fields in future versions
- Error messages now include the file path, a snippet of the problematic content, and the original exception type and message

### Fixed

- Robust parsing of `plan.json`, `task.json`, and `result.json` files that were previously failing due to extra fields or malformed JSON
- Clearer error messages that help identify which file caused the issue and include context for debugging

### Internal

- All parsing functions now use try/except blocks to catch pydantic `ValidationError` and wrap them in custom exception types
- Models use `extra='ignore'` for graceful degradation with extra fields