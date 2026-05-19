# Multi-Agent Orchestration — Implementation Plan

## Architecture Corrections (vs original doc)

### Decision 1: Agent Trigger = Hybrid Push/Pull
- **Internal agents** (Design via Claude API): Brain spawns via `delegate_tool` pattern
- **External agents** (Dev/Validate/Deploy via Claude Code/Codex): SSE event subscription → claim task → execute → submit result
- **Rationale**: `delegate_tool.py` only works for in-process AIAgent children. External CLIs need daemon pattern with SSE.

### Decision 2: Task DAG = Junction Table
Original schema has `parent_id TEXT` (single parent). Need multi-dependency support.

```sql
CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);
ALTER TABLE tasks ADD COLUMN dependency_status TEXT DEFAULT 'blocked';
```

### Decision 3: Transport = HTTP-first + optional stdio bridge
Claude Code CLI doesn't speak MCP stdio natively for custom servers. HTTP + SSE is universal. Stdio bridge is optional for future MCP-native clients.

### Decision 4: Artifacts = File system + DB paths
DB stores JSON paths, files store content. Matches existing Hermes pattern (sessions DB + separate voice files).

### Decision 5: Heartbeat = `last_heartbeat_at` column
Agent crashes leave tasks stuck "running". Background monitor checks every 30s, marks stale after 2x heartbeat interval.

---

## Phase 0: Foundation (2-3 days)

### 0.1 `coordinator/__init__.py`
Package marker. Empty file.

### 0.2 `coordinator/models.py` — Pydantic task models

```python
from enum import Enum
from typing import Optional
from datetime import datetime
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
    dependency_status: str = "blocked"  # blocked | satisfied
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
```

### 0.3 `coordinator/db.py` — SQLite CRUD (pattern from `hermes_state.py`)

Functions to implement:

| Function | Purpose |
|----------|---------|
| `init_db(db_path)` | Create tables, enable WAL mode, set PRAGMAs |
| `create_task(conn, task: TaskCreate)` | Insert task + dependencies, return Task |
| `get_task(conn, task_id)` | SELECT task + dependencies, return Task or None |
| `update_task_status(conn, task_id, status, assigned_to?)` | Atomic status update |
| `claim_task(conn, task_id, agent_id)` | Atomic CAS: pending→running |
| `submit_result(conn, task_id, artifacts, error?)` | Set completed/failed + artifacts |
| `list_tasks(conn, status?, type?)` | Filtered list |
| `add_dependency(conn, task_id, depends_on)` | Insert into junction table |
| `resolve_dependencies(conn, completed_task_id)` | When task completes, check blocked tasks |
| `get_ready_tasks(conn, type?)` | pending + dependency_status=satisfied |
| `update_heartbeat(conn, task_id)` | Touch last_heartbeat_at |
| `get_stale_tasks(conn, timeout_seconds)` | heartbeat older than threshold |

SQLite schema (in `init_db`):

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    artifacts       TEXT DEFAULT '{}',
    error           TEXT,
    assigned_to     TEXT,
    dependency_status TEXT DEFAULT 'blocked',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    last_heartbeat_at TIMESTAMP,
    metadata        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    data        TEXT DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(type);
CREATE INDEX IF NOT EXISTS idx_tasks_dependency_status ON tasks(dependency_status);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_created ON task_events(created_at);
```

### 0.4 `coordinator/events.py` — SSE broadcaster (pattern from `mcp_serve.py` EventBridge)

```python
class TaskEventBroadcaster:
    """SSE event broadcaster. Multiple subscribers, async-safe."""

    # Key methods:
    # subscribe(task_type=None, task_id=None) -> asyncio.Queue
    # unsubscribe(queue)
    # publish(event_type, task_id, data)
    # get_events_since(task_id, event_id) -> list
