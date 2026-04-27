"""Failing tests for rejected-proposal logging and `mas proposals rejected` CLI.

All tests are designed to fail semantically (AssertionError / NotImplementedError)
against the current code and stubs, passing only once the real implementation is
in place.

Coverage:
  (a) _materialize_proposal writes rejected.jsonl on similarity hit
  (b) No rejected.jsonl created when no duplicate found
  (c) Write failures logged at WARNING, not propagated
  (d) `mas proposals rejected` Rich-table / JSON / --since / --limit output
  (e) Missing rejected.jsonl → empty output, exit 0, file not created
  (f) Malformed line skipped with exactly one WARNING per run
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from rich.console import Console
from typer.testing import CliRunner

from mas import board
from mas.cli import app
from mas.proposals import RejectedProposal
from mas.schemas import MasConfig, ProviderConfig, Result, RoleConfig, Task
from mas.tick import TickEnv, _materialize_proposal

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wide_console():
    from mas import cli
    original = cli.console
    cli.console = Console(width=200)
    yield
    cli.console = original


def _cfg(
    max_proposed: int = 10,
    proposal_similarity_threshold: float = 0.7,
) -> MasConfig:
    return MasConfig(
        providers={"mock": ProviderConfig(cli="sh", max_concurrent=2, extra_args=[])},
        roles={
            "proposer": RoleConfig(provider="mock"),
            "orchestrator": RoleConfig(provider="mock"),
            "implementer": RoleConfig(provider="mock"),
            "tester": RoleConfig(provider="mock"),
            "evaluator": RoleConfig(provider="mock"),
        },
        max_proposed=max_proposed,
        proposal_similarity_threshold=proposal_similarity_threshold,
    )


def _seed_existing_task(
    mas: Path,
    column: str = "proposed",
    task_id: str = "20260101-existing-abcd",
    goal: str = "Create an MCP tool that returns budget utilization metrics",
) -> Path:
    d = mas / "tasks" / column / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps({
        "id": task_id,
        "role": "orchestrator",
        "goal": goal,
    }))
    return d


def _similar_goal() -> str:
    return "Create an MCP tool that returns conversion tracking metrics"


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# (a) Rejected proposal schema and write on duplicate
# ---------------------------------------------------------------------------

class TestRejectedProposalSchema:
    def test_extra_forbid_raises_validation_error(self):
        """RejectedProposal must use extra='forbid' so unknown fields are rejected."""
        with pytest.raises((ValidationError, Exception)):
            RejectedProposal(
                timestamp=_now_z(),
                summary="test",
                goal="some goal",
                similarity_score=0.8,
                matched_task_id="20260101-existing-abcd",
                matched_column="proposed",
                threshold=0.7,
                unexpected_field="should_fail",
            )

    def test_valid_record_round_trips(self):
        """A well-formed RejectedProposal must serialise and deserialise."""
        rec = RejectedProposal(
            timestamp=_now_z(),
            summary="dup summary",
            goal="Create an MCP tool that returns conversion metrics",
            similarity_score=0.75,
            matched_task_id="20260101-existing-abcd",
            matched_column="proposed",
            threshold=0.7,
        )
        reloaded = RejectedProposal.model_validate(json.loads(rec.model_dump_json()))
        assert reloaded.similarity_score == 0.75
        assert reloaded.matched_column == "proposed"

    def test_timestamp_must_end_with_z(self):
        """Timestamps must end in 'Z' per the schema contract."""
        rec = RejectedProposal(
            timestamp="2026-04-27T12:00:00+00:00",  # not Z-terminated
            summary="s",
            goal="g",
            similarity_score=0.8,
            matched_task_id="20260101-existing-abcd",
            matched_column="proposed",
            threshold=0.7,
        )
        assert rec.timestamp.endswith("Z"), (
            "RejectedProposal.timestamp must end with 'Z' (UTC suffix)"
        )

    def test_matched_column_must_be_valid(self):
        """matched_column must be one of {proposed, doing, done, failed}."""
        valid = {"proposed", "doing", "done", "failed"}
        rec = RejectedProposal(
            timestamp=_now_z(),
            summary="s",
            goal="g",
            similarity_score=0.8,
            matched_task_id="20260101-existing-abcd",
            matched_column="nonexistent_column",
            threshold=0.7,
        )
        assert rec.matched_column in valid, (
            f"matched_column {rec.matched_column!r} is not in {valid}"
        )


class TestMaterializeProposalWritesRejected:
    def test_rejected_jsonl_created_on_duplicate(self, tmp_path):
        """When similarity check hits, rejected.jsonl is written inside .mas/proposals/."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        result = Result(
            task_id="prop-dup-a",
            status="success",
            summary="duplicate proposal",
            handoff={"goal": _similar_goal()},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.5))
        _materialize_proposal(env, result)

        rejected_path = mas / "proposals" / "rejected.jsonl"
        assert rejected_path.exists(), (
            "Expected .mas/proposals/rejected.jsonl to be created when a proposal is dropped"
        )

    def test_rejected_record_schema(self, tmp_path):
        """Each appended record must match the documented schema."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        existing_id = "20260101-existing-abcd"
        _seed_existing_task(mas, task_id=existing_id)

        result = Result(
            task_id="prop-dup-b",
            status="success",
            summary="dup for schema test",
            handoff={"goal": _similar_goal()},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.5))
        _materialize_proposal(env, result)

        rejected_path = mas / "proposals" / "rejected.jsonl"
        lines = [l for l in rejected_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

        rec = json.loads(lines[0])

        # timestamp must end with Z
        assert isinstance(rec.get("timestamp"), str)
        assert rec["timestamp"].endswith("Z"), "timestamp must end with 'Z'"

        # summary field
        assert "summary" in rec

        # goal truncated to <=500 chars
        assert "goal" in rec
        assert len(rec["goal"]) <= 500

        # similarity_score must be a float
        assert isinstance(rec.get("similarity_score"), float)

        # matched_task_id
        assert "matched_task_id" in rec
        assert rec["matched_task_id"] == existing_id

        # matched_column in allowed set
        assert rec.get("matched_column") in {"proposed", "doing", "done", "failed"}

        # threshold must be a float
        assert isinstance(rec.get("threshold"), float)

    def test_long_goal_truncated_with_ellipsis(self, tmp_path):
        """Goals longer than 500 chars must be truncated and end with '...'."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        # Seed a very long similar goal
        long_seed = "Create an MCP tool that " + "x " * 250
        _seed_existing_task(mas, goal=long_seed)

        long_goal = "Create an MCP tool that " + "y " * 250
        result = Result(
            task_id="prop-dup-c",
            status="success",
            summary="long goal test",
            handoff={"goal": long_goal},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.4))
        _materialize_proposal(env, result)

        rejected_path = mas / "proposals" / "rejected.jsonl"
        assert rejected_path.exists()
        rec = json.loads(rejected_path.read_text().strip())
        assert len(rec["goal"]) <= 500
        if len(long_goal) > 500:
            assert rec["goal"].endswith("..."), (
                "goal exceeding 500 chars must end with '...'"
            )

    def test_records_appended_across_multiple_calls(self, tmp_path):
        """Each duplicate drop must append a new line; existing lines must not be overwritten."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.5))

        for i in range(3):
            result = Result(
                task_id=f"prop-dup-append-{i}",
                status="success",
                summary=f"dup {i}",
                handoff={"goal": _similar_goal()},
            )
            _materialize_proposal(env, result)

        rejected_path = mas / "proposals" / "rejected.jsonl"
        lines = [l for l in rejected_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 rejection records, got {len(lines)}"


# ---------------------------------------------------------------------------
# (b) No rejected file when no duplicate found
# ---------------------------------------------------------------------------

class TestNoRejectedFileOnDistinctGoal:
    def test_no_rejected_jsonl_for_unique_goal(self, tmp_path):
        """A distinct goal must not create rejected.jsonl."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        result = Result(
            task_id="prop-unique-1",
            status="success",
            summary="unique proposal",
            handoff={"goal": "Refactor worktree pruning to handle detached HEAD scenarios"},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=10))
        _materialize_proposal(env, result)

        rejected_path = mas / "proposals" / "rejected.jsonl"
        assert not rejected_path.exists(), (
            "rejected.jsonl must NOT be created when no similarity hit occurs"
        )

    def test_distinct_goal_still_materializes_task(self, tmp_path):
        """A distinct goal must still create a new proposed/ task card."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        result = Result(
            task_id="prop-unique-2",
            status="success",
            summary="unique proposal",
            handoff={"goal": "Refactor worktree pruning to handle detached HEAD scenarios"},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(max_proposed=10))
        _materialize_proposal(env, result)

        # Two tasks in proposed/: the seed + new one
        proposed_count = len(list((mas / "tasks" / "proposed").iterdir()))
        assert proposed_count == 2, f"Expected 2 proposed tasks, got {proposed_count}"


# ---------------------------------------------------------------------------
# (c) Write failures caught, logged at WARNING, do not propagate
# ---------------------------------------------------------------------------

class TestWriteFailureCaughtAndLogged:
    def test_unwritable_dir_logs_warning_not_propagated(self, tmp_path, caplog):
        """An unwritable proposals dir triggers a WARNING log; _materialize_proposal must not raise."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        result = Result(
            task_id="prop-writefail",
            status="success",
            summary="write fail test",
            handoff={"goal": _similar_goal()},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.5))

        proposals_dir = mas / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        proposals_dir.chmod(0o444)  # read-only → write will fail

        try:
            with caplog.at_level(logging.WARNING, logger="mas"):
                _materialize_proposal(env, result)  # must not raise
        finally:
            proposals_dir.chmod(0o755)

        mas_warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name.startswith("mas")]
        assert mas_warnings, (
            "Expected at least one WARNING from 'mas' logger when rejected.jsonl write fails"
        )

    def test_warning_exactly_not_error_or_critical(self, tmp_path, caplog):
        """Write failure must produce WARNING, not ERROR or CRITICAL."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        _seed_existing_task(mas)

        result = Result(
            task_id="prop-writefail2",
            status="success",
            summary="write fail test2",
            handoff={"goal": _similar_goal()},
        )
        env = TickEnv(repo=tmp_path, mas=mas, cfg=_cfg(proposal_similarity_threshold=0.5))

        proposals_dir = mas / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        proposals_dir.chmod(0o444)

        try:
            with caplog.at_level(logging.DEBUG, logger="mas"):
                _materialize_proposal(env, result)
        finally:
            proposals_dir.chmod(0o755)

        severe = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("mas")]
        assert not severe, f"Write failure must not produce ERROR/CRITICAL; got: {severe}"


# ---------------------------------------------------------------------------
# (d) CLI: `mas proposals rejected` command
# ---------------------------------------------------------------------------

SAMPLE_RECORDS = [
    {
        "timestamp": "2026-04-27T10:00:00Z",
        "summary": "oldest dup",
        "goal": "Create an MCP tool for budget metrics",
        "similarity_score": 0.82,
        "matched_task_id": "20260101-budget-aa01",
        "matched_column": "proposed",
        "threshold": 0.7,
    },
    {
        "timestamp": "2026-04-27T11:00:00Z",
        "summary": "middle dup",
        "goal": "Add a CLI command to display board health",
        "similarity_score": 0.78,
        "matched_task_id": "20260102-health-bb02",
        "matched_column": "doing",
        "threshold": 0.7,
    },
    {
        "timestamp": "2026-04-27T12:00:00Z",
        "summary": "newest dup",
        "goal": "Expose webhook retry stats in the web UI",
        "similarity_score": 0.91,
        "matched_task_id": "20260103-webhooks-cc03",
        "matched_column": "done",
        "threshold": 0.7,
    },
]


@pytest.fixture
def rejected_board(tmp_path):
    """Board with .mas/proposals/rejected.jsonl seeded with SAMPLE_RECORDS."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    proposals_dir = mas / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / "rejected.jsonl").write_text(
        "\n".join(json.dumps(r) for r in SAMPLE_RECORDS) + "\n"
    )
    return tmp_path  # monkeypatch.chdir target


