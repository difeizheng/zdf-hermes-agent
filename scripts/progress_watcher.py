#!/usr/bin/env python3
"""Progress Watcher — subscribes to coordinator SSE, pushes progress to DingTalk.

Long-lived process that:
1. Opens SSE connection to the coordinator's /tasks/events endpoint
2. For each event, looks up the task's `chat_id` from its metadata
3. Sends a formatted progress message to that DingTalk chat via the gateway

The chat_id is set by the Brain Agent when it creates tasks via the
orchestrate tool. Without chat_id, the event is logged but not sent.

Important: This process must be able to import the gateway's DingTalk
platform. If running in a different venv or without gateway deps, it
will fall back to logging-only mode.

Usage:
    python scripts/progress_watcher.py [--coordinator-url http://localhost:9100]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [progress-watcher] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# -- DingTalk sender abstraction -------------------------------------------

class _DingTalkSender:
    """Lazy-init wrapper around the DingTalk platform's send() method.

    If the gateway can't be imported (e.g., missing deps), falls back to
    logging-only mode so the watcher never crashes.
    """

    def __init__(self) -> None:
        self._platform = None
        self._init_attempted = False
        self._available = False

    def _try_init(self) -> None:
        if self._init_attempted:
            return
        self._init_attempted = True
        try:
            from gateway.platforms.dingtalk import DingTalkPlatform
            from plugins.orchestrator.config import load_orchestrator_config
            import yaml

            cfg_path = Path.home() / ".hermes" / "config.yaml"
            if not cfg_path.exists():
                logger.warning("Config not found at %s, watcher in log-only mode", cfg_path)
                return

            with open(cfg_path, encoding="utf-8-sig") as f:
                raw = yaml.safe_load(f) or {}
            dingtalk_cfg = raw.get("platforms", {}).get("dingtalk", {})
            extra = dingtalk_cfg.get("extra", {})

            client_id = os.environ.get("DINGTALK_CLIENT_ID") or extra.get("client_id", "")
            client_secret = os.environ.get("DINGTALK_CLIENT_SECRET") or extra.get("client_secret", "")
            if not client_id or not client_secret:
                logger.warning("DingTalk credentials not found, watcher in log-only mode")
                return

            self._platform = DingTalkPlatform(
                name="progress-watcher",
                config={"client_id": client_id, "client_secret": client_secret},
            )
            self._available = True
            logger.info("DingTalk platform initialized for progress watcher")
        except Exception as e:
            logger.warning("Failed to init DingTalk platform: %s — log-only mode", e)

    async def send(self, chat_id: str, text: str) -> bool:
        self._try_init()
        if not self._available or self._platform is None:
            logger.info("[log-only] would send to %s: %s", chat_id, text[:80])
            return False
        try:
            result = await self._platform.send(chat_id, text)
            if not getattr(result, "success", False):
                logger.warning("DingTalk send failed: %s", getattr(result, "error", "unknown"))
                return False
            return True
        except Exception as e:
            logger.warning("DingTalk send exception: %s", e)
            return False


_sender = _DingTalkSender()


# -- Coordinator client ----------------------------------------------------

async def _fetch_task_meta(coordinator_url: str, task_id: str) -> dict:
    """Fetch a task's metadata (including chat_id) from the coordinator."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        if resp.status_code != 200:
            return {}
        return resp.json()
    except Exception as e:
        logger.warning("Failed to fetch task %s: %s", task_id[:8], e)
        return {}


# -- Event formatting ------------------------------------------------------

# Map of task_type → human-readable Chinese label
_TYPE_LABELS_CN = {
    "design": "设计",
    "dev": "开发",
    "validate": "验证",
    "deploy": "部署",
}

_STATUS_LABELS_CN = {
    "pending": "等待中",
    "running": "进行中",
    "completed": "已完成",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已取消",
}


# -- N8: progress streaming state -------------------------------------------

