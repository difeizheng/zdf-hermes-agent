"""Orchestrator plugin entry point.

Registers:
- `orchestrate` tool via ctx.register_tool()
- pre_gateway_dispatch hook for DingTalk routing
- /orchestrator slash command for manual task management
"""

from __future__ import annotations

import logging
from pathlib import Path
from plugins.orchestrator.config import load_orchestrator_config
from plugins.orchestrator.tools import register_tools

logger = logging.getLogger(__name__)

_BRAIN_ORCHESTRATOR_PROMPT: str | None = None


def _load_brain_prompt() -> str:
    """Load the brain-orchestrator skill prompt (cached)."""
    global _BRAIN_ORCHESTRATOR_PROMPT
    if _BRAIN_ORCHESTRATOR_PROMPT is not None:
        return _BRAIN_ORCHESTRATOR_PROMPT
    try:
        # Walk up from this file to project root, then into skills/
        project_root = Path(__file__).resolve().parent.parent.parent
        prompt_path = project_root / "skills" / "brain-orchestrator" / "prompt.md"
        if prompt_path.exists():
            _BRAIN_ORCHESTRATOR_PROMPT = prompt_path.read_text(encoding="utf-8")
        else:
            _BRAIN_ORCHESTRATOR_PROMPT = ""
            logger.warning("brain-orchestrator prompt not found at %s", prompt_path)
    except Exception as e:
        _BRAIN_ORCHESTRATOR_PROMPT = ""
        logger.warning("Failed to load brain-orchestrator prompt: %s", e)
    return _BRAIN_ORCHESTRATOR_PROMPT

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
    """Route DingTalk messages into Brain mode when orchestrator is enabled.

    Handles both text and voice messages. For voice messages, the text may
    be empty at this point (STT runs later in the gateway). In that case
    we still route to the Brain Agent with a voice-specific prompt — the
    gateway's STT transcription will be prepended to the message before
    the agent sees it, so the Brain will receive the transcribed text.
    """
    source = event.source
    platform = source.platform.value if source.platform else "unknown"
    text = event.text or ""
    chat_id = getattr(source, "chat_id", None) or ""
    msg_type = getattr(event, "message_type", None)
    logger.info("orchestrator hook fired: platform=%s, chat=%s, type=%s, text=%r", platform, chat_id, getattr(msg_type, 'value', msg_type), text[:80])

    if platform != "dingtalk":
        logger.debug("orchestrator: not dingtalk, skipping")
        return None

    is_voice = (getattr(msg_type, 'value', None) == "voice") if msg_type else False

    if text.startswith("/task ") or _looks_like_task(text) or (is_voice and not text):
        prompt = _load_brain_prompt()

        # Stash chat context so the orchestrate tool can inject chat_id into
        # newly-created tasks. progress_watcher reads this to push updates back.
        try:
            from plugins.orchestrator.tools import set_chat_context
            set_chat_context(platform, chat_id)
        except Exception as e:
            logger.debug("Failed to set chat context: %s", e)

        # Fetch recent task history from coordinator (more useful than chat history)
        history_lines = []
        try:
            history_lines = _get_recent_task_history(limit=5)
        except Exception as e:
            logger.warning("Failed to fetch recent task history: %s", e)

        history_block = ""
        if history_lines:
            history_block = (
                "\n## Recent Tasks (from coordinator)\n"
                + "\n".join(history_lines)
                + "\n\n(Use the tasks above for context. If the user refers to a previous task, "
                "use `orchestrate` action=status with the task_id to get details.)\n"
            )

        # For voice messages: user text will be STT transcription prepended by
        # the gateway. Tell Brain to use that transcription as the request.
        voice_instruction = ""
        if is_voice and not text:
            voice_instruction = (
                "\n## Voice Message\n"
                "The user sent a voice message. The speech-to-text transcription will appear\n"
                "BEFORE this prompt as: '[The user sent a voice message~ Here's what they said: \"...\"]'.\n"
                "Use the transcribed text as the user's request for task decomposition.\n"
                "If the transcription says it couldn't understand, reply politely asking the user\n"
                "to send a text message instead.\n"
            )

        logger.info(
            "Task detected (voice=%s), returning rewrite action (prompt_len=%d, history=%d lines)",
            is_voice, len(prompt), len(history_lines),
        )
        return {
            "action": "rewrite",
            "text": (
                f"You are the Brain orchestrator. You MUST follow ALL instructions below.\n\n"
                f"{prompt}\n"
                f"{voice_instruction}"
                f"{history_block}"
                f"---\nUser request: {text if text else '(voice message — see transcription above)'}\n\n"
                f"Use the `orchestrate` tool with action=create to handle this request. "
                f"When creating tasks via the orchestrate tool, ALWAYS use the "
                f"same language as the user's message for the task title and description. "
                f"If the user writes in Chinese, all title and description fields MUST be in Chinese."
            ),
        }
    logger.debug("orchestrator: no task keyword in %r", text[:50])
    return None


