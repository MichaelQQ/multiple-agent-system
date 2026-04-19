# Testing Strategy

This document defines the testing approach for the MAS project, including test layers, component mapping, mocking guidance, and organization conventions.

## Test Layers

### Unit Tests

**Purpose**: Test individual components in isolation with no external dependencies.

**Scope**:
- Pure functions and data transformations
- Individual classes and their methods
- Schema validation
- Configuration parsing
- State transitions within a single component

**Examples**:
- `board.py` - Board state management, task operations
- `schemas.py` - Data validation, type conversions
- `ids.py` - ID generation and parsing

### Integration Tests

**Purpose**: Test interactions between components and with external services.

**Scope**:
- Adapter dispatch and protocol handling
- State machine transitions across components
- External API communication (Ollama, Gemini, Claude Code)
- Process spawning and lifecycle management

**Examples**:
- `tick.py` - State machine coordination
- `adapters/` - External service integration
- `test_ollama.py` - Ollama API integration

### E2E Tests

**Purpose**: Test complete workflows from start to finish.

**Scope**:
- Full tick loop execution
- Worktree creation and management
- Multi-agent coordination
- End-to-end user scenarios

## Component Layer Mapping

| Component | Layer | Rationale |
|-----------|-------|----------|
| `board.py` | Unit | Board state is internal data structure |
| `schemas.py` | Unit | Pure data validation |
| `tick.py` (state machine) | Integration | Coordinates between components |
| `adapters/` | Integration | External service communication |
| Full tick loop | E2E | Complete workflow |
| Worktree creation | E2E | Filesystem and process integration |

## Mocking Guidance

### Subprocess Calls

Use `unittest.mock` or `monkeypatch` to mock subprocess calls in adapters:

```python
from unittest.mock import patch, MagicMock

@patch('subprocess.run')
def test_adapter(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    # test adapter code
```

### Filesystem Operations

Use `tmp_path` fixture for board/worktree filesystem operations:

```python
def test_worktree_creation(tmp_path):
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    # test filesystem operations
```

### External API Responses

Mock Ollama HTTP responses with a fixture or `responses` library:

```python
import responses

@responses.activate
def test_ollama():
    responses.add(responses.POST, "http://localhost:11434/api/generate",
                 json={"response": "test"}, status=200)
    # test Ollama client
```

### Process Checks

Mock PID-based process checks for daemon testing:

```python
@patch('os.kill')
def test_process_check(mock_kill):
    mock_kill.side_effect = OSError("No such process")
    # test process checks
```

## Existing Test Mapping

| Test File | Current Layer | Recommended Layer |
|----------|--------------|-------------------|
| `test_board.py` | Unit | Unit |
| `test_schemas.py` | Unit | Unit |
| `test_ids.py` | Unit | Unit |
| `test_ollama.py` | Integration | Integration |
| `test_orphan.py` | Integration | Integration |
| `test_retry_marker.py` | Integration | Integration |

## Test Organization Conventions

### Directory Structure

```
tests/
├── unit/           # Unit tests
├── integration/    # Integration tests
└── e2e/           # E2E tests
```

### Naming Conventions

- Unit tests: `test_<module>.py` (e.g., `test_board.py`)
- Integration tests: `test_<feature>_integration.py`
- E2E tests: `test_<workflow>_e2e.py`

### Markers

Use pytest markers to categorize tests:

```python
@pytest.mark.unit
def test_board_state():
    pass

@pytest.mark.integration
def test_adapter_dispatch():
    pass

@pytest.mark.e2e
def test_full_tick_loop():
    pass
```

### Running Tests by Layer

```bash
# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration

# Run only E2E tests
pytest -m e2e
```