# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Cost estimation on task detail view**: the task detail page (`/task/<id>`) now shows an estimated cost per role before dispatch. Baselines are computed from historical per-role medians with ±standard deviation uncertainty, displayed as `$X.XX ± $Y.YY`. Estimates require ≥ 3 completed subtasks per role; otherwise "Estimate unavailable" is shown. The total is summed from per-role estimates. Implemented in `src/mas/cost_helpers.py` with `estimate_task_cost()`.

- **Stuck-task detection**: the tick loop now detects tasks that are stuck and marks them accordingly.
  - New `StuckDetectionConfig` schema with `current_subtask_timeout_hours` (default 8) and `task_idle_timeout_hours` (default 24). Negative values are rejected by a Pydantic validator.
  - New `MasConfig.stuck_detection` field (`StuckDetectionConfig`, defaults to factory instance).
  - New `Task.stuck` boolean field (defaults to `False`); set to `True` when the tick loop detects the task is stuck.
  - `_is_task_stuck()` in `tick.py`: checks (a) `.current_subtask` marker age against `current_subtask_timeout_hours`, (b) if no marker, checks whether any subtask has a `result.json`, (c) if no results, checks idle time from `.transitions.log` against `task_idle_timeout_hours`.
  - `_advance_one()` integration: calls `_is_task_stuck()` before normal advancement; on detection, logs a `WARNING` (`"task stuck: <reason>"`) and sets `task.stuck = True`.
  - Configure in `.mas/config.yaml` under `stuck_detection:` (optional; defaults used when omitted):
    ```yaml
    stuck_detection:
      current_subtask_timeout_hours: 8    # fail subtask if marker exceeds this
      task_idle_timeout_hours: 24          # fail task if idle (no subtask results) exceeds this
    ```

- **`GET /health` endpoint** — returns daemon liveness status for monitoring systems (Kubernetes, systemd, cron). Writes `.mas/tick_heartbeat` (ISO-8601 UTC) at the end of each successful tick; the endpoint returns `200` with `{"status": "ok"}` when the heartbeat is fresh (within `2 × daemon.interval`, default 600 s), or `503` with `{"status": "degraded", "reason": "tick stalled"}` when the file is missing, corrupt, or stale. Includes Kubernetes liveness probe snippet and curl-based monitoring examples in the README.
- **Cost dashboard features**:
  - Per-role cost breakdown (proposer/orchestrator/implementer/tester/evaluator) on task detail view (`/task/<id>`)
  - Global cost summary with per-role aggregation on stats page (`/stats`)
  - At-risk alert section on board view (`/`) flagging tasks exceeding 80% of their budget
  - Per-subtask detail table showing model, tokens_in, tokens_out, duration_s, and cost_usd on task detail view
  - `GET /costs` JSON endpoint for programmatic access to per-role cost breakdown and global totals
  - `GET /costs/at-risk` JSON endpoint returning task IDs flagged as exceeding the 80% budget threshold
  - Graceful fallback when budget is unset or pricing is unavailable (no crash, empty/zero values)
  - New `src/mas/cost_helpers.py` module with `aggregate_costs_by_role()` and `at_risk_tasks()` helpers

- **Cost anomaly detection**:
  - `compute_role_baselines()` in `src/mas/cost_helpers.py` calculates per-role median and 75th percentile (p75) cost baselines from historical task data.
  - `detect_anomalies()` flags tasks where any role's cost exceeds 2× the computed baseline.
  - `/stats` page now includes a "Cost Anomalies" section listing detected anomalous tasks with role, baseline, actual cost, and multiplier.
  - `/task/<id>` page shows an anomaly badge next to roles whose cost exceeds 2× the baseline in the "Cost by Role" section.

