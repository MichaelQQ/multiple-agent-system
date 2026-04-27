# mas — Multi-Agents Orchestration System

Coordinate multiple coding CLIs (Claude Code, Codex, Gemini CLI, Ollama, OpenCode) as a
role-based team driven by a directory-as-job-board. Design details:
[`docs/PLAN.md`](docs/PLAN.md).

## Install

```sh
mise use python@3.12          # or pyenv, system python ≥ 3.11
python -m venv .venv
.venv/bin/pip install -e .
```

Put `.venv/bin` on your `$PATH` or use `.venv/bin/mas` directly.

Optional web UI dependencies:

```sh
.venv/bin/pip install -e ".[web]"
```

## Per-project setup

Inside each target repo:

```sh
mas init                      # creates .mas/ with config, roles, prompts
```

Edit `.mas/config.yaml` (provider CLIs and concurrency caps) and
`.mas/roles.yaml` (role → provider/model/timeouts/allowlists). Defaults bind:

| role         | provider     | model                        | note                               |
|--------------|--------------|------------------------------|------------------------------------|
| proposer     | claude-code  | claude-haiku-4-5-20251001    | read-only (bypassPermissions)      |
| orchestrator | claude-code  | claude-opus-4-6              | emits plan.json                    |
| implementer  | opencode     | —                            | writes code inside the worktree    |
| tester       | opencode     | —                            | runs/authors tests                 |
| evaluator    | ollama       | gemma4:e4b                   | read-only verdict (pass/fail/rev)  |

> **Quota overrides:** When Gemini or Codex quota is available, they are preferred alternatives for the tester role. Override in `.mas/roles.yaml`:
> ```yaml
> tester:
>   provider: gemini   # or codex
> ```

Seed proposer context in `.mas/ideas.md` (one bullet per idea).

## Daily workflow

```sh
mas validate                  # validate config, providers, and prompts (runs automatically before tick/daemon)
mas tick                      # run one pass: reap → advance → dispatch
mas show                      # print the board
mas show --json               # emit board as pretty-printed JSON (for dashboards/CI)
mas show <id>                 # render one task's subtask tree
mas show <id> --json          # emit task details as pretty-printed JSON
mas promote <id>              # proposed/  → doing/  (human approval gate)
mas retry   <id>              # failed/    → doing/
mas delete  <id> [<id>…] [-y] # remove tasks from any column (proposed/doing/done/failed)
mas logs    <id> [-f]         # print/follow the latest worker log
mas tail    <id> [-n 50] [-f] # tail task logs with line control
mas prune                     # prune leftover worktrees under done/ and failed/
mas audit   <id>              # display structured audit timeline for a task
mas events  [--follow] [--json] [filters…]  # aggregate events across all tasks
mas cost    <id>              # print per-subtask token/cost breakdown
mas stats                     # print aggregate board/role/provider/token statistics
```

### Observability

Every board move, subtask dispatch, and completion is appended to `{task_dir}/audit.jsonl`.
Use `mas audit <id>` to view it as a Rich table:

```sh
mas audit <id>                         # all events for a task
mas audit <id> --role implementer      # filter by role
mas audit <id> --status success        # filter by status
mas audit <id> --since 2026-04-20T00:00:00Z --until 2026-04-21T00:00:00Z
```

`mas logs <id>` and `mas tail <id>` show raw subprocess output; `mas audit` shows
the structured event timeline emitted by the tick loop itself.

#### Cross-task event aggregation with `mas events`

`mas events` aggregates audit events across **all** tasks on the board into a
single Rich table, sorted by timestamp ascending:

```sh
mas events                                    # all events from all tasks
mas events --task 20260423-my-task-ab12       # single task
mas events --role implementer                 # filter by role
mas events --status success                   # filter by outcome status
mas events --event completion                 # filter by event type
mas events --since 2026-04-20T00:00:00Z --until 2026-04-21T00:00:00Z

# Stream new events as they appear (Ctrl-C to stop)
mas events --follow
mas events --follow --interval 5              # poll every 5 s (default: 2)

# Machine-readable newline-delimited JSON
mas events --json
mas events --role implementer --status success --json
```

Flags:

| Flag | Short | Description |
|---|---|---|
| `--task` | | Filter to a single task ID |
| `--role` | | Filter by role name |
| `--status` | | Filter by outcome status |
| `--event` | | Filter by event type (`dispatch`, `completion`, `state_transition`) |
| `--since` | | ISO-8601 lower bound (inclusive) |
| `--until` | | ISO-8601 upper bound (inclusive) |
| `--follow` | `-f` | Poll for new events (live tail) |
| `--interval` | | Polling interval in seconds when `--follow` is active (default: 2) |
| `--json` | | Emit one JSON object per line instead of a Rich table |

