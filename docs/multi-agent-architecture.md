# Multi-Agent Orchestration Architecture

## 1. Overview

Design a multi-agent system where a **Brain Agent** receives user requests via DingTalk (voice or text), decomposes tasks, and dispatches to four specialized agents:

| Agent | Role | Execution Environment |
|-------|------|----------------------|
| **Brain** | Intent parsing, task decomposition, routing, status tracking | Hermes Agent (dingtalk gateway) |
| **Design** | Architecture planning, tech docs, PRD generation | 专业设计工具 (Claude Opus via API, or local LLM) |
| **Dev** | Code implementation, feature development | Claude Code (CLI, `claude -p`) |
| **Validate** | Code review, test execution, security audit | OpenCode / Codex (GitHub API or CLI) |
| **Deploy** | CI/CD pipeline, packaging, release | Claude Code (CLI, `claude -p`) |

Agents run as independent processes. Communication happens via a shared **MCP Coordinator Server**.

## 2. Existing Foundation (What Hermes Already Provides)

### 2.1 DingTalk Gateway
`gateway/platforms/dingtalk.py` — Stream-mode DingTalk adapter already in Hermes. Supports:
- Text messages
- Voice/audio (via DingTalk media API → local file download)
- Group @mentions with configurable wake words
- Markdown card responses (AI Cards for rich output)

Config:
```yaml
platforms:
  dingtalk:
    enabled: true
    extra:
      client_id: "DINGTALK_CLIENT_ID"
      client_secret: "DINGTALK_CLIENT_SECRET"
```

### 2.2 Voice Transcription
Hermes already has `faster-whisper` for local STT (in `.[voice]` extra). Voice messages from DingTalk can be transcribed in-gateway before passing to Brain Agent.

### 2.3 Subagent Delegation
`tools/delegate_tool.py` — Hermes can spawn isolated child `AIAgent` instances with:
- Restricted toolsets
- Fresh conversation context
- Parent-blocking execution
- Depth control (default: 1 level, configurable to 3)

### 2.4 MCP Server
`mcp_serve.py` — Hermes already exposes messaging conversations as MCP tools:
- `conversations_list`, `messages_read`, `messages_send`
- `events_poll`, `events_wait` (long-polling for real-time delivery)
- `channels_list`

This proves the pattern: Hermes can serve as an MCP stdio server for any client.

### 2.5 Skills System
`skills/` directory — procedural skill definitions that agents can invoke. Existing relevant skills:
- `software-development/` — dev workflows
- `devops/` — deployment
- `diagramming/` — design artifacts
- `autonomous-ai-agents/` — multi-agent patterns

### 2.6 Task State Storage
`~/.hermes/sessions/` — SQLite-backed session storage with JSON index. `mcp_serve.py` uses `EventBridge` for event polling. We extend this pattern for task tracking.

## 3. Architecture

### 3.1 High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User (DingTalk)                             │
│   [Voice msg] ──┐                                                   │
│   [Text msg] ───┼──► DingTalk Stream Gateway                       │
│                 │       (gateway/platforms/dingtalk.py)             │
│                 │       ↓                                           │
│                 │   Voice → Whisper STT (optional)                  │
│                 │       ↓                                           │
│                 │   Brain Agent (Hermes AIAgent)                    │
│                 │       ↓                                           │
│                 │   Task Router                                     │
│                 │   ┌──────────┬──────────┬──────────┬──────────┐   │
│                 │   │ Design   │ Dev      │ Validate │ Deploy   │   │
│                 │   │ Agent    │ Agent    │ Agent    │ Agent    │   │
│                 │   │(Claude O)│(C.Code)  │(Codex)   │(C.Code)  │   │
│                 │   └────┬─────┴────┬─────┴────┬─────┴────┬─────┘   │
│                 │        │          │          │          │          │
│                 └────────┼──────────┼──────────┼──────────┼─────────┘
│                          │          │          │          │
│                    ┌─────┴──────────┴──────────┴──────────┴─────┐   │
│                    │         MCP Coordinator Server              │   │
│                    │  (FastAPI + HTTP-SSE, port 9100)            │   │
│                    │                                             │   │
│                    │  Tools:                                     │   │
│                    │    create_task / get_task_status            │   │
│                    │    submit_result / get_artifacts            │   │
│                    │    list_tasks / cancel_task                 │   │
│                    │                                             │   │
│                    │  State: SQLite (~/.hermes/tasks.db)         │   │
│                    │  Events: SSE for real-time push             │   │
│                    └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Communication Protocol: MCP + HTTP-SSE