- **Failure-pattern index** (`.mas/patterns.jsonl`): every tick now ends by regenerating a JSONL aggregate of recurring failure signatures from `tasks/failed/`. Each record carries `signature`, `terminal_reason` (last `to_state == failed` transition reason), `goal_sample`, `count`, `last_seen`, `task_ids`, and a `rejected_attempts_sample` projected from each parent's `state.json`. Two failures sharing the same normalized goal-token set and terminal reason collapse into one record. `gather_proposer_signals()` now reads this file and exposes the top 20 entries on `ProposerSignals.failure_patterns`; the proposer prompt is updated to disqualify any candidate whose goal matches an entry with `count >= 2` or a `revision_cycles_exhausted` / `max_retries_exceeded` / `convergence_detected` reason — preventing the board from re-proposing tasks that have already failed for the same reason. New `src/mas/patterns.py` module exposes `compute_patterns()`, `read_patterns()`, `write_patterns()`, and `refresh()` (best-effort: errors are logged at WARNING and never abort a tick). Closes gap 7 / TODO #18 in `docs/reliability-gaps.md`.

- **Success-pattern index** (`.mas/success_patterns.jsonl`): every tick now also regenerates a JSONL aggregate of success patterns from `tasks/done/`. Each record carries `signature` (normalized goal tokens), `goal_sample`, `count`, `avg_duration_s`, `avg_cost_usd`, `task_ids`, and `success_context` (verdict and notes from evaluator). Tasks are grouped by normalized goal tokens; running averages of duration and cost are maintained across aggregated tasks. `gather_proposer_signals()` exposes the top 10 entries on `ProposerSignals.success_patterns`; the proposer biases toward high-count success patterns when suggesting new work. New `SuccessPattern` model in `src/mas/patterns.py` with fields: `signature`, `goal_sample`, `count`, `avg_duration_s`, `avg_cost_usd`, `task_ids`, `success_context`.
- **Failure-pattern filtering in proposer**: `_materialize_proposal` in `tick.py` now calls `_blocked_by_failure_pattern()` before the similarity dedup block. This helper reads `.mas/patterns.jsonl` (via `read_patterns()` from `src/mas/patterns.py`) and blocks any proposal whose goal matches a pattern with `count >= 2` or a terminal reason (`revision_cycles_exhausted`, `max_retries_exceeded`, `convergence_detected`) using `goal_similarity` from `src/mas/roles.py`. The `patterns.jsonl` file is regenerated every tick from `tasks/failed/` — each record carries `goal_sample`, `count`, `last_seen`, `task_ids`, `terminal_reason`, and `rejected_attempts_sample`. New `src/mas/patterns.py` module exposes `compute_patterns()`, `read_patterns()`, `write_patterns()`, and `refresh()` (best-effort: errors are logged at WARNING and never abort a tick). Closes gap 7 / TODO #18 in `docs/reliability-gaps.md`.
- **Failure history on task detail page** (`/task/<id>`): the web UI now renders a **Failure history** section that displays patterns from `patterns.jsonl` matching the task's goal (via `goal_similarity` threshold). Each entry shows `goal_sample`, `terminal_reason`, `count`, `last_seen`, clickable links to related task IDs, and expandable rejected attempt snippets. The `?failure_filter=blocking` query parameter restricts the view to patterns with terminal reasons (`revision_cycles_exhausted`, `max_retries_exceeded`, `convergence_detected`). Implemented in `src/mas/web/app.py` (`_task_detail` route) and `src/mas/web/templates/task.html`.

- **Failure-pattern injection for implementer/tester**: `$pattern_block` template variable now injects a markdown block of up to 5 recurring failure patterns (from `.mas/patterns.jsonl`) into implementer and tester prompts, filtered by goal similarity (threshold 0.15 via `goal_similarity` from `src/mas/roles.py`). Patterns are sorted by count descending. Orchestrator prompts omit this variable. Returns empty string when no relevant patterns exist. The block renders with header `Recurring failure patterns` and per-pattern lines showing `signature`, `count`, `terminal_reason`, and `goal_sample`.

- **`mas trace` now renders graph + transitions**: the per-task trace command and `build_trace()` payload now include the task graph (`graph.json`: nodes with `subtask_id`/`role`/`cycle`/`status`/`verdict`/`summary`/`feedback`; edges with `from_id`/`to_id`/`kind` ∈ {`sequence`,`revision`,`arbiter`,`replan`}/`reason`) and the lifecycle transitions log (newly read from the production `.transitions.log` filename, with the legacy `transitions.jsonl` retained as a fallback). The default Rich view adds **Graph nodes**, **Graph edges**, and **Transitions** tables after the stages timeline; `--json` exposes top-level `graph` and `transitions` keys alongside the existing `task_id`/`goal`/`stages`/cost rollup. Closes gap 7 from `docs/reliability-gaps.md`.