```

Pattern: maintain list of async queues. On publish, broadcast to all matching subscribers. SSE endpoint yields `data: {...}` lines.

---

## Phase 1: HTTP Server (2 days)

### 1.1 `coordinator/server.py` — FastAPI application

Endpoints:

| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| POST | `/tasks` | `create_task` | Create task, broadcast "created" |
| GET | `/tasks` | `list_tasks` | Filter by status/type |
| GET | `/tasks/{id}` | `get_task` | Task details + dependencies |
| POST | `/tasks/{id}/claim` | `claim_task` | Atomic CAS pending→running |
| PATCH | `/tasks/{id}` | `update_task` | Status/assignment update |
| POST | `/tasks/{id}/result` | `submit_result` | Complete/fail + artifacts |
| POST | `/tasks/{id}/heartbeat` | `heartbeat` | Touch heartbeat |
| GET | `/tasks/{id}/artifacts` | `get_artifacts` | Read artifact file |
| PUT | `/tasks/{id}/artifacts/{name}` | `upload_artifact` | Write artifact file |
| GET | `/tasks/events` | `event_stream` | SSE stream |
| GET | `/health` | `health` | Liveness probe |

Background tasks registered on startup:
- `stale_task_monitor`: every 30s, check heartbeat, mark TIMEOUT
- `dependency_resolver`: on task completion, resolve blocked tasks, broadcast "ready"

### 1.2 `coordinator/config.py`

Load from `~/.hermes/config.yaml` under `orchestrator` key:

```python
DEFAULT_CONFIG = {
    "port": 9100,
    "db_path": None,  # defaults to ~/.hermes/tasks.db
    "heartbeat_interval": 60,
    "stale_timeout": 120,
    "artifact_dir": None,  # defaults to ~/.hermes/tasks/
    "max_retries": 0,
    "retry_delay": 30,
}
```

### 1.3 `scripts/run_coordinator.py` — Entry point

```bash
python scripts/run_coordinator.py [--port 9100] [--dev]
```

Starts uvicorn, initializes DB, starts background monitors.

### 1.4 Tests

- `tests/coordinator/test_db.py`: CRUD, dependencies, claim CAS, stale detection
- `tests/coordinator/test_server.py`: HTTP endpoints, SSE subscription, error cases

---

## Phase 2: Orchestrator Plugin + DingTalk (3 days)

### 2.1 `plugins/orchestrator/__init__.py` — Plugin entry point

```python
def register(ctx) -> None:
    from plugins.orchestrator.tools import register_tools
    register_tools(ctx)
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
```

**走 plugin 系统，不改核心文件。**

### 2.2 `plugins/orchestrator/tools.py` — Orchestrator tool (注册 via `ctx.register_tool`)

```python
def register_tools(ctx):
    ctx.register_tool(
        name="orchestrate",
        toolset="orchestrator",
        schema={...},
        handler=orchestrate_handler,
        is_async=True,
        description="Create and manage multi-agent orchestrated tasks.",
    )
```

Actions: `create`, `list`, `status`, `cancel`.
Handler calls coordinator HTTP API at `coordinator_url` from config.

### 2.3 `plugins/orchestrator/hooks.py` — pre_gateway_dispatch 路由

拦截钉钉消息，判断是否进入 Brain 编排模式。重写消息文本让 Brain Agent 自动调用 `orchestrate` tool。

### 2.4 `plugins/orchestrator/config.py` — 独立配置加载

从 `~/.hermes/config.yaml` 的 `orchestrator` 段读取，不依赖 `DEFAULT_CONFIG` 合并逻辑。

### 2.5 `skills/brain-orchestrator/prompt.md` — Brain 系统提示词

System prompt defining:
- Task type selection rules
- Dependency patterns (design→dev→validate→deploy chain)
- Progress reporting format
- Error handling strategy

### 2.5 `hermes_cli/config.py` (modify) — Add orchestrator defaults

```python
"orchestrator": {
    "enabled": False,
    "coordinator_url": "http://localhost:9100",
    "auto_respond": True,
    "max_concurrent_tasks": 3,
    "voice_stt": "whisper",
}
```

### 2.6 DingTalk routing (modify `gateway/platforms/dingtalk.py`)

When `orchestrator.enabled=true`:
- Incoming message → check if it's a task command (prefix like `/task` or natural language)
- Route to Brain skill instead of normal chat
- Voice → Whisper STT → same path

**Testing milestone**: Voice command "帮我开发一个用户管理系统" → Brain creates 4 tasks with dependency chain → DingTalk responds with progress card.

---

## Phase 3: Internal Design Agent (2 days)

### 3.1 `coordinator/agent_runner.py` — Agent execution base class

```python
class AgentRunner(ABC):
    def __init__(self, coordinator_url: str, config: dict): ...
    async def run(self): ...  # main loop: claim → execute → submit
    @abstractmethod
    async def execute_task(self, task: Task) -> dict: ...
```

### 3.2 `coordinator/design_agent.py` — Claude API design agent

```python
class DesignAgentRunner(AgentRunner):
    async def execute_task(self, task: Task) -> dict:
        # 1. Call Claude Opus API with task.description as system prompt
        # 2. Parse response into PRD/architecture/system_design sections
        # 3. Write markdown to artifact_dir/{task_id}/artifacts/
        # 4. Return artifact paths
```

Uses `anthropic` SDK. Already in Hermes `.[all]` deps.

### 3.3 `tests/coordinator/test_integration.py`

End-to-end: Brain creates design task → Design Agent executes → Result submitted → Brain receives SSE event.

---

## Phase 4: External Agent Daemons (3-4 days)

### 4.1 `coordinator/agent_daemon.py` — Base daemon framework

```python
class AgentDaemon:
    """Long-lived daemon subscribing to SSE, claiming tasks, executing."""

    async def run(self):
        # 1. Connect to SSE /tasks/events?type={agent_type}
        # 2. On "created" event → claim_task
        # 3. Execute task in subprocess
        # 4. Submit result
        # 5. Heartbeat thread runs concurrently
