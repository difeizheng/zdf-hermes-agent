"""Pydantic models for task orchestration."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    DESIGN = "design"
    DEV = "dev"
    VALIDATE = "validate"
    DEPLOY = "deploy"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskEvent(str, Enum):
    CREATED = "created"
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    type: TaskType
    title: str = Field(..., max_length=200)
    description: str = Field(..., min_length=10)
    depends_on: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    artifacts: Optional[dict] = None
    error: Optional[str] = None
    assigned_to: Optional[str] = None


class Task(BaseModel):
    id: str
    type: TaskType
    status: TaskStatus
    title: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)
    error: Optional[str] = None
    assigned_to: Optional[str] = None
    dependency_status: str = "blocked"
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)


class TaskEventModel(BaseModel):
    id: int
    task_id: str
    type: TaskEvent
    data: dict = Field(default_factory=dict)
    created_at: datetime