- **`mas webhooks test`** command — sends a synthetic POST payload to all configured webhooks (or a specific `--url`) to verify reachability without waiting for a real board transition. Flags: `--url <url>` (exit 2 if the URL is not in config), `--event <name>` (event name used for the `events` filter check; default: `test`), `--timeout-s <secs>` (override per-webhook timeout). Prints a Rich table with columns `URL`, `Event filter`, `Result`, `Latency`, and `Detail`. Webhooks whose filter does not match `--event` appear as `skipped` and do not affect the exit code. The synthetic payload has exactly 10 keys (`task_id`, `role`, `goal`, `from`, `to`, `summary`, `status`, `timestamp`, `task_dir`, `_synthetic`) with `_synthetic: true` so receivers can ignore test pings. Exit codes: 0 = all tested webhooks 2xx; 1 = any non-2xx / timeout / error; 2 = `--url` not found in config. Parent task: `20260427-add-a-mas-webhooks-test-url-url-fd71`.

- **`mas daemon pause` / `mas daemon resume`**: new daemon subcommands to temporarily suspend new work dispatch without stopping the daemon. `pause` creates `.mas/PAUSED`; `resume` removes it. While paused, each tick still runs the reaper and drains in-flight results (workers that have already written `result.json` are advanced normally), but no new worker processes are dispatched and the proposer is not called. Exactly one `INFO` log line `"paused (.mas/PAUSED present), skipping dispatch"` is emitted per paused tick. `mas daemon status` now always includes a `paused: yes/no` line. The `.mas/PAUSED` marker is honoured by both the daemon loop and standalone `mas tick` invocations.

- `mas pr <task-id>` command — opens a GitHub PR for a completed (`done/`) task on its `mas/<task-id>` branch via the `gh` CLI. Requires `gh` installed and authenticated. Reads `result.json` summary for the PR title (falls back to the task goal truncated to 70 chars) and composes the body from the goal, summary, evaluator feedback, and token/cost totals. Pushes the branch to origin if not already present (no force-push). Detects an already-open PR from `gh` output and exits 0 printing the existing URL. Flags: `--draft` (mark as draft), `--base <branch>` (override target branch; default is the repo default branch queried via `gh repo view`), `--reviewer <handle>` (request a reviewer; repeatable).

- **Web UI `/config/roles` page**: A new **Config** nav link in the web UI header points to `GET /config/roles`, which renders `.mas/roles.yaml` inside a `<textarea>` for in-browser editing. Submitting the form (`POST /config/roles`) validates the content as YAML (`yaml.safe_load`) and then against the `dict[str, RoleConfig]` pydantic schema. A validation error returns HTTP 400 with a red error banner and the submitted text preserved in the textarea; the file is not written. On success the content is written atomically: a temporary file `.mas/roles.yaml.<pid>.tmp` is written first, then swapped into place with `os.replace()` so a crash or write failure can never leave a partial file. If `os.replace()` fails, the temporary file is removed and an error banner is shown (HTTP 200) with the file byte-identical to its pre-submit state. After a successful save, the daemon's `ConfigWatcher` detects the updated modification time before its next tick cycle and reloads the config — no daemon restart required.

- `mas config show` command — prints the fully-resolved `MasConfig` and roles map (top-level keys `config` and `roles`) as YAML by default, or as JSON with `--json`. `--field <dotted.path>` extracts a single nested value (supports list indices); exits 2 with an error on stderr when the path is not found. String values whose key name matches `key`, `token`, `secret`, or `password`, and URL query-string parameters with matching names, are redacted to `***`; `--unsafe-show-secrets` disables masking.