**Why not filesystem?** Latency, no cross-machine support, no event push.
**Why not Redis?** External dependency,运维成本, overkill for this use case.
**Why MCP + SSE?**
- Hermes already uses MCP (`mcp` SDK, stdio transport)
- Claude Code supports MCP natively (via `~/.claude/settings.json` or `claude_desktop_config.json`)
- Any agent that can make HTTP requests can participate
- SSE provides real-time push without polling

**Two transport layers:**

| Layer | Purpose | Transport |
|-------|---------|-----------|
| MCP (stdio) | Agent ↔ MCP Coordinator (same machine) | stdio pipes |
| HTTP-SSE (network) | Remote agents, DingTalk gateway, Codex | HTTP + Server-Sent Events |

### 3.3 Task Data Model

```python
# tasks.db (SQLite)
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,           -- UUID
    parent_id   TEXT,                       -- parent task (for nesting)
    type        TEXT NOT NULL,              -- design | dev | validate | deploy
    status      TEXT NOT NULL,              -- pending | running | completed | failed | cancelled
    title       TEXT NOT NULL,
    description TEXT NOT NULL,              -- full prompt for the agent
    artifacts   TEXT,                       -- JSON: output files, PR URLs, etc.
    error       TEXT,                       -- error message if failed
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at  TIMESTAMP,
    completed_at TIMESTAMP,
    assigned_to TEXT,                       -- agent identifier
    metadata    TEXT                        -- JSON: git_repo, branch, model, etc.
);

CREATE TABLE task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    type        TEXT NOT NULL,              -- created | started | progress | completed | failed
    data        TEXT,                       -- JSON payload
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 4. Agent Specifications

### 4.1 Brain Agent (Hermes)

**Location:** Runs inside Hermes AIAgent loop (`run_agent.py:AIAgent`).
**Trigger:** New DingTalk message → gateway → Brain Agent conversation.

**Responsibilities:**
1. Parse user intent (text or transcribed voice)
2. Decompose into task DAG (directed acyclic graph)
3. Create tasks via `create_task` MCP tool
4. Monitor task completion via `events_wait`
5. Aggregate results and respond to user via DingTalk

**Flow:**
```
User: "帮我开发一个用户管理系统，支持CRUD操作"
  ↓
Brain parses intent:
  - Design task: "设计用户管理系统架构，定义数据模型和API接口"
  - Dev task: "实现用户管理系统代码" (depends on Design)
  - Validate task: "审查代码并运行测试" (depends on Dev)
  - Deploy task: "打包并发布" (depends on Validate)
  ↓
Brain creates 4 tasks with dependency chain
  ↓
Brain waits for completion, reports progress to user
  ↓
DingTalk response: "任务已创建，设计阶段进行中..."
```

**Implementation:** Custom Hermes skill (`skills/brain-orchestrator/`) + tool handler registered via `tools/registry.py`.

### 4.2 Design Agent

**Environment:** Claude Opus via API (highest reasoning quality for architecture).
**Input:** Task description from MCP Coordinator.
**Output:** Markdown documents (PRD, architecture, system design).

**Execution:**
```bash
# Option A: Claude API direct call
curl -X POST https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-opus-4-7-20250514",
    "max_tokens": 8000,
    "system": "You are a senior software architect. Output design docs in markdown.",
    "messages": [{"role": "user", "content": "<task_description>"}]
  }'
```

**Output artifacts written to:**
```
~/.hermes/tasks/{task_id}/artifacts/
  ├── prd.md
  ├── architecture.md
  └── system_design.md
