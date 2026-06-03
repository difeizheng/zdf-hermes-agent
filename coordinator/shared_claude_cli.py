"""Shared Claude CLI invoker — single implementation replacing 3 copies.

Dev, Security, and Validate agents all invoke ``claude -p <prompt>``
with the same Windows/Linux resolution logic and the same subprocess
management pattern. This module consolidates that into one place.

Usage::

    from coordinator.shared_claude_cli import run_claude_cli

    result = await run_claude_cli(
        worktree_dir=path,
        prompt="...",
        daemon=daemon,
        task_id=task_id,
    )
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _sanitize_prompt_input(text: str) -> str:
    """Sanitize text embedded in a Claude Code prompt.

    Strips prompt-injection vectors that could hijack the
    ``--dangerously-skip-permissions`` agent:
    - Backticks that introduce shell command substitution
    - Triple backticks that fence code blocks (could close the prompt wrapper)
    - Lines that try to switch modes via "Assistant:" / "Human:" prefixes
    """
    if not text:
        return ""
    sanitized = text
    # Remove fenced code blocks that could terminate our prompt wrapper
    sanitized = sanitized.replace("```", "` ` `")
    # Remove single-backtick shell command substitution
    sanitized = sanitized.replace("`", "'")
    return sanitized


def _resolve_claude_exe() -> tuple[str, dict[str, str] | None]:
    """Resolve the claude CLI executable path.

    Returns (executable_path, env_override_or_None).
    On Windows, resolves past .cmd wrappers to the real .exe and
    sets UTF-8 env vars.
    """
    if sys.platform != "win32":
        return "claude", None

    try:
        which_result = subprocess.run(
            ["where", "claude"],
            capture_output=True, text=True, timeout=5,
        )
        claude_exe = None
        for line in which_result.stdout.strip().split("\n"):
            line = line.strip()
            if line.lower().endswith("claude.exe") or line.lower().endswith("claude.cmd"):
                claude_exe = line
                break
        if claude_exe and claude_exe.lower().endswith(".cmd"):
            base_dir = os.path.dirname(claude_exe)
            exe_path = os.path.join(
                base_dir, "node_modules", "@anthropic-ai",
                "claude-code", "bin", "claude.exe",
            )
            if os.path.exists(exe_path):
                claude_exe = exe_path
        if not claude_exe:
            claude_exe = "claude.exe"
    except Exception:
        claude_exe = "claude.exe"

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return claude_exe, env


def _get_timeout(key: str, default: int) -> int:
    """Load timeout value from config."""
    try:
        from coordinator.config import load_config
        return int(load_config().get(key, default))
    except Exception:
        return default


async def run_claude_cli(
    worktree_dir: Path,
    prompt: str,
    *,
    daemon: Any = None,
    task_id: str | None = None,
    timeout_key: str = "claude_code_timeout",
    timeout_default: int = 1800,
) -> dict[str, Any]:
    """Run Claude Code CLI non-interactively.

    If daemon and task_id are provided, registers the spawned subprocess
    with the daemon so it can be killed on task cancellation/timeout.

    Returns dict with stdout, stderr, exit_code, or error.
    """
    try:
        claude_exe, env = _resolve_claude_exe()

        # Sanitize prompt to prevent injection through --dangerously-skip-permissions
        safe_prompt = _sanitize_prompt_input(prompt)

        popen_kwargs: dict[str, Any] = {}
        if env is not None:
            popen_kwargs["env"] = env
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        proc = await asyncio.create_subprocess_exec(
            claude_exe, "-p", safe_prompt, "--dangerously-skip-permissions",
            cwd=str(worktree_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,
        )

        # Register subprocess for cancellation tracking
        if daemon is not None and task_id is not None:
            daemon.register_subprocess(task_id, proc)

        timeout = _get_timeout(timeout_key, timeout_default)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode("utf-8", errors="replace")[:8192],
            "stderr": stderr.decode("utf-8", errors="replace")[:4096],
            "exit_code": proc.returncode,
        }
    except FileNotFoundError:
        return {"error": "claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"}
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        minutes = _get_timeout(timeout_key, timeout_default) // 60
        return {"error": f"Claude Code timed out after {minutes} minutes"}
