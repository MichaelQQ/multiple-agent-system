# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ProposalHandoff` model in `src/mas/schemas.py` for typed proposer handoffs
- `board.read_plan()` helper to read and validate `plan.json` files
- `Task.id` field validation against pattern `{yyyymmdd}-{slug}-{hash4}`
- `Result.duration_s` validator rejecting negative values

### Changed

- `board.read_task()` now uses `model_validate_json()` (strict validation)
- All schemas use `extra="forbid"` to reject unknown fields

### Breaking Changes

- **Unknown fields in JSON files cause validation errors.** Manually-crafted
  `task.json`, `result.json`, `plan.json`, or `config.yaml` files with
  extra fields will fail to load. Remove unknown fields before loading.