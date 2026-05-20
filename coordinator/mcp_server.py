"""Coordinator MCP stdio server.

Wraps the coordinator HTTP API as an MCP server using FastMCP.
Allows Claude Code and other MCP clients to manage tasks via stdio.

Usage:
    python -m coordinator.mcp_server
    python -m coordinator.mcp_server --verbose
    python -m coordinator.mcp_server --coordinator-url http://localhost:9100

MCP client config (e.g. claude_desktop_config.json):
    {
        "mcpServers": {
            "coordinator": {
                "command": "python",
                "args": ["-m", "coordinator.mcp_server"]
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# -- Lazy MCP SDK import ---------------------------------------------------

_MCP_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    FastMCP = None  # type: ignore[assignment,misc]


# -- Helpers ---------------------------------------------------------------

def _get_coordinator_url(override: Optional[str] = None) -> str:
    """Return coordinator base URL from override or config."""
    if override:
        return override.rstrip("/")
    try:
        from coordinator.config import load_config
        cfg = load_config()
        return f"http://localhost:{cfg.get('port', 9100)}"
    except Exception:
        return "http://localhost:9100"


def _http_get(url: str, params: Optional[dict] = None) -> dict:
    """Sync HTTP GET to coordinator."""
    import httpx
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _http_post(url: str, json_body: dict) -> dict:
    """Sync HTTP POST to coordinator."""
    import httpx
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=json_body)
        resp.raise_for_status()
        return resp.json()


def _http_patch(url: str, json_body: dict) -> dict:
    """Sync HTTP PATCH to coordinator."""
    import httpx
    with httpx.Client(timeout=30) as client:
        resp = client.patch(url, json=json_body)
        resp.raise_for_status()
        return resp.json()


# -- MCP Server ------------------------------------------------------------

def create_mcp_server(coordinator_url: Optional[str] = None) -> "FastMCP":
    """Create and return the coordinator MCP server with all tools."""
    if not _MCP_AVAILABLE:
        raise ImportError(
            "MCP server requires the 'mcp' package. "
            f"Install with: {sys.executable} -m pip install 'mcp'"
        )

    base_url = _get_coordinator_url(coordinator_url)

    mcp = FastMCP(
        "coordinator",
        instructions=(
            "Hermes Task Coordinator. Use these tools to create, monitor, "
            "and manage multi-agent tasks (design, dev, validate, deploy)."
        ),
    )

    @mcp.tool()
    def create_task(
        type: str,
        title: str,
        description: str,
        depends_on: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Create a new orchestrated task.

        Args:
            type: Task type — design, dev, validate, or deploy
            title: Short task title
            description: Full task description / prompt for the agent
            depends_on: List of parent task IDs that must complete first
            metadata: Optional JSON metadata (git repo, branch, model, etc.)
        """
        body: dict = {"type": type, "title": title, "description": description}
        if depends_on:
            body["depends_on"] = depends_on
        if metadata:
            body["metadata"] = metadata
        try:
            result = _http_post(f"{base_url}/tasks", body)
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    @mcp.tool()
    def get_task(task_id: str) -> str:
        """Get details of a single task by ID.

        Args:
            task_id: The task UUID
        """
        try:
            result = _http_get(f"{base_url}/tasks/{task_id}")
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    @mcp.tool()
    def list_tasks(
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> str:
        """List tasks with optional filters.

        Args:
            status: Filter by status (pending, running, completed, failed, cancelled, timeout)
            type: Filter by type (design, dev, validate, deploy)
        """
        params: dict = {}
        if status:
            params["status"] = status
        if type:
            params["type"] = type
        try:
            result = _http_get(f"{base_url}/tasks", params=params or None)
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    @mcp.tool()
    def submit_result(
        task_id: str,
        artifacts: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> str:
        """Submit task completion result or failure.

        Args:
            task_id: The task UUID
            artifacts: Dict of artifact paths/names (omit if error)
            error: Error message if task failed
        """
        body: dict = {}
        if artifacts:
            body["artifacts"] = artifacts
        if error:
            body["error"] = error
            body["status"] = "failed"
        else:
            body["status"] = "completed"
        try:
            result = _http_post(f"{base_url}/tasks/{task_id}/result", body)
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    @mcp.tool()
    def cancel_task(task_id: str) -> str:
        """Cancel a pending or running task.

        Args:
            task_id: The task UUID
        """
        try:
            result = _http_patch(
                f"{base_url}/tasks/{task_id}",
                {"status": "cancelled"},
            )
            return json.dumps(result, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)}, indent=2)

    return mcp


# -- Entry point -----------------------------------------------------------

def run_mcp_server(
    verbose: bool = False,
    coordinator_url: Optional[str] = None,
) -> None:
    """Start the coordinator MCP server on stdio."""
    if not _MCP_AVAILABLE:
        print(
            "Error: MCP server requires the 'mcp' package.\n"
            f"Install with: {sys.executable} -m pip install 'mcp'",
            file=sys.stderr,
        )
        sys.exit(1)

    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    import asyncio

    server = create_mcp_server(coordinator_url=coordinator_url)

    async def _run():
        await server.run_stdio_async()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Coordinator MCP stdio server")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--coordinator-url",
        default=None,
        help="Coordinator HTTP URL (default: from config or http://localhost:9100)",
    )
    args = parser.parse_args()
    run_mcp_server(verbose=args.verbose, coordinator_url=args.coordinator_url)
