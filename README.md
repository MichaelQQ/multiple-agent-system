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
mas promote <id>              # proposed/  → doing/  (human approval gate)
mas retry   <id>              # failed/    → doing/
mas logs    <id> [-f]         # tail the latest worker log
```

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

### System cron

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

## Tests

```sh
.venv/bin/pytest -q                                          # all tests
.venv/bin/pytest tests/e2e/ -q                               # E2E tests only
```

### Test layers

- **Unit** (`tests/test_*.py`): Schemas, board moves, PID counter, previous-failure injection, id generator.
- **Integration** (`tests/integration/`): Board and tick interactions with mocked adapters.
- **E2E** (`tests/e2e/`): Full lifecycle scenarios using a real `.mas` directory with config/roles, real tick loop and board operations, but mocked adapter dispatch.

### E2E test coverage

The E2E suite (`tests/e2e/test_lifecycle.py`) contains 8 tests covering:

| Test class | Coverage |
|------------|----------|
| `TestHappyPathLifecycle` | Happy path: proposal → orchestration → implementation → testing → evaluation → done. Board transitions. |
| `TestRevisionCycleLifecycle` | Revision cycles bounded by `max_revision_cycles`, feedback propagation from evaluator to implementer. |
| `TestFailureRecovery` | Subtask max_retries moves parent to `failed/`. |
| `TestWorktreeLifecycle` | Git worktree created on orchestration, pruned on done. |
| `TestPriorResultsPropagation` | Prior results injected into implementer's `task.json`. |

All E2E tests use a real `.mas` directory with `config.yaml`/`roles.yaml`, real tick loop and board operations, but mock adapter dispatch calls to simulate agent output.

See [TESTING_STRATEGY.md](TESTING_STRATEGY.md) for the full testing approach.

### E2E Tests

The E2E test suite (`tests/e2e/`) exercises the full MAS task lifecycle from
proposed → doing → done. It validates:

- Task lifecycle transitions and state machine behavior
- Schema validation of task.json and result.json
- transitions.jsonl logging
- Git worktree creation and pruning

Run E2E tests:

```sh
.venv/bin/pytest tests/e2e/ -v
```

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

Implemented: init, validate, tick, show, promote, retry, logs, cron install/uninstall/
status, daemon start/stop/status. Out of scope (v2): `mas pr`, `mas kill`,
`mas prune`, `mas stats`, `mas doctor`, launchd, parallel child execution,
auto-PR/merge.
