"""FastAPI server exposing the local Qwen-14B engine.

Serves both the legacy ``/generate`` endpoint and a minimal OpenAI-compatible
``/v1/chat/completions`` surface, backed by :class:`model.llm.Qwen14BEngine`.
Telemetry and ``faulthandler`` stay enabled for crash diagnostics.
"""

import faulthandler
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import extract
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes
from pydantic import BaseModel

from model.openai_compat import (
    apply_stop_sequences,
    build_chat_completion_response,
    parse_tool_call_output,
    render_chat_messages,
    tool_choice_requires_call,
    validate_tool_request,
)

faulthandler.enable()  # dumps Python traceback to stderr on SIGSEGV/SIGFPE

# model.llm builds the (heavy) ExLlamaV2 engine at import time; import it
# explicitly here — after telemetry wiring — and trace how long it takes.
print(
    f"[DEBUG] server.py importing model.llm"
    f"  (pid={os.getpid()}, tid={threading.get_ident()})",
    flush=True,
)
from model.llm import engine  # noqa: E402  (intentional post-faulthandler load)

print("[DEBUG] model.llm imported OK", flush=True)


# ── Shared OTel resource ─────────────────────────────────────────────────────
_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
# Normalise to a base URL with trailing slash so signal paths append correctly.
# The SDK only auto-appends /v1/{signal} when using the env-var path; when
# endpoint= is passed explicitly it uses the value verbatim (SDK ≥ 1.x).
_OTLP_BASE = _OTLP_ENDPOINT.rstrip("/") + "/"
_resource = Resource.create(
    {
        ResourceAttributes.SERVICE_NAME: "llm-qwen14b",
        ResourceAttributes.SERVICE_VERSION: "1.0.0",
        "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "local"),
        "gen_ai.model.name": "qwen-14b",
        "gen_ai.model.provider": "local",
        "gen_ai.provider.name": "custom",
        "rca.model.runtime": "exllamav2",
        "rca.model.model_dir": os.environ.get("MODEL_DIR", ""),
        "rca.model.max_seq_len": os.environ.get("MAX_SEQ_LEN", "8192"),
        "rca.model.quantization": os.environ.get("MODEL_QUANTIZATION", "exl2"),
        "rca.model.tokenizer": os.environ.get("MODEL_TOKENIZER", "qwen14b"),
        "rca.model.revision": os.environ.get("MODEL_REVISION", "unknown"),
    }
)

# ── Traces ───────────────────────────────────────────────────────────────────
_trace_provider = TracerProvider(resource=_resource)
_trace_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=_OTLP_BASE + "v1/traces"))
)
trace.set_tracer_provider(_trace_provider)
_tracer = trace.get_tracer(__name__)

# ── Metrics ──────────────────────────────────────────────────────────────────
_meter_provider = MeterProvider(
    resource=_resource,
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_OTLP_BASE + "v1/metrics"),
            export_interval_millis=30_000,
        )
    ],
)
metrics.set_meter_provider(_meter_provider)
_meter = metrics.get_meter(__name__)

_hist_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration",
    unit="s",
    description="Wall-clock time of a /generate call (seconds)",
)
_hist_tokens = _meter.create_histogram(
    "gen_ai.client.token.usage",
    unit="{token}",
    description="Token usage per request (gen_ai.token.type=input|output)",
)


def _capture_content() -> bool:
    """Whether prompt/response text may be attached to spans (opt-in)."""
    flag = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


# ── Logs ─────────────────────────────────────────────────────────────────────
_log_provider = LoggerProvider(resource=_resource)
_log_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=_OTLP_BASE + "v1/logs"))
)
_log_handler = LoggingHandler(level=logging.DEBUG, logger_provider=_log_provider)
_logger = logging.getLogger("llm-qwen14b")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)
# ─────────────────────────────────────────────────────────────────────────────


def _count_tokens(text: str) -> int:
    """Count tokens using the loaded ExLlamaV2 tokenizer."""
    ids = engine.tokenizer.encode(text)
    return int(ids.shape[-1])


@asynccontextmanager
async def _lifespan(_application: FastAPI):
    print(
        f"[DEBUG] lifespan startup  pid={os.getpid()} otlp={_OTLP_ENDPOINT}",
        flush=True,
    )
    yield
    print(f"[DEBUG] lifespan shutdown pid={os.getpid()}", flush=True)
    _trace_provider.shutdown()
    _meter_provider.shutdown()
    _log_provider.shutdown()


app = FastAPI(title="Qwen 14B Local API (ExLlamaV2)", lifespan=_lifespan)
FastAPIInstrumentor.instrument_app(app)


class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 1024


class ChatCompletionRequest(BaseModel):
    model: str = "qwen-14b"
    messages: list[dict[str, Any]]
    max_tokens: int = 1024
    temperature: Optional[float] = None
    stop: Optional[Any] = None
    stream: bool = False
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Any = "auto"
    parallel_tool_calls: bool = False