#### `audit.jsonl` format

Each line is a JSON object with the following fields:

| Field         | Type             | Description                                                   |
|---------------|------------------|---------------------------------------------------------------|
| `timestamp`   | ISO-8601 UTC     | When the event was recorded                                   |
| `event`       | string           | One of `dispatch`, `completion`, `state_transition`           |
| `role`        | string \| null   | Role that triggered the event (e.g. `implementer`)            |
| `provider`    | string \| null   | Provider CLI used (e.g. `claude-code`, `ollama`)              |
| `task_id`     | string           | Parent task ID                                                |
| `subtask_id`  | string \| null   | Child subtask ID (set for `dispatch` and `completion` events) |
| `status`      | string \| null   | Outcome status (e.g. `success`, `failure`, `needs_revision`)  |
| `duration_s`  | float \| null    | Wall-clock seconds for the event (set on `completion`)        |
| `summary`     | string           | Human-readable description                                    |
| `details`     | object           | Extra key/value context (may be empty `{}`)                   |

Event types:

- **`dispatch`** — a subtask was dispatched to a provider. Written to the *parent* task's `audit.jsonl`.
- **`completion`** — a subtask result was reaped (success, failure, or needs_revision). Written to the *parent* task's `audit.jsonl`.
- **`state_transition`** — the parent task moved between board columns (`proposed → doing`, `doing → done`, etc.).

### Webhooks

Outbound HTTP notifications fired on every board transition. Configure in `.mas/config.yaml`:

```yaml
webhooks:
  - url: https://hooks.example.com/mas
    events: ["done", "failed"]   # column names or "from->to" transitions
    timeout_s: 10                # 1..120, default 10
```

`events` accepts column names (e.g. `"done"`, `"failed"`) or explicit transitions (e.g. `"doing->done"`). An empty list means all transitions.

Each delivery is a JSON POST with these fields:

| Field       | Type         | Description                                      |
|-------------|--------------|--------------------------------------------------|
| `task_id`   | string       | Parent task ID                                   |
| `role`      | string\|null | Role that triggered the transition               |
| `goal`      | string       | Task goal text                                   |
| `from`      | string       | Source board column                              |
| `to`        | string       | Destination board column                        |
| `summary`   | string\|null | Result summary (if available)                    |
| `status`    | string\|null | Result status (success, failure, needs_revision) |
| `timestamp` | ISO-8601 UTC | When the transition occurred                     |
| `task_dir`  | string       | Absolute path to the task directory              |

Delivery is best-effort and non-blocking. Non-2xx responses, timeouts, and connection errors are caught and logged at `WARNING` level; they never block the tick loop.

### Validation

`mas validate` checks:
- Config is not empty and has required fields
- All provider CLIs are available in PATH
- All role prompt templates exist in `.mas/prompts/`

Exit codes:
- **0** — validation passed
- **1** — validation failed (errors printed to stderr)

The `validate_config()` function is also available for programmatic use:

```python
from mas.config import validate_config, load_config, project_dir

cfg = load_config(project_dir())
issues = validate_config(cfg, project_dir())
# issues: list[ValidationIssue] — empty if valid
```

Validation runs automatically before `mas tick` and `mas daemon start` to prevent orphaned tasks.

A tick is safe to run any time — it takes a flock, reaps dead workers,
advances the state machine, then dispatches new work within per-provider
concurrency caps.

### Human gates (only two)

1. **Promotion.** A proposer keeps `.mas/tasks/proposed/` topped up to
   `max_proposed`. You review cards there and run `mas promote <id>` to move
   approved ones to `doing/`.
2. **PR.** When a task lands in `done/`, its branch `mas/<id>` is preserved
   (worktree pruned). You open the PR yourself with `gh pr create`.

## Cost tracking

Each agent writes `tokens_in`, `tokens_out`, and `cost_usd` into its `result.json`. When a parent task completes, the tick loop aggregates those values across all subtask results and writes a summary `result.json` at the parent level.

```sh
mas cost <task-id>            # print per-subtask token/cost breakdown with totals
```

Providers that do not report token usage leave `tokens_in`, `tokens_out`, and `cost_usd` as `null` in `result.json`; the aggregation treats `null` as 0. Cost rates are defined in `src/mas/pricing.py` — add new provider/model entries there to enable cost calculation.

### Cost budgets

You can cap spending per task or project-wide:

- **Per-task**: set `cost_budget_usd` in the task's `task.json` (optional `float`).
- **Project default**: set `default_cost_budget_usd` in `.mas/config.yaml` (optional `float`). The per-task value takes precedence when both are set.

