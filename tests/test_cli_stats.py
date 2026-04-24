"""Failing tests for `mas stats` CLI command.

These tests pin the full contract of the new `mas stats` command.
They are designed to fail semantically (AssertionError / NotImplementedError)
against stub implementations, not with harness errors.

Expected top-level keys in --json output:
    board         : {proposed: int, doing: int, done: int, failed: int}
    success_rate  : float 0.0–1.0  (done / (done+failed), or 0 if no terminal tasks)
    revision_rate : float 0.0–1.0  (terminal tasks with cycle > 0 / total terminal)
    roles         : {role: {mean_s: float, p50_s: float, p95_s: float, count: int}}
    providers     : {provider: int}  — from task.inputs["provider"]
    tokens        : {tokens_in: int, tokens_out: int, cost_usd: float}
    env_errors    : int  — tasks with status==environment_error OR .env_retries marker
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.schemas import Plan, Result, SubtaskSpec, Task

runner = CliRunner()

# ---------------------------------------------------------------------------
# Autouse: widen the Rich console so table output is not truncated
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _wide_console():
    from mas import cli
    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tid(slug: str, h: str = "aa00") -> str:
    """Build a valid task ID. h must be exactly 4 chars from [a-f0-9]."""
    return f"20260423-{slug}-{h}"


def _make_task(
    task_id: str,
    role: str = "implementer",
    provider: str = "claude_code",
    created_at: datetime | None = None,
    cycle: int = 0,
) -> Task:
    kw: dict[str, Any] = {
        "id": task_id,
        "role": role,
        "goal": f"goal for {task_id}",
        "inputs": {"provider": provider},
        "cycle": cycle,
    }
    if created_at is not None:
        kw["created_at"] = created_at
    return Task(**kw)


def _make_result(
    task_id: str,
    status: str = "success",
    duration_s: float = 10.0,
    tokens_in: int = 100,
    tokens_out: int = 50,
    cost_usd: float = 0.01,
) -> Result:
    return Result(
        task_id=task_id,
        status=status,
        summary=f"result for {task_id}",
        duration_s=duration_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )


def _write_task(
    mas_dir: Path,
    column: str,
    task: Task,
    result: Result | None = None,
    transition_ts: str | None = None,
) -> Path:
    task_dir = mas_dir / "tasks" / column / task.id
    board.write_task(task_dir, task)
    if result is not None:
        (task_dir / "result.json").write_text(result.model_dump_json())
    if transition_ts is not None:
        # Overwrite the .transitions.log created by write_task with a specific timestamp
        (task_dir / ".transitions.log").write_text(
            f"{transition_ts}|doing|{column}|completed\n"
        )
    return task_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_board(tmp_path):
    """Synthetic board: 2 proposed, 1 doing, 3 done (success), 2 failed."""
    mas_dir = tmp_path / ".mas"
    board.ensure_layout(mas_dir)
    now = datetime.now(timezone.utc)

    # proposed: 2
    for i in range(2):
        t = _make_task(_tid(f"prop{i}", f"a00{i}"), role="proposer", provider="ollama")
        _write_task(mas_dir, "proposed", t)

    # doing: 1
    t = _make_task(_tid("doing0", "b000"), role="orchestrator", provider="claude_code")
    _write_task(mas_dir, "doing", t)

    # done: 3 (success, implementer, claude_code)
    for i in range(3):
        t = _make_task(
            _tid(f"done{i}", f"c00{i}"),
            role="implementer",
            provider="claude_code",
            created_at=now - timedelta(hours=1),
        )
        r = _make_result(
            t.id,
            status="success",
            duration_s=float(10 + i * 5),
            tokens_in=100 * (i + 1),
            tokens_out=50 * (i + 1),
            cost_usd=0.01 * (i + 1),
        )
        _write_task(mas_dir, "done", t, r, (now - timedelta(minutes=30)).isoformat())

    # failed: 2 (failure, evaluator, codex)
    for i in range(2):
        t = _make_task(
            _tid(f"fail{i}", f"d00{i}"),
            role="evaluator",
            provider="codex",
            created_at=now - timedelta(hours=2),
        )
        r = _make_result(t.id, status="failure", duration_s=5.0 + i)
        _write_task(mas_dir, "failed", t, r, (now - timedelta(hours=1)).isoformat())

    return mas_dir


# ---------------------------------------------------------------------------
# 1. Board counts (populated board)
# ---------------------------------------------------------------------------

class TestBoardCounts:
    def test_all_four_columns_in_output(self, populated_board, monkeypatch):
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0
        output = result.output.lower()
        for col in ("proposed", "doing", "done", "failed"):
            assert col in output, f"'{col}' not in stats output"

    def test_board_counts_via_json(self, populated_board, monkeypatch):
        """--json board counts must match fixture: proposed=2 doing=1 done=3 failed=2."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        b = data["board"]
        assert b["proposed"] == 2
        assert b["doing"] == 1
        assert b["done"] == 3
        assert b["failed"] == 2


