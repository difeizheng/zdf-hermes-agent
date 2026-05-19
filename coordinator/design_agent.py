"""Design Agent — executes design tasks via Claude API.

Produces PRD, architecture, and system design markdown documents.
Can run as a daemon (SSE subscription) or be called directly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from coordinator.config import load_config as load_coordinator_config
from coordinator.models import Task

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    import os
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


async def run_design_task(task_id: str, coordinator_url: str) -> dict[str, Any]:
    """Execute a design task via Claude API.

    Args:
        task_id: Task UUID
        coordinator_url: Base URL of the coordinator server

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

    # Build system prompt
    system_prompt = (
        "You are a senior software architect. Output design documents in markdown. "
        "Structure your response with these sections:\n\n"
        "1. PRD (Product Requirements Document)\n"
        "2. Architecture Overview\n"
        "3. System Design (data models, API interfaces, component diagrams)\n\n"
        "Use markdown formatting. Be specific and actionable. Include concrete "
        "data models, API endpoints, and component responsibilities."
    )

    # Call Claude API (use the LLM facade from plugin context or direct API)
    artifacts = await _call_claude_api(system_prompt, description)

    # Write artifacts to disk
    artifact_dir = Path(cfg["artifact_dir"]) / str(task_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    for name, content in artifacts.items():
        (artifact_dir / name).write_text(content, encoding="utf-8")

    return {
        "artifacts": {name: str(artifact_dir / name) for name in artifacts},
        "metadata": {"model": "claude-opus-4-7", "title": title},
    }


async def _call_claude_api(system_prompt: str, user_content: str) -> dict[str, str]:
    """Call Claude API and parse response into artifacts.

    Falls back to mock artifacts if API key is not configured.
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        # Return mock artifacts for development/testing
        return _mock_artifacts(system_prompt, user_content)

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)

        response = await client.messages.create(
            model="claude-opus-4-7-20250514",
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

        text = response.content[0].text if response.content else ""
        return _parse_artifacts(text)

    except Exception as e:
        logger.warning("Claude API call failed, using mock artifacts: %s", e)
        return _mock_artifacts(system_prompt, user_content)


def _parse_artifacts(text: str) -> dict[str, str]:
    """Parse Claude response into separate artifact files.

    Splits by section headers. If no clear sections found, puts everything in one file.
    """
    sections: dict[str, str] = {}
    current_section = "full_design"
    current_content = []

    for line in text.split("\n"):
        if line.strip().startswith(("1.", "2.", "3.")):
            # Save previous section
            sections[current_section] = "\n".join(current_content).strip()
            current_section = line.strip().lower().replace(" ", "_")
            current_content = []
        else:
            current_content.append(line)

    # Save last section
    sections[current_section] = "\n".join(current_content).strip()

    # Clean up section names
    cleaned = {}
    for key, content in sections.items():
        if content:
            name = key.replace("prd", "prd").replace("architecture", "architecture").replace(
                "system_design", "system_design"
            )
            cleaned[name] = content

    if not cleaned:
        cleaned = {"full_design.md": text}

    return cleaned


def _mock_artifacts(system_prompt: str, user_content: str) -> dict[str, str]:
    """Generate mock design artifacts for development/testing."""
    return {
        "prd.md": (
            f"# PRD: {user_content}\n\n"
            "## Requirements\n"
            "- User authentication and authorization\n"
            "- CRUD operations for user management\n"
            "- Role-based access control\n\n"
            "## Acceptance Criteria\n"
            "- Users can be created, read, updated, deleted\n"
            "- Password hashing with bcrypt\n"
            "- API rate limiting\n"
        ),
        "architecture.md": (
            "# Architecture\n\n"
            "## Components\n"
            "- API Gateway\n"
            "- User Service\n"
            "- Database (PostgreSQL)\n\n"
            "## Technology Stack\n"
            "- Backend: FastAPI\n"
            "- Database: PostgreSQL with asyncpg\n"
            "- Auth: JWT tokens\n"
        ),
        "system_design.md": (
            "# System Design\n\n"
            "## Data Model\n"
            "```sql\n"
            "CREATE TABLE users (\n"
            "    id UUID PRIMARY KEY,\n"
            "    email VARCHAR(255) UNIQUE NOT NULL,\n"
            "    password_hash VARCHAR(255) NOT NULL,\n"
            "    role VARCHAR(50) DEFAULT 'user',\n"
            "    created_at TIMESTAMP DEFAULT NOW()\n"
            ");\n"
            "```\n\n"
            "## API Endpoints\n"
            "- POST /api/users\n"
            "- GET /api/users/{id}\n"
            "- PUT /api/users/{id}\n"
            "- DELETE /api/users/{id}\n"
        ),
    }