```

**Result submitted via:** `submit_result` MCP tool with artifact paths.

### 4.3 Dev Agent (Claude Code)

**Environment:** Claude Code CLI, non-interactive mode.
**Input:** Design artifacts + task description.
**Output:** Code changes in a git repository.

**Execution:**
```bash
cd /path/to/repo
claude -p "
  Implement the feature described below. Follow existing code style.
  Create tests. Commit changes.

  Design docs:
  $(cat ~/.hermes/tasks/{task_id}/artifacts/prd.md)
  $(cat ~/.hermes/tasks/{task_id}/artifacts/architecture.md)

  Task: <task_description>
"
```

**Post-execution:**
1. Read git diff to confirm changes
2. Run project tests (if `test_command` in metadata)
3. Submit result with commit SHA, test results, changed files

**MCP Integration:** Claude Code connects to MCP Coordinator via `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "coordinator": {
      "command": "curl",
      "args": ["-X", "POST", "http://localhost:9100/mcp", "-d", "@-"],
      "transport": "http"
    }
  }
}
```

### 4.4 Validate Agent (OpenCode / Codex)

**Environment:** GitHub Codex CLI or OpenCode.
**Input:** Git branch with Dev Agent changes.
**Output:** Review report, test pass/fail.

**Execution (Codex):**
```bash
# Checkout the branch created by Dev Agent
git fetch origin && git checkout feature/{task_id}
# Run Codex review
codex review \
  --focus "security, correctness, test coverage" \
  --output ~/.hermes/tasks/{task_id}/artifacts/review.md
# Run tests
npm test 2>&1 | tee ~/.hermes/tasks/{task_id}/artifacts/test_output.log
```

**Result:** Review markdown + test logs → `submit_result`.

### 4.5 Deploy Agent (Claude Code)

**Environment:** Claude Code CLI.
**Input:** Validated code branch.
**Output:** Deployed artifact (Docker image, release tag, etc.).

**Execution:**
```bash
cd /path/to/repo
git checkout feature/{task_id}
claude -p "
  Deploy this branch. Build Docker image, push to registry.
  Create GitHub release if tests pass.
  Report the deployment URL and version.
"
```

## 5. MCP Coordinator Server

### 5.1 Location & Tech

**Path:** `coordinator/` (new directory in hermes-agent repo)
**Tech:** FastAPI + SQLite + SSE
**Port:** 9100 (configurable)
**Dependencies:** `fastapi`, `uvicorn`, `aiosqlite` (already in `.[all]`)

### 5.2 API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tasks` | POST | Create a new task |
| `/tasks/{id}` | GET | Get task status + details |
| `/tasks/{id}` | PATCH | Update task status / submit result |
| `/tasks` | GET | List all tasks (with filters) |
| `/tasks/{id}/events` | GET (SSE) | Subscribe to task events |
| `/tasks/{id}/artifacts` | GET | Download task artifacts |

### 5.3 MCP Tools (exposed via stdio)

| Tool | Description | Parameters |
|------|-------------|------------|
| `create_task` | Create a new task | `type`, `title`, `description`, `depends_on?`, `metadata?` |
| `get_task` | Get task details | `task_id` |
| `list_tasks` | List tasks with filters | `status?`, `type?` |
| `submit_result` | Submit task completion | `task_id`, `artifacts`, `status` |
| `cancel_task` | Cancel a pending task | `task_id` |

### 5.4 Event Flow

```
Brain creates task via create_task
  → MCP Coordinator writes to tasks.db (status: pending)
  → SSE event: {type: "created", task_id: "..."}

Dev Agent polls /task/{id} or receives SSE event
  → Updates status to "running" via submit_result
  → SSE event: {type: "started", task_id: "..."}

Dev Agent finishes
  → Submits result with artifacts
  → SSE event: {type: "completed", task_id: "...", artifacts: [...]}

Brain receives SSE event
  → Updates internal state
  → Triggers next dependent task
  → Sends progress update to DingTalk user
```

## 6. Implementation Plan

