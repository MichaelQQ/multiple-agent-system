# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `mas pr <task-id>` command — opens a GitHub PR for a completed (`done/`) task on its `mas/<task-id>` branch via the `gh` CLI. Requires `gh` installed and authenticated. Reads `result.json` summary for the PR title (falls back to the task goal truncated to 70 chars) and composes the body from the goal, summary, evaluator feedback, and token/cost totals. Pushes the branch to origin if not already present (no force-push). Detects an already-open PR from `gh` output and exits 0 printing the existing URL. Flags: `--draft` (mark as draft), `--base <branch>` (override target branch; default is the repo default branch queried via `gh repo view`), `--reviewer <handle>` (request a reviewer; repeatable).

- **Web UI `/config/roles` page**: A new **Config** nav link in the web UI header points to `GET /config/roles`, which renders `.mas/roles.yaml` inside a `<textarea>` for in-browser editing. Submitting the form (`POST /config/roles`) validates the content as YAML (`yaml.safe_load`) and then against the `dict[str, RoleConfig]` pydantic schema. A validation error returns HTTP 400 with a red error banner and the submitted text preserved in the textarea; the file is not written. On success the content is written atomically: a temporary file `.mas/roles.yaml.<pid>.tmp` is written first, then swapped into place with `os.replace()` so a crash or write failure can never leave a partial file. If `os.replace()` fails, the temporary file is removed and an error banner is shown (HTTP 200) with the file byte-identical to its pre-submit state. After a successful save, the daemon's `ConfigWatcher` detects the updated modification time before its next tick cycle and reloads the config — no daemon restart required.

- `mas config show` command — prints the fully-resolved `MasConfig` and roles map (top-level keys `config` and `roles`) as YAML by default, or as JSON with `--json`. `--field <dotted.path>` extracts a single nested value (supports list indices); exits 2 with an error on stderr when the path is not found. String values whose key name matches `key`, `token`, `secret`, or `password`, and URL query-string parameters with matching names, are redacted to `***`; `--unsafe-show-secrets` disables masking.

- `mas trace <task-id>` command — shows a per-task stage-by-stage wall-clock timeline (orchestrator → implementer → tester → evaluator) coloured by status. Each row includes the role label with revision cycle (e.g. `implementer[rev-0]`), `started_at`, `ended_at`, `duration_s`, `status`, and `cost_usd`. In-flight stages are shown with `status=running` and no end time. `--json` emits a JSON object with `task_id`, `goal`, `started_at`, `ended_at`, `total_duration_s`, `total_cost_usd`, and a `stages` array. Exits 1 with `not found` for unknown task IDs; prints `no stage data yet` when no dispatch events exist.
- `src/mas/trace.py` module — `build_trace(task_dir)` that parses `audit.jsonl` via `audit.read_events()`, pairs dispatch/completion events by `(subtask_id, cycle)`, resolves `cost_usd` from subtask `result.json` files, and marks unmatched dispatch events as in-flight.

- **`--json-logs` flag for `mas daemon start`**: switches `.mas/logs/daemon.log` from plain text to newline-delimited JSON. Every record includes stable keys `ts` (UTC ISO-8601 with `Z` suffix), `level` (lowercase: `info`, `warn`, `error`), and `event` (machine-readable name such as `daemon_start`, `daemon_stop`, `tick_start`, `tick_done`, `tick_error`, `config_reloaded`), plus event-specific context fields (`pid`, `interval_s`, `duration_s`, `error`, `changes`). The `JsonFormatter` in `src/mas/logging.py` normalises `WARNING` to `warn` for consistency.