```

### 4.2 `coordinator/dev_agent.py` — Dev Agent (Claude Code CLI)

```python
class DevAgentDaemon(AgentDaemon):
    async def execute_task(self, task: Task) -> dict:
        # 1. Fetch dependency artifacts (design docs) via HTTP
        # 2. Create git worktree at artifact_dir/{task_id}/worktree
        # 3. Invoke Claude Code: claude -p "<prompt with design docs>"
        # 4. Capture stdout/stderr
        # 5. Read git diff, commit SHA
        # 6. Run test_command if in metadata
        # 7. Return: commit_sha, changed_files, test_results
```

### 4.3 `coordinator/validate_agent.py` — Validate Agent (Codex/OpenCode)

```python
class ValidateAgentDaemon(AgentDaemon):
    async def execute_task(self, task: Task) -> dict:
        # 1. Checkout Dev Agent's branch
        # 2. Run codex review → review.md
        # 3. Run tests → test_output.log
        # 4. Return: review_status, test_pass, artifacts
```

### 4.4 `coordinator/deploy_agent.py` — Deploy Agent (Claude Code CLI)

```python
class DeployAgentDaemon(AgentDaemon):
    async def execute_task(self, task: Task) -> dict:
        # 1. Checkout validated branch
        # 2. Build Docker / create release
        # 3. Return: deployment_url, version, status
```

### 4.5 `scripts/run_dev_agent.py`, `run_validate_agent.py`, `run_deploy_agent.py`

Entry points for each daemon.

---

## Phase 5: Error Handling & Observability (2 days)

### 5.1 Timeout & Retry

- `timeout_monitor.py`: Background task, already built into server.py
- `retry_handler.py`: Configurable max_retries, retry with error context

### 5.2 Progress streaming to DingTalk

- DingTalk AI Cards with live status updates
- On each SSE event, push card update to user

### 5.3 Metrics

- `/metrics` endpoint: task counts by status, avg latency, success rate
- Simple counters, no Prometheus dependency

---

## File Tree (All New Files)

```
coordinator/
├── __init__.py              # Phase 0
├── models.py                # Phase 0 - Pydantic models
├── db.py                    # Phase 0 - SQLite CRUD
├── events.py                # Phase 0 - SSE broadcaster
├── server.py                # Phase 1 - FastAPI app + background tasks
├── config.py                # Phase 1 - Config loading
├── agent_runner.py          # Phase 3 - Base agent runner
├── design_agent.py          # Phase 3 - Claude API agent
├── agent_daemon.py          # Phase 4 - Daemon base class
├── dev_agent.py             # Phase 4 - Dev agent (Claude Code)
├── validate_agent.py        # Phase 4 - Validate agent (Codex)
├── deploy_agent.py          # Phase 4 - Deploy agent
├── timeout_monitor.py       # Phase 5 - Stale task detection
├── retry_handler.py         # Phase 5 - Retry logic
└── metrics.py               # Phase 5 - Observability

plugins/orchestrator/
├── __init__.py              # Phase 2 - register(ctx) 入口
├── tools.py                 # Phase 2 - orchestrate tool 注册
├── hooks.py                 # Phase 2 - pre_gateway_dispatch 路由
└── config.py                # Phase 2 - 独立配置加载

skills/brain-orchestrator/
└── prompt.md                # Phase 2 - Brain system prompt

tools/
└── orchestrate_tool.py      # Phase 2 - Hermes tool (self-registering)

skills/
└── brain-orchestrator/
    ├── skill.yaml           # Phase 2 - Skill definition
    └── prompt.md            # Phase 2 - Brain system prompt

scripts/
├── run_coordinator.py       # Phase 1 - Coordinator entry point
├── run_dev_agent.py         # Phase 4 - Dev agent entry point
├── run_validate_agent.py    # Phase 4 - Validate agent entry point
└── run_deploy_agent.py      # Phase 4 - Deploy agent entry point

tests/
└── coordinator/
    ├── test_db.py           # Phase 1 - DB CRUD tests
    ├── test_server.py       # Phase 1 - HTTP endpoint tests
    └── test_integration.py  # Phase 3 - E2E integration test
```

## Modified Files

**零核心文件修改。** 全部走 plugin 系统。上游 `git rebase` 零冲突。

---

## Dependency Order (What to build first)

```
Phase 0: models.py → db.py → events.py
              ↓
Phase 1: server.py (+ config.py, run_coordinator.py, tests)
              ↓
Phase 2: plugin __init__.py → tools.py → hooks.py → config.py → prompt.md
              ↓
Phase 3: agent_runner.py → design_agent.py (+ integration tests)
              ↓
Phase 4: agent_daemon.py → dev/validate/deploy agents
              ↓
Phase 5: timeout_monitor.py → retry_handler.py → metrics.py
```

Each phase is independently testable. Phase 2 end-to-end milestone: DingTalk voice → Brain → Design task → Result back to DingTalk. External agents (Phase 4) add after foundation works.
