"""Orchestrator plugin entry point.

Registers:
- `orchestrate` tool via ctx.register_tool()
- pre_gateway_dispatch hook for DingTalk routing
- /orchestrator slash command for manual task management
"""

from __future__ import annotations

from plugins.orchestrator.config import load_orchestrator_config
from plugins.orchestrator.tools import register_tools


def register(ctx) -> None:
    cfg = load_orchestrator_config()
    if not cfg.get("enabled", False):
        return
    register_tools(ctx)
    ctx.register_hook("pre_gateway_dispatch", _on_gateway_dispatch)
    ctx.register_command(
        "orchestrator",
        handler=_handle_slash,
        description="Manage orchestrated tasks.",
        args_hint="<status|list|cancel> [task_id]",
    )


def _on_gateway_dispatch(event, gateway, session_store, **_):
    """Route DingTalk messages into Brain mode when orchestrator is enabled."""
    if event.platform != "dingtalk":
        return None
    text = event.text or ""
    # Group @mentions or /task prefix trigger orchestration
    if text.startswith("/task ") or _looks_like_task(text):
        event.text = (
            "You are the Brain orchestrator. Use the `orchestrate` tool with "
            f"action=create to handle this request:\n\n{text}"
        )
    return None


def _looks_like_task(text: str) -> bool:
    """Heuristic: detect task-like requests in natural language."""
    lower = text.lower()
    keywords = [
        "开发", "创建", "实现", "设计", "部署", "构建",
        "develop", "create", "implement", "design", "deploy", "build",
    ]
    return any(kw in lower for kw in keywords)


def _handle_slash(raw_args: str) -> str:
    """Handle /orchestrator slash command."""
    from coordinator.models import TaskStatus
    from plugins.orchestrator.config import load_orchestrator_config
    import httpx

    cfg = load_orchestrator_config()
    url = cfg.get("coordinator_url", "http://localhost:9100")

    argv = raw_args.strip().split()
    if not argv:
        return "Usage: /orchestrator <status|list|cancel> [task_id]\n" \
               "  status <task_id>  - Get task details\n" \
               "  list [type]       - List tasks (design|dev|validate|deploy)\n" \
               "  cancel <task_id>  - Cancel a pending task"

    action = argv[0]

    try:
        if action == "status" and len(argv) >= 2:
            resp = httpx.get(f"{url}/tasks/{argv[1]}")
            resp.raise_for_status()
            t = resp.json()
            return (
                f"Task: {t['title']}\n"
                f"Type: {t['type']} | Status: {t['status']}\n"
                f"Created: {t['created_at']}\n"
                f"Description: {t['description']}"
            )

        elif action == "list":
            params = {}
            if len(argv) >= 2:
                params["type"] = argv[1]
            resp = httpx.get(f"{url}/tasks", params=params)
            resp.raise_for_status()
            tasks = resp.json()
            if not tasks:
                return "No tasks found."
            lines = []
            for t in tasks[:10]:
                lines.append(f"  [{t['id'][:8]}] {t['status']:10s} {t['type']:10s} {t['title']}")
            return "\n".join(lines)

        elif action == "cancel" and len(argv) >= 2:
            resp = httpx.patch(
                f"{url}/tasks/{argv[1]}",
                json={"status": "cancelled"},
            )
            resp.raise_for_status()
            return f"Task {argv[1]} cancelled."

        else:
            return f"Unknown action: {action}. Use status, list, or cancel."

    except Exception as e:
        return f"Orchestrator error: {e}"