- `mas doctor` command — diagnoses the local environment and prints a Rich table of check results across four groups: **Config** (parses and validates `config.yaml`/`roles.yaml`), **Provider** (verifies each used provider's CLI binary is present in PATH via `shutil.which`), **Board** (detects orphan git worktrees where a `mas/<id>` branch has no corresponding task directory, and stale worker PID files), and **Daemon** (checks `daemon.pid` liveness — missing file is OK, dead PID is FAIL). Flags: `--json` emits `{"checks": [...], "summary": {"ok", "warn", "fail"}}` with no ANSI sequences; `--strict` treats WARN-level checks as failures. Exit codes: **0** if all checks are OK (or only WARNs without `--strict`); **1** if any FAIL is present, or any WARN is present with `--strict`. New `src/mas/doctor.py` module exposes `run_checks(mas_dir)` returning a list of `CheckRecord` TypedDicts.

- **Rejected proposal log**: when the proposer's similarity check drops a duplicate, the tick loop appends a record to `.mas/proposals/rejected.jsonl` (created lazily; does not require `mas init` or `mas upgrade`). Each line is a JSON object with fields: `timestamp` (ISO-8601 UTC, Z-terminated), `summary` (proposer result summary), `goal` (proposed goal, truncated to 500 chars with `...` when longer), `similarity_score` (Jaccard score, float), `matched_task_id`, `matched_column` (one of `proposed`/`doing`/`done`/`failed`), and `threshold` (config threshold at rejection time). Write failures are caught and logged at `WARNING`; they never interrupt the tick loop.
- `mas proposals rejected [--since <Nh|Nd|Nw>] [--limit N] [--json]` — list rejected (duplicate) proposals from `.mas/proposals/rejected.jsonl`. Displays a Rich table with columns `timestamp`, `summary`, `score` (3 decimal places), `matched_task_id`, and `matched_column`, sorted newest-first. `--since` accepts the same `h`/`d`/`w` duration strings as `mas stats`. `--json` emits newline-delimited JSON. `--limit` caps results (default 50). Exits 0 when the log is missing or empty.

- **Daemon log rotation**: `mas daemon` now routes every line it emits (`_say()` messages, tick start/done, tick failures, unhandled tracebacks) through a `logging.handlers.RotatingFileHandler` attached to the `mas` logger at `.mas/logs/daemon.log`. Two new `MasConfig.daemon` knobs control rotation: `log_max_bytes` (default 10 MiB — file rotates to `daemon.log.1`, prior `.1`→`.2`, …) and `log_backup_count` (default 5 — older backups are deleted). `validate_config` rejects non-positive `log_max_bytes` and negative `log_backup_count`. Raw stdout/stderr are now redirected to `/dev/null` so no second unrotated sink exists. Caps disk usage on long-lived daemons, which is important now that config hot-reload removed the main reason to restart them.
- **Per-role wall-clock timeout**: the tick loop now reaps workers that are still alive but stuck (PID alive, no `result.json`, no progress). PID files include a dispatch timestamp (`pid\nepoch_seconds\n`); on every tick, for each live worker PID whose `now - dispatch_time > roles[<role>].timeout_s`, the reaper sends `SIGTERM`, waits 5 s, then `SIGKILL`s if still alive, synthesizes `Result(status="failure", summary="timeout exceeded after Ns", feedback=<log tail>)`, and appends a `dispatched → timeout` transition so the normal retry / fail-parent path runs unchanged. Legacy single-line pidfiles without a timestamp are treated as unknown-age and skipped. `RoleConfig.timeout_s` drives the budget (existing field; defaults already shipped via `.mas/roles.yaml`). Timeout failures are classified as regular `failure`, not `environment_error`, so they consume the role's retry budget — preventing an infinite-loop worker from retrying forever.

- **Task deletion**: new `mas delete <id> [<id>…] [-y/--yes]` command permanently removes one or more tasks from any column (`proposed/`, `doing/`, `done/`, `failed/`). `board.delete_task()` SIGTERMs any live worker PIDs, escalates to SIGKILL after 3 s, prunes the task's worktree (branch preserved), then removes the task directory. Exits non-zero if any requested ID is not on the board (still deletes the ones that exist). The web UI adds a **Delete** button on the task detail page (`POST /task/{id}/delete`) and per-task checkboxes + a **Delete selected** bulk action on the board (`POST /tasks/delete`, accepts repeated `task_ids` form fields).
- **Markdown rendering on web UI task page**: task goals, result summaries/feedback, previous-failure text, and subtask goals/summaries now render as Markdown (headings, lists, fenced code with syntax class, tables, nl2br) via a `md` Jinja filter. New collapsible **Task info** card shows id, role, column, parent, created timestamp, cycle/attempt, budget, and pretty-printed `inputs`/`constraints`. Requires the new `markdown>=3.5` dependency in the `web` extra.

- **Web UI parity with CLI**: the web app now exposes the remaining CLI commands and gets a visual refresh.
  - New routes: `GET /events` (cross-task event feed with `task/role/status/event/limit` filters, reuses `read_board_events`), `GET /validate` (runs `validate_environment` and shows a providers/roles summary), `GET /cron` + `POST /cron/install` + `POST /cron/uninstall` (drives `mas.cron`), `POST /upgrade` (spawns detached `mas upgrade --yes`), and `GET /daemon/status` (JSON).
  - Board tasks are sorted by most recent transition (newest first), per column.
  - Refreshed templates (`base.html`, `board.html`, `task.html`, new `events.html` / `validate.html` / `cron.html`): CSS-variable palette, header nav (Board / Events / Validate / Cron), colored status pills, per-column left-border accents on task cards, tabbed log viewer with an active-tab indicator, budget row in the subtask totals, and flash messages on the board for tick/prune/upgrade.

- `mas show --json` / `mas show <id> --json` — new `--json` flag emits a pretty-printed JSON document on stdout instead of the Rich table/tree, suitable for dashboards and CI scripts. Board view returns a list of task objects; task view returns a single object with subtask plan details. Unknown task IDs with `--json` print `{"error": "not found: <id>"}` and exit 1.

- **Per-task cost budget**: `Task.cost_budget_usd` (optional `float`) sets a USD spending cap for a single task. `MasConfig.default_cost_budget_usd` (optional `float`) sets the project-wide default applied when a task does not specify its own budget.
- **Cost budget short-circuit in tick**: Before dispatching the next subtask, `_advance_one()` sums `cost_usd` from all completed child `result.json` files. If the running total meets or exceeds the effective budget (`task.cost_budget_usd` takes precedence over `config.default_cost_budget_usd`), the tick writes a failure `result.json` with `summary="cost budget exceeded"` and a `handoff` containing `spent_usd`, `budget_usd`, and `last_completed_subtask_id`, then moves the parent task to `failed/` with transition reason `cost_budget_exceeded` without dispatching further work.
- **`mas cost` budget column**: When `cost_budget_usd` is set on the parent task, `mas cost <task-id>` now prints a `Budget:` line showing `spent / budget (% utilized)` after the per-subtask table.

- `mas events` command — aggregates `audit.jsonl` events across all tasks on the board (`doing/`, `done/`, `failed/`) into a single Rich table sorted by timestamp ascending. Supports the following flags:
  - `--task <id>` — restrict to a single task
  - `--role <name>` — filter by role
  - `--status <value>` — filter by outcome status
  - `--event <type>` — filter by event type (`dispatch`, `completion`, `state_transition`)
  - `--since <ISO>` / `--until <ISO>` — time-range bounds (passed through to `audit.read_events()`)
  - `--follow` / `-f` — poll for new events and print them as they appear; exits 0 on `KeyboardInterrupt`
  - `--interval <seconds>` — polling interval in seconds when `--follow` is active (default: 2)
  - `--json` — emit one newline-delimited JSON object per event instead of a Rich table
- `src/mas/events.py` module with `read_board_events()` — walks `.mas/tasks/{doing,done,failed}/`, calls `audit.read_events()` per task directory, injects `task_id` when absent, applies task/event post-hoc filters, and returns events sorted by timestamp ascending.

- **Webhooks**: outbound HTTP notifications on board transitions. Configure `webhooks` in `.mas/config.yaml` with `url`, `events` (column names or `from->to` strings), and `timeout_s`. Payloads include `task_id`, `role`, `goal`, `from`, `to`, `summary`, `status`, `timestamp`, and `task_dir`. Delivery is best-effort and non-blocking; errors are logged at `WARNING` and never interrupt the tick loop.

- **Config hot-reload for daemon**: The daemon now automatically detects changes to `.mas/config.yaml` and `.mas/roles.yaml` without requiring a restart. Before each tick cycle, it checks the config file modification time and reloads if changed. If the new config is invalid (malformed YAML, missing required fields, unknown provider), the daemon keeps the previous valid configuration and logs a warning.

- `mas stats` command — prints aggregate board counts, success/revision rates, per-role timing (mean/p50/p95), per-provider task counts, cumulative token/cost totals, and environment-error counts across all board columns. Flags: `--since <duration>` (e.g. `24h`, `7d`, `2w`) to filter by recency; `--json` to emit raw JSON instead of a Rich table.
- `src/mas/stats.py` module — `compute_stats(mas_dir, since)` that walks all four board columns and aggregates the stats structure above. `parse_since(s)` parses h/d/w duration strings.

- **Current subtask visibility** — The web UI now displays which subtask is currently executing on each task card, showing role, provider, PID, and elapsed time. Marker files (`.current_subtask`) are written during subtask dispatch and cleaned up on result collection.
- `mas cost <task-id>` command prints a per-subtask breakdown of `tokens_in`, `tokens_out`, and `cost_usd`, with a TOTAL row. Exits 1 if the task ID is not found.
- Adapter token/cost population: the Ollama adapter now calls `pricing.compute_cost_usd()` to populate `cost_usd` in `result.json` based on reported token counts. Providers without token reporting leave the fields `null`.
- Parent task aggregation: `_finalize_parent` in `tick.py` sums `tokens_in`, `tokens_out`, and `cost_usd` from all subtask `result.json` files and writes an aggregated `result.json` for the parent task before moving it to `done/`. `null` values are treated as 0.
- `src/mas/pricing.py` module with a `compute_cost_usd(provider, model, tokens_in, tokens_out)` function and a rate table covering `claude-code`, `gemini-cli`, `opencode`, and `codex` providers. Returns `0.0` for unknown providers/models or `None` token counts.

- `mas upgrade` now prints a unified diff for each changed template file and prompts for confirmation before writing. New `-y/--yes` flag skips the prompt.
- `mas upgrade` detects a running daemon and offers to restart it so the new templates take effect. The previous tick interval is restored from a new `.mas/daemon.interval` sidecar written by `mas daemon start`.
- `mas.daemon.read_interval(mas)` helper returning the last-started interval (defaults to 300s when missing or corrupt).
- **Audit logging** — every board move, subtask dispatch, and completion is now appended to `{task_dir}/audit.jsonl` as a structured JSONL event. Fields: `timestamp`, `event`, `role`, `provider`, `task_id`, `subtask_id`, `status`, `duration_s`, `summary`, `details`. Event types: `dispatch`, `completion`, `state_transition`.
- `mas audit <task-id>` command — display a formatted audit timeline for a task and its subtasks as a Rich table. Supports filtering via `--role`, `--status`, `--since <ISO>`, `--until <ISO>`.
- `src/mas/audit.py` module with `append_event()` and `read_events()` helpers. `read_events()` skips corrupt lines with a `UserWarning` and supports role/status/since/until filters.
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

- `board.move()` now appends a `state_transition` audit event to the destination task directory after every column move.
- `tick._advance_one()` now appends a `dispatch` audit event to the parent task directory after every subtask dispatch.
- `tick._handle_child_result()` now appends a `completion` audit event to the parent task directory when a subtask result is reaped.
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