class TestProposalsRejectedCli:
    def test_command_exits_zero(self, rejected_board, monkeypatch):
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, f"Non-zero exit: {result.output}\n{result.exception}"

    def test_table_shows_required_columns(self, rejected_board, monkeypatch):
        """Default table output must include: timestamp, summary, score, matched_task_id, matched_column."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, result.output
        out = result.output
        for col in ("timestamp", "summary", "score", "matched_task_id", "matched_column"):
            assert col in out.lower(), f"Expected column '{col}' in table output, got:\n{out}"

    def test_table_shows_score_with_3_decimals(self, rejected_board, monkeypatch):
        """Score column must display 3 decimal places (e.g. 0.820)."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, result.output
        # One of our records has score 0.82 → should render as "0.820"
        assert "0.820" in result.output or "0.780" in result.output or "0.910" in result.output, (
            f"Expected 3-decimal score in output, got:\n{result.output}"
        )

    def test_table_newest_first(self, rejected_board, monkeypatch):
        """Default (no --limit) output must be newest-first."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, result.output
        out = result.output
        pos_newest = out.find("newest dup")
        pos_oldest = out.find("oldest dup")
        assert pos_newest != -1, "Could not find 'newest dup' in output"
        assert pos_oldest != -1, "Could not find 'oldest dup' in output"
        assert pos_newest < pos_oldest, "Newest record must appear before oldest in output"

    def test_limit_caps_results(self, rejected_board, monkeypatch):
        """--limit 2 must return at most 2 records."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected", "--limit", "2"])
        assert result.exit_code == 0, result.output
        # With 3 records in fixture and --limit 2, oldest dup should be absent
        assert "oldest dup" not in result.output, (
            "--limit 2 with newest-first ordering should exclude the oldest record"
        )

    def test_since_filters_old_records(self, rejected_board, monkeypatch):
        """--since 30m (or similar) should filter out old records."""
        # All fixture records have fixed past timestamps (2026-04-27T10-12)
        # "since 1h" relative to wall clock (2026-04-27 ~now) would keep recent ones
        # Use very small window to filter everything out
        monkeypatch.chdir(rejected_board)
        # Use --since 1h; since all fixture records are "in the past" at test runtime,
        # they should all be within 1h of their seeded time. Instead seed a clearly
        # old record explicitly.
        old_rec = {
            "timestamp": "2020-01-01T00:00:00Z",
            "summary": "ancient dup",
            "goal": "Something from years ago",
            "similarity_score": 0.75,
            "matched_task_id": "20200101-ancient-aa01",
            "matched_column": "done",
            "threshold": 0.7,
        }
        mas = rejected_board / ".mas"
        rejected_path = mas / "proposals" / "rejected.jsonl"
        with rejected_path.open("a") as f:
            f.write(json.dumps(old_rec) + "\n")

        result = runner.invoke(app, ["proposals", "rejected", "--since", "1d"])
        assert result.exit_code == 0, result.output
        assert "ancient dup" not in result.output, (
            "Records older than --since window must be filtered out"
        )

    def test_json_output_ndjson_format(self, rejected_board, monkeypatch):
        """--json must emit one JSON object per line (NDJSON), newest-first."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected", "--json"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 NDJSON lines, got {len(lines)}: {lines}"

        records = [json.loads(l) for l in lines]
        # Newest-first ordering
        assert records[0]["summary"] == "newest dup"
        assert records[-1]["summary"] == "oldest dup"

    def test_json_output_schema_fields(self, rejected_board, monkeypatch):
        """Each JSON line must contain all schema fields."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected", "--json"])
        assert result.exit_code == 0, result.output
        for line in result.output.splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            for field in ("timestamp", "summary", "goal", "similarity_score", "matched_task_id", "matched_column", "threshold"):
                assert field in rec, f"Missing field '{field}' in JSON record: {rec}"

    def test_json_limit_honored(self, rejected_board, monkeypatch):
        """--json --limit 1 must emit exactly 1 record (the newest)."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected", "--json", "--limit", "1"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 1, f"Expected 1 line with --limit 1, got {len(lines)}"
        rec = json.loads(lines[0])
        assert rec["summary"] == "newest dup"

    def test_since_invalid_value_rejected(self, rejected_board, monkeypatch):
        """--since with an invalid token must exit non-zero (bad parameter)."""
        monkeypatch.chdir(rejected_board)
        result = runner.invoke(app, ["proposals", "rejected", "--since", "99z"])
        assert result.exit_code != 0, "Invalid --since value should cause non-zero exit"


# ---------------------------------------------------------------------------
# (e) Missing rejected.jsonl → empty output, exit 0, file not created
# ---------------------------------------------------------------------------

class TestMissingRejectedFile:
    def test_empty_board_exits_zero(self, tmp_path, monkeypatch):
        """No rejected.jsonl → `mas proposals rejected` exits 0."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, f"Expected exit 0 on missing file, got {result.exit_code}: {result.output}"

    def test_empty_board_no_file_created(self, tmp_path, monkeypatch):
        """No rejected.jsonl → the command must not create the file."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["proposals", "rejected"])
        rejected_path = mas / "proposals" / "rejected.jsonl"
        assert not rejected_path.exists(), "Command must not create rejected.jsonl when it doesn't exist"

    def test_empty_board_json_zero_lines(self, tmp_path, monkeypatch):
        """No rejected.jsonl with --json must emit zero lines."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["proposals", "rejected", "--json"])
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert lines == [], f"Expected zero JSON lines on missing file, got: {lines}"


