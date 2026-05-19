"""FastAPI HTTP server + SSE broadcaster + background monitors."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from coordinator.config import load_config
from coordinator.db import TaskDB
from coordinator.events import TaskEventBroadcaster
from coordinator.metrics import MetricsCollector
from coordinator.models import TaskCreate, TaskEvent, TaskStatus, TaskUpdate

logger = logging.getLogger(__name__)

# -- global refs set by lifespan -----------------------------------------------
db: Optional[TaskDB] = None
broadcaster: Optional[TaskEventBroadcaster] = None
metrics: Optional[MetricsCollector] = None
cfg: Optional[dict] = None


# -- lifespan ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, broadcaster, cfg, metrics
    cfg = load_config()
    db_path = Path(cfg["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path(cfg["artifact_dir"])
    artifact_dir.mkdir(parents=True, exist_ok=True)

    db = TaskDB(db_path)
    broadcaster = TaskEventBroadcaster()
    metrics = MetricsCollector()

    # Start background monitors
    monitor = asyncio.create_task(_stale_task_monitor())
    dep_monitor = asyncio.create_task(_dependency_monitor())

    yield

    monitor.cancel()
    dep_monitor.cancel()
    if db:
        db.close()


app = FastAPI(title="Hermes Task Coordinator", lifespan=lifespan)


# -- helpers -------------------------------------------------------------------

def _ensure_db() -> TaskDB:
    if db is None:
        raise HTTPException(503, "Coordinator not initialized")
    return db


def _ensure_broadcaster() -> TaskEventBroadcaster:
    if broadcaster is None:
        raise HTTPException(503, "Coordinator not initialized")
    return broadcaster


def _get_metrics() -> MetricsCollector:
    if metrics is None:
        raise HTTPException(503, "Coordinator not initialized")
    return metrics


# -- request / response models -------------------------------------------------

class ClaimRequest(BaseModel):
    agent_id: str


class ResultRequest(BaseModel):
    artifacts: Optional[dict] = None
    error: Optional[str] = None


# -- task endpoints ------------------------------------------------------------

@app.post("/tasks", status_code=201)
async def create_task(task_in: TaskCreate):
    """Create a new task with optional dependencies."""
    task = _ensure_db().create_task(task_in)
    await _ensure_broadcaster().publish(
        TaskEvent.CREATED,
        task_id=str(task.id),
        task_type=task.type.value,
        data={"title": task.title},
    )
    _get_metrics().record_task_created(task.type.value)
    return task


@app.get("/tasks")
def list_tasks(
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
):
    """List tasks with optional filters."""
    s = TaskStatus(status) if status else None
    tasks = _ensure_db().list_tasks(status=s, task_type=type)
    return tasks


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    """Get task details."""
    task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, req: ClaimRequest):
    """Atomic CAS: pending → running."""
    task = _ensure_db().claim_task(task_id, req.agent_id)
    if task is None:
        raise HTTPException(409, "Task not claimable (not pending or not found)")
    await _ensure_broadcaster().publish(
        TaskEvent.STARTED,
        task_id=str(task_id),
        task_type=task.type.value,
        data={"agent_id": req.agent_id},
    )
    _ensure_db().update_heartbeat(task_id)
    return task


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, update: TaskUpdate):
    """Update task status / assignment."""
    if update.status:
        task = _ensure_db().update_task_status(task_id, update.status, update.assigned_to)
    else:
        task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, req: ResultRequest):
    """Submit task completion or failure."""
    task = _ensure_db().submit_result(task_id, req.artifacts, req.error)
    if task is None:
        raise HTTPException(404, "Task not found")
    event_type = TaskEvent.FAILED if req.error else TaskEvent.COMPLETED
    if req.error:
        _get_metrics().record_task_failed(task.type.value, "execution_error")
    else:
        if task.started_at:
            from datetime import datetime
            start = datetime.fromisoformat(task.started_at) if isinstance(task.started_at, str) else task.started_at
            end = datetime.now() if task.completed_at is None else (datetime.fromisoformat(task.completed_at) if isinstance(task.completed_at, str) else task.completed_at)
            _get_metrics().record_task_completed(task.type.value, (end - start).total_seconds())
    await _ensure_broadcaster().publish(
        event_type,
        task_id=str(task_id),
        task_type=task.type.value,
        data={"error": req.error} if req.error else {"artifacts": req.artifacts},
    )
    return task


@app.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: str):
    """Update task heartbeat."""
    _ensure_db().update_heartbeat(task_id)
    return {"status": "ok"}


@app.get("/tasks/{task_id}/artifacts")
def get_artifacts(task_id: str):
    """Read artifact file from disk."""
    task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    artifact_dir = Path(cfg["artifact_dir"])
    artifacts = {}
    base = artifact_dir / str(task_id) / "artifacts"
    if base.exists():
        for f in base.iterdir():
            if f.is_file():
                artifacts[f.name] = f.read_text(encoding="utf-8")
    return artifacts


@app.put("/tasks/{task_id}/artifacts/{name}")
async def upload_artifact(task_id: str, name: str, request: Request):
    """Write artifact file to disk."""
    body = await request.body()
    artifact_dir = Path(cfg["artifact_dir"])
    target = artifact_dir / str(task_id) / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(body)
    return {"status": "ok", "path": str(target / name)}


# -- SSE endpoint --------------------------------------------------------------

@app.get("/tasks/events")
def event_stream(
    type: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
):
    """SSE stream of task events."""
    return StreamingResponse(
        _ensure_broadcaster().sse_stream(task_type=type, task_id=task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# -- health --------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus-style metrics endpoint."""
    if metrics is None:
        raise HTTPException(503, "Coordinator not initialized")
    return metrics.format_prometheus()


# -- background monitors -------------------------------------------------------

async def _stale_task_monitor() -> None:
    """Check for stale tasks every 30s, mark as TIMEOUT."""
    while True:
        await asyncio.sleep(30)
        try:
            stale_timeout = cfg["stale_timeout"] if cfg else 120
            stale_tasks = _ensure_db().get_stale_tasks(stale_timeout)
            for task in stale_tasks:
                _ensure_db().update_task_status(task.id, TaskStatus.TIMEOUT)
                await _ensure_broadcaster().publish(
                    TaskEvent.TIMEOUT,
                    task_id=str(task.id),
                    task_type=task.type.value,
                    data={"reason": "heartbeat_timeout"},
                )
                _get_metrics().record_task_timeout(task.type.value)
                logger.warning("Task %s marked as TIMEOUT (stale heartbeat)", task.id)
        except Exception:
            logger.exception("Stale task monitor error")


async def _dependency_monitor() -> None:
    """On task completion, broadcast 'ready' for newly-satisfied tasks."""
    last_known_count = 0
    while True:
        await asyncio.sleep(5)
        try:
            ready_tasks = _ensure_db().get_ready_tasks()
            if len(ready_tasks) > last_known_count:
                for task in ready_tasks:
                    await _ensure_broadcaster().publish(
                        TaskEvent.CREATED,
                        task_id=str(task.id),
                        task_type=task.type.value,
                        data={"title": task.title, "dependency_resolved": True},
                    )
                last_known_count = len(ready_tasks)
        except Exception:
            logger.exception("Dependency monitor error")
