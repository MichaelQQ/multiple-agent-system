# mas — Multi-Agents Orchestration System

Coordinate multiple coding CLIs (Claude Code, Codex, Gemini CLI, Ollama) as a
role-based team driven by a directory-as-job-board. Design details:
[`docs/PLAN.md`](docs/PLAN.md).

## Install

```sh
mise use python@3.12          # or pyenv, system python ≥ 3.11
python -m venv .venv
.venv/bin/pip install -e .
```

Put `.venv/bin` on your `$PATH` or use `.venv/bin/mas` directly.

## Per-project setup

Inside each target repo:

```sh
mas init                      # creates .mas/ with config, roles, prompts
```

Edit `.mas/config.yaml` (provider CLIs and concurrency caps) and
`.mas/roles.yaml` (role → provider/model/timeouts/allowlists). Defaults bind:

| role         | provider     | note                               |
|--------------|--------------|------------------------------------|
| proposer     | claude-code  | haiku, read-only                   |
| orchestrator | claude-code  | opus, emits plan.json              |
| implementer  | codex        | writes code inside the worktree    |
| tester       | gemini       | runs/authors tests                 |
| evaluator    | ollama       | read-only verdict (pass/fail/rev)  |

Seed proposer context in `.mas/ideas.md` (one bullet per idea).

## Daily workflow

```sh
mas tick                      # run one pass: reap → advance → dispatch
mas show                      # print the board
mas promote <id>              # proposed/  → doing/  (human approval gate)
mas retry   <id>              # failed/    → doing/
mas logs    <id> [-f]         # tail the latest worker log
```

A tick is safe to run any time — it takes a flock, reaps dead workers,
advances the state machine, then dispatches new work within per-provider
concurrency caps.

### Human gates (only two)

1. **Promotion.** A proposer keeps `.mas/tasks/proposed/` topped up to
   `max_proposed`. You review cards there and run `mas promote <id>` to move
   approved ones to `doing/`.
2. **PR.** When a task lands in `done/`, its branch `mas/<id>` is preserved
   (worktree pruned). You open the PR yourself with `gh pr create`.

## Scheduling

```sh
mas cron install              # */5 * * * *  cd <project> && mas tick
mas cron install --interval 10
mas cron status
mas cron uninstall
```

Cron entries are scoped per project (hash of the absolute path), so multiple
projects can each install their own schedule without colliding.

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
      logs/{role}-{n}.log
      subtasks/{child}/{task.json, result.json, logs/, pids/}
    done/{id}/   failed/{id}/
```

Agents communicate via JSON files, never prose. Each worker reads `task.json`
from its own directory and writes `result.json` before exiting. Stdout is
logs only. Schemas live in `src/mas/schemas.py`.

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

## Tests

```sh
.venv/bin/pytest -q
```

Unit tests cover schemas, board moves, PID counter, previous-failure
injection, id generator.

## Scope of v1

Implemented: init, tick, show, promote, retry, logs, cron install/uninstall/
status. Out of scope (v2): `mas pr`, `mas kill`, `mas prune`, `mas stats`,
`mas doctor`, launchd, parallel child execution, auto-PR/merge.
