import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from mas import board, logging as mas_logging


def _configure_json_logging_to_file(logger_name: str, log_file: Path) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(mas_logging.JsonFormatter())
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


class TestSetupLogging:
    def test_setup_logging_configures_mas_root_logger(self):
        mas_root = logging.getLogger("mas")
        for h in mas_root.handlers[:]:
            mas_root.removeHandler(h)
        mas_root.setLevel(logging.NOTSET)

        mas_logging.setup_logging(level=logging.DEBUG)

        assert mas_root.level == logging.DEBUG
        assert len(mas_root.handlers) == 1
        handler = mas_root.handlers[0]
        assert isinstance(handler.formatter, mas_logging.JsonFormatter)
        assert handler.level == logging.DEBUG

    def test_setup_logging_idempotent(self):
        mas_logger = logging.getLogger("mas.test_idempotent")
        for h in mas_logger.handlers[:]:
            mas_logger.removeHandler(h)
        mas_logger.setLevel(logging.NOTSET)

        mas_logging.setup_logging()
        handler_count_1 = len(mas_logger.handlers)

        mas_logging.setup_logging()
        handler_count_2 = len(mas_logger.handlers)

        assert handler_count_1 == handler_count_2


class TestJsonFormatter:
    def test_outputs_valid_json(self, tmp_path: Path):
        log_file = tmp_path / "test_json.log"
        logger = _configure_json_logging_to_file("mas.test_json", log_file)
        logger.info("hello world")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_required_fields_present(self, tmp_path: Path):
        log_file = tmp_path / "test_fields.log"
        logger = _configure_json_logging_to_file("mas.test_fields", log_file)
        logger.info("test message")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert "level" in parsed
        assert parsed["level"] == "INFO"
        assert "logger" in parsed
        assert parsed["logger"] == "mas.test_fields"
        assert "message" in parsed
        assert parsed["message"] == "test message"


class TestTaskLogger:
    def test_injects_task_id(self, tmp_path: Path):
        log_file = tmp_path / "test_task_id.log"
        logger = _configure_json_logging_to_file("mas.test_task_id", log_file)
        task_logger = mas_logging.get_task_logger(logger, task_id="task-123", component="orchestrator")
        task_logger.info("processing task")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "task_id" in parsed
        assert parsed["task_id"] == "task-123"

    def test_injects_component(self, tmp_path: Path):
        log_file = tmp_path / "test_component.log"
        logger = _configure_json_logging_to_file("mas.test_component", log_file)
        task_logger = mas_logging.get_task_logger(logger, task_id="task-456", component="tester")
        task_logger.info("running tests")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "component" in parsed
        assert parsed["component"] == "tester"

    def test_injects_both_task_id_and_component(self, tmp_path: Path):
        log_file = tmp_path / "test_both.log"
        logger = _configure_json_logging_to_file("mas.test_both", log_file)
        task_logger = mas_logging.get_task_logger(logger, task_id="task-789", component="implementer")
        task_logger.info("implementing feature")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert parsed["task_id"] == "task-789"
        assert parsed["component"] == "implementer"

    def test_task_id_can_be_none(self, tmp_path: Path):
        log_file = tmp_path / "test_none_task_id.log"
        logger = _configure_json_logging_to_file("mas.test_none_task_id", log_file)
        task_logger = mas_logging.get_task_logger(logger, task_id=None, component="proposer")
        task_logger.info("proposing")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "task_id" in parsed
        assert parsed["task_id"] is None


class TestBoardMoveLogging:
    def test_board_move_includes_task_id(self, tmp_path: Path):
        log_file = tmp_path / "board_move_task_id.log"
        board_logger = _configure_json_logging_to_file("mas.board", log_file)

        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        src = board.task_dir(mas, "proposed", "move-test-1")
        src.mkdir(parents=True)
        board.write_task(src, board.Task(id="move-test-1", role="orchestrator", goal="g"))
        dst = board.task_dir(mas, "doing", "move-test-1")

        board.move(src, dst)

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert parsed["task_id"] == "move-test-1"

    def test_board_move_includes_from_column(self, tmp_path: Path):
        log_file = tmp_path / "board_move_from.log"
        board_logger = _configure_json_logging_to_file("mas.board", log_file)

        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        src = board.task_dir(mas, "proposed", "move-test-2")
        src.mkdir(parents=True)
        board.write_task(src, board.Task(id="move-test-2", role="orchestrator", goal="g"))
        dst = board.task_dir(mas, "doing", "move-test-2")

        board.move(src, dst)

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "from_column" in parsed
        assert parsed["from_column"] == "proposed"

    def test_board_move_includes_to_column(self, tmp_path: Path):
        log_file = tmp_path / "board_move_to.log"
        board_logger = _configure_json_logging_to_file("mas.board", log_file)

        mas = tmp_path / ".mas"
        board.ensure_layout(mas)

        src = board.task_dir(mas, "doing", "move-test-3")
        src.mkdir(parents=True)
        board.write_task(src, board.Task(id="move-test-3", role="orchestrator", goal="g"))
        dst = board.task_dir(mas, "done", "move-test-3")

        board.move(src, dst)

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "to_column" in parsed
        assert parsed["to_column"] == "done"


