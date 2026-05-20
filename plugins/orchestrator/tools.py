"""Orchestrator tool — registered via ctx.register_tool().

Allows the Brain Agent to create, list, check status, and cancel tasks
through the coordinator HTTP API.
"""

from __future__ import annotations

import httpx

from plugins.orchestrator.config import load_orchestrator_config

_ORCHESTRATE_SCHEMA = {
    "name": "orchestrate",
    "description": (
        "Create, list, check status, or cancel orchestrated tasks. "
        "Use action=create to decompose a request into a task DAG. "
        "Use action=list to see all tasks. "
        "Use action=status to check a specific task. "
        "Use action=cancel to cancel a pending task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "status", "cancel"],
                "description": "Action to perform.",
            },
            "type": {
                "type": "string",
                "enum": ["design", "dev", "validate", "deploy"],
                "description": "Task type. Required for action=create.",
            },
            "title": {
                "type": "string",
                "description": "Task title. Required for action=create.",
            },
            "description": {
                "type": "string",
                "description": "Full prompt for the agent. Required for action=create.",
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of task IDs this task depends on.",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID. Required for action=status and action=cancel.",
            },
            "metadata": {
                "type": "object",
                "description": "Optional metadata (git_repo, branch, test_command, etc.).",
            },
        },
        "required": ["action"],
    },
}


def _coordinator_url() -> str:
    cfg = load_orchestrator_config()
    return cfg.get("coordinator_url", "http://localhost:9100")


def orchestrate_handler(args: dict, **kwargs) -> str:
    import json

    action = args.get("action", "")

    try:
        if action == "create":
            return _create_task(args)
        elif action == "list":
            return _list_tasks(args)
        elif action == "status":
            return _get_task_status(args)
        elif action == "cancel":
            return _cancel_task(args)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Coordinator HTTP error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Orchestrator error: {e}"})


def _create_task(args: dict) -> str:
    import json

    task_type = args.get("type")
    title = args.get("title")
    description = args.get("description")
    depends_on = args.get("depends_on", [])
    metadata = args.get("metadata", {})

    if not all([task_type, title, description]):
        return json.dumps({"error": "type, title, and description are required for create"})

    resp = httpx.post(
        f"{_coordinator_url()}/tasks",
        json={
            "type": task_type,
            "title": title,
            "description": description,
            "depends_on": depends_on,
            "metadata": metadata,
        },
    )
    resp.raise_for_status()
    return resp.text


def _list_tasks(args: dict) -> str:
    status = args.get("status")
    task_type = args.get("type")
    params = {}
    if status:
        params["status"] = status
    if task_type:
        params["type"] = task_type

    resp = httpx.get(f"{_coordinator_url()}/tasks", params=params)
    resp.raise_for_status()
    return resp.text


def _get_task_status(args: dict) -> str:
    task_id = args.get("task_id")
    if not task_id:
        import json
        return json.dumps({"error": "task_id is required for status"})

    resp = httpx.get(f"{_coordinator_url()}/tasks/{task_id}")
    resp.raise_for_status()
    return resp.text


def _cancel_task(args: dict) -> str:
    task_id = args.get("task_id")
    if not task_id:
        import json
        return json.dumps({"error": "task_id is required for cancel"})

    resp = httpx.patch(
        f"{_coordinator_url()}/tasks/{task_id}",
        json={"status": "cancelled"},
    )
    resp.raise_for_status()
    return resp.text


def register_tools(ctx) -> None:
    ctx.register_tool(
        name="orchestrate",
        toolset="orchestrator",
        schema=_ORCHESTRATE_SCHEMA,
        handler=orchestrate_handler,
        is_async=False,
        description="Create and manage multi-agent orchestrated tasks.",
    )
