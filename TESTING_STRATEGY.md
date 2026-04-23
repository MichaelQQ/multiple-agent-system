# Testing Strategy

This project uses three test layers: **Unit Tests**, **Integration Tests**, and **E2E Tests**. The goal is to keep board/state logic easy to refactor, keep adapter and CLI wiring honest, and preserve one realistic lifecycle suite that exercises the full task pipeline.

## Unit Tests

**Purpose**: validate local behavior with minimal dependencies and fast feedback.

**Primary targets**:
- `board.py`: task reads, moves, task lookup, PID bookkeeping, result handling
- `schemas.py`: Pydantic validation and JSON contracts
- `ids.py`: task id generation and parsing rules
- `config.py`: config loading, merging, and validation
- `worktree.py`: branch/worktree helpers and pruning behavior
- focused adapter helpers where the behavior is local and deterministic

**Current location**:
- Most unit tests live at `tests/test_*.py`
- Representative files: `tests/test_board.py`, `tests/test_ids.py`, `tests/test_schemas.py`, `tests/test_config.py`, `tests/test_worktree.py`

Use `tmp_path`, `monkeypatch`, and direct function calls by default. Do not mock Pydantic validation itself; exercise the real models.

## Integration Tests

**Purpose**: verify interaction between components with realistic filesystem state and mocked external CLIs/services.

**Primary targets**:
- `tick.py`: dispatch, retries, revision cycles, parent finalization
- `adapters/`: provider command construction and subprocess protocol behavior
- CLI commands in `cli.py`
- config and board interactions across module boundaries

**Current location**:
- `tests/integration/`
- adapter-specific suites in `tests/adapters/`
- some cross-module cases still live in top-level files such as `tests/test_tick.py`, `tests/test_cli.py`, `tests/test_orphan.py`, and `tests/test_retry_marker.py`

Representative files:
- `tests/adapters/test_ollama.py`
- `tests/adapters/test_script_adapter.py`
- `tests/integration/test_cli.py`
- `tests/test_tick.py`

Mock subprocesses and external APIs at the boundary. For Ollama or other external API behavior, patch the HTTP/client call sites and assert the translated MAS result shape, not just raw transport details.

## E2E Tests

**Purpose**: exercise the full MAS lifecycle on a real temp repo with a real `.mas/` layout.

**Current location**:
- `tests/e2e/test_lifecycle.py`
- `tests/e2e/test_lifecycle_script.py`

These suites cover:
- proposal to done flow
- failure and retry behavior
- evaluator-driven revision cycles
- worktree creation and pruning
- script-provider subprocess execution

E2E tests should use real board directories and real tick progression. External agent CLIs are still replaced with deterministic scripts or mocks so the tests stay repeatable.

## Mocking Guidance

- Use `tmp_path` for filesystem-heavy tests instead of mocking every `Path` operation.
- Use `monkeypatch` or `unittest.mock.patch` for subprocess spawning, PID checks, and provider-specific boundary calls.
- Mock external APIs such as Ollama at the transport boundary and keep MAS schema validation real.
- Prefer fake adapters or script adapters when you need to simulate end-to-end worker behavior.

## Layout Conventions

The current tree is intentionally mixed:
- `tests/test_*.py` holds most unit tests and some older integration-style tests
- `tests/adapters/` holds provider adapter coverage
- `tests/integration/` holds broader CLI and cross-component scenarios
- `tests/e2e/` holds lifecycle tests

New tests do not need a large directory migration. Place them where they fit best:
- small isolated module behavior: top-level `tests/test_*.py`
- provider-specific behavior: `tests/adapters/`
- multi-module or CLI wiring: `tests/integration/`
- full lifecycle flows: `tests/e2e/`

## Running Tests

```sh
.venv/bin/pytest -q
.venv/bin/pytest tests/adapters/ tests/integration/ -q
.venv/bin/pytest tests/e2e/ -q
```