# ---------------------------------------------------------------------------
# (f) Malformed line skipped with one WARNING per run
# ---------------------------------------------------------------------------

class TestMalformedLines:
    def _build_fixture(self, mas: Path, bad_line: str = "{not valid json!!!}") -> Path:
        proposals_dir = mas / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        good_rec = SAMPLE_RECORDS[0]
        lines = [bad_line, json.dumps(good_rec)]
        (proposals_dir / "rejected.jsonl").write_text("\n".join(lines) + "\n")
        return proposals_dir / "rejected.jsonl"

    def test_malformed_line_skipped_valid_shown(self, tmp_path, monkeypatch):
        """A malformed line must be skipped; remaining valid records must be listed."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        self._build_fixture(mas)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, result.output
        # The valid record summary must appear
        assert SAMPLE_RECORDS[0]["summary"] in result.output, (
            "Valid record must still appear after a malformed line is skipped"
        )

    def test_malformed_line_exactly_one_warning(self, tmp_path, monkeypatch, caplog):
        """Exactly one WARNING must be emitted per malformed line, not one per call."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        self._build_fixture(mas)
        monkeypatch.chdir(tmp_path)

        with caplog.at_level(logging.WARNING, logger="mas"):
            result = runner.invoke(app, ["proposals", "rejected"])

        assert result.exit_code == 0, result.output
        warning_count = sum(
            1 for r in caplog.records
            if r.levelno == logging.WARNING and r.name.startswith("mas")
        )
        assert warning_count >= 1, "Expected at least one WARNING for a malformed line"
        # Only one bad line → exactly one warning
        assert warning_count == 1, (
            f"Expected exactly 1 WARNING for 1 malformed line, got {warning_count}"
        )

    def test_malformed_json_line_skipped_not_crash(self, tmp_path, monkeypatch):
        """A completely non-JSON line must not crash the command."""
        mas = tmp_path / ".mas"
        board.ensure_layout(mas)
        self._build_fixture(mas, bad_line="this is not json at all")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["proposals", "rejected"])
        assert result.exit_code == 0, (
            f"Command must not crash on non-JSON line; got exit {result.exit_code}: {result.output}"
        )
