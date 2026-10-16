"""OpenAI-compatible OpenTelemetry proxy for the local Qwen3.5 llama.cpp server."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from opentelemetry.trace import Status, StatusCode

os.environ.setdefault("OTEL_SERVICE_NAME", "llm-qwen35")

from experiment.platform.backend.observability.otel import (  # noqa: E402
    GenAI,
    capture_content,
    configure_llm_io_audit,
    get_tracer,
    record_llm_io,
    record_llm_metrics,
    setup_telemetry,
)

_UPSTREAM = os.environ.get("QWEN35_UPSTREAM_URL", "http://127.0.0.1:8001").rstrip("/")
_MODEL = os.environ.get("LLM_MODEL", "qwen3.5-9b-grafana")
_MODEL_PATH = os.environ.get("QWEN35_MODEL_PATH", "")
_QUANTIZATION = os.environ.get("QWEN35_QUANTIZATION", "Q4_K_M")
_CONTEXT_SIZE = int(os.environ.get("QWEN35_CTX_SIZE", "131072"))
_UPSTREAM_TIMEOUT = float(os.environ.get("QWEN35_UPSTREAM_TIMEOUT", "180"))

app = FastAPI(title="Qwen3.5 OpenTelemetry Proxy", version="1.0.0")
setup_telemetry(app)
configure_llm_io_audit()


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _read_upstream(payload: dict[str, Any]) -> tuple[int, bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    upstream_request = urllib.request.Request(
        f"{_UPSTREAM}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            upstream_request, timeout=_UPSTREAM_TIMEOUT
        ) as upstream:
            return upstream.status, upstream.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _record_io(
    *,
    model: str,
    duration_s: float,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    response_data: Optional[dict[str, Any]],
    prompt: str,
    error: Optional[str] = None,
    status_code: Optional[int] = None,
) -> None:
    choices = (response_data or {}).get("choices") or []
    finish_reasons = [
        str(choice["finish_reason"])
        for choice in choices
        if isinstance(choice, dict) and choice.get("finish_reason")
    ]
    response_model = (response_data or {}).get("model") or model
    entry: dict[str, Any] = {
        "event": "gen_ai.chat",
        "gen_ai.system": "qwen",
        "gen_ai.provider.name": "custom",
        "gen_ai.request.model": model,
        "gen_ai.response.model": response_model,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.response.finish_reasons": finish_reasons,
        "gen_ai.client.operation.duration": round(duration_s, 3),
        "rca.model.runtime": "llama.cpp",
        "rca.model.quantization": _QUANTIZATION,
        "rca.model.context_size": _CONTEXT_SIZE,
        "prompt": prompt,
        "response": json.dumps(response_data, ensure_ascii=False)
        if response_data is not None
        else "",
    }
    if _MODEL_PATH:
        entry["rca.model.model_path"] = _MODEL_PATH
    if status_code is not None:
        entry["http.response.status_code"] = status_code
    if error:
        entry["error.message"] = error[:500]
    record_llm_io(entry)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": _MODEL, "upstream": _UPSTREAM}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": _MODEL, "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/chat/completions")
def chat_completions(payload: dict[str, Any], request: Request) -> Response:
    if payload.get("stream"):
        raise HTTPException(
            status_code=400, detail="stream=true is not supported by the telemetry proxy"
        )

    model = str(payload.get("model") or _MODEL)
    if not isinstance(payload.get("messages"), list):
        raise HTTPException(status_code=422, detail="messages must be a JSON array")

    prompt = json.dumps(
        {"messages": payload["messages"], "tools": payload.get("tools") or []},
        ensure_ascii=False,
    )
    capture = capture_content()
    tracer = get_tracer()
    started = time.perf_counter()
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    response_data: Optional[dict[str, Any]] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None

    with tracer.start_as_current_span("gen_ai.chat") as span:
        span.set_attribute(GenAI.SYSTEM, "qwen")
        span.set_attribute(GenAI.PROVIDER_NAME, "custom")
        span.set_attribute(GenAI.OPERATION_NAME, "chat")
        span.set_attribute(GenAI.REQUEST_MODEL, model)
        span.set_attribute("rca.model.runtime", "llama.cpp")
        span.set_attribute("rca.model.quantization", _QUANTIZATION)
        span.set_attribute("rca.model.context_size", _CONTEXT_SIZE)
        if _MODEL_PATH:
            span.set_attribute("rca.model.model_path", _MODEL_PATH)
        max_tokens = _optional_int(payload.get("max_tokens"))
        if max_tokens is not None:
            span.set_attribute(GenAI.REQUEST_MAX_TOKENS, max_tokens)
        if capture:
            span.set_attribute(GenAI.PROMPT, prompt)

        try:
            status_code, response_body = _read_upstream(payload)
            if status_code >= 400:
                error_message = f"llama-server returned HTTP {status_code}"
                span.set_status(Status(StatusCode.ERROR, error_message))
                span.set_attribute("http.response.status_code", status_code)
                _record_io(
                    model=model,
                    duration_s=time.perf_counter() - started,
                    input_tokens=None,
                    output_tokens=None,
                    response_data=None,
                    prompt=prompt,
                    error=error_message,
                    status_code=status_code,
                )
                return Response(
                    content=response_body,
                    status_code=status_code,
                    media_type="application/json",
                )

            try:
                decoded = json.loads(response_body)
            except json.JSONDecodeError as exc:
                error_message = "llama-server returned invalid JSON"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, error_message))
                _record_io(
                    model=model,
                    duration_s=time.perf_counter() - started,
                    input_tokens=None,
                    output_tokens=None,
                    response_data=None,
                    prompt=prompt,
                    error=error_message,
                    status_code=502,
                )
                raise HTTPException(status_code=502, detail=error_message) from exc

            if not isinstance(decoded, dict):
                error_message = "llama-server returned a non-object JSON payload"
                span.set_status(Status(StatusCode.ERROR, error_message))
                _record_io(
                    model=model,
                    duration_s=time.perf_counter() - started,
                    input_tokens=None,
                    output_tokens=None,
                    response_data=None,
                    prompt=prompt,
                    error=error_message,
                    status_code=502,
                )
                raise HTTPException(status_code=502, detail=error_message)

            response_data = decoded
            usage = decoded.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens = _optional_int(usage.get("prompt_tokens"))
                output_tokens = _optional_int(usage.get("completion_tokens"))
            if input_tokens is not None:
                span.set_attribute(GenAI.USAGE_INPUT_TOKENS, input_tokens)
            if output_tokens is not None:
                span.set_attribute(GenAI.USAGE_OUTPUT_TOKENS, output_tokens)

            response_model = decoded.get("model")
            if response_model:
                span.set_attribute(GenAI.RESPONSE_MODEL, str(response_model))
            response_id = decoded.get("id")
            if response_id:
                span.set_attribute(GenAI.RESPONSE_ID, str(response_id))

            choices = decoded.get("choices") or []
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason")
                if finish_reason:
                    span.set_attribute(GenAI.RESPONSE_FINISH_REASONS, [finish_reason])
                message = choices[0].get("message") or {}
                tool_calls = message.get("tool_calls") or []
                span.set_attribute("rca.gen_ai.response.tool_calls_count", len(tool_calls))
                for tool_call in tool_calls:
                    function = (tool_call or {}).get("function") or {}
                    span.add_event(
                        "gen_ai.tool_call",
                        {
                            GenAI.TOOL_NAME: str(function.get("name") or "unknown"),
                            GenAI.TOOL_CALL_ID: str(tool_call.get("id") or ""),
                        },
                    )
                if capture:
                    span.set_attribute(
                        GenAI.COMPLETION,
                        json.dumps(message, ensure_ascii=False, default=str),
                    )

            duration = time.perf_counter() - started
            _record_io(
                model=model,
                duration_s=duration,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_data=decoded,
                prompt=prompt,
                status_code=status_code,
            )
            return Response(
                content=response_body,
                status_code=status_code or 200,
                media_type="application/json",
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            error_message = f"Qwen3.5 upstream is unreachable: {exc}"
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, error_message))
            _record_io(
                model=model,
                duration_s=time.perf_counter() - started,
                input_tokens=None,
                output_tokens=None,
                response_data=None,
                prompt=prompt,
                error=error_message,
                status_code=502,
            )
            raise HTTPException(status_code=502, detail=error_message) from exc
        finally:
            duration = time.perf_counter() - started
            record_llm_metrics(
                duration_s=duration,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
            )
