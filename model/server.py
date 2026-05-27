import faulthandler
import json
import logging
import os
import threading
import time
faulthandler.enable()  # dumps Python traceback to stderr on SIGSEGV/SIGFPE

from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

print(
    f"[DEBUG] server.py importing model.llm"
    f"  (pid={os.getpid()}, tid={threading.get_ident()})",
    flush=True,
)
from model.llm import engine
print("[DEBUG] model.llm imported OK", flush=True)


# ── Shared OTel resource ─────────────────────────────────────────────────────
_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
)
# Normalise to a base URL with trailing slash so signal paths append correctly.
# The SDK only auto-appends /v1/{signal} when using the env-var path; when
# endpoint= is passed explicitly it uses the value verbatim (SDK ≥ 1.x).
_OTLP_BASE = _OTLP_ENDPOINT.rstrip("/") + "/"
_resource = Resource.create({
    ResourceAttributes.SERVICE_NAME: "llm-qwen14b",
    ResourceAttributes.SERVICE_VERSION: "1.0.0",
    "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "local"),
})

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
    "llm.request.duration",
    unit="s",
    description="Wall-clock time of a /generate call (seconds)",
)
_hist_tokens_prompt = _meter.create_histogram(
    "llm.tokens.prompt",
    unit="{token}",
    description="Input token count per request",
)
_hist_tokens_completion = _meter.create_histogram(
    "llm.tokens.completion",
    unit="{token}",
    description="Output token count per request",
)

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
    try:
        ids = engine.tokenizer.encode(text)
        return int(ids.shape[-1])
    except Exception:
        return -1


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(
        f"[DEBUG] lifespan startup  pid={os.getpid()} otlp={_OTLP_ENDPOINT}",
        flush=True,
    )
    yield
    print(f"[DEBUG] lifespan shutdown pid={os.getpid()}", flush=True)
    _trace_provider.shutdown()
    _meter_provider.shutdown()
    _log_provider.shutdown()


app = FastAPI(title="Qwen 14B Local API (ExLlamaV2)", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: int = 1024


@app.get("/health")
def health():
    return {"status": "ok", "model": "qwen-14b"}


@app.post("/generate")
def generate(req: GenerateRequest):
    tokens_in = _count_tokens(req.prompt)

    with _tracer.start_as_current_span("llm.generate") as span:
        span.set_attribute("llm.model", "qwen-14b")
        span.set_attribute("llm.prompt", req.prompt)
        span.set_attribute("llm.prompt_length", len(req.prompt))
        span.set_attribute("llm.max_new_tokens", req.max_new_tokens)
        if tokens_in >= 0:
            span.set_attribute("llm.tokens_prompt", tokens_in)

        t0 = time.perf_counter()
        output = engine.generate(
            prompt=req.prompt,
            max_new_tokens=req.max_new_tokens,
        )
        duration = time.perf_counter() - t0

        tokens_out = _count_tokens(output)
        span.set_attribute("llm.response", output)
        span.set_attribute("llm.response_length", len(output))
        span.set_attribute("llm.duration_s", round(duration, 3))
        if tokens_out >= 0:
            span.set_attribute("llm.tokens_completion", tokens_out)

    _attrs = {"llm.model": "qwen-14b"}
    _hist_duration.record(duration, attributes=_attrs)
    if tokens_in >= 0:
        _hist_tokens_prompt.record(tokens_in, attributes=_attrs)
    if tokens_out >= 0:
        _hist_tokens_completion.record(tokens_out, attributes=_attrs)

    _logger.info(
        json.dumps({
            "event": "llm.generate",
            "llm.model": "qwen-14b",
            "llm.prompt": req.prompt,
            "llm.response": output,
            "llm.tokens_prompt": tokens_in,
            "llm.tokens_completion": tokens_out,
            "llm.duration_s": round(duration, 3),
        }),
        extra={
            "llm.model": "qwen-14b",
            "llm.prompt": req.prompt,
            "llm.response": output,
            "llm.tokens_prompt": tokens_in,
            "llm.tokens_completion": tokens_out,
            "llm.duration_s": round(duration, 3),
        },
    )

    return {"response": output}

