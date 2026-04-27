# Testing Strategy for MAS (Multi-Agents Orchestration System)

This document defines a three-layer testing strategy: **Unit Tests**, **Integration Tests**, and **E2E Tests**.

---

## 1. Unit Tests

**Scope**: Isolated functions/classes with no cross-module dependencies.

### Modules to Cover

| Module | Coverage |
|--------|----------|
| `ids.py` | ID format validation, slug generation, task_id generation with date/slug/hash |
| `schemas.py` | Validation, serialization, JSON encoding/decoding of all Pydantic models (Task, Result, Plan, SubtaskSpec, RoleConfig, ProviderConfig, MasConfig) |
| `board.py` | Column moves (`move()`), PID tracking (`write_pid`, `clear_pid`, `count_active_pids`), task read/write operations |
| `roles.py` | Prompt rendering (`render_prompt` with Template substitution), plan parsing |
| `adapters/*` | Each adapter's `build_command()` method returning correct CLI invocation |
| `worktree.py` | Branch naming (`branch_name()`), worktree path construction |

### Mocking Guidelines

- Use `unittest.mock.patch` for filesystem calls: `shutil.move`, `Path.mkdir`, `Path.write_text`, `Path.read_text`, `Path.exists`
- Mock `subprocess.run` and `subprocess.Popen` for external command invocation
- Do NOT mock Pydantic validation — test with real models to ensure validation works
- For adapter tests, mock the provider CLI execution (the command is built but not run)

### Test Location

Most current unit tests live at the top level (`tests/test_*.py`) — e.g.
`tests/test_ids.py`, `tests/test_schemas.py`, `tests/test_board.py`,
`tests/test_config.py`, `tests/test_worktree.py`. Adapter-specific suites live
under `tests/adapters/`. New unit tests can be added in either place.

---

## 2. Integration Tests

**Scope**: Verify interactions between two or more components using real temp directories but mocked external CLIs.

### Key Scenarios

| Scenario | Components | Description |
|----------|------------|-------------|
| Task lifecycle | `board.py` ↔ `tick.py` | Task moves through proposed → doing → done, `transitions.jsonl` written correctly |
| Worker dispatch | `tick.py` ↔ adapters | Dispatch creates PID files, reap detects dead workers |
| Prompt rendering | `roles.py` ↔ `schemas.py` | Prompt rendering produces valid Task JSON |
| Worktree lifecycle | `worktree.py` ↔ `tick.py` | Worktree creation/pruning lifecycle |

### Setup

- Use `tmp_path` fixtures for all tests
- Create real `.mas/` directory structure: `.mas/tasks/{proposed,doing,done,failed}/`, `.mas/pids/`, `.mas/logs/`, `.mas/prompts/`
- Mock external CLIs (agentic providers) to avoid actual AI calls
- Use a `FakeAdapter` that implements the `Adapter` ABC but returns canned responses

### Test Location

Cross-module and CLI scenarios live in `tests/integration/`. Some older
cross-module suites also remain at the top level
(`tests/test_tick.py`, `tests/test_cli.py`, `tests/test_orphan.py`,
`tests/test_retry_marker.py`).

---

## 3. E2E Tests

**Scope**: Full tick cycle from task creation to completion.

### Scenarios

| Scenario | Description |
|----------|-------------|
| Happy path | proposed → doing → done with all subtask roles (orchestrator → implementer → tester → evaluator) |
| Failure + retry | Adapter fails once, retry succeeds, task completes |
| Max retries exhausted | All attempts fail → task moves to `failed/` |
| Orphan detection | Worker dies without writing result.json, tick reaps and triggers retry |

### Setup

- Initialize a real `.mas/` board in a temporary git repo
- Inject a mock adapter (a shell script that writes a canned `result.json`)
- Run `tick` repeatedly until the task reaches `done/` or `failed/`
- Use `subprocess.run` to invoke the CLI directly

### Teardown

- Remove temp repo
- Clean up any git worktrees created during test

### Test Markers

E2E tests are not currently gated behind a marker — they live in `tests/e2e/`
and run as part of the default `pytest` invocation. Run only the E2E layer with
`.venv/bin/pytest tests/e2e/ -q`.

### Test Location

Tests go in `tests/e2e/` (currently `test_lifecycle.py` and
`test_lifecycle_script.py`, with shared fixtures in `tests/e2e/conftest.py`
and helper scripts under `tests/e2e/scripts/`).

---

## 4. Mocking Guidelines

### `unittest.mock` Usage

- **Patching**: Use `@patch` decorator or `with patch():` context manager for:
  - `subprocess.Popen` and `subprocess.run`
  - `shutil.move`, `shutil.rmtree`
  - `Path.mkdir`, `Path.write_text`, `Path.read_text`
  - `os.kill` for PID checking

### FakeAdapter Fixture

For adapter tests, create a reusable `FakeAdapter` that:
- Implements `Adapter` ABC
- Returns canned responses via `build_command()`
- Optionally captures prompt for verification

```python
class FakeAdapter(Adapter):
    name = "fake"
    agentic = True
    
    def __init__(self, provider_cfg, role_cfg):
        super().__init__(provider_cfg, role_cfg)
        self.canned_response = {"status": "success", "summary": "Done"}
    
    def build_command(self, prompt, task_dir, cwd):
        return ["echo", "fake-adapter"]
```

### CLI Tests with Typer

Use `Typer`/`CliRunner` for CLI integration tests:

```python
from typer.testing import CliRunner
runner = CliRunner()

def test_cli_command():
    result = runner.invoke(app, ["command", "--arg", "value"])
    assert result.exit_code == 0
```

### What NOT to Mock

- **Pydantic validation**: Always test with real models to ensure validation logic works
- **Core business logic**: Board moves, task transitions, schema validation

---

## 5. Directory Layout

```
tests/
├── conftest.py            # shared fixtures
├── test_*.py              # unit + some cross-module integration tests
├── adapters/              # provider adapter suites
│   ├── test_claude_code.py
│   ├── test_gemini_cli.py
│   ├── test_health_check.py
│   ├── test_ollama.py
│   ├── test_opencode.py
│   └── test_script_adapter.py
├── integration/           # CLI + multi-module scenarios
│   ├── test_cli.py
│   └── test_cli_cost.py
└── e2e/                   # full lifecycle suites
    ├── conftest.py
    ├── scripts/           # role scripts for the script adapter
    ├── test_lifecycle.py
    └── test_lifecycle_script.py
```

> **Note:** The older "Unit / Integration / E2E" subdirectory split was never
> migrated to. The current convention is the layout above; place new tests
> where they fit best (small isolated logic → top-level `tests/test_*.py`;
> provider-specific → `tests/adapters/`; CLI / cross-module → `tests/integration/`;
> full lifecycle → `tests/e2e/`).

---

## 6. conftest.py Fixtures

### tmp_board

Creates a temporary `.mas/` directory structure.

```python
import pytest
from pathlib import Path
from mas.board import ensure_layout

@pytest.fixture
def tmp_board(tmp_path):
    mas_dir = tmp_path / ".mas"
    ensure_layout(mas_dir)
    return mas_dir
```

### fake_adapter

A mock adapter returning canned responses.

```python
import pytest
from mas.adapters.base import Adapter
from mas.schemas import ProviderConfig, RoleConfig

class FakeAdapter(Adapter):
    name = "fake"
    agentic = True
    
    def __init__(self, provider_cfg=None, role_cfg=None):
        if provider_cfg is None:
            provider_cfg = ProviderConfig(cli="echo", max_concurrent=1)
        if role_cfg is None:
            role_cfg = RoleConfig(provider="fake", model=None, timeout_s=30)
        super().__init__(provider_cfg, role_cfg)
        self.captured_prompts = []
    
    def build_command(self, prompt, task_dir, cwd):
        self.captured_prompts.append(prompt)
        return ["echo", "fake"]

@pytest.fixture
def fake_adapter():
    return FakeAdapter()
```

### git_repo

Initializes a bare git repo for worktree tests.

```python
import pytest
import subprocess
from pathlib import Path

@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True
    )
    # Initial commit
    (repo / "README").write_text("# Test\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    return repo
```

---

## 7. tmp_path vs Pure Mocks

| Test Type | Approach | When to Use |
|-----------|----------|-------------|
| Unit | Pure mocks (`@patch`) | No filesystem needed, testing logic in isolation |
| Integration | `tmp_path` + mocks | Real filesystem operations, but external commands mocked |
| E2E | Real `.mas/` + real git repo | Full system behavior, real CLI execution |

---

## 8. Running Tests

```sh
.venv/bin/pytest -q                                          # all tests
.venv/bin/pytest tests/adapters/ tests/integration/ -q       # adapter + CLI suites
.venv/bin/pytest tests/e2e/ -q                               # E2E only
.venv/bin/pytest tests/test_tick.py -q                       # single file
.venv/bin/pytest tests/test_tick.py::test_name -q            # single test
```