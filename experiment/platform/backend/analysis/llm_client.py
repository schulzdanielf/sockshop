"""HTTP client + response parser for the local Qwen 14B server.

The server (``model/server.py``) exposes ``POST /generate`` taking
``{prompt, max_new_tokens}`` and returning ``{response}``. This module
keeps the integration narrow so it can be swapped for any other text
completion backend later.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


DEFAULT_LLM_URL = os.environ.get("LLM_URL", "http://localhost:8001/generate")
DEFAULT_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))


class LLMClientError(RuntimeError):
    """Raised when the LLM cannot be reached or returns an invalid payload."""


def call_llm(
    prompt: str,
    *,
    max_new_tokens: int = 512,
    url: str = DEFAULT_LLM_URL,
    timeout: float = DEFAULT_LLM_TIMEOUT,
) -> str:
    """Call ``POST /generate`` and return the raw response text."""
    payload = json.dumps({
        "prompt": prompt,
        "max_new_tokens": int(max_new_tokens),
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise LLMClientError(f"LLM unreachable at {url}: {exc}") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"LLM returned non‑JSON: {body[:200]}") from exc
    text = data.get("response")
    if not isinstance(text, str):
        raise LLMClientError(f"LLM payload missing 'response': {data}")
    return text


# ── parsing ──────────────────────────────────────────────────────────────
_VALID_VERDICTS = {"resilient", "degraded_recoverable", "degraded_persistent"}


def _extract_first_json_object(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` block in ``text`` or ``None``.

    Handles common LLM quirks:
    * Markdown fences (```json ... ```) — ignored, we just scan chars.
    * Multiple back-to-back JSON copies — we stop at the first balanced one.
    * Trailing chain-of-thought — discarded once braces close.
    * Strings containing ``{`` or ``}`` are respected (with ``\\`` escapes).
    """
    if not text:
        return None
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start:i + 1]
    return None


def parse_verdict_response(text: str) -> Dict[str, Any]:
    """Best‑effort parse of the LLM JSON answer.

    Returns a dict with ``verdict``, ``rca``, ``fault_category``,
    ``confidence``, ``reasoning``, ``citations``, ``follow_ups`` and
    ``parse_error``. Missing fields are filled with safe defaults so
    callers always get a stable shape.
    """
    out: Dict[str, Any] = {
        "verdict": None,
        "rca": None,
        "fault_category": None,
        "confidence": None,
        "reasoning": text.strip() if text else "",
        "citations": [],
        "follow_ups": [],
        "parse_error": None,
        "raw_response": text,
    }
    if not text:
        out["parse_error"] = "empty response"
        return out

    candidate = _extract_first_json_object(text)
    if candidate is None:
        out["parse_error"] = "no JSON block found"
        return out

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        out["parse_error"] = f"json decode: {exc}"
        return out

    verdict = data.get("verdict")
    if isinstance(verdict, str) and verdict in _VALID_VERDICTS:
        out["verdict"] = verdict
    else:
        out["parse_error"] = f"invalid verdict: {verdict!r}"

    rca = data.get("rca")
    if isinstance(rca, str) and rca.strip():
        out["rca"] = rca.strip()

    fault_category = data.get("fault_category")
    if isinstance(fault_category, str) and fault_category.strip():
        out["fault_category"] = fault_category.strip()

    conf = data.get("confidence")
    if isinstance(conf, (int, float)):
        out["confidence"] = max(0.0, min(1.0, float(conf)))

    reasoning = data.get("reasoning")
    if isinstance(reasoning, str):
        out["reasoning"] = reasoning.strip()

    citations = data.get("citations") or []
    if isinstance(citations, list):
        out["citations"] = [str(c) for c in citations if c]

    follow_ups = data.get("follow_ups") or []
    if isinstance(follow_ups, list):
        out["follow_ups"] = [str(f) for f in follow_ups if f]

    return out


def llm_health(url: Optional[str] = None, timeout: float = 5.0) -> Dict[str, Any]:
    """Probe ``GET /health`` of the LLM server."""
    base = (url or DEFAULT_LLM_URL).rsplit("/", 1)[0]
    health_url = f"{base}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
        return {"ok": True, "url": health_url, **data}
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "url": health_url, "error": str(exc)}
