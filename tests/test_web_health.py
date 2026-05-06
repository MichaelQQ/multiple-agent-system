"""Tests for the GET /health endpoint."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mas import board
from mas.web.app import create_app


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tmp project with a .mas/ layout; mas.web operates on this root."""
    mas = tmp_path / ".mas"
    board.ensure_layout(mas)
    return tmp_path


@pytest.fixture
def client(project: Path) -> TestClient:
    app = create_app(project)
    return TestClient(app)


def test_health_happy_path_recent_heartbeat(project: Path, client: TestClient):
    """GET /health returns 200 with status=ok when heartbeat is recent."""
    mas = project / ".mas"
    # Write recent timestamp (10s ago, well within 2*600=1200s default threshold)
    recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    (mas / "tick_heartbeat").write_text(recent_ts)

    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "timestamp" in data
    # Verify timestamp is valid ISO8601
    datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
    assert r.headers["content-type"] == "application/json"


def test_health_stalled_old_heartbeat(project: Path, client: TestClient):
    """GET /health returns 503 with status=degraded when heartbeat is too old."""
    mas = project / ".mas"
    # Write timestamp older than 2*600=1200s default threshold
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=2000)).isoformat()
    (mas / "tick_heartbeat").write_text(old_ts)

    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "tick stalled"
    assert "timestamp" in data
    assert r.headers["content-type"] == "application/json"


def test_health_no_heartbeat_file(project: Path, client: TestClient):
    """GET /health returns 503 with status=degraded when heartbeat file missing."""
    mas = project / ".mas"
    heartbeat = mas / "tick_heartbeat"
    if heartbeat.exists():
        heartbeat.unlink()

    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert r.headers["content-type"] == "application/json"


def test_health_stalled_with_custom_interval(project: Path, client: TestClient):
    """GET /health returns 503 when heartbeat is older than 2x custom daemon interval."""
    mas = project / ".mas"
    # Set daemon interval to 300s, threshold becomes 600s
    (mas / "daemon.interval").write_text("300")
    # Write timestamp 700s old (older than 600s threshold)
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    (mas / "tick_heartbeat").write_text(old_ts)

    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["reason"] == "tick stalled"


def test_health_response_is_json(project: Path, client: TestClient):
    """GET /health returns Content-Type: application/json with correct status."""
    mas = project / ".mas"
    recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    (mas / "tick_heartbeat").write_text(recent_ts)

    r = client.get("/health")
    # Endpoint must exist and return either 200 or 503 (not 404)
    assert r.status_code in (200, 503), f"Expected 200 or 503, got {r.status_code}"
    assert r.headers["content-type"] == "application/json"
    # Verify body is valid JSON
    data = r.json()
    assert isinstance(data, dict)
    assert "status" in data
