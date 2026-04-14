from datetime import datetime, timezone

from mas.ids import slugify, task_id


def test_slugify():
    assert slugify("Add retry logic!") == "add-retry-logic"
    assert slugify("") == "task"
    assert slugify("a" * 100).startswith("a" * 32)


def test_task_id_format():
    now = datetime(2026, 4, 14, tzinfo=timezone.utc)
    tid = task_id("Add retry logic", now=now)
    assert tid.startswith("20260414-add-retry-logic-")
    assert len(tid.split("-")[-1]) == 4


def test_task_id_unique_with_salt():
    now = datetime(2026, 4, 14, tzinfo=timezone.utc)
    a = task_id("goal", now=now, salt="1")
    b = task_id("goal", now=now, salt="2")
    assert a != b