### Phase 1: MCP Coordinator Server (Week 1)
- [ ] Create `coordinator/` directory
- [ ] Implement FastAPI server with SQLite backend
- [ ] Add SSE event stream
- [ ] Implement MCP stdio server (wrap HTTP API)
- [ ] Write integration tests

### Phase 2: Brain Agent + DingTalk Integration (Week 2)
- [ ] Create `skills/brain-orchestrator/` skill definition
- [ ] Implement task decomposition logic in Hermes
- [ ] Register `orchestrate` tool via `tools/registry.py`
- [ ] Wire DingTalk voice → Whisper STT → Brain Agent
- [ ] Test: voice command → task creation → DingTalk response

### Phase 3: Dev Agent Integration (Week 3)
- [ ] Claude Code non-interactive invocation wrapper
- [ ] Git branch management (create branch per task)
- [ ] Artifact collection + result submission
- [ ] Test: text command → Dev Agent → code committed

### Phase 4: Validate + Deploy Agents (Week 4)
- [ ] Codex/OpenCode integration
- [ ] Test runner wiring
- [ ] Deploy Agent with CI/CD triggers
- [ ] End-to-end test: voice → design → dev → validate → deploy

### Phase 5: Error Handling & Observability (Week 5)
- [ ] Timeout handling (task stalls)
- [ ] Retry logic for failed tasks
- [ ] Progress streaming to DingTalk (AI Cards with live updates)
- [ ] Audit log for all task transitions

## 7. Security Considerations

| Risk | Mitigation |
|------|-----------|
| DingTalk message spoofing | Stream-mode SDK validates signatures |
| Agent code execution | Claude Code runs in isolated workspace / Docker |
| Task injection | Brain Agent validates task type against allowlist |
| MCP Server auth | Localhost only; optional API key for remote access |
| Secret exposure | Agent prompts exclude credential env vars; use `.env` scoping |
| Git credential leakage | Each task uses isolated git worktree; no shared credentials |

## 8. Failure Modes & Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Design Agent timeout (>5 min) | Task status stuck "running" | Cancel, retry with simpler prompt |
| Dev Agent code breaks build | Test results fail | Report to Brain, request fix iteration |
| Validate Agent finds critical issues | Review marks CRITICAL | Route back to Dev Agent with feedback |
| Deploy Agent fails | CI pipeline error | Report to Brain, rollback if needed |
| DingTalk message lost | No response within 30s | Gateway retry (Stream-mode auto-reconnects) |
| MCP Coordinator crashes | HTTP 500 / connection refused | SQLite file survives; restart process |

## 9. Config Example

```yaml
# ~/.hermes/config.yaml — orchestrator section
orchestrator:
  enabled: true
  coordinator_url: "http://localhost:9100"
  agents:
    design:
      provider: "anthropic"
      model: "claude-opus-4-7-20250514"
      max_tokens: 8000
      timeout_seconds: 300
    dev:
      runtime: "claude-code"
      command: "claude"
      timeout_seconds: 1800
      git_base_branch: "main"
    validate:
      runtime: "codex"
      command: "codex"
      timeout_seconds: 600
    deploy:
      runtime: "claude-code"
      command: "claude"
      timeout_seconds: 1800
  dingtalk:
    voice_stt: "whisper"        # or "azure", "local"
    auto_respond: true           # auto-reply with progress
    max_concurrent_tasks: 3
```

## 10. File Map (New Files)

```
hermes-agent/
├── coordinator/
│   ├── __init__.py
│   ├── server.py              # FastAPI + SSE
│   ├── mcp_server.py          # MCP stdio wrapper
│   ├── models.py              # Pydantic task models
│   ├── db.py                  # SQLite CRUD
│   └── events.py              # SSE event broadcaster
├── tools/
│   └── orchestrate_tool.py    # Hermes tool for task dispatch
├── skills/
│   └── brain-orchestrator/
│       ├── skill.yaml           # Skill definition
│       └── prompt.md            # Brain Agent system prompt
├── scripts/
│   └── run_coordinator.sh     # Coordinator startup script
└── tests/
    └── coordinator/
        ├── test_server.py
        ├── test_events.py
        └── test_integration.py
```