When the running total of `cost_usd` across completed subtasks reaches the effective budget, the tick loop stops dispatching further subtasks, writes a failure `result.json` (`summary: "cost budget exceeded"`, `handoff` with `spent_usd`, `budget_usd`, and `last_completed_subtask_id`), and moves the parent task to `failed/` with transition reason `cost_budget_exceeded`.

`mas cost <task-id>` shows a `Budget:` line (e.g., `Budget: 0.012300 / 0.050000 (24.6% utilized)`) when `cost_budget_usd` is set on the task.

## Stats

```sh
mas stats                     # aggregate stats across all board columns
mas stats --since 7d          # only tasks with activity in the last 7 days
mas stats --since 24h         # last 24 hours (also accepts: h, d, w suffixes)
mas stats --json              # emit raw JSON instead of a Rich table
```

`mas stats` scans all four board columns (`proposed`, `doing`, `done`, `failed`) and prints a summary table covering:

| Section | Fields |
|---------|--------|
| Board counts | tasks per column |
| Rates | success rate, revision rate |
| Role timing | mean / p50 / p95 duration (seconds) per role |
| Provider usage | task count per provider |
| Token totals | `tokens_in`, `tokens_out`, `cost_usd` |
| Environment errors | tasks with `status=environment_error` or env-retry markers |

With `--json` the output is a single JSON object — useful for piping into `jq` or dashboards.

## Upgrading templates

```sh
mas upgrade                   # show unified diff, prompt before applying
mas upgrade --dry-run         # show diff only, never write
mas upgrade --yes             # apply without prompting, auto-restart daemon
```

`mas upgrade` refreshes `.mas/config.yaml`, `.mas/roles.yaml`, and
`.mas/prompts/*.md` from the installed package. Tasks, logs, and `ideas.md`
are preserved. Per changed file, a unified diff is printed so you can review
before confirming. When there is nothing to change, the command exits quickly
with `already up to date`.

If a daemon is running, the command prompts to restart it so the new templates
take effect; the previous interval (persisted in `.mas/daemon.interval`) is
reused.

## Scheduling

### Daemon (no system cron)

```sh
mas daemon start              # fork detached process, tick every 300 s
mas daemon start --interval 60
mas daemon status
mas daemon stop
```

The daemon writes its PID to `.mas/daemon.pid`, its configured interval to
`.mas/daemon.interval`, and logs to `.mas/logs/daemon.log`. Only one daemon
may run per project; starting a second raises an error.

**Config hot-reload:** The daemon automatically detects changes to `.mas/config.yaml`
and `.mas/roles.yaml` without requiring a restart. Before each tick cycle, it checks
the config file modification time and reloads if changed. If the new config is invalid
(e.g., malformed YAML, missing required fields, unknown provider), the daemon keeps
the previous valid configuration and logs a warning.

### System cron

```sh
mas cron install              # */5 * * * *  cd <project> && mas tick
mas cron install --interval 10
mas cron status
mas cron uninstall
```

Cron entries are scoped per project (hash of the absolute path), so multiple
projects can each install their own schedule without colliding.

## Web UI

Install the optional `web` extra, then run:

```sh
mas web
mas web --host 127.0.0.1 --port 8765
```

The local UI shows the board, task details, recent audit events, cost totals,
and log tails. Tasks within each column are sorted by most recent transition
(newest first). The header navigation exposes four pages:

| Page        | Route       | Purpose                                                           |
|-------------|-------------|-------------------------------------------------------------------|
| Board       | `/`         | Kanban view; run tick, start/stop daemon, prune, upgrade          |
| Events      | `/events`   | Cross-task audit feed with `task/role/status/event/limit` filters |
| Validate    | `/validate` | Runs `validate_environment` and shows providers/roles summary     |
| Cron        | `/cron`     | Inspect, install, and uninstall the per-project cron entry        |

Actions available from the board and task pages mirror the CLI: `tick`,
`promote`, `retry`, `delete`, `prune`, `daemon start/stop`, and `upgrade`
(runs `mas upgrade --yes` in a detached subprocess). The board page has
a per-task checkbox, a "select all" toggle, and a **Delete selected**
button for bulk deletion. The task detail page shows a collapsible
**Task info** section (id, role, column, parent, created, cycle/attempt,
budget, inputs, constraints, previous failure), plus plan/subtasks, audit
timeline, transitions, cost totals with the per-task budget row, and a
tabbed log viewer. Task goals, result summaries/feedback, and
previous-failure text are rendered as Markdown (headings, lists, fenced
code, tables).

It is designed for local loopback use and has no auth layer.

## Layout

```
.mas/
  config.yaml      roles.yaml      ideas.md
  prompts/{proposer,orchestrator,implementer,tester,evaluator}.md
  logs/tick.log
  tasks/
    proposed/{id}/task.json
    doing/{id}/
      task.json  plan.json  worktree/  pids/{role}.{provider}.pid
      audit.jsonl
      logs/{role}-{n}.log
      subtasks/{child}/{task.json, result.json, logs/, pids/}
    done/{id}/   failed/{id}/
```

