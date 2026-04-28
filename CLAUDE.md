# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mas` (Multi-Agents Orchestration System) coordinates multiple coding CLIs (Claude Code, Codex, Gemini CLI, Ollama, OpenCode) as a role-based team. A directory-based job board (`.mas/tasks/{proposed,doing,done,failed}/`) holds tasks; a stateless `mas tick` loop reaps workers, advances a state machine, and dispatches new work as detached subprocesses. Agents communicate exclusively via JSON files (`task.json` in, `result.json` out) — never prose.

## Build & test

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first time
.venv/bin/pytest -q                                          # all tests
.venv/bin/pytest tests/test_tick.py -q                       # single file
.venv/bin/pytest tests/test_tick.py::test_name -q            # single test
```

Python 3.11+. `mise.toml` pins 3.12. No linter/formatter configured yet.

## Architecture

**Tick loop** (`src/mas/tick.py`): The core. Single-pass, flock-guarded. Sequence: acquire lock → reap dead workers → advance doing/ tasks → maybe dispatch proposer → release lock. Each task in `doing/` is advanced through a state machine: ensure worktree → dispatch orchestrator → orchestrator writes `plan.json` with subtask specs → dispatch subtasks sequentially (implementer → tester → evaluator) → finalize parent to `done/`. A `.current_subtask` marker file is written on subtask dispatch and cleaned up when the result is collected.

**Board** (`src/mas/board.py`): Directory-as-kanban helpers. Four columns: `proposed/`, `doing/`, `done/`, `failed/`. Moves are `shutil.move` with transition logging. PID files track live workers (`pids/{role}.{provider}.pid`).

**Schemas** (`src/mas/schemas.py`): Pydantic models — `Task`, `Result`, `Plan`, `SubtaskSpec`, `MasConfig`, `RoleConfig`, `ProviderConfig`. All inter-agent data flows through these. `extra="forbid"` on all models.

**Adapters** (`src/mas/adapters/`): One per provider CLI. All extend `Adapter` ABC (`base.py`). Two categories:
- *Agentic* (`claude_code`, `codex`, `gemini_cli`, `opencode`): prompt passed as CLI arg; agent explores workspace and writes `result.json` itself.
- *Text* (`ollama`): prompt piped via stdin; tick parses JSON from stdout.

Each adapter's `build_command()` returns the CLI invocation; `dispatch()` (inherited from base) launches it as a detached subprocess.

**Roles** (`src/mas/roles.py`): Prompt rendering via `string.Template.safe_substitute`. Proposer signal gathering (repo scan, git log, ideas.md, CI output). Plan parsing from orchestrator output.

**Five roles**: proposer (suggests work) → orchestrator (decomposes into plan.json + subtasks) → implementer (writes code in worktree) → tester (runs/writes tests) → evaluator (pass/fail/needs_revision verdict). Evaluator `needs_revision` appends a new impl→test→eval cycle, bounded by `max_revision_cycles`.

**Worktree** (`src/mas/worktree.py`): Git worktree per parent task on branch `mas/{task_id}`. Shared across all subtasks of that parent. Pruned on completion (branch preserved).

**CLI** (`src/mas/cli.py`): Typer app. Commands: `init`, `validate`, `tick`, `show`, `promote`, `retry`, `delete`, `prune`, `logs`, `tail`, `audit`, `events`, `cost`, `stats`, `upgrade`, `doctor`, `web`, `cron {install,uninstall,status}`, `daemon {start,stop,status}`.

**ConfigWatcher** (`src/mas/config.py`): Tracks `config.yaml` and `roles.yaml` modification times. Provides `has_changed()` and `mark_checked()` methods. Used by the daemon to implement config hot-reload with fallback on invalid config.

**Web** (`src/mas/web/app.py`): Flask app. Renders board, task details (reads `.current_subtask` marker to show executing subtask), audit events, and cost totals. Exposes UI actions.

## Key conventions

- Task IDs: `{yyyymmdd}-{slug}-{hash4}` (generated in `src/mas/ids.py`).
- Failure handling: per-role `max_retries` (default 2). Previous failure summary injected into next attempt's `task.json.previous_failure`. Retries exhausted → parent moves to `failed/`.
- Concurrency: per-provider `max_concurrent` cap enforced by counting live PID files before dispatch.
- Orphan detection: if a worker's log exists but no PID is alive and no `result.json` written, tick synthesizes a failure result to trigger retry/fail paths.
- Transitions: every board move is logged to `transitions.jsonl` inside the task directory for audit.
- Templates at `templates/` are packaged as `mas/_templates/` via hatchling `force-include`; `mas init` copies them into `.mas/`. `mas upgrade` refreshes them interactively — per-file unified diff, confirmation prompt (skip with `-y/--yes`), and an optional daemon restart using the interval persisted in `.mas/daemon.interval`.