- `mas trace <task-id>` command — shows a per-task stage-by-stage wall-clock timeline (orchestrator → implementer → tester → evaluator) coloured by status. Each row includes the role label with revision cycle (e.g. `implementer[rev-0]`), `started_at`, `ended_at`, `duration_s`, `status`, and `cost_usd`. In-flight stages are shown with `status=running` and no end time. `--json` emits a JSON object with `task_id`, `goal`, `started_at`, `ended_at`, `total_duration_s`, `total_cost_usd`, and a `stages` array. Exits 1 with `not found` for unknown task IDs; prints `no stage data yet` when no dispatch events exist.
- `src/mas/trace.py` module — `build_trace(task_dir)` that parses `audit.jsonl` via `audit.read_events()`, pairs dispatch/completion events by `(subtask_id, cycle)`, resolves `cost_usd` from subtask `result.json` files, and marks unmatched dispatch events as in-flight.
- **Web UI Trace page** (`GET /trace/<task_id>`): renders the same per-task stage timeline as `mas trace` in the browser. The header shows task id, role, goal, total wall-clock time, token count, and cost. Each row in the timeline table corresponds to one subtask stage and carries `data-task-id`/`data-subtask-id` attributes plus a CSS status class (`status-failure` for failed stages, `in-flight` for stages still running). Returns 404 with the task id in the body for unknown tasks. The task detail page (`/task/<task_id>`) now includes a **Trace** link pointing to this page.

- **`--json-logs` flag for `mas daemon start`**: switches `.mas/logs/daemon.log` from plain text to newline-delimited JSON. Every record includes stable keys `ts` (UTC ISO-8601 with `Z` suffix), `level` (lowercase: `info`, `warn`, `error`), and `event` (machine-readable name such as `daemon_start`, `daemon_stop`, `tick_start`, `tick_done`, `tick_error`, `config_reloaded`), plus event-specific context fields (`pid`, `interval_s`, `duration_s`, `error`, `changes`). The `JsonFormatter` in `src/mas/logging.py` normalises `WARNING` to `warn` for consistency.

