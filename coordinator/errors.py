"""Shared error types for all coordinator agents.

Provides a consistent exception hierarchy so that agent_daemon can
handle errors uniformly instead of mixing raise / return error dict patterns.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base exception for agent execution failures.

    Agents should raise this (or a subclass) to report failures.
    The daemon catches this and converts to a structured error result.
    """

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class WorktreeNotFoundError(AgentError):
    """The dev worktree directory does not exist."""


class DependencyNotFoundError(AgentError):
    """A required dependency task was not found."""


class LLMTimeoutError(AgentError):
    """An LLM API call timed out."""


class QualityGateError(AgentError):
    """Quality gate check failed (used for structured reporting)."""
