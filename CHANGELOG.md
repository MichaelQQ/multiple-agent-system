# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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