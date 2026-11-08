"""Telegram alerts via the Bot API (stdlib urllib — no dependency, and it
degrades to log-only when no token/chat is configured).

When Bot HQ is configured and TRANSPORT includes web, alerts are also
mirrored to the Bot HQ dashboard as alert events via bridge.notify().
The bridge is fail-silent and never changes send_alert's return contract
(the return value reflects Telegram delivery only).
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def send_alert(config, text: str) -> bool:
    """Send a message to the admin chat. Returns True if actually sent."""
    sent = False
    if not config.bot_token or not config.admin_chat_id:
        log.info("ALERT (not sent, Telegram not configured): %s", text)
        sent = False
    else:
        url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": config.admin_chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = json.loads(resp.read().decode("utf-8")).get("ok", False)
            if not ok:
                log.warning("Telegram alert returned not-ok: %s", text[:80])
            sent = bool(ok)
        except Exception as exc:
            log.warning("Telegram alert failed (%s): %s", exc, text[:80])
            sent = False

    # Bot HQ mirror — fail-silent, never changes the return contract.
    try:
        make_bridge = getattr(config, "make_bridge", None)
        if callable(make_bridge):
            bridge = make_bridge()
            if bridge is not None and bridge.enabled and bridge.use_web:
                bridge.notify("Trading alert", text)
    except Exception as exc:  # noqa: BLE001 - fail-silent by design
        log.warning("Bot HQ alert mirror failed: %s", exc)

    return sent