- `mas doctor` command — diagnoses the local environment and prints a Rich table of check results across four groups: **Config** (parses and validates `config.yaml`/`roles.yaml`), **Provider** (verifies each used provider's CLI binary is present in PATH via `shutil.which`), **Board** (detects orphan git worktrees where a `mas/<id>` branch has no corresponding task directory, and stale worker PID files), and **Daemon** (checks `daemon.pid` liveness — missing file is OK, dead PID is FAIL). Flags: `--json` emits `{"checks": [...], "summary": {"ok", "warn", "fail"}}` with no ANSI sequences; `--strict` treats WARN-level checks as failures. Exit codes: **0** if all checks are OK (or only WARNs without `--strict`); **1** if any FAIL is present, or any WARN is present with `--strict`. New `src/mas/doctor.py` module exposes `run_checks(mas_dir)` returning a list of `CheckRecord` TypedDicts.

- **Rejected proposal log**: when the proposer's similarity check drops a duplicate, the tick loop appends a record to `.mas/proposals/rejected.jsonl` (created lazily; does not require `mas init` or `mas upgrade`). Each line is a JSON object with fields: `timestamp` (ISO-8601 UTC, Z-terminated), `summary` (proposer result summary), `goal` (proposed goal, truncated to 500 chars with `...` when longer), `similarity_score` (Jaccard score, float), `matched_task_id`, `matched_column` (one of `proposed`/`doing`/`done`/`failed`), and `threshold` (config threshold at rejection time). Write failures are caught and logged at `WARNING`; they never interrupt the tick loop.
- `mas proposals rejected [--since <Nh|Nd|Nw>] [--limit N] [--json]` — list rejected (duplicate) proposals from `.mas/proposals/rejected.jsonl`. Displays a Rich table with columns `timestamp`, `summary`, `score` (3 decimal places), `matched_task_id`, and `matched_column`, sorted newest-first. `--since` accepts the same `h`/`d`/`w` duration strings as `mas stats`. `--json` emits newline-delimited JSON. `--limit` caps results (default 50). Exits 0 when the log is missing or empty.

- **`/stats` web page**: new `GET /stats` route in the web UI renders aggregate board counts, cumulative token usage, and total cost — the same data as `mas stats` on the CLI. A `?since=<window>` query parameter (e.g. `?since=1h`, `?since=7d`) filters results to tasks with activity in the given window; invalid values return HTTP 200 with an inline error banner instead of a 4xx. A **Stats** link has been added to the header navigation in `base.html`.

- **Daemon log rotation**: `mas daemon` now routes every line it emits (`_say()` messages, tick start/done, tick failures, unhandled tracebacks) through a `logging.handlers.RotatingFileHandler` attached to the `mas` logger at `.mas/logs/daemon.log`. Two new `MasConfig.daemon` knobs control rotation: `log_max_bytes` (default 10 MiB — file rotates to `daemon.log.1`, prior `.1`→`.2`, …) and `log_backup_count` (default 5 — older backups are deleted). `validate_config` rejects non-positive `log_max_bytes` and negative `log_backup_count`. Raw stdout/stderr are now redirected to `/dev/null` so no second unrotated sink exists. Caps disk usage on long-lived daemons, which is important now that config hot-reload removed the main reason to restart them.
- **Per-role wall-clock timeout**: the tick loop now reaps workers that are still alive but stuck (PID alive, no `result.json`, no progress). PID files include a dispatch timestamp (`pid\nepoch_seconds\n`); on every tick, for each live worker PID whose `now - dispatch_time > roles[<role>].timeout_s`, the reaper sends `SIGTERM`, waits 5 s, then `SIGKILL`s if still alive, synthesizes `Result(status="failure", summary="timeout exceeded after Ns", feedback=<log tail>)`, and appends a `dispatched → timeout` transition so the normal retry / fail-parent path runs unchanged. Legacy single-line pidfiles without a timestamp are treated as unknown-age and skipped. `RoleConfig.timeout_s` drives the budget (existing field; defaults already shipped via `.mas/roles.yaml`). Timeout failures are classified as regular `failure`, not `environment_error`, so they consume the role's retry budget — preventing an infinite-loop worker from retrying forever.

- **Task deletion**: new `mas delete <id> [<id>…] [-y/--yes]` command permanently removes one or more tasks from any column (`proposed/`, `doing/`, `done/`, `failed/`). `board.delete_task()` SIGTERMs any live worker PIDs, escalates to SIGKILL after 3 s, prunes the task's worktree (branch preserved), then removes the task directory. Exits non-zero if any requested ID is not on the board (still deletes the ones that exist). The web UI adds a **Delete** button on the task detail page (`POST /task/{id}/delete`) and per-task checkboxes + a **Delete selected** bulk action on the board (`POST /tasks/delete`, accepts repeated `task_ids` form fields).
- **Markdown rendering on web UI task page**: task goals, result summaries/feedback, previous-failure text, and subtask goals/summaries now render as Markdown (headings, lists, fenced code with syntax class, tables, nl2br) via a `md` Jinja filter. New collapsible **Task info** card shows id, role, column, parent, created timestamp, cycle/attempt, budget, and pretty-printed `inputs`/`constraints`. Requires the new `markdown>=3.5` dependency in the `web` extra.

- **Logs viewer in web UI**: the task detail page (`/task/<id>`) now includes a logs viewer tab.
  - New `GET /task/<id>/logs` JSON endpoint returning `{"logs": [...]}` with entries containing `name`, `role`, and `size` (bytes).
  - Role extraction from log filenames: split on `-` or `.` to extract the role prefix (e.g., `implementer-1.log` → `implementer`, `tester.claude_code.log` → `tester`).
  - Optional `?role=<name>` query parameter to filter logs by role exactly.
  - Returns `{"logs": []}` when no logs directory exists or no files match the filter; returns HTTP 404 for nonexistent tasks.
  - Web UI renders role filter buttons (All, proposer, orchestrator, implementer, tester, evaluator) and shows "No logs available" when empty.
  - Log files are discovered from the task's `logs/` directory, with role extracted from filename patterns.

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

- **Dashboard filtering**: the web UI board page (`/`) now supports server-side filtering with URL query parameter persistence for shareability. A collapsible filter form accepts: `task_id` (substring match), `status` (proposed/doing/done/failed), `cost_min`/`cost_max` (USD range), `failure_reason` (substring match), and `date_from`/`date_to` (ISO-8601 bounds). Empty filters return all tasks. Filter state is encoded in URL query params so links can be copied and shared.
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