class TestAdapterDispatchLogging:
    def test_dispatch_includes_task_id(self, tmp_path: Path):
        log_file = tmp_path / "dispatch_task_id.log"
        adapter_logger = _configure_json_logging_to_file("mas.adapters", log_file)

        from mas.adapters.base import Adapter
        from mas.schemas import ProviderConfig, RoleConfig

        class FakeAdapter(Adapter):
            name = "fake"
            agentic = True

            def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
                return ["true"]

        adapter = FakeAdapter(ProviderConfig(cli="true", max_concurrent=1, extra_args=[]), RoleConfig(provider="fake", max_retries=2))

        task_dir = tmp_path / "task-xyz"
        task_dir.mkdir(parents=True)
        log_path = tmp_path / "dispatch.log"

        adapter.dispatch(
            prompt="test prompt",
            task_dir=task_dir,
            cwd=tmp_path,
            log_path=log_path,
            role="implementer",
        )

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert parsed["task_id"] == "task-xyz"

    def test_dispatch_includes_role(self, tmp_path: Path):
        log_file = tmp_path / "dispatch_role.log"
        adapter_logger = _configure_json_logging_to_file("mas.adapters", log_file)

        from mas.adapters.base import Adapter
        from mas.schemas import ProviderConfig, RoleConfig

        class FakeAdapter(Adapter):
            name = "testprovider"
            agentic = True

            def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
                return ["true"]

        adapter = FakeAdapter(ProviderConfig(cli="true", max_concurrent=1, extra_args=[]), RoleConfig(provider="testprovider", max_retries=2))

        task_dir = tmp_path / "role-test"
        task_dir.mkdir(parents=True)
        log_path = tmp_path / "dispatch.log"

        adapter.dispatch(
            prompt="test",
            task_dir=task_dir,
            cwd=tmp_path,
            log_path=log_path,
            role="tester",
        )

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "role" in parsed
        assert parsed["role"] == "tester"

    def test_dispatch_includes_provider(self, tmp_path: Path):
        log_file = tmp_path / "dispatch_provider.log"
        adapter_logger = _configure_json_logging_to_file("mas.adapters", log_file)

        from mas.adapters.base import Adapter
        from mas.schemas import ProviderConfig, RoleConfig

        class AnotherFakeAdapter(Adapter):
            name = "myprovider"
            agentic = True

            def build_command(self, prompt: str, task_dir: Path, cwd: Path) -> list[str]:
                return ["true"]

        adapter = AnotherFakeAdapter(ProviderConfig(cli="true", max_concurrent=1, extra_args=[]), RoleConfig(provider="myprovider", max_retries=2))

        task_dir = tmp_path / "prov-test"
        task_dir.mkdir(parents=True)
        log_path = tmp_path / "dispatch.log"

        adapter.dispatch(
            prompt="test",
            task_dir=task_dir,
            cwd=tmp_path,
            log_path=log_path,
            role="evaluator",
        )

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "provider" in parsed
        assert parsed["provider"] == "myprovider"


class TestTickLogging:
    def test_tick_log_still_works(self, tmp_path: Path):
        log_file = tmp_path / "tick_test.log"
        tick_logger = _configure_json_logging_to_file("mas.tick", log_file)

        tick_logger.info("tick test message")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert "message" in parsed
        assert "tick test message" in parsed["message"]

    def test_tick_get_task_logger_injects_fields(self, tmp_path: Path):
        log_file = tmp_path / "tick_task_fields.log"
        tick_logger = _configure_json_logging_to_file("mas.tick_fields", log_file)

        task_logger = mas_logging.get_task_logger(tick_logger, task_id="tick-task-1", component="proposer")
        task_logger.info("proposer activity")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert parsed["task_id"] == "tick-task-1"
        assert parsed["component"] == "proposer"

    def test_tick_mas_logger_name_in_output(self, tmp_path: Path):
        log_file = tmp_path / "tick_name.log"
        tick_logger = _configure_json_logging_to_file("mas.tick_name", log_file)

        tick_logger.info("logger name test")

        output = log_file.read_text().strip()
        parsed = json.loads(output)
        assert parsed["logger"] == "mas.tick_name"
