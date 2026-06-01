"""Orchestrator tool — registered via ctx.register_tool().

Allows the Brain Agent to create, list, check status, and cancel tasks
through the coordinator HTTP API.

When the tool creates a task from a gateway-originated request, the
pre_gateway_dispatch hook has stored the chat_id in a thread-safe keyed
store. The tool then auto-injects that chat_id into the task's metadata
so the progress_watcher can push updates back to the user.

The store uses chat_id as key (not a single slot) to prevent concurrent
requests from different chats from overwriting each other's context.
"""

from __future__ import annotations

import threading
import time

import httpx

from plugins.orchestrator.config import load_orchestrator_config

# Thread-safe keyed store. Each chat_id gets its own entry so concurrent
# requests from different DingTalk chats don't race on a single slot.
_chat_context: dict[str, tuple[str, float]] = {}
_chat_ctx_lock = threading.Lock()


def set_chat_context(source: str, chat_id: str) -> None:
    """Store chat context for subsequent orchestrate tool calls.

    Called by the pre_gateway_dispatch hook before the Brain processes
    a DingTalk message. The tool reads this when creating tasks.

    Uses chat_id as the key (not a single slot) so that concurrent
    requests from different chats don't overwrite each other's context.
    """
    if not chat_id:
        return
    with _chat_ctx_lock:
        _chat_context[chat_id] = (f"{source}:{chat_id}", time.time())


def _get_chat_context(max_age_seconds: int = 300) -> list[dict]:
    """Read all valid chat contexts set within max_age_seconds.

    Returns a list of {"source": ..., "chat_id": ...} dicts. Empty list
    if no recent contexts. The tool iterates all of them and injects
    chat_id metadata for each unique context.
    """
    import time
    now = time.time()
    result = []
    with _chat_ctx_lock:
        # Clean up expired entries
        expired = [k for k, (_, set_at) in _chat_context.items()
                    if now - set_at > max_age_seconds]
        for k in expired:
            del _chat_context[k]
        # Return all remaining entries
        for stored, set_at in _chat_context.values():
            if ":" in stored:
                source, chat_id = stored.split(":", 1)
                result.append({"source": source, "chat_id": chat_id})
    return result


_ORCHESTRATE_SCHEMA = {
    "name": "orchestrate",
    "description": (
        "Create, list, check status, or cancel orchestrated tasks. "
        "Use action=create to decompose a request into a task DAG. "
        "STRICT chain: design → dev → validate → deploy. Deploy MUST depend on validate, not dev. "
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
                "description": (
                    "List of task IDs this task depends on. "
                    "CRITICAL: deploy MUST depend on a validate task ID (not dev or design). "
                    "Chain: design(no deps) → dev(deps=[design_id]) → validate(deps=[dev_id]) → deploy(deps=[validate_id])."
                ),
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


async def orchestrate_handler(args: dict, **kwargs) -> str:
    import json

    action = args.get("action", "")

    try:
        if action == "create":
            return await _create_task(args)
        elif action == "list":
            return await _list_tasks(args)
        elif action == "status":
            return await _get_task_status(args)
        elif action == "cancel":
            return await _cancel_task(args)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Coordinator HTTP error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Orchestrator error: {e}"})


async def _create_task(args: dict) -> str:
    import json

    task_type = args.get("type")
    title = args.get("title")
    description = args.get("description")
    depends_on = args.get("depends_on", [])
    metadata = args.get("metadata", {})

    if not all([task_type, title, description]):
        return json.dumps({"error": "type, title, and description are required for create"})

    # Enforce dependency rules: dev/validate/deploy MUST have depends_on
    if task_type in ("dev", "validate", "deploy") and not depends_on:
        return json.dumps({
            "error": f"REJECTED: {task_type} tasks MUST have a 'depends_on' field. "
            f"Chain: design(no deps) → dev(depends_on: design_id) → validate(depends_on: dev_id) → deploy(depends_on: validate_id). "
            f"Create a design task first (no depends_on), then create this {task_type} task with depends_on=[design_task_id]. "
            f"You MUST create ALL tasks in the chain before returning to the user.",
        })

    # Auto-inject chat context into metadata so progress_watcher can route
    # status updates back to the originating DingTalk chat.
    # _get_chat_context returns a list; for single-user scenarios there's
    # exactly one entry. For concurrent multi-chat, we use the first one
    # (the keyed store prevents overwrites, but the tool can't distinguish
    # which chat is "current" — a future improvement would thread chat_id
    # through the tool call kwargs).
    chat_ctxs = _get_chat_context()
    if chat_ctxs and "chat_id" not in metadata:
        ctx = chat_ctxs[0]  # most recent valid entry
        metadata = {**metadata, "chat_id": ctx["chat_id"], "source": ctx["source"]}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # HARD constraint: deploy MUST depend on validate (not dev or design).
        # Validate must review code before deploy ships it.
        if task_type == "deploy":
            for dep_id in depends_on:
                dep_resp = await client.get(f"{_coordinator_url()}/tasks/{dep_id}")
                dep_resp.raise_for_status()
                dep = dep_resp.json()
                if dep.get("type") != "validate":
                    return json.dumps({
                        "error": f"REJECTED: deploy task MUST depend on a validate task. "
                        f"Task {dep_id} is type={dep.get('type')!r}, not validate. "
                        f"Correct chain: design → dev → validate → deploy(deps=[validate_id]). "
                        f"Create a validate task that depends on dev, then create deploy with depends_on=[validate_id].",
                    })

        resp = await client.post(
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


async def _list_tasks(args: dict) -> str:
    status = args.get("status")
    task_type = args.get("type")
    params = {}
    if status:
        params["status"] = status
    if task_type:
        params["type"] = task_type

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{_coordinator_url()}/tasks", params=params)
        resp.raise_for_status()
        return resp.text


async def _get_task_status(args: dict) -> str:
    task_id = args.get("task_id")
    if not task_id:
        import json
        return json.dumps({"error": "task_id is required for status"})

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{_coordinator_url()}/tasks/{task_id}")
        resp.raise_for_status()
        return resp.text


async def _cancel_task(args: dict) -> str:
    task_id = args.get("task_id")
    if not task_id:
        import json
        return json.dumps({"error": "task_id is required for cancel"})

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(
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
        is_async=True,
        description="Create and manage multi-agent orchestrated tasks.",
    )
