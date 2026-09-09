"""HTTP client + response parser for the local Qwen server.

Supports both the legacy ``POST /generate`` endpoint and an OpenAI-style
``POST /v1/chat/completions`` surface so the platform can evolve toward
multi-agent orchestration without changing its call sites.
"""
from __future__ import annotations

import json
import importlib.util
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_LLM_URL = os.environ.get("LLM_URL", "http://localhost:8001/generate")
DEFAULT_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "qwen-14b")
DEFAULT_LLM_API_STYLE = os.environ.get("LLM_API_STYLE", "auto")

_OTEL_SPEC = importlib.util.find_spec("opentelemetry")
if _OTEL_SPEC is not None:
    from opentelemetry.propagate import inject as _otel_inject
else:
    _otel_inject = None


class LLMClientError(RuntimeError):
    """Raised when the LLM cannot be reached or returns an invalid payload."""


def _messages_to_prompt(messages: Iterable[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip().lower() or "user"
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        else:
            text = str(content or "").strip()
        if not text:
            continue
        blocks.append(f"[{role.upper()}]\n{text}")
    return "\n\n".join(blocks) + ("\n\n[ASSISTANT]\n" if blocks else "")


def _resolve_api_style(url: str, api_style: Optional[str]) -> str:
    style = (api_style or DEFAULT_LLM_API_STYLE or "auto").strip().lower()
    if style in {"legacy", "openai"}:
        return style
    if url.rstrip("/").endswith("/v1/chat/completions"):
        return "openai"
    return "legacy"


def _request_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if _otel_inject is not None:
        _otel_inject(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise LLMClientError(f"LLM unreachable at {url}: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"LLM returned non-JSON: {body[:200]}") from exc


def _extract_openai_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMClientError(f"OpenAI-style payload missing 'choices': {data}")
    choice = choices[0] or {}
    message = choice.get("message") or {}
    text = message.get("content")
    if not isinstance(text, str):
        raise LLMClientError(f"OpenAI-style payload missing message.content: {data}")
    return text


def call_llm_messages(
    messages: List[Dict[str, Any]],
    *,
    max_new_tokens: int = 512,
    url: str = DEFAULT_LLM_URL,
    timeout: float = DEFAULT_LLM_TIMEOUT,
    model: str = DEFAULT_LLM_MODEL,
    api_style: Optional[str] = None,
) -> str:
    """Call the configured LLM using chat messages as the source input."""
    style = _resolve_api_style(url, api_style)
    if style == "openai":
        data = _request_json(
            url,
            {
                "model": model,
                "messages": messages,
                "max_tokens": int(max_new_tokens),
            },
            timeout,
        )
        return _extract_openai_text(data)

    prompt = _messages_to_prompt(messages)
    data = _request_json(
        url,
        {
            "prompt": prompt,
            "max_new_tokens": int(max_new_tokens),
        },
        timeout,
    )
    text = data.get("response")
    if not isinstance(text, str):
        raise LLMClientError(f"LLM payload missing 'response': {data}")
    return text


def call_llm(
    prompt: str,
    *,
    max_new_tokens: int = 512,
    url: str = DEFAULT_LLM_URL,
    timeout: float = DEFAULT_LLM_TIMEOUT,
) -> str:
    """Call the configured LLM and return the raw response text."""
    return call_llm_messages(
        [{"role": "user", "content": prompt}],
        max_new_tokens=max_new_tokens,
        url=url,
        timeout=timeout,
    )


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
                return text[start : i + 1]
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
    raw_url = url or DEFAULT_LLM_URL
    if raw_url.rstrip("/").endswith("/v1/chat/completions"):
        base = raw_url.rsplit("/v1/chat/completions", 1)[0]
    else:
        base = raw_url.rsplit("/", 1)[0]
    health_url = f"{base}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
        return {"ok": True, "url": health_url, **data}
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "url": health_url, "error": str(exc)}
