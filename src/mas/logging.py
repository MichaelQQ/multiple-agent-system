from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        msg: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "task_id"):
            msg["task_id"] = record.task_id
        if hasattr(record, "component"):
            msg["component"] = record.component
        for key, val in record.__dict__.get("_extra", {}).items():
            if key not in ({"task_id", "component"} & {"task_id", "component"}):
                msg[key] = val
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "filename", "funcName", "levelname",
                "levelno", "lineno", "module", "msecs", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "exc_info", "exc_text",
                "thread", "threadName", "message", "_name", "_extra",
            ):
                if key not in msg:
                    msg[key] = val
        return json.dumps(msg, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("mas")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level)
    root.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("mas.tick").setLevel(level)
    logging.getLogger("mas.daemon").setLevel(level)


class TaskLogger(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra = kwargs.get("extra", {})
        extra["task_id"] = self.extra.get("task_id")
        extra["component"] = self.extra.get("component")
        kwargs["extra"] = extra
        return msg, kwargs


def get_task_logger(logger: logging.Logger, task_id: str | None = None, component: str | None = None) -> TaskLogger:
    return TaskLogger(logger, {"task_id": task_id, "component": component})


def setup_daemon_logging(log_dir: Path, max_bytes: int, backup_count: int) -> logging.handlers.RotatingFileHandler:
    """Install a RotatingFileHandler at `log_dir/daemon.log` on the `mas` logger.

    Removes any pre-existing RotatingFileHandler on the `mas` logger so repeated
    calls (e.g. config hot-reload) don't stack handlers.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daemon.log"

    root = logging.getLogger("mas")
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=max_bytes, backupCount=backup_count
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return handler
