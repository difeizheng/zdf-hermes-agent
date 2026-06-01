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
from coordinator.kanban_sync import sync_create, sync_status, backfill_all as _kanban_backfill
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

    db = TaskDB(db_path)
    broadcaster = TaskEventBroadcaster()
    metrics = MetricsCollector()

    # Backfill existing tasks to Kanban (idempotent, skips already-synced)

    # Backfill existing tasks to Kanban (idempotent, skips already-synced)
    backfilled = _kanban_backfill()
    if backfilled:
        logger.info("Kanban backfill: %d tasks synced", backfilled)

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
    metadata: Optional[dict] = None
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
    sync_create(str(task.id), task.type.value, task.title, task.description)
    return task


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
    # Audit log
    _ensure_db().add_event(task_id, TaskEvent.STARTED, {"agent_id": req.agent_id})
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
    """Update task status, assignment, or dependencies."""
    if update.depends_on is not None:
        task = _ensure_db().update_task_dependencies(task_id, update.depends_on)
    elif update.status:
        task = _ensure_db().update_task_status(task_id, update.status, update.assigned_to)
    else:
        task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/result")
async def submit_result(task_id: str, req: ResultRequest):
    """Submit task completion or failure.

    The Brain Agent is responsible for creating the full task chain
    (design → dev → validate → deploy) upfront. This endpoint only:
    1. Updates task status in DB (including _resolve_dependencies)
    2. Records audit event
    3. Publishes SSE event for status change
    4. Syncs to Kanban

    No auto-chaining — that logic was removed to eliminate race conditions
    between the Brain's manual chain and the coordinator's auto-chain.
    """
    task = _ensure_db().submit_result(task_id, req.artifacts, req.error, metadata=req.metadata)
    if task is None:
        raise HTTPException(404, "Task not found")
    event_type = TaskEvent.FAILED if req.error else TaskEvent.COMPLETED
    # Audit log
    _ensure_db().add_event(task_id, event_type, {"error": req.error} if req.error else {"artifacts": req.artifacts})
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
    new_status = "failed" if req.error else "completed"
    sync_status(str(task_id), new_status)

    return task


@app.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: str):
    """Update task heartbeat."""
    _ensure_db().update_heartbeat(task_id)
    from coordinator.kanban_sync import sync_heartbeat
    sync_heartbeat(task_id)
    return {"status": "ok"}


@app.get("/tasks/{task_id}/history")
def get_task_history(task_id: str):
    """Get audit log of all events for a task."""
    task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    events = _ensure_db().get_events_since(task_id)
    return events


@app.get("/tasks/{task_id}/artifacts")
def get_artifacts(task_id: str):
    """Read artifact files from disk."""
    task = _ensure_db().get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    workspace_dir = Path(cfg.get("workspace_dir", "D:/hermes/workspace")) / str(task_id) / "artifacts"
    artifacts = {}
    if workspace_dir.exists():
        for f in workspace_dir.iterdir():
            if f.is_file():
                artifacts[f.name] = f.read_text(encoding="utf-8")
    return artifacts


@app.put("/tasks/{task_id}/artifacts/{name}")
async def upload_artifact(task_id: str, name: str, request: Request):
    """Write artifact file to disk."""
    body = await request.body()
    workspace_dir = Path(cfg.get("workspace_dir", "D:/hermes/workspace")) / str(task_id) / "artifacts"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / name).write_bytes(body)
    return {"status": "ok", "path": str(workspace_dir / name)}


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
    """Check for stale tasks every 30s, mark as TIMEOUT.

    After marking a task TIMEOUT, calls _resolve_dependencies so that
    any dependent tasks are not permanently blocked. This handles the
    case where an agent process crashes (OOM, kill -9, machine power
    loss) and the task stays in 'running' state forever without this
    monitor triggering dependency resolution.
    """
    while True:
        await asyncio.sleep(30)
        try:
            stale_timeout = cfg["stale_timeout"] if cfg else 120
            stale_tasks = _ensure_db().get_stale_tasks(stale_timeout)
            for task in stale_tasks:
                _ensure_db().update_task_status(task.id, TaskStatus.TIMEOUT)
                # Resolve dependencies so dependents are not permanently blocked
                _ensure_db()._resolve_dependencies(str(task.id))
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
    """On task completion, broadcast 'ready' for newly-satisfied tasks.

    Tracks notified task IDs to avoid re-publishing events for tasks that
    were already announced. Previous count-based logic missed new ready tasks
    when the total count didn't exceed the previous peak (e.g. after tasks
    were claimed and new ones became ready).
    """
    notified_ids: set[str] = set()
    while True:
        await asyncio.sleep(5)
        try:
            ready_tasks = _ensure_db().get_ready_tasks()
            current_ids = {str(t.id) for t in ready_tasks}
            # Publish events only for newly-ready tasks
            for task in ready_tasks:
                if str(task.id) not in notified_ids:
                    await _ensure_broadcaster().publish(
                        TaskEvent.CREATED,
                        task_id=str(task.id),
                        task_type=task.type.value,
                        data={"title": task.title, "dependency_resolved": True},
                    )
                    notified_ids.add(str(task.id))
            # Prune IDs that are no longer ready (claimed or completed)
            notified_ids -= notified_ids - current_ids
        except Exception:
            logger.exception("Dependency monitor error")