class _ProgressStreamState:
    """Tracks byte offsets and chat routing for per-task progress logs.

    Each running task has a progress.log file that the agent appends to.
    The watcher periodically polls each registered task's progress.log
    and forwards new lines to DingTalk.
    """

    def __init__(self) -> None:
        # task_id → {"chat_id": str, "byte_offset": int, "last_flush": float}
        self._tasks: dict[str, dict] = {}

    def register(self, task_id: str, chat_id: str) -> None:
        """Start tracking a task for progress streaming."""
        if task_id in self._tasks:
            return
        self._tasks[task_id] = {
            "chat_id": chat_id,
            "byte_offset": 0,
            "last_flush": 0.0,
        }

    def unregister(self, task_id: str) -> None:
        """Stop tracking a task."""
        self._tasks.pop(task_id, None)

    def flush_remaining(self, task_id: str, chat_id: str) -> None:
        """Read any remaining progress and send it. Called on task end."""
        state = self._tasks.get(task_id)
        if not state:
            return
        try:
            from coordinator.progress import read_progress_since
            content, _ = read_progress_since(task_id, state["byte_offset"])
            if content.strip():
                # Send a final progress line so the user knows it ended
                tail = content.strip().splitlines()[-3:]  # last 3 lines
                msg = f"📝 最后进度:\n" + "\n".join(tail)
                asyncio.create_task(_sender.send(chat_id, msg))
        except Exception as e:
            logger.debug("flush_remaining error: %s", e)

    def active_task_ids(self) -> list[str]:
        return list(self._tasks.keys())


_stream_state = _ProgressStreamState()


async def _stream_progress_loop() -> None:
    """Background task: poll all active progress.logs every 5s, forward new lines."""
    try:
        while True:
            await asyncio.sleep(5)
            for task_id, state in list(_stream_state._tasks.items()):
                try:
                    await _emit_new_progress(task_id, state)
                except Exception as e:
                    logger.debug("Stream emit error for %s: %s", task_id[:8], e)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Stream loop crashed")


async def _emit_new_progress(task_id: str, state: dict) -> None:
    """Read new bytes from a task's progress.log and forward to DingTalk.

    Batches up to 3 lines per poll to avoid spamming the chat.
    """
    from coordinator.progress import read_progress_since
    content, new_offset = read_progress_since(task_id, state["byte_offset"])
    if not content:
        return
    state["byte_offset"] = new_offset

    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return
    # Only forward the most recent few lines to avoid spam
    sample = lines[-3:]
    chat_id = state["chat_id"]
    msg = "📝 进度更新:\n" + "\n".join(sample)
    await _sender.send(chat_id, msg)


def _format_event(event_type: str, task: dict) -> str | None:
    """Return user-facing message for an event, or None if event is uninteresting.

    Filters to: started, completed, failed, timeout (skip intermediate progress).
    """
    task_type = task.get("type", "")
    task_id = str(task.get("id", ""))[:8]
    title = task.get("title", "")
    label = _TYPE_LABELS_CN.get(task_type, task_type)

    if event_type == "started":
        return f"⚙️ {label} 阶段开始\n\n任务: {title}\nID: {task_id}"

    if event_type == "completed":
        return f"✅ {label} 阶段完成\n\n任务: {title}\nID: {task_id}"

    if event_type == "failed":
        error = task.get("error", "未知错误")
        return f"❌ {label} 阶段失败\n\n任务: {title}\nID: {task_id}\n错误: {error[:200]}"

    if event_type == "timeout":
        return f"⏰ {label} 阶段超时\n\n任务: {title}\nID: {task_id}"

    # Skip CREATED, PROGRESS, and other intermediate events
    return None


# -- Main loop -------------------------------------------------------------

async def run_watcher(coordinator_url: str) -> None:
    """Subscribe to coordinator SSE and dispatch progress events."""
    sse_url = f"{coordinator_url}/tasks/events"
    logger.info("Progress watcher connecting to %s", sse_url)

    import httpx
    import json

    # Start progress streaming background task
    stream_task = asyncio.create_task(_stream_progress_loop())
    logger.info("Progress streaming loop started")

    try:
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", sse_url) as resp:
                        resp.raise_for_status()
                        logger.info("SSE connected")
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(line[5:])
                            except Exception:
                                continue
                            await _handle_event(coordinator_url, payload)
            except (httpx.HTTPError, ConnectionError) as e:
                logger.warning("SSE connection lost, reconnecting in 5s: %s", e)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Watcher loop error: %s", e)
                await asyncio.sleep(5)
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass


async def _handle_event(coordinator_url: str, payload: dict) -> None:
    """Process a single SSE event."""
    event_type = payload.get("type", "")
    task_id = payload.get("task_id", "")
    if not task_id:
        return

    # Filter to user-facing events
    if event_type not in ("started", "completed", "failed", "timeout"):
        return

    # Fetch full task to get chat_id and title
    task = await _fetch_task_meta(coordinator_url, task_id)
    if not task:
        return

    chat_id = (task.get("metadata") or {}).get("chat_id", "")
    if not chat_id:
        # No chat_id means the Brain didn't tag this task — can't route back to user
        logger.debug("Task %s has no chat_id in metadata, skipping push", task_id[:8])
        return

    message = _format_event(event_type, task)
    if not message:
        return

    logger.info("Pushing %s for task %s to chat %s", event_type, task_id[:8], chat_id[:8])
    sent = await _sender.send(chat_id, message)
    if not sent:
        logger.info("(Log-only mode) %s", message[:80])

    # N8: When a task starts, register it for progress streaming
    if event_type == "started":
        _stream_state.register(task_id, chat_id)

    # When a task ends, unregister and emit any remaining progress
    if event_type in ("completed", "failed", "timeout"):
        _stream_state.flush_remaining(task_id, chat_id)
        _stream_state.unregister(task_id)

    # N7: When a deploy task finishes, send a final summary covering the whole chain
    if event_type in ("completed", "failed", "timeout") and task.get("type") == "deploy":
        try:
            summary = await _build_chain_summary(coordinator_url, task)
            if summary:
                await _sender.send(chat_id, summary)
        except Exception as e:
            logger.warning("Failed to send chain summary: %s", e)


async def _build_chain_summary(coordinator_url: str, deploy_task: dict) -> str:
    """Build a final summary message covering all tasks in the chain.

    Walks the dependency chain backwards from deploy → validate → dev → design
    and produces a single user-facing message that summarizes each stage's
    outcome and key artifacts. This compensates for the Brain Agent being
    a single-shot response — without this, the user sees the final deploy
    event but not the integrated view of the whole pipeline.
    """
    import httpx

    chat_id = (deploy_task.get("metadata") or {}).get("chat_id", "")
    if not chat_id:
        return ""

    # Collect tasks in this chain
    chain_tasks: list[dict] = [deploy_task]
    current = deploy_task
    visited = {str(deploy_task.get("id", ""))}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for _ in range(5):  # safety bound
                dep_ids = current.get("depends_on", []) or []
                if not dep_ids:
                    break
                prev_id = dep_ids[0]
                if str(prev_id) in visited:
                    break
                resp = await client.get(f"{coordinator_url}/tasks/{prev_id}")
                if resp.status_code != 200:
                    break
                current = resp.json()
                chain_tasks.append(current)
                visited.add(str(prev_id))
    except Exception as e:
        logger.debug("Chain walk error: %s", e)

    chain_tasks.reverse()  # design → dev → validate → deploy

    # Build summary
    lines = ["📋 任务执行汇总", ""]
    for t in chain_tasks:
        t_type = t.get("type", "unknown")
        label = _TYPE_LABELS_CN.get(t_type, t_type)
        status = t.get("status", "unknown")
        status_label = _STATUS_LABELS_CN.get(status, status)
        title = t.get("title", "")
        t_id = str(t.get("id", ""))[:8]

        if status == "completed":
            icon = "✅"
        elif status in ("failed", "timeout"):
            icon = "❌"
        else:
            icon = "⏳"
        lines.append(f"{icon} {label}: {title} ({status_label}, ID: {t_id})")

    # Append key artifacts from the dev and deploy tasks
    for t in chain_tasks:
        artifacts = t.get("artifacts") or {}
        if t.get("type") == "dev":
            commit = artifacts.get("commit_sha", "")
            branch = artifacts.get("branch", "")
            if commit and branch:
                lines.append(f"\n🔀 代码: 分支 `{branch}` @ `{commit[:10]}`")
        elif t.get("type") == "deploy":
            commit_sha = artifacts.get("commit_sha", "")
            if commit_sha and not any("代码" in line for line in lines):
                lines.append(f"\n🔀 已部署: `{commit_sha[:10]}`")
            # Look for deployment URL in the nested artifacts
            nested = artifacts.get("artifacts") or {}
            deploy_url = (
                nested.get("url")
                or nested.get("deployment_url")
                or nested.get("service_url")
            )
            if deploy_url:
                lines.append(f"🌐 访问: {deploy_url}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Orchestrator Progress Watcher")
    parser.add_argument(
        "--coordinator-url",
        default="http://localhost:9100",
        help="Coordinator HTTP URL (default: http://localhost:9100)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_watcher(args.coordinator_url))
    except KeyboardInterrupt:
        logger.info("Progress watcher stopped")


if __name__ == "__main__":
    main()
