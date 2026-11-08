"""LLM client for GT's Moon-Dev-style agent panel.

Moon Dev's agents are LLM-driven: a model reads the market context and
answers in a strict format. GT gets the same design, pointed at the
FREE local models on D'Vaughn's Mac Studio (LM Studio, OpenAI-compatible
API over the tailnet) instead of paid Claude/GPT keys — same prompts,
same format, zero metered cost, nothing leaves his hardware.

Hard rules:
  * Stdlib only (urllib) — the bot's core never gains a dependency.
  * Fail-silent: any error (server off, timeout, bad JSON) returns None
    and the deterministic strategy read stands on its own. The LLM is
    advisory — it narrates and votes; it can NEVER place or trigger an
    order. Only the backtested strategy code can do that.
  * Generous token budgets: the local Qwen3 models are reasoning models
    and spend most of the budget thinking before they answer (an
    under-budgeted call returns empty content — learned on the harness).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "GlenrideGT/1.0 (+https://bots.dvaughnhouse.com)"
DEFAULT_BASE_URL = "https://mac-studio.tail22a20c.ts.net/v1"
DEFAULT_MODEL = "qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"


def server_up(base_url: str, timeout_s: int = 8) -> bool:
    """Cheap preflight: is the model server answering at all? Keeps a
    sleeping Mac Studio from turning every panel call into a long
    timeout — the panel checks once, then stands down politely."""
    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not up"
        return False


def chat_completion(
    base_url: str,
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 1500,
    timeout_s: int = 120,
    api_key: str = "",
) -> str | None:
    """One chat completion. Returns the answer text, or None on any
    failure. Never raises."""
    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"content-type": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"].get("content") or ""
    except Exception as exc:  # noqa: BLE001 - fail-silent by design
        log.warning("LLM call failed (%s): %s", url, exc)
        return None
    # Some local models inline their reasoning in <think> tags.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return content or None


_VERDICT_WORDS = ("BUY", "SELL", "NOTHING")


def parse_verdict(text: str | None) -> tuple[str, str, int | None] | None:
    """Parse a Moon-format 3-line verdict:
        BUY | SELL | NOTHING
        <one short reason>
        Confidence: X%
    Tolerant of extra whitespace/markdown; returns None when the text
    doesn't carry a verdict word at all."""
    if not text:
        return None
    lines = [ln.strip(" *_#") for ln in text.strip().splitlines() if ln.strip()]
    word = ""
    for ln in lines[:3]:
        up = ln.upper()
        for w in _VERDICT_WORDS:
            if up == w or up.startswith(w + " ") or up.startswith(w + ":"):
                word = w
                break
        if word:
            break
    if not word:
        return None
    reason = ""
    for ln in lines:
        if ln.upper().startswith(word):
            continue
        if ln.lower().startswith("confidence"):
            continue
        reason = ln
        break
    conf = None
    # Confidence comes from the "Confidence:" line ONLY — a naive % hunt
    # matches things like "+0.37%" in the reason text (observed live).
    for ln in lines:
        if ln.lower().startswith("confidence"):
            m = re.search(r"(\d{1,3})\s*%", ln)
            if m:
                conf = max(0, min(100, int(m.group(1))))
            break
    if conf is None:
        m = re.search(r"confidence[^0-9]{0,12}(\d{1,3})\s*%", text, re.I)
        if m:
            conf = max(0, min(100, int(m.group(1))))
    return word, reason, conf
