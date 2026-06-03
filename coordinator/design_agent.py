"""Design Agent — executes design tasks via Claude API.

Produces PRD, architecture, and system design markdown documents.
Can run as a daemon (SSE subscription) or be called directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from coordinator.config import load_config as load_coordinator_config
from coordinator.config import load_llm_config as _load_llm_config
from coordinator.shared_heartbeat import start_heartbeat
from coordinator.shared_helpers import get_workspace_dir

logger = logging.getLogger(__name__)


async def run_design_task(
    task_id: str,
    coordinator_url: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a design task via Claude API.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server
        profile: Optional profile configuration from profiles.py.

    Returns:
        Result dict with artifact paths
    """
    cfg = load_coordinator_config()

    # Fetch task details
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{coordinator_url}/tasks/{task_id}")
        resp.raise_for_status()
        task_data = resp.json()

    description = task_data["description"]
    title = task_data["title"]

    # Start heartbeat to prevent timeout during long Claude API calls
    hb = start_heartbeat(task_id, coordinator_url)

    try:
        return await _execute_design(
            task_id, coordinator_url, cfg, description, title, profile,
        )
    finally:
        await hb.cancel_and_wait()


async def _execute_design(
    task_id: str,
    coordinator_url: str,
    cfg: dict[str, Any],
    description: str,
    title: str,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Core design execution, called inside heartbeat try/finally."""
    # Load memory context (decisions + errors from past projects)
    memory_context = ""
    try:
        workspace_dir = Path(get_workspace_dir(cfg))
        from coordinator.memory import load_memory_context
        memory_context = load_memory_context(workspace_dir, categories=["errors", "decisions"])
    except Exception as e:
        logger.warning("Failed to load memory context: %s", e)

    # Build system prompt — use profile behavior if available
    if profile:
        behavior = profile.get("behavior", "")
        rules_str = ", ".join(profile.get("rules", []))
        output = profile.get("output", "")
        system_prompt = (
            f"You are a senior software architect. {behavior}\n\n"
            f"Rules: {rules_str}\n\n"
            f"Expected output: {output}\n\n"
            "Structure your response with these sections:\n\n"
            "1. PRD (Product Requirements Document)\n"
            "2. Architecture Overview\n"
            "3. System Design (data models, API interfaces, component diagrams)\n\n"
            "Use markdown formatting. Be specific and actionable."
        )
    else:
        system_prompt = (
            "You are a senior software architect. Output design documents in markdown. "
            "Structure your response with these sections:\n\n"
            "1. PRD (Product Requirements Document)\n"
            "2. Architecture Overview\n"
            "3. System Design (data models, API interfaces, component diagrams)\n\n"
            "Use markdown formatting. Be specific and actionable. Include concrete "
            "data models, API endpoints, and component responsibilities."
        )

    # Call Claude API — inject memory context into user prompt
    user_content = description
    if memory_context:
        user_content = f"{memory_context}\n\n---\n\n{description}"
    # Pass description separately so the mock fallback doesn't echo the
    # memory-laden user_content into prd.md (Round 9-bugfix regression guard).
    artifacts = await _call_claude_api(system_prompt, user_content, description=description)

    # Resolve actual model used for metadata
    llm = _load_llm_config()
    actual_model = llm.get("model", "claude-opus-4-8")

    # Write artifacts to disk under configured workspace_dir
    workspace_dir = Path(get_workspace_dir(cfg)) / str(task_id) / "artifacts"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    for name, content in artifacts.items():
        (workspace_dir / name).write_text(content, encoding="utf-8")

    return {
        "artifacts": {name: str(workspace_dir / name) for name in artifacts},
        "metadata": {"model": actual_model, "title": title},
    }


async def _call_claude_api(
    system_prompt: str,
    user_content: str,
    *,
    description: str = "",
) -> dict[str, str]:
    """Call LLM API and parse response into artifacts.

    Reads model, api_key, base_url from config.yaml orchestrator.design_llm.
    Supports two modes:
      - Direct: model + api_key_env + optional base_url
      - Provider reference: provider=<custom_provider name> + model

    Falls back to mock artifacts if API key is not configured. The mock
    path intentionally receives only ``description`` (not ``user_content``,
    which contains memory context) so it cannot echo memory into prd.md.
    """
    llm = _load_llm_config()
    api_key = llm.get("api_key")
    model = llm.get("model", "claude-opus-4-8")
    max_tokens = llm.get("max_tokens", 8000)
    base_url = llm.get("base_url")

    if not api_key:
        logger.info("No API key configured for design LLM, using mock artifacts")
        return _mock_artifacts(description=description)

    try:
        import anthropic
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        client = anthropic.AsyncAnthropic(**client_kwargs)

        logger.info(
            "Calling design LLM: model=%s base_url=%s max_tokens=%d",
            model, base_url or "default", max_tokens,
        )

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        text = response.content[0].text if response.content else ""
        return _parse_artifacts(text)

    except Exception as e:
        logger.warning("LLM API call failed (model=%s): %s", model, e)
        return _mock_artifacts(description=description)


def _parse_artifacts(text: str) -> dict[str, str]:
    """Parse Claude response into separate artifact files.

    Splits by markdown headers (## Title). Falls back to putting everything
    in one file if no headers are found. Previous numbered-list splitting
    was fragile and broke when Claude used heading-based formatting.
    """
    import re

    # Map common header keywords to canonical filenames
    _NAME_MAP = {
        "prd": "prd.md",
        "product": "prd.md",
        "requirements": "prd.md",
        "architecture": "architecture.md",
        "overview": "architecture.md",
        "system": "system_design.md",
        "design": "system_design.md",
        "data model": "system_design.md",
        "api": "system_design.md",
    }

    sections: dict[str, str] = {}
    current_name = "full_design.md"
    current_lines: list[str] = []

    for line in text.split("\n"):
        header_match = re.match(r'^(#{1,4})\s+(.+)', line)
        if header_match:
            # Save previous section
            content = "\n".join(current_lines).strip()
            if content:
                sections[current_name] = content
            # Determine filename from header text
            header_text = header_match.group(2).strip().lower()
            current_name = "full_design.md"  # default
            for keyword, filename in _NAME_MAP.items():
                if keyword in header_text:
                    current_name = filename
                    break
            current_lines = [line]
        else:
            current_lines.append(line)

    # Save last section
    content = "\n".join(current_lines).strip()
    if content:
        sections[current_name] = content

    if not sections:
        sections = {"full_design.md": text}

    return sections


def _mock_artifacts(system_prompt: str = "", *, description: str = "") -> dict[str, str]:
    """Generate mock design artifacts for development/testing.

    This fallback is only used when no ANTHROPIC_API_KEY is configured
    (or the LLM call fails). The mock MUST NOT echo the memory-laden
    ``user_content`` that the real LLM path receives — doing so would
    dump the entire error/decision memory into prd.md, which is what
    Round 9 caught in production.

    The first sentence of the task ``description`` is used as a title
    hint. All other content is generic placeholder. Output files are
    marked with a ⚠️ MOCK banner so downstream consumers (and humans)
    immediately know the content is not real.
    """
    first_sentence = (description or "系统设计任务").split("。")[0][:50]
    if not first_sentence.strip():
        first_sentence = "系统设计任务"

    mock_banner = "⚠️ MOCK ARTIFACT — no ANTHROPIC_API_KEY configured"

    return {
        "prd.md": (
            f"# PRD ({mock_banner})\n\n"
            f"任务：{first_sentence}\n\n"
            "## Requirements\n"
            "- (待 LLM 调用生成真实需求 — 当前走 mock 路径)\n"
            "- 用户认证与授权\n"
            "- 基础 CRUD 操作\n\n"
            "## Acceptance Criteria\n"
            "- (待 LLM 调用生成真实验收标准)\n"
            "- 关键功能端到端可演示\n"
        ),
        "architecture.md": (
            f"# Architecture ({mock_banner})\n\n"
            "## Components\n"
            "- API Gateway\n"
            "- Service Layer\n"
            "- Database\n\n"
            "## Technology Stack\n"
            "- Backend: (待 LLM 选定 — 建议 FastAPI)\n"
            "- Database: (待 LLM 选定)\n"
            "- Auth: (待 LLM 选定)\n"
        ),
        "system_design.md": (
            f"# System Design ({mock_banner})\n\n"
            "## Data Model\n"
            "```sql\n"
            "-- (待 LLM 生成真实 schema)\n"
            "CREATE TABLE placeholder (\n"
            "    id UUID PRIMARY KEY,\n"
            "    created_at TIMESTAMP DEFAULT NOW()\n"
            ");\n"
            "```\n\n"
            "## API Endpoints\n"
            "- (待 LLM 生成真实接口列表)\n"
        ),
    }
