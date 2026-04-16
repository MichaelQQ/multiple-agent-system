from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
