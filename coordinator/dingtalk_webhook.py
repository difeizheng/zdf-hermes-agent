"""DingTalk outbound webhook for one-way progress pushes.

The gateway's ``DingTalkAdapter`` is a bidirectional stream-mode reply
client: it requires an incoming ``session_webhook`` to reply to. The
progress watcher has no inbound message context — it only has the
``chat_id`` injected by the Brain Agent into task metadata. This module
fills that gap with a one-way push to a DingTalk group-bot webhook,
which is the simplest outbound channel that works without per-chat
state.

Why group-bot webhook and not OpenAPI:
  - Zero permissioning: any DingTalk group admin can add a custom bot
    in 30 seconds and paste the webhook URL into config.
  - Stateless: no need to map chat_id → user_id, no need for app
    permissions like ``机器人单聊消息``.
  - Limitation: messages only land in the configured group(s); the
    originating user must be a member.

For per-user (DM) outbound, see the roadmap note in
``docs/feiyang-v2-refactor-plan.md``: an OpenAPI-based
``DingTalkAdapter.send_proactive()`` is the proper long-term fix.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Env-var keys to consult, in priority order. Config file overrides env.
_ENV_KEYS: tuple[str, ...] = ("DINGTALK_PROGRESS_WEBHOOK", "DINGTALK_WEBHOOK")


def resolve_webhook_url(explicit: str | None = None) -> str:
    """Return the configured webhook URL, or empty string if none.

    Lookup order: explicit arg > env var > empty.
    """
    if explicit:
        return explicit
    for key in _ENV_KEYS:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


async def post_markdown(
    webhook_url: str,
    text: str,
    *,
    title: str = "Hermes",
    at_mobiles: list[str] | None = None,
    timeout: float = 10.0,
) -> bool:
    """Post a markdown message to a DingTalk group-bot webhook.

    Returns True on HTTP 2xx, False otherwise. Never raises — callers
    are fire-and-forget paths that should log and continue.
    """
    if not webhook_url:
        logger.debug("post_markdown called with empty webhook_url (no-op)")
        return False

    payload: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }
    if at_mobiles:
        payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(webhook_url, json=payload)
        if resp.status_code >= 300:
            logger.warning(
                "DingTalk webhook returned HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False
        # DingTalk returns {"errcode": 0, "errmsg": "ok"} on success.
        try:
            body = resp.json()
        except Exception:
            return True  # 2xx without JSON body is still success
        errcode = body.get("errcode", 0)
        if errcode:
            logger.warning("DingTalk webhook errcode=%d errmsg=%s", errcode, body.get("errmsg"))
            return False
        return True
    except Exception as e:
        logger.warning("DingTalk webhook POST failed: %s", e)
        return False
