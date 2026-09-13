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


def _get_api_key(override: Optional[str] = None) -> Optional[str]:
    if override:
        return override
    return (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("COPILOT_API_KEY")
    )

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
    if api_style and api_style.strip().lower() in {"legacy", "openai", "gemini"}:
        return api_style.strip().lower()
    clean = url.rstrip("/").lower()
    if "generativelanguage.googleapis.com" in clean or "gemini" in clean:
        return "gemini"
    if (
        clean.endswith("/v1/chat/completions")
        or clean.endswith("/chat/completions")
        or "inference.ai.azure.com" in clean
        or "api.github.com" in clean
        or "api.openai.com" in clean
    ):
        return "openai"
    env_style = (DEFAULT_LLM_API_STYLE or "auto").strip().lower()
    if env_style in {"legacy", "openai", "gemini"}:
        return env_style
    return "legacy"


def _request_json(
    url: str,
    payload: Dict[str, Any],
    timeout: float,
    api_key: Optional[str] = None,
    send_auth_header: bool = True,
) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MicroservicesDemo-RCA/1.0",
    }
    key = _get_api_key(api_key)
    if key and send_auth_header:
        headers["Authorization"] = f"Bearer {key}"
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
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise LLMClientError(f"LLM HTTP {exc.code} from {url}: {err_body[:500]}") from exc
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


def _extract_gemini_text(data: Dict[str, Any]) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LLMClientError(f"Gemini payload missing 'candidates': {data}")
    cand = candidates[0] or {}
    content = cand.get("content") or {}
    parts = content.get("parts") or []
    if not parts or not isinstance(parts, list):
        raise LLMClientError(f"Gemini payload missing 'parts': {data}")
    text = parts[0].get("text")
    if not isinstance(text, str):
        raise LLMClientError(f"Gemini payload missing text in parts[0]: {data}")
    return text