def _get_recent_task_history(limit: int = 5) -> list[str]:
    """Fetch recent tasks from the coordinator for multi-turn context.

    Returns a list of formatted strings describing recent tasks, e.g.
    "design <task_id[:8]> 'User Management Design' (status: completed)".

    The Brain uses this to understand context like:
    - "刚才那个任务" → the most recent task in 'running' or 'completed' state
    - "再加一个功能" → continue from the last successful dev task
    """
    import httpx
    from plugins.orchestrator.config import load_orchestrator_config

    cfg = load_orchestrator_config()
    url = cfg.get("coordinator_url", "http://localhost:9100")
    lines: list[str] = []
    try:
        with httpx.Client(timeout=10.0) as client:
            # Fetch all tasks; coordinator returns most-recent first
            resp = client.get(f"{url}/tasks")
            resp.raise_for_status()
            tasks = resp.json()
    except Exception:
        return lines

    for t in tasks[:limit]:
        task_id = str(t.get("id", ""))[:8]
        task_type = t.get("type", "unknown")
        title = t.get("title", "")
        status = t.get("status", "unknown")
        if title and task_id:
            lines.append(f"{task_type} [{task_id}] '{title}' (status: {status})")
    return lines


def _looks_like_task(text: str) -> bool:
    """Heuristic: detect development task-like requests in natural language.

    Uses multi-word patterns to avoid false positives from broad keywords
    like "创建" (which matches "创建一个会议") or "build" (which matches
    "build a team"). Requires a verb + object compound that signals software
    development intent.

    Patterns are layered from most-precise to most-permissive:
    1. Tight adjacent-object pattern (highest precision)
    2. Adjacent-with-adjective pattern (e.g. "new", "simple")
    3. Object-elsewhere pattern (catches "build a backend for the project")
    4. Imperative at sentence start (catches "Build X", "Let's develop Y")
    """
    import re
    lower = text.lower().strip()

    # Chinese compound patterns: verb + measure/object
    cn_patterns = [
        r"开发\s*(一个|一款|一套|一个|那个|这)",
        r"实现\s*(一个|功能|系统|模块|服务|接口|api)",
        r"构建\s*(项目|应用|服务|系统|微服务)",
        r"部署\s*(到|服务|应用|项目|系统|这个)",
        r"设计\s*(一个.*?(系统|服务|架构|接口|api|应用))",
        r"写\s*(一个|一段|一个.*?代码|个.*?功能)",
        r"帮我\s*(开发|实现|构建|部署|设计|写).*?(系统|应用|服务|功能|项目|模块)",
    ]
    for pat in cn_patterns:
        if re.search(pat, lower):
            return True

    # English patterns — layered precision/recall tradeoff.
    # All tiers require a software-object noun (feature, app, system, etc.)
    # within the same sentence. This prevents false positives like
    # "build a team" or "create a logo" while catching real dev requests
    # like "build a new feature" or "build a backend for the project".
    _sw_objects = (
        r"(?:feature|app|application|system|service|project|module|api|component|"
        r"microservice|backend|frontend|server|client|website|web\s+app|tool|library|"
        r"plugin|script|function|endpoint|page|dashboard|bot|cli|architecture|"
        r"schema|database|table|model|route|controller|middleware|stack|pipeline)"
    )

    en_patterns = [
        # Tier 1: tight adjacent object (highest precision)
        # "build a feature", "design the architecture"
        rf"(?:develop|build|implement|create|design|deploy|write|code)\s+"
        rf"(?:a |an |the )?{_sw_objects}\b",

        # Tier 2: object preceded by adjective OR adjective + compound modifier
        # "build a new feature", "create a simple web app", "design a clean API"
        rf"(?:develop|build|implement|create|design|deploy|write|code)\s+"
        rf"(?:a |an |the )?(?:new |simple |clean |small |large |basic |full |quick )?"
        rf"(?:web\s+|rest\s+|graphql\s+)?{_sw_objects}\b",

        # Tier 3: verb + article + noun + connector + object
        # "build a backend for the project", "create an API for users"
        rf"(?:develop|build|implement|create|design|deploy|write)\s+"
        rf"(?:a |an |the )?\w+\s+(?:for|of|to|with|using|in)\s+(?:the |a |an |my |our )?"
        rf"{_sw_objects}\b",
    ]
    for pat in en_patterns:
        if re.search(pat, lower):
            return True

    return False


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
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{url}/tasks/{argv[1]}")
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
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(f"{url}/tasks", params=params)
                resp.raise_for_status()
                tasks = resp.json()
            if not tasks:
                return "No tasks found."
            lines = []
            for t in tasks[:10]:
                lines.append(f"  [{t['id'][:8]}] {t['status']:10s} {t['type']:10s} {t['title']}")
            return "\n".join(lines)

        elif action == "cancel" and len(argv) >= 2:
            with httpx.Client(timeout=10.0) as client:
                resp = client.patch(
                    f"{url}/tasks/{argv[1]}",
                    json={"status": "cancelled"},
                )
                resp.raise_for_status()
            return f"Task {argv[1]} cancelled."

        else:
            return f"Unknown action: {action}. Use status, list, or cancel."

    except Exception as e:
        return f"Orchestrator error: {e}"
