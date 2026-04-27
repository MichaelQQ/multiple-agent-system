# Multi-Agents Orchestration System (`mas`) — Plan

## Context

Greenfield project at `/Users/_vpon/Documents/multi-agents`. Goal: build a personal, provider-agnostic orchestrator (`mas`) that coordinates multiple coding-capable CLI agents (Claude Code, Codex, Gemini CLI, Ollama) as a role-based team. A job board in `.mas/tasks/` holds tasks across columns (`proposed/`, `doing/`, `done/`, `failed/`). A **proposer** agent suggests work, the user moves approved cards `proposed → doing`, an **orchestrator** picks them up, decomposes into visible **child tasks**, and drives them through **implementer → tester → evaluator** inside a per-task git worktree. Multi-provider lets the user route cheap text-only roles (evaluation, proposal) to Ollama while coding roles run on Claude Code / Codex / Gemini.

The user wants strict structured data between agents (JSON files, no prose hand-offs), minimal infrastructure (no daemon), per-project isolation (run `mas` inside each target project like they run `claude`), and human-in-the-loop at two checkpoints: promoting proposals and opening PRs.

## Decisions (locked)

| # | Area | Decision |
|---|---|---|
| 1 | Substrate | Standalone provider-agnostic app — not embedded in Claude Code. |
| 2 | Invocation | Pure subprocess to CLIs only (no API/SDK). |
| 3 | Inter-agent contract | File-based workspace per task. Orchestrator/tick writes `task.json`; agent writes `result.json`. Stdout is logs only. Pydantic-validated schemas. |
| 4 | Execution topology | Stateless `mas tick` CLI + cron. Long-running workers are detached subprocesses tracked via PID files in the task workspace. |
| 5 | Language | Python. `uv` install, `pydantic` schemas, `typer` CLI, `asyncio` for dispatch fan-out. |
| 6 | Job board | `tasks/{status}/{id}/` directory per card. Columns: `proposed/`, `doing/`, `done/`, `failed/`. Moves are `mv`. Proposer throttles when `tasks/proposed/` ≥ 10. |
| 7 | Role catalog | 5 roles: **proposer**, **orchestrator**, **implementer**, **tester**, **evaluator**. Router is deterministic tick logic (no LLM). |
| 8 | Scope | Per-project tool. Global install; invoked inside any project. `.mas/` per project holds config, prompts, tasks, logs. Global defaults in `~/.config/mas/`. |
| 9 | Sandboxing | Git worktree per parent task at `.mas/tasks/{id}/worktree/` on branch `mas/{id}`. Shared across the parent's children. Pruned on `done/`. |
| 10 | Role prompts | One expert prompt template per role at `.mas/prompts/{role}.md`. Rendered with task variables at dispatch. No composable skill system. |
| 11 | Providers v1 | 5 adapters. 4 agentic (Claude Code, Codex, Gemini CLI, OpenCode) — given a workspace, explore and write `result.json` themselves. 1 text (Ollama) — tick pre-gathers context, pipes prompt via stdin, parses JSON from stdout. Common `Adapter.run(role_config, task_dir) -> Result` interface. |
| 12 | Default role → provider | proposer=Claude Code (`claude-haiku-4-5-20251001`) · orchestrator=Claude Code (`claude-opus-4-6`) · implementer=OpenCode · tester=OpenCode · evaluator=Ollama (`gemma4:e4b`). Overridable in `roles.yaml`. **When Gemini quota is available, prefer it for tester** (dedicated test CLI). **When Codex quota is available, it is a viable alternative for tester or implementer.** |
| 13 | Concurrency | Per-provider `max_concurrent` cap. Defaults: claude-code=2, codex=1, gemini=1, ollama=4, opencode=2. Each tick counts live PID files per provider; skips dispatch when capped. |
| 14 | Failure policy | Per-role `max_retries` (default 2) with backoff. Each retry's `task.json` carries a `previous_failure` summary so the agent can learn. Exhausted retries → `tasks/failed/{id}/` with full forensic state. `mas retry <id>` pushes back to `doing/`. |
| 15 | Proposer inputs | Four signals, pre-gathered by tick: (a) repo scan, (b) recent git log + diffs, (c) `.mas/ideas.md`, (d) local CI / test failure command output (configurable). |
| 16 | Pipeline driver | Orchestrator emits **child tasks** (visible on board). Tick dispatches children; orchestrator itself is short-lived. |
| 17 | Child layout | Children nested at `tasks/doing/{parent}/subtasks/{child}/`. Auto-ready (parent was approved). Sequential execution in orchestrator-declared order. All children share parent's worktree. Parent → `done/` when last child completes and evaluator passes. |
| 18 | Eval verdicts | `pass` / `fail` / `needs_revision`. `pass` → parent to `done/`. `fail` → retry per failure policy. `needs_revision` → orchestrator appends a new implementer+tester+evaluator child triplet with evaluator feedback, bounded by `max_revision_cycles` (default 2). |
| 19 | Completion | Worktree pruned (branch preserved). Task moves to `tasks/done/{id}/`. Human runs `gh pr create` or (v2) `mas pr <id>`. No auto-merge. |
| 20 | Scheduling | `mas cron install` writes a crontab entry (`*/5 * * * * cd <project> && mas tick >> .mas/logs/tick.log 2>&1`). `uninstall`, `status` siblings. macOS may use launchd — v2. |
| 21 | CLI surface (v1) | `mas init`, `mas tick`, `mas show`, `mas promote <id>`, `mas retry <id>`, `mas logs <id> [-f]`, `mas cron {install,uninstall,status}`. v2 shipped: `validate`, `delete`, `tail`, `prune`, `audit`, `events`, `cost`, `stats`, `upgrade`, `daemon {start,stop,status}`, `web`, `pr <id>` (open a GitHub PR for a done task via `gh`). v2 remaining: `kill`, `doctor`. |

## Shipping defaults (pushable-back)

- **Tool allowlists** in role config. `evaluator` = read-only (Read, Grep, Glob), `permission_mode: bypassPermissions`. `proposer` = read-only (Read, Grep, Glob), `permission_mode: bypassPermissions`. `orchestrator` = read + write `task.json`/`plan.json`/subtask dirs (no source edits), `permission_mode: default`. `implementer`, `tester` = full (Read, Edit, Write, Bash, etc.). Enforced per-provider via adapter flags (e.g., Claude Code `--permission-mode` + `--allowedTools`).
- **Cost/usage tracking**: each adapter emits `tokens_in`, `tokens_out`, `duration_s`, `cost_usd` into `result.json` when the provider reports them (Claude Code does, Ollama emits duration only). Aggregated by `mas stats` in v2; v1 just records.
- **Logging**: per-task logs at `.mas/tasks/{id}/logs/{role}-{attempt}.log` (stdout+stderr of the subprocess). Tick-level log at `.mas/logs/tick.log`.
- **Task ID format**: `{yyyymmdd}-{slug}-{hash4}` (e.g., `20260414-add-retry-logic-a3f9`).

## Directory layout

```
.mas/
  config.yaml                 # providers, global defaults
  roles.yaml                  # role → provider/model/timeouts/allowlist
  prompts/
    proposer.md
    orchestrator.md
    implementer.md
    tester.md
    evaluator.md
  ideas.md                    # user-curated proposal seeds
  logs/
    tick.log
  tasks/
    proposed/{id}/task.json
    doing/{id}/
      task.json
      plan.json               # orchestrator output
      worktree/               # git worktree (branch mas/{id})
      pids/{role}.pid         # active-worker tracking
      logs/{role}-{n}.log
      subtasks/{child_id}/
        task.json
        result.json
        logs/...
    done/{id}/                # moved here after pass; worktree pruned
    failed/{id}/               # full forensic state retained
```

## Key data contracts (pydantic)

```python
class Task(BaseModel):
    id: str
    parent_id: str | None
    role: Literal["proposer","orchestrator","implementer","tester","evaluator"]
    goal: str
    inputs: dict                # context, file paths, prior result refs
    constraints: dict           # scope bounds, disallowed paths, timeouts
    previous_failure: str | None  # populated on retry
    cycle: int                  # revision cycle count
    created_at: datetime

class Result(BaseModel):
    task_id: str
    status: Literal["success","failure","needs_revision"]
    summary: str
    artifacts: list[str]        # paths relative to worktree
    handoff: dict | None        # optional structured context for next role
    verdict: Literal["pass","fail","needs_revision"] | None  # evaluator only
    feedback: str | None        # evaluator only; used as next cycle's previous_failure
    tokens_in: int | None
    tokens_out: int | None
    duration_s: float
    cost_usd: float | None
```

## Tick loop (single pass)

1. Acquire lockfile at `.mas/tick.lock` (flock). Skip if another tick is running.
2. Reap completed workers: for each task dir with a PID file, check if PID is alive; if not, move `result.json.tmp` → `result.json`, update task state.
3. Advance state machines:
   - `proposed/` with `role=proposer` → leave (human review gate).
   - `doing/{id}/` without `plan.json` → dispatch orchestrator (writes `plan.json` + subtask dirs).
   - `doing/{id}/` with `plan.json` → dispatch next ready subtask (respecting sequential order + concurrency caps).
   - Subtask `done` → if evaluator → apply verdict; else mark subtask `done` and advance parent.
   - Parent all-children-done + evaluator pass → `mv` to `done/`, prune worktree (keep branch).
4. Gather proposer signals (repo scan summary, `git log -20`, `ideas.md`, CI command output) if `len(tasks/proposed/) < 10`. Dispatch proposer if no proposer active.
5. Release lock.

Each dispatch: render role prompt → write task-dir `task.json` → `Popen([cli, flags...], cwd=worktree)` detached → write PID file → return.

## Files to create

- `src/mas/cli.py` — typer entry, subcommands.
- `src/mas/tick.py` — the tick loop above.
- `src/mas/board.py` — directory-as-board helpers (move, list, state).
- `src/mas/schemas.py` — pydantic `Task`, `Result`, `Plan`, config models.
- `src/mas/config.py` — merge `~/.config/mas/` + `.mas/` configs.
- `src/mas/adapters/base.py` — `Adapter` ABC.
- `src/mas/adapters/claude_code.py`, `codex.py`, `gemini_cli.py`, `ollama.py`.
- `src/mas/roles.py` — prompt rendering + role-specific logic (proposer signal gathering, orchestrator plan parsing, evaluator verdict handling).
- `src/mas/worktree.py` — git worktree lifecycle.
- `src/mas/cron.py` — install/uninstall/status crontab entries.
- `src/mas/ids.py` — task ID generator.
- `pyproject.toml` — `uv`-managed deps: `typer`, `pydantic`, `pyyaml`, `rich` (logs).
- `templates/prompts/{role}.md` — default prompt templates packaged with the app (copied by `mas init`).
- `templates/config.yaml`, `templates/roles.yaml` — defaults copied by `mas init`.

## Verification plan

1. **Unit**: pydantic round-trip for `Task`/`Result`/`Plan`; board moves are atomic; concurrency-cap counter respects PID files; retry injects `previous_failure`.
2. **Adapter stubs**: add a `mock` adapter (reads a canned `result.json` from fixtures, sleeps, exits) to exercise tick loop end-to-end without real APIs.
3. **E2E smoke** with real CLIs on a throwaway repo:
   - `mas init`, drop an `ideas.md` entry, run `mas tick` → proposer writes a `proposed/` card.
   - `mas promote <id>` → run `mas tick` repeatedly → orchestrator writes plan + subtasks, each subtask dispatches, worktree contains edits, evaluator returns verdict, task lands in `done/`.
   - Inject a failing test to force `needs_revision` path; verify bounded cycles.
   - Kill a worker mid-flight, run next tick → reap + retry with `previous_failure`.
   - `mas cron install` → verify crontab entry, then `uninstall`.
4. **Multi-provider smoke**: flip evaluator to Ollama (`llama3.2` or similar), rerun E2E; confirm text-adapter path works.

## Shipped in v2

- `mas validate` — config + provider + prompt-template validation. Runs automatically before `mas tick` and `mas daemon start`.
- `mas delete <id>…` — permanently remove tasks from any column; SIGTERMs live workers, prunes the worktree (branch preserved).
- `mas tail <id>` — line-controlled log tail (`-n`, `-f`).
- `mas prune` — clean up leftover worktrees under `done/` and `failed/` (branch preserved).
- `mas audit <id>` — Rich timeline of structured events from `{task_dir}/audit.jsonl` (`dispatch`, `completion`, `state_transition`).
- `mas events` — board-wide audit feed across `doing/`, `done/`, `failed/`. Flags: `--task`, `--role`, `--status`, `--event`, `--since`, `--until`, `--follow`/`-f`, `--interval`, `--json`.
- `mas cost <id>` — per-subtask token / cost breakdown plus `Budget:` row when `cost_budget_usd` is set on the task or `default_cost_budget_usd` in config.
- `mas stats` — aggregate board/role/provider/token statistics. Flags: `--since <duration>` (h/d/w), `--json`.
- `mas show --json` / `mas show <id> --json` — machine-readable board / task tree.
- `mas upgrade` — refresh `.mas/` templates; per-file unified diff, confirm prompt (`-y` to skip), optional daemon restart using interval persisted in `.mas/daemon.interval`.
- `mas daemon {start,stop,status}` — detached per-project tick loop with rotating logs (`daemon.log_max_bytes`, `daemon.log_backup_count`) and config hot-reload (`config.yaml` + `roles.yaml` mtime check before each tick; falls back on the previous valid config if the new one fails to validate).
- `mas web` — local FastAPI/Jinja UI (loopback only, no auth). Pages: Board, Events, Validate, Cron. Mirrors CLI actions (`tick`, `promote`, `retry`, `delete`, single + bulk; `prune`, `daemon start/stop`, `upgrade`). Markdown rendering for goals, summaries, feedback, and previous-failure text.
- **Webhooks** — outbound HTTP POST on every `board.move()`. Configured under `webhooks:` in `config.yaml` with `url`, `events` (column names or `from->to` strings), and `timeout_s`. Best-effort and non-blocking.
- **Per-task cost budgets** — `Task.cost_budget_usd` (per-task) and `MasConfig.default_cost_budget_usd` (project default). Tick short-circuits before dispatching the next subtask once spent ≥ budget; parent moves to `failed/` with reason `cost_budget_exceeded`.
- **Per-role wall-clock timeout** — PID files include a dispatch timestamp; reaper SIGTERM/SIGKILLs workers exceeding `roles[<role>].timeout_s`, synthesizes a `failure` result with `summary="timeout exceeded after Ns"` so the normal retry path runs unchanged.
- **Audit logging** — every dispatch, completion, and state transition is appended to `{task_dir}/audit.jsonl` (timestamp, event, role, provider, task_id, subtask_id, status, duration_s, summary, details).
- **Cost tracking** — adapters populate `tokens_in`, `tokens_out`, `cost_usd` on `result.json` where reported; `tick._finalize_parent` aggregates child totals into the parent `result.json`. Rate table in `src/mas/pricing.py`.
- **Strict schema validation** — all pydantic models use `extra="forbid"`; `Task.id` is regex-validated; `Result.duration_s` must be ≥ 0; custom errors (`PlanParseError`, `TaskReadError`, `ResultReadError`) carry file path + content snippet + root cause.

## Out of scope for v1 (remaining)

- `mas kill`, `mas doctor`.
- launchd plist support (crontab only; `mas daemon` covers the no-system-cron case).
- Parallel child execution / merge strategy.
- Auto-PR / auto-merge.
- External issue tracker integration.
- Multi-repo orchestration.
- Composable skill fragments.

## Risks & mitigations

- **CLI output format drift** (Claude Code / Codex / Gemini flags change): pin adapter logic to stable flags (non-interactive JSON output); version-check in `doctor` (v2).
- **Agent ignores the `result.json` contract**: every prompt ends with "Your last action MUST be writing a valid `result.json` matching this schema." Adapter validates on exit; missing/invalid counts as failure and triggers retry-with-feedback.
- **Infinite revision loop**: `max_revision_cycles` cap; after exhaustion parent moves to `failed/`.
- **Worktree corruption if tick is killed mid-`git worktree add`**: wrap in a try/finally; on next tick, detect half-created worktrees (branch exists, no `.git` in worktree dir) and clean up.
- **Cron drift**: `mas tick` is idempotent and takes a flock — overlapping fires are safe.
- **Ollama context pre-gather quality**: for v1, keep Ollama roles to evaluator (narrow context: diff + spec + rubric). Broader Ollama roles come later once we observe what context shape works.
