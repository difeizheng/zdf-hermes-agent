"""Shared test fixtures for coordinator tests."""

from __future__ import annotations

import os
import tempfile
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


@pytest.fixture
def test_artifact_dir(tmp_path: Path) -> Path:
    """Temporary artifact directory."""
    d = tmp_path / "artifacts"
    d.mkdir()
    return d
