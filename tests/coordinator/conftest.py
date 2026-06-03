"""Shared test fixtures for coordinator tests."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from coordinator.db import TaskDB
from coordinator.events import TaskEventBroadcaster
from coordinator.metrics import MetricsCollector
from coordinator.models import TaskEvent


@pytest.fixture
def tmp_db() -> TaskDB:
    """Temporary SQLite database, cleaned up after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = TaskDB(Path(path))
    yield db
    db.close()
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def broadcaster() -> TaskEventBroadcaster:
    """Fresh event broadcaster."""
    return TaskEventBroadcaster()


@pytest.fixture
def metrics_collector() -> MetricsCollector:
    """Fresh metrics collector."""
    return MetricsCollector()


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def coordinator_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Start a real uvicorn server on a random port, yield the base URL.

    Isolates HERMES_HOME, DB, workspace_dir, and all coordinator globals.
    The server runs in a daemon thread and is cleaned up after the test.
    """
    import coordinator.server as srv

    # ── Isolate environment ──
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    db_path = tmp_path / "test.db"

    # ── Save originals for teardown ──
    original_db = srv.db
    original_broadcaster = srv.broadcaster
    original_metrics = srv.metrics
    original_cfg = srv.cfg

    # ── Inject test values ──
    srv.db = TaskDB(db_path)
    srv.broadcaster = TaskEventBroadcaster()
    srv.metrics = MetricsCollector()
    srv.cfg = {
        "port": 0,
        "db_path": str(db_path),
        "workspace_dir": str(workspace_dir),
        "heartbeat_interval": 60,
        "stale_timeout": 300,
        "max_retries": 0,
        "retry_delay": 30,
    }

    # ── Start real server ──
    port = _find_free_port()
    srv.cfg["port"] = port

    import uvicorn

    thread = threading.Thread(
        target=uvicorn.run,
        args=(srv.app,),
        kwargs={"host": "127.0.0.1", "port": port, "log_level": "error"},
        daemon=True,
    )
    thread.start()

    # Wait for server to be ready (poll health endpoint)
    import httpx

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Coordinator server did not start within 5s on port {port}")

    yield base_url

    # ── Restore originals ──
    srv.db = original_db
    srv.broadcaster = original_broadcaster
    srv.metrics = original_metrics
    srv.cfg = original_cfg
