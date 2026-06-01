"""Progress streaming helpers for agent tasks.

Each agent can append progress notes to `<workspace_dir>/<task_id>/progress.log`
during execution. The progress_watcher tails these files and forwards new
lines to DingTalk, giving the user near-real-time visibility into long
agent runs (e.g., "Claude Code: reading design docs...").

The write functions are intentionally sync (file I/O) — agents already
shell out to Claude Code which itself can take 30+ minutes, so a tiny
sync write is negligible overhead.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator.config import load_config


def _progress_log_path(task_id: str) -> Path:
    """Return the path to the progress log for a task.

    Creates the parent directory if needed.
    """
    cfg = load_config()
    workspace = Path(cfg.get("workspace_dir", "D:/hermes/workspace")) / str(task_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace / "progress.log"


def write_progress(task_id: str, note: str, level: str = "info") -> None:
    """Append a progress note to the task's progress log.

    Args:
        task_id: Task UUID
        note: Human-readable progress message (one line, no newlines)
        level: Log level — "info", "warn", "error" (purely for display)
    """
    if "\n" in note:
        # Strip newlines to keep log grep-friendly
        note = note.replace("\n", " ").strip()
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level.upper()}] {note}"
    try:
        log_path = _progress_log_path(task_id)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Progress writes must never break agent execution
        pass


def read_progress_since(task_id: str, byte_offset: int) -> tuple[str, int]:
    """Read progress log content from a given byte offset.

    Args:
        task_id: Task UUID
        byte_offset: File byte offset to start reading from

    Returns:
        Tuple of (new_content, new_byte_offset)
    """
    log_path = _progress_log_path(task_id)
    if not log_path.exists():
        return ("", byte_offset)
    try:
        with open(log_path, "rb") as f:
            f.seek(byte_offset)
            data = f.read()
        return (data.decode("utf-8", errors="replace"), byte_offset + len(data))
    except Exception:
        return ("", byte_offset)


def get_progress_size(task_id: str) -> int:
    """Get current size of progress log (used as the offset cursor)."""
    log_path = _progress_log_path(task_id)
    if not log_path.exists():
        return 0
    try:
        return log_path.stat().st_size
    except Exception:
        return 0
