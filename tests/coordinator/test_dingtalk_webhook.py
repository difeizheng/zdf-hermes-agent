"""Tests for coordinator.dingtalk_webhook outbound push module.

The Round 10-bugfix incident: progress_watcher.py tried to import a
non-existent ``DingTalkPlatform`` class from ``gateway.platforms.dingtalk``,
silently falling back to log-only mode. Users saw no progress in DingTalk.

The fix: a stateless webhook module that posts to a DingTalk group-bot
URL. This module is what the watcher now uses.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coordinator.dingtalk_webhook import post_markdown, resolve_webhook_url


class _MockResp:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self._text = text

    def json(self) -> dict:
        return self._body


class _MockClient:
    """Captures the URL and payload from a POST for assertion."""

    def __init__(self, *args, **kwargs):
        self.captured: list[tuple[str, dict]] = []
        self._resp = _MockResp()

    async def __aenter__(self) -> "_MockClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, json: dict) -> _MockResp:
        self.captured.append((url, json))
        return self._resp


def test_resolve_webhook_url_explicit_arg_wins() -> None:
    """Explicit arg takes precedence over env vars."""
    os.environ["DINGTALK_PROGRESS_WEBHOOK"] = "https://from-env"
    result = resolve_webhook_url("https://from-arg")
    assert result == "https://from-arg"


def test_resolve_webhook_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to env var when no explicit arg."""
    monkeypatch.setenv("DINGTALK_PROGRESS_WEBHOOK", "https://from-env")
    result = resolve_webhook_url()
    assert result == "https://from-env"


def test_resolve_webhook_url_falls_back_to_secondary_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DINGTALK_WEBHOOK is the secondary env key."""
    monkeypatch.delenv("DINGTALK_PROGRESS_WEBHOOK", raising=False)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://from-secondary")
    result = resolve_webhook_url()
    assert result == "https://from-secondary"


def test_resolve_webhook_url_empty_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns empty string when no source is configured."""
    for key in ("DINGTALK_PROGRESS_WEBHOOK", "DINGTALK_WEBHOOK"):
        monkeypatch.delenv(key, raising=False)
    result = resolve_webhook_url()
    assert result == ""


@pytest.mark.asyncio
async def test_post_markdown_sends_correct_payload() -> None:
    """POST body should be DingTalk's markdown schema."""
    client = _MockClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        ok = await post_markdown("https://test", "hello **world**", title="MyTitle")

    assert ok is True
    assert len(client.captured) == 1
    url, payload = client.captured[0]
    assert url == "https://test"
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "MyTitle"
    assert payload["markdown"]["text"] == "hello **world**"


@pytest.mark.asyncio
async def test_post_markdown_default_title() -> None:
    """Title defaults to 'Hermes' when not provided."""
    client = _MockClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await post_markdown("https://test", "hi")

    assert client.captured[0][1]["markdown"]["title"] == "Hermes"


@pytest.mark.asyncio
async def test_post_markdown_empty_url_is_noop() -> None:
    """Empty URL is a no-op (returns False, no network call)."""
    client = _MockClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        ok = await post_markdown("", "text")

    assert ok is False
    assert client.captured == []


@pytest.mark.asyncio
async def test_post_markdown_returns_false_on_http_error() -> None:
    """Non-2xx response → returns False."""
    client = _MockClient()
    client._resp = _MockResp(status_code=403, text="forbidden")
    with patch.object(httpx, "AsyncClient", return_value=client):
        ok = await post_markdown("https://test", "text")

    assert ok is False


@pytest.mark.asyncio
async def test_post_markdown_returns_false_on_dingtalk_errcode() -> None:
    """DingTalk returns 200 with errcode != 0 on auth/perm errors. Must catch that."""
    client = _MockClient()
    client._resp = _MockResp(body={"errcode": 310000, "errmsg": "invalid token"})
    with patch.object(httpx, "AsyncClient", return_value=client):
        ok = await post_markdown("https://test", "text")

    assert ok is False


@pytest.mark.asyncio
async def test_post_markdown_does_not_raise_on_network_error() -> None:
    """Network exceptions must not propagate (watcher is fire-and-forget)."""
    with patch.object(httpx, "AsyncClient", side_effect=httpx.ConnectError("boom")):
        ok = await post_markdown("https://test", "text")
    assert ok is False


@pytest.mark.asyncio
async def test_post_markdown_includes_at_when_provided() -> None:
    """@mobile list is forwarded to DingTalk's at payload."""
    client = _MockClient()
    with patch.object(httpx, "AsyncClient", return_value=client):
        await post_markdown("https://test", "text", at_mobiles=["13800000000"])

    payload = client.captured[0][1]
    assert payload["at"]["atMobiles"] == ["13800000000"]
    assert payload["at"]["isAtAll"] is False