Agents communicate via JSON files, never prose. Each worker reads `task.json`
from its own directory and writes `result.json` before exiting. Stdout is
logs only. Schemas live in `src/mas/schemas.py`.

### Schema validation

All models use `extra="forbid"` — unknown fields in `task.json`,
`result.json`, `plan.json`, or `config.yaml` cause validation errors.
The `board.read_task()` and `board.read_plan()` helpers parse with
`model_validate_json()` to enforce this.

- `Task.id` validates against pattern `{yyyymmdd}-{slug}-{hash4}`
- `Result.duration_s` must be non-negative
- `ProposalHandoff` model for proposer handoffs

### Error handling

mas uses custom exception types in `src/mas/errors.py` for clear, actionable error messages:

| Exception         | Raised By                  | Includes                                         |
|------------------|----------------------------|--------------------------------------------------|
| `PlanParseError` | `parse_plan()` in roles.py | file path, content snippet, root cause            |
| `TaskReadError`  | `read_task()` in board.py  | file path, content snippet, root cause          |
| `ResultReadError`| `read_result()` in board.py| file path, content snippet, root cause          |

These exceptions provide context for debugging: file path, a snippet of the problematic content, and the original exception.

## Failure handling

- Per-role `max_retries` (default 2) with the previous failure summary
  injected into the next attempt's `task.json` (`previous_failure` field).
- Evaluator verdict `needs_revision` appends a fresh
  implementer→tester→evaluator triplet, bounded by `max_revision_cycles`
  (default 2). Exhausted → parent moves to `failed/`.
- `mas retry <id>` moves a failed parent back to `doing/`.

## Environment contract for custom adapters

Every worker subprocess is launched with:

- `cwd` set to the parent's git worktree (for agentic roles) so the agent
  can freely Read/Edit there.
- `$MAS_ROLE` — the current role name.
- `$MAS_TASK_DIR` — absolute path the worker must write `result.json` into.

Prompt templates (`$task_dir`, `$worktree`, `$mas_dir`, `$goal`, etc.) are
rendered with `string.Template.safe_substitute` before dispatch.

Project config is loaded from `.mas/` and merged with optional user defaults in
`~/.config/mas/`.

## Tests

```sh
.venv/bin/pytest -q                                          # all tests
.venv/bin/pytest tests/adapters/ tests/integration/ -q       # integration-heavy suites
.venv/bin/pytest tests/e2e/ -q                               # E2E tests only
```

### Test layers

- **Unit** (`tests/test_*.py`): Schemas, board moves, PID counter, previous-failure injection, id generator.
- **Integration** (`tests/integration/`): Board and tick interactions with mocked adapters.
- **E2E** (`tests/e2e/`): Full lifecycle scenarios using a real `.mas` directory with config/roles, real tick loop and board operations, but mocked adapter dispatch.

The E2E suites (`tests/e2e/test_lifecycle.py` and
`tests/e2e/test_lifecycle_script.py`) exercise the full MAS task lifecycle from
proposed → doing → done. They validate task transitions, schema compliance,
revision cycles, worktree lifecycle, and script-provider subprocess behavior.

See [docs/testing-strategy.md](docs/testing-strategy.md) for the full testing approach.

#### Script Adapter

The **script adapter** (`script` provider) is a special adapter that executes
shell scripts as subprocesses instead of invoking AI agents. It's primarily
used for E2E testing but can also run simple automation scripts.

Configuration example in `.mas/roles.yaml`:

```yaml
proposer:
  provider: script
  extra_args:
    - --script
    - path/to/script.sh
```

The adapter receives `$MAS_ROLE` and `$MAS_TASK_DIR` environment variables and
must write `result.json` to `$MAS_TASK_DIR` before exiting.

#### Adding New E2E Scenarios

To add a new E2E test scenario:

1. Create shell scripts for the roles you need in `tests/e2e/scripts/`
2. Each script must write a valid `result.json` to `$MAS_TASK_DIR`
3. Add test cases in `tests/e2e/test_lifecycle.py` that use the script provider
4. Run `.venv/bin/pytest tests/e2e/ -v` to verify

See `tests/e2e/scripts/` for examples of role scripts.

## Scope of v1

Implemented: `init`, `upgrade`, `validate`, `tick`, `show`, `promote`,
`retry`, `delete`, `logs`, `tail`, `audit`, `cost`, `stats`, `prune`,
`cron`, `daemon`, and the optional `web` UI. Out of scope (v2):
`mas pr`, `mas kill`, `mas doctor`, launchd, parallel child execution,
auto-PR/merge.