def _run_generation(
    prompt: str, max_new_tokens: int, request: Request, stop: Any = None
) -> dict[str, Any]:
    """Generate a completion and capture telemetry metadata."""
    tokens_in = _count_tokens(prompt)
    ctx = extract(dict(request.headers))
    capture = _capture_content()

    with _tracer.start_as_current_span("chat qwen-14b", context=ctx) as span:
        span.set_attribute("gen_ai.system", "qwen")
        span.set_attribute("gen_ai.provider.name", "custom")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", "qwen-14b")
        span.set_attribute("gen_ai.request.max_tokens", max_new_tokens)
        if capture:
            span.set_attribute("gen_ai.prompt", prompt)
        if tokens_in >= 0:
            span.set_attribute("gen_ai.usage.input_tokens", tokens_in)

        t0 = time.perf_counter()
        output, generated_tokens = engine.generate_with_metadata(
            prompt=prompt,
            max_new_tokens=max_new_tokens,
        )
        output_before_stop = output
        output = apply_stop_sequences(output, stop)
        duration = time.perf_counter() - t0

        tokens_out = generated_tokens
        visible_tokens_out = _count_tokens(output)
        finish_reason = (
            "stop"
            if output != output_before_stop or generated_tokens < max_new_tokens
            else "length"
        )
        span.set_attribute("gen_ai.response.model", "qwen-14b")
        if capture:
            span.set_attribute("gen_ai.completion", output)
        span.set_attribute("gen_ai.client.operation.duration", round(duration, 3))
        if tokens_out >= 0:
            span.set_attribute("gen_ai.usage.output_tokens", tokens_out)
        span.set_attribute("gen_ai.response.finish_reasons", [finish_reason])
        span.set_attribute("rca.llm.visible_output_tokens", visible_tokens_out)
        span.set_attribute("rca.llm.prefill_duration_available", False)
        span.set_attribute("rca.llm.decode_duration_available", False)

    _attrs = {"gen_ai.system": "qwen", "gen_ai.request.model": "qwen-14b"}
    _hist_duration.record(duration, attributes=_attrs)
    if tokens_in >= 0:
        _hist_tokens.record(
            tokens_in, attributes={**_attrs, "gen_ai.token.type": "input"}
        )
    if tokens_out >= 0:
        _hist_tokens.record(
            tokens_out, attributes={**_attrs, "gen_ai.token.type": "output"}
        )

    _logger.info(
        json.dumps(
            {
                "event": "gen_ai.chat",
                "gen_ai.system": "qwen",
                "gen_ai.request.model": "qwen-14b",
                "gen_ai.prompt": prompt if capture else None,
                "gen_ai.completion": output if capture else None,
                "gen_ai.usage.input_tokens": tokens_in,
                "gen_ai.usage.output_tokens": tokens_out,
                "gen_ai.response.finish_reasons": [finish_reason],
                "rca.llm.visible_output_tokens": visible_tokens_out,
                "gen_ai.client.operation.duration": round(duration, 3),
            }
        ),
        extra={
            "gen_ai.system": "qwen",
            "gen_ai.request.model": "qwen-14b",
            "gen_ai.usage.input_tokens": tokens_in,
            "gen_ai.usage.output_tokens": tokens_out,
            "gen_ai.client.operation.duration": round(duration, 3),
        },
    )
    return {
        "output": output,
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "visible_completion_tokens": visible_tokens_out,
        "finish_reason": finish_reason,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": "qwen-14b",
        "endpoints": ["/generate", "/v1/chat/completions", "/v1/models"],
    }


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "qwen-14b",
                "object": "model",
                "owned_by": "local",
            }
        ],
    }


@app.post("/generate")
def generate(req: GenerateRequest, request: Request):
    result = _run_generation(req.prompt, req.max_new_tokens, request)
    return {
        "response": result["output"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "total_tokens": result["prompt_tokens"] + result["completion_tokens"],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest, request: Request):
    if req.stream:
        raise HTTPException(status_code=400, detail="stream=true is not supported")
    if req.model and req.model != "qwen-14b":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported model: {req.model}",
        )
    try:
        validate_tool_request(req.tools, req.tool_choice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    prompt = render_chat_messages(
        req.messages,
        tools=req.tools,
        tool_choice=req.tool_choice,
        parallel_tool_calls=req.parallel_tool_calls,
    )
    result = _run_generation(prompt, req.max_tokens, request, stop=req.stop)
    with _tracer.start_as_current_span("qwen.tool_call.translate") as span:
        span.set_attribute("gen_ai.system", "qwen")
        span.set_attribute("gen_ai.provider.name", "custom")
        span.set_attribute("gen_ai.request.model", "qwen-14b")
        span.set_attribute("rca.gen_ai.request.tools_count", len(req.tools or []))
        span.set_attribute(
            "rca.gen_ai.request.tool_choice",
            req.tool_choice if isinstance(req.tool_choice, str) else "function",
        )
        span.set_attribute(
            "rca.gen_ai.request.parallel_tool_calls", req.parallel_tool_calls
        )
        tool_calls = parse_tool_call_output(
            result["output"],
            req.tools,
            tool_choice=req.tool_choice,
            parallel_tool_calls=req.parallel_tool_calls,
        )
        if tool_choice_requires_call(req.tool_choice) and not tool_calls:
            span.set_attribute("rca.gen_ai.response.tool_calls_count", 0)
            span.set_attribute("rca.gen_ai.tool_call.translation_valid", False)
            raise HTTPException(
                status_code=502,
                detail="model did not return a valid tool call for the requested tool_choice",
            )
        span.set_attribute(
            "rca.gen_ai.response.tool_calls_count", len(tool_calls or [])
        )
        span.set_attribute("rca.gen_ai.tool_call.translation_valid", True)
        finish_reason = "tool_calls" if tool_calls else result["finish_reason"]
        span.set_attribute("gen_ai.response.finish_reasons", [finish_reason])
        for tool_call in tool_calls or []:
            function = tool_call.get("function") or {}
            span.add_event(
                "gen_ai.tool_call",
                {
                    "gen_ai.tool.name": str(function.get("name") or "unknown"),
                    "gen_ai.tool.call.id": str(tool_call.get("id") or ""),
                },
            )
        return build_chat_completion_response(
            content=None if tool_calls else result["output"],
            model="qwen-14b",
            prompt_tokens=result["prompt_tokens"],
            completion_tokens=result["completion_tokens"],
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