def call_llm_messages(
    messages: List[Dict[str, Any]],
    *,
    max_new_tokens: int = 2048,
    url: str = DEFAULT_LLM_URL,
    timeout: float = DEFAULT_LLM_TIMEOUT,
    model: str = DEFAULT_LLM_MODEL,
    api_style: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Call the configured LLM using chat messages as the source input."""
    style = _resolve_api_style(url, api_style)
    key = _get_api_key(api_key)

    if style == "gemini":
        target_url = url
        # Automatically append API key parameter if needed
        if key and "key=" not in target_url:
            delimiter = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{delimiter}key={key}"
        
        prompt = _messages_to_prompt(messages)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": int(max_new_tokens),
                "responseMimeType": "application/json",
            },
        }
        data = _request_json(target_url, payload, timeout, api_key=None, send_auth_header=False)
        return _extract_gemini_text(data)

    if style == "openai":
        payload_openai: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": int(max_new_tokens),
            "response_format": {"type": "json_object"},
        }
        data = _request_json(
            url,
            payload_openai,
            timeout,
            api_key=key,
        )
        return _extract_openai_text(data)

    prompt = _messages_to_prompt(messages)
    data = _request_json(
        url,
        {
            "prompt": prompt,
            "max_new_tokens": int(max_new_tokens),
            "response_format": {"type": "json_object"},
        },
        timeout,
        api_key=api_key,
    )
    text = data.get("response")
    if not isinstance(text, str):
        raise LLMClientError(f"LLM payload missing 'response': {data}")
    return text


def call_llm(
    prompt: str,
    *,
    max_new_tokens: int = 2048,
    url: str = DEFAULT_LLM_URL,
    timeout: float = DEFAULT_LLM_TIMEOUT,
    model: str = DEFAULT_LLM_MODEL,
    api_style: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Call the configured LLM and return the raw response text."""
    return call_llm_messages(
        [{"role": "user", "content": prompt}],
        max_new_tokens=max_new_tokens,
        url=url,
        timeout=timeout,
        model=model,
        api_style=api_style,
        api_key=api_key,
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


from typing import Any, Dict, Iterable, List, Optional, Literal
from pydantic import BaseModel, Field


# ── Pydantic Schema para validação sintática e contratual da resposta RCA ────
class RcaAnalysisResponse(BaseModel):
    reasoning: str = Field(
        ...,
        description="Análise passo a passo da telemetria, métricas e logs",
    )
    verdict: Optional[Literal["resilient", "degraded_recoverable", "degraded_persistent"]] = Field(
        default=None,
        description="Veredito de resiliência da plataforma",
    )
    rca: Optional[str] = Field(
        default=None,
        description="Nome do serviço causa-raiz identificado",
    )
    fault_category: Optional[str] = Field(
        default=None,
        description="Categoria da falha identificada",
    )
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Nível de confiança na inferência (0.0 a 1.0)",
    )
    citations: List[str] = Field(
        default_factory=list,
        description="Lista de referências a execuções anteriores citadas",
    )
    follow_ups: List[str] = Field(
        default_factory=list,
        description="Ações recomendadas de remediação ou observabilidade",
    )


def parse_verdict_response(text: str) -> Dict[str, Any]:
    """Best‑effort parse of the LLM JSON answer with Pydantic validation.

    Returns a dict with ``reasoning``, ``verdict``, ``rca``, ``fault_category``,
    ``confidence``, ``citations``, ``follow_ups`` and ``parse_error``.
    Missing/invalid fields are handled gracefully so callers get a stable shape.
    """
    out: Dict[str, Any] = {
        "reasoning": text.strip() if text else "",
        "verdict": None,
        "rca": None,
        "fault_category": None,
        "confidence": None,
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
        raw_data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        out["parse_error"] = f"json decode: {exc}"
        return out

    # Validação sintática e de campos via Pydantic schema
    try:
        validated = RcaAnalysisResponse.model_validate(raw_data)
        out["reasoning"] = validated.reasoning.strip()
        out["verdict"] = validated.verdict
        out["rca"] = validated.rca.strip() if validated.rca else None
        out["fault_category"] = validated.fault_category.strip() if validated.fault_category else None
        out["confidence"] = validated.confidence
        out["citations"] = validated.citations
        out["follow_ups"] = validated.follow_ups
    except Exception as exc:
        # Fallback gracioso com parsing campo a campo se a validação Pydantic falhar parcialmente
        out["parse_error"] = f"pydantic validation: {str(exc)[:150]}"
        reasoning = raw_data.get("reasoning")
        if isinstance(reasoning, str):
            out["reasoning"] = reasoning.strip()

        verdict = raw_data.get("verdict")
        if isinstance(verdict, str) and verdict in _VALID_VERDICTS:
            out["verdict"] = verdict

        rca = raw_data.get("rca")
        if isinstance(rca, str) and rca.strip():
            out["rca"] = rca.strip()

        fault_category = raw_data.get("fault_category")
        if isinstance(fault_category, str) and fault_category.strip():
            out["fault_category"] = fault_category.strip()

        conf = raw_data.get("confidence")
        if isinstance(conf, (int, float)):
            out["confidence"] = max(0.0, min(1.0, float(conf)))

        citations = raw_data.get("citations") or []
        if isinstance(citations, list):
            out["citations"] = [str(c) for c in citations if c]

        follow_ups = raw_data.get("follow_ups") or []
        if isinstance(follow_ups, list):
            out["follow_ups"] = [str(f) for f in follow_ups if f]

    return out


def llm_health(
    url: Optional[str] = None,
    timeout: float = 5.0,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Probe health/availability of the configured LLM endpoint."""
    raw_url = url or DEFAULT_LLM_URL
    style = _resolve_api_style(raw_url, None)
    key = _get_api_key(api_key)

    # For cloud Gemini endpoints
    if style == "gemini":
        if not key:
            return {
                "ok": False,
                "url": raw_url,
                "style": "gemini_cloud",
                "error": "Missing API key. Set GEMINI_API_KEY or LLM_API_KEY",
            }
        return {
            "ok": True,
            "url": raw_url,
            "style": "gemini_cloud",
            "model": DEFAULT_LLM_MODEL,
        }

    # For cloud OpenAI / GitHub Models / Azure endpoints
    if style == "openai" and not (
        "localhost" in raw_url or "127.0.0.1" in raw_url or "0.0.0.0" in raw_url
    ):
        if not key:
            return {
                "ok": False,
                "url": raw_url,
                "style": "openai_cloud",
                "error": "Missing API key. Set GITHUB_TOKEN, LLM_API_KEY or OPENAI_API_KEY",
            }
        return {
            "ok": True,
            "url": raw_url,
            "style": "openai_cloud",
            "model": DEFAULT_LLM_MODEL,
        }

    if raw_url.rstrip("/").endswith("/v1/chat/completions"):
        base = raw_url.rsplit("/v1/chat/completions", 1)[0]
    elif raw_url.rstrip("/").endswith("/chat/completions"):
        base = raw_url.rsplit("/chat/completions", 1)[0]
    else:
        base = raw_url.rsplit("/", 1)[0]
    health_url = f"{base}/health"
    try:
        req = urllib.request.Request(
            health_url,
            headers={"User-Agent": "MicroservicesDemo-RCA/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
        return {"ok": True, "url": health_url, **data}
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "url": health_url, "error": str(exc)}