# ---------------------------------------------------------------------------
# 2. Success rate and revision-cycle rate
# ---------------------------------------------------------------------------

class TestRates:
    def test_success_rate_60_percent(self, populated_board, monkeypatch):
        """3 done / (3 done + 2 failed) = 0.6."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert abs(data["success_rate"] - 0.6) < 0.01, (
            f"Expected success_rate ~0.6, got {data.get('success_rate')}"
        )

    def test_revision_rate_nonzero_when_cycle_present(self, tmp_path, monkeypatch):
        """A task with cycle=1 contributes to a non-zero revision_rate."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        # 2 done tasks: one with cycle=0, one with cycle=1
        for cycle, h in [(0, "ee00"), (1, "ee01")]:
            tid = _tid(f"rev{cycle}", h)
            t = _make_task(tid, role="implementer", provider="claude_code", cycle=cycle)
            r = _make_result(tid, status="success")
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # 1 out of 2 terminal tasks had a revision cycle → 0.5
        assert data["revision_rate"] > 0, (
            f"Expected revision_rate > 0, got {data.get('revision_rate')}"
        )

    def test_success_rate_all_success(self, tmp_path, monkeypatch):
        """All-success board gives success_rate = 1.0."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        for i in range(3):
            tid = _tid(f"succ{i}", f"f00{i}")
            t = _make_task(tid, role="implementer", provider="claude_code")
            r = _make_result(tid, status="success")
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert abs(data["success_rate"] - 1.0) < 0.001


# ---------------------------------------------------------------------------
# 3. Per-role duration stats
# ---------------------------------------------------------------------------

class TestPerRoleDuration:
    def test_duration_stats_for_implementer(self, tmp_path, monkeypatch):
        """Duration stats appear for implementer role with known values."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        # Three tasks with durations 10, 20, 30 → mean=20, p50=20
        for i, dur in enumerate([10.0, 20.0, 30.0]):
            tid = _tid(f"idur{i}", f"0a0{i}")
            t = _make_task(tid, role="implementer", provider="claude_code")
            r = _make_result(tid, status="success", duration_s=dur)
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "roles" in data, f"'roles' key missing from --json output: {list(data.keys())}"
        roles = data["roles"]
        assert "implementer" in roles, f"'implementer' missing from roles: {list(roles.keys())}"

        impl = roles["implementer"]
        assert "mean_s" in impl, f"'mean_s' missing from implementer stats: {list(impl.keys())}"
        assert "p50_s" in impl
        assert "p95_s" in impl
        assert "count" in impl

        assert impl["count"] == 3
        assert abs(impl["mean_s"] - 20.0) < 0.1, f"Expected mean ~20, got {impl['mean_s']}"

    def test_p95_present_in_json(self, tmp_path, monkeypatch):
        """p95 duration key is present in --json roles output."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        for i in range(5):
            tid = _tid(f"p95t{i}", f"0b0{i}")
            t = _make_task(tid, role="tester", provider="claude_code")
            r = _make_result(tid, status="success", duration_s=float(i * 10 + 10))
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        tester_stats = data.get("roles", {}).get("tester", {})
        assert "p95_s" in tester_stats, f"'p95_s' missing from tester stats: {tester_stats}"

    def test_duration_stats_in_text_output(self, tmp_path, monkeypatch):
        """Text (non-JSON) output references role names and numeric durations."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        tid = _tid("textdur0", "0c00")
        t = _make_task(tid, role="implementer", provider="claude_code")
        r = _make_result(tid, status="success", duration_s=42.0)
        _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0
        output = result.output.lower()
        assert "implementer" in output


# ---------------------------------------------------------------------------
# 4. Per-provider task counts
# ---------------------------------------------------------------------------

class TestPerProviderCounts:
    def test_provider_counts_in_json(self, tmp_path, monkeypatch):
        """3 claude_code + 2 codex tasks → providers.claude_code=3, providers.codex=2."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        for i in range(3):
            tid = _tid(f"cctask{i}", f"0d0{i}")
            t = _make_task(tid, role="implementer", provider="claude_code")
            r = _make_result(tid, status="success")
            _write_task(mas_dir, "done", t, r)

        for i in range(2):
            tid = _tid(f"cxtask{i}", f"0e0{i}")
            t = _make_task(tid, role="evaluator", provider="codex")
            r = _make_result(tid, status="success")
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "providers" in data, f"'providers' key missing: {list(data.keys())}"
        providers = data["providers"]
        assert providers.get("claude_code") == 3, (
            f"Expected claude_code=3, got {providers}"
        )
        assert providers.get("codex") == 2, (
            f"Expected codex=2, got {providers}"
        )


# ---------------------------------------------------------------------------
# 5. Aggregate tokens / cost
# ---------------------------------------------------------------------------

class TestAggregateTokens:
    def test_aggregate_tokens_and_cost(self, tmp_path, monkeypatch):
        """tokens_in / tokens_out / cost_usd are summed across all tasks."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        # 3 tasks: (100,50,0.01), (200,100,0.02), (300,150,0.03) → sums 600,300,0.06
        for i in range(3):
            tid = _tid(f"tktask{i}", f"0f0{i}")
            t = _make_task(tid, role="implementer", provider="claude_code")
            tin, tout, cost = 100 * (i + 1), 50 * (i + 1), 0.01 * (i + 1)
            r = _make_result(tid, status="success", tokens_in=tin, tokens_out=tout, cost_usd=cost)
            _write_task(mas_dir, "done", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        assert "tokens" in data, f"'tokens' key missing: {list(data.keys())}"
        tokens = data["tokens"]
        assert tokens.get("tokens_in") == 600, f"Expected tokens_in=600, got {tokens}"
        assert tokens.get("tokens_out") == 300, f"Expected tokens_out=300, got {tokens}"
        assert abs(tokens.get("cost_usd", 0) - 0.06) < 0.001, (
            f"Expected cost_usd≈0.06, got {tokens}"
        )


# ---------------------------------------------------------------------------
# 6. Environment error incidence
# ---------------------------------------------------------------------------

class TestEnvErrorIncidence:
    def test_env_error_status_counted(self, tmp_path, monkeypatch):
        """Tasks with status=environment_error increment env_errors count."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        tid = _tid("enverr0", "1a00")
        t = _make_task(tid, role="implementer", provider="claude_code")
        r = _make_result(tid, status="environment_error")
        _write_task(mas_dir, "failed", t, r)

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "env_errors" in data, f"'env_errors' key missing: {list(data.keys())}"
        assert data["env_errors"] >= 1, f"Expected env_errors ≥ 1, got {data['env_errors']}"

    def test_env_retries_marker_counted(self, tmp_path, monkeypatch):
        """Tasks with a .env_retries marker file are counted as env-error incidences."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        tid = _tid("envmark0", "1b00")
        t = _make_task(tid, role="implementer", provider="claude_code")
        task_dir = _write_task(mas_dir, "failed", t)
        (task_dir / ".env_retries").write_text("2")

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data.get("env_errors", 0) >= 1, (
            f"Expected env_errors ≥ 1 for .env_retries marker task, got {data.get('env_errors')}"
        )

    def test_env_result_file_counted(self, tmp_path, monkeypatch):
        """Tasks with a result.env-*.json file are counted as env-error incidences."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        tid = _tid("envfile0", "1c00")
        t = _make_task(tid, role="implementer", provider="claude_code")
        task_dir = _write_task(mas_dir, "failed", t)
        # Write a renamed env-error result (as tick.py does)
        env_result = _make_result(tid, status="environment_error")
        (task_dir / "result.env-1.json").write_text(env_result.model_dump_json())

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data.get("env_errors", 0) >= 1, (
            f"Expected env_errors ≥ 1 for result.env-1.json task, got {data.get('env_errors')}"
        )


# ---------------------------------------------------------------------------
# 7. Empty board
# ---------------------------------------------------------------------------

class TestEmptyBoard:
    def test_empty_board_exits_zero(self, tmp_path, monkeypatch):
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0

    def test_empty_board_json_has_zero_counts(self, tmp_path, monkeypatch):
        """Empty board --json has board counts all zero and success_rate = 0."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        b = data["board"]
        assert b["proposed"] == 0
        assert b["doing"] == 0
        assert b["done"] == 0
        assert b["failed"] == 0
        assert data["success_rate"] == 0.0
        assert data["env_errors"] == 0


# ---------------------------------------------------------------------------
# 8. --since filter (h / d / w suffixes)
# ---------------------------------------------------------------------------

def _build_since_board(mas_dir: Path, now: datetime) -> dict[str, datetime]:
    """
    Create 3 done tasks with transitions at different times:
      'recent'  — 30 min ago  (within 1h, within 1d, within 1w)
      'old'     — 2 hours ago (outside 1h, within 1d, within 1w)
      'ancient' — 3 days ago  (outside 1h, outside 1d, within 1w)
    Returns {task_id: transition_ts}.
    """
    timestamps: dict[str, datetime] = {
        _tid("recent",  "2a00"): now - timedelta(minutes=30),
        _tid("old",     "2b00"): now - timedelta(hours=2),
        _tid("ancient", "2c00"): now - timedelta(days=3),
    }
    for tid, ts in timestamps.items():
        t = _make_task(tid, role="implementer", provider="claude_code", created_at=ts)
        r = _make_result(tid, status="success")
        _write_task(mas_dir, "done", t, r, ts.isoformat())
    return timestamps


class TestSinceFilter:
    def test_since_1h_includes_only_recent(self, tmp_path, monkeypatch):
        """--since 1h filters done count to 1 (only 30m-ago task)."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        _build_since_board(mas_dir, datetime.now(timezone.utc))

        result = runner.invoke(app, ["stats", "--since", "1h", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["board"]["done"] == 1, (
            f"--since 1h: expected done=1, got {data['board']['done']}"
        )

    def test_since_1d_includes_recent_and_old(self, tmp_path, monkeypatch):
        """--since 1d filters done count to 2 (30m and 2h tasks)."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        _build_since_board(mas_dir, datetime.now(timezone.utc))

        result = runner.invoke(app, ["stats", "--since", "1d", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["board"]["done"] == 2, (
            f"--since 1d: expected done=2, got {data['board']['done']}"
        )

    def test_since_1w_includes_all(self, tmp_path, monkeypatch):
        """--since 1w includes all 3 done tasks."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        _build_since_board(mas_dir, datetime.now(timezone.utc))

        result = runner.invoke(app, ["stats", "--since", "1w", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["board"]["done"] == 3, (
            f"--since 1w: expected done=3, got {data['board']['done']}"
        )

    def test_since_boundary_inclusive(self, tmp_path, monkeypatch):
        """Task whose most recent transition is exactly at the --since boundary is INCLUDED."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        now = datetime.now(timezone.utc)
        # Place the transition exactly 1 hour ago
        boundary_ts = now - timedelta(hours=1)
        tid = _tid("boundary0", "2d00")
        t = _make_task(tid, role="implementer", provider="claude_code", created_at=boundary_ts)
        r = _make_result(tid, status="success")
        _write_task(mas_dir, "done", t, r, boundary_ts.isoformat())

        result = runner.invoke(app, ["stats", "--since", "1h", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Task at exactly the boundary should be included (>= not >)
        assert data["board"]["done"] == 1, (
            f"Boundary task should be included in --since 1h; got done={data['board']['done']}"
        )


# ---------------------------------------------------------------------------
# 9. Invalid --since format
# ---------------------------------------------------------------------------

class TestInvalidSince:
    def test_invalid_since_exits_2(self, tmp_path, monkeypatch):
        """'mas stats --since 7x' exits with code 2 (bad parameter)."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stats", "--since", "7x"])
        assert result.exit_code == 2, (
            f"Expected exit_code=2 for invalid --since, got {result.exit_code}"
        )

    def test_invalid_since_error_on_stderr(self, tmp_path, monkeypatch):
        """'mas stats --since 7x' prints a clear error mentioning --since or 'invalid'."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["stats", "--since", "7x"])
        error_text = (result.stderr or result.output or "").lower()
        assert any(kw in error_text for kw in ("since", "invalid", "error", "format")), (
            f"Expected error message about '--since', got: {error_text!r}"
        )


# ---------------------------------------------------------------------------
# 10. --json output contract
# ---------------------------------------------------------------------------

EXPECTED_JSON_KEYS = frozenset({
    "board",
    "success_rate",
    "revision_rate",
    "roles",
    "providers",
    "tokens",
    "env_errors",
})


class TestJsonOutput:
    def test_json_is_parseable(self, populated_board, monkeypatch):
        """--json emits a single parseable JSON document."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0, f"Unexpected exit: {result.exit_code}\n{result.output}"
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Output is not valid JSON: {exc}\nOutput: {result.output!r}")
        assert isinstance(data, dict)

    def test_json_has_all_required_keys(self, populated_board, monkeypatch):
        """--json output has every key from EXPECTED_JSON_KEYS."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        missing = EXPECTED_JSON_KEYS - set(data.keys())
        assert not missing, f"Missing keys in --json output: {missing}"

    def test_json_no_rich_markup(self, populated_board, monkeypatch):
        """--json output contains no Rich markup sequences."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        output = result.output
        assert "[bold]" not in output
        assert "[/]" not in output
        assert "[green]" not in output
        assert "[dim]" not in output

    def test_json_roles_structure(self, populated_board, monkeypatch):
        """--json roles section has the right per-role shape."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        roles = data.get("roles", {})
        for role_name, role_data in roles.items():
            for key in ("mean_s", "p50_s", "p95_s", "count"):
                assert key in role_data, (
                    f"Role '{role_name}' missing '{key}'; got: {list(role_data.keys())}"
                )

    def test_json_tokens_structure(self, populated_board, monkeypatch):
        """--json tokens section has tokens_in, tokens_out, cost_usd."""
        monkeypatch.chdir(populated_board.parent)
        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        tokens = data.get("tokens", {})
        for key in ("tokens_in", "tokens_out", "cost_usd"):
            assert key in tokens, f"'tokens.{key}' missing; got: {list(tokens.keys())}"


# ---------------------------------------------------------------------------
# 11. Malformed files are skipped gracefully
# ---------------------------------------------------------------------------

class TestMalformedFiles:
    def test_malformed_result_json_does_not_crash(self, tmp_path, monkeypatch):
        """Malformed result.json is skipped and stats exits 0."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        # Good task
        tid_good = _tid("good0", "3a00")
        t_good = _make_task(tid_good, role="implementer", provider="claude_code")
        r_good = _make_result(tid_good, status="success")
        _write_task(mas_dir, "done", t_good, r_good)

        # Task with malformed result.json
        tid_bad = _tid("badres0", "3b00")
        t_bad = _make_task(tid_bad, role="implementer", provider="claude_code")
        task_dir = mas_dir / "tasks" / "done" / tid_bad
        board.write_task(task_dir, t_bad)
        (task_dir / "result.json").write_text("{this is not valid JSON!}")

        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, (
            f"stats should not crash on malformed result.json; got exit={result.exit_code}"
        )

    def test_malformed_transitions_log_does_not_crash(self, tmp_path, monkeypatch):
        """Malformed .transitions.log lines are skipped and stats exits 0."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        tid = _tid("badtrans0", "3c00")
        t = _make_task(tid, role="implementer", provider="claude_code")
        r = _make_result(tid, status="success")
        task_dir = _write_task(mas_dir, "done", t, r)
        # Overwrite with garbage
        (task_dir / ".transitions.log").write_text("garbage\n{invalid json}\nmore garbage\n")

        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, (
            f"stats should not crash on malformed .transitions.log; got exit={result.exit_code}"
        )

    def test_malformed_result_json_good_task_still_counted(self, tmp_path, monkeypatch):
        """A malformed result.json in one task does not prevent counting other tasks."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        # 2 good tasks
        for i in range(2):
            tid = _tid(f"goodtask{i}", f"3d0{i}")
            t = _make_task(tid, role="implementer", provider="claude_code")
            r = _make_result(tid, status="success")
            _write_task(mas_dir, "done", t, r)

        # 1 bad result.json task (still in done/)
        tid_bad = _tid("badres1", "3e00")
        t_bad = _make_task(tid_bad, role="implementer", provider="claude_code")
        bad_dir = mas_dir / "tasks" / "done" / tid_bad
        board.write_task(bad_dir, t_bad)
        (bad_dir / "result.json").write_text("NOT JSON")

        result = runner.invoke(app, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # done/ has 3 directories even though 1 has a bad result
        assert data["board"]["done"] == 3, (
            f"Expected done=3 (including bad-result task), got {data['board']['done']}"
        )


# ---------------------------------------------------------------------------
# 12. Performance: 100 tasks under 1 second
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestPerformance:
    def test_100_terminal_tasks_under_1s(self, tmp_path, monkeypatch):
        """Synthetic board of 100 terminal tasks must complete stats in < 1.0s wall time."""
        mas_dir = tmp_path / ".mas"
        board.ensure_layout(mas_dir)
        monkeypatch.chdir(tmp_path)

        now = datetime.now(timezone.utc)
        for i in range(100):
            # hash4: format i as 4 hex chars, always [a-f0-9]{4}
            h4 = format(i, "04x")
            tid = f"20260423-perf{i:04d}-{h4}"
            t = Task(
                id=tid,
                role="implementer",
                goal=f"perf task {i}",
                inputs={"provider": "claude_code"},
                created_at=now - timedelta(seconds=i),
            )
            col = "done" if (i % 4 != 0) else "failed"
            task_dir = mas_dir / "tasks" / col / tid
            board.write_task(task_dir, t)
            r = Result(
                task_id=tid,
                status="success" if col == "done" else "failure",
                summary=f"perf {i}",
                duration_s=float(i % 60 + 1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.001,
            )
            (task_dir / "result.json").write_text(r.model_dump_json())

        start = time.perf_counter()
        result = runner.invoke(app, ["stats"])
        elapsed = time.perf_counter() - start

        assert result.exit_code == 0, (
            f"stats failed on 100-task board: exit={result.exit_code}\n{result.output}"
        )
        assert elapsed < 1.0, (
            f"stats took {elapsed:.3f}s for 100 tasks (cap: 1.0s)"
        )
