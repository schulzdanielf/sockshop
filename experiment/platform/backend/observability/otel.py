"""OpenTelemetry bootstrap + GenAI/AgentOps helpers for the RCA platform.

This module centralises tracing/metrics wiring for the chaos-RCA decision
pipeline so the whole ``POST /api/runs/{id}/llm-analysis`` flow shows up as a
single *decision trace* (RAG → prompt → LLM → parse → validators → persist).

Design goals
------------
* **Env-driven & idempotent.** ``setup_telemetry`` configures the global
  providers at most once, reading the standard ``OTEL_*`` env vars.
* **Safe when offline.** If ``OTEL_SDK_DISABLED`` is truthy the SDK is never
  installed and every helper degrades to a cheap no-op, so unit tests and
  air-gapped runs are unaffected. The OTLP exporter itself buffers/retries, so
  a missing collector never breaks a request either.
* **OTel GenAI semantic conventions.** Span/metric/attribute names follow the
  emerging ``gen_ai.*`` conventions (model, token usage, operation duration)
  instead of ad-hoc keys, so standard AgentOps tooling can read them.
* **Content capture is opt-in.** Prompt/response text is only attached when
  ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is truthy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opentelemetry import metrics, trace

# ── GenAI semantic-convention attribute keys ─────────────────────────────────
# Pinned as string constants (the semconv package still ships these under the
# unstable ``0.x`` line, so we avoid importing names that may be renamed).


class GenAI:
    """OpenTelemetry GenAI / AgentOps attribute keys used across the pipeline."""

    SYSTEM = "gen_ai.system"
    OPERATION_NAME = "gen_ai.operation.name"
    REQUEST_MODEL = "gen_ai.request.model"
    RESPONSE_MODEL = "gen_ai.response.model"
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    TOKEN_TYPE = "gen_ai.token.type"
    PROMPT = "gen_ai.prompt"
    COMPLETION = "gen_ai.completion"
    TOOL_NAME = "gen_ai.tool.name"

    # Metric instrument names.
    METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
    METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"

    # Project-specific AgentOps attributes (decision provenance).
    RAG_MODE = "rca.rag.mode"
    DECISION_SOURCE_RCA = "rca.decision.rca.source"
    DECISION_SOURCE_FAULT = "rca.decision.fault_category.source"


_DEFAULT_ENDPOINT = "http://localhost:4318"
_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "rca-platform")

_configured = False
_tracer: Optional[trace.Tracer] = None
_hist_duration = None
_hist_tokens = None
_counter_override = None
_llm_io_logger: Optional[logging.Logger] = None
_llm_io_stream_logger: Optional[logging.Logger] = None
_llm_io_otlp_logger: Optional[logging.Logger] = None
_llm_io_otlp_handler: Optional[logging.Handler] = None


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def capture_content() -> bool:
    """Whether prompt/response text may be attached to spans (opt-in)."""
    return _truthy(os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"))


def configure_llm_io_audit(path: Optional[str] = None) -> str:
    """Configure JSONL audit logging for all LLM request/response attempts.

    The default path can be overridden with ``LLM_IO_AUDIT_PATH``.
    """
    global _llm_io_logger, _llm_io_stream_logger, _llm_io_otlp_logger
    log_path = path or os.environ.get("LLM_IO_AUDIT_PATH")
    if not log_path:
        log_path = "experiment/platform/data/llm_io_audit.jsonl"

    dst = Path(log_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("rca.llm_io_audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Idempotent handler setup: replace existing handlers when reconfigured.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except OSError:
            pass

    handler = logging.FileHandler(dst, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _llm_io_logger = logger

    stream_logger = logging.getLogger("rca.llm_io_audit.stdout")
    stream_logger.setLevel(logging.INFO)
    stream_logger.propagate = False
    for h in list(stream_logger.handlers):
        stream_logger.removeHandler(h)
        try:
            h.close()
        except OSError:
            pass
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    stream_logger.addHandler(stream_handler)
    _llm_io_stream_logger = stream_logger

    if _llm_io_otlp_handler is not None:
        otlp_logger = logging.getLogger("rca.llm_io_audit.otlp")
        otlp_logger.setLevel(logging.INFO)
        otlp_logger.propagate = False
        for h in list(otlp_logger.handlers):
            otlp_logger.removeHandler(h)
            try:
                h.close()
            except OSError:
                pass
        otlp_logger.addHandler(_llm_io_otlp_handler)
        _llm_io_otlp_logger = otlp_logger
    return str(dst)


def _trace_context_ids() -> dict:
    """Return current trace/span IDs as hex strings when available."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx or not ctx.is_valid:
        return {"trace_id": None, "span_id": None}
    return {
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
    }


def record_llm_io(entry: dict) -> None:
    """Write one JSONL row for an LLM attempt (best-effort).

    Expected fields are flexible; callers can attach request context,
    response body, parse status, retry marker, and errors.
    """
    if _llm_io_logger is None:
        configure_llm_io_audit()
    if _llm_io_logger is None:
        return

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": _SERVICE_NAME,
        **_trace_context_ids(),
        **(entry or {}),
    }
    try:
        line = json.dumps(payload, ensure_ascii=True)
        _llm_io_logger.info(line)
        # Stdout emission enables direct Loki/Grafana log queries in
        # containerized deployments that scrape process output.
        if _llm_io_stream_logger is not None:
            _llm_io_stream_logger.info(line)
        # OTLP bridge enables forwarding logs directly to the collector,
        # even when this process is not running inside Kubernetes.
        if _llm_io_otlp_logger is not None:
            _llm_io_otlp_logger.info(line)
    except (TypeError, ValueError, OSError):
        # Observability must never break request execution.
        return


def setup_telemetry(app=None) -> None:
    """Configure global tracer/meter providers (idempotent).

    When ``OTEL_SDK_DISABLED`` is truthy the SDK is left uninstalled and the
    global API no-op providers are used, so spans/metrics become free shims.
    Pass the FastAPI ``app`` to also enable inbound HTTP auto-instrumentation.
    """
    global _configured, _tracer, _hist_duration, _hist_tokens, _counter_override
    if _configured:
        if app is not None:
            _instrument_fastapi(app)
        return
    _configured = True

    if not _truthy(os.environ.get("OTEL_SDK_DISABLED")):
        _install_sdk()

    _tracer = trace.get_tracer("rca.platform")
    meter = metrics.get_meter("rca.platform")
    _hist_duration = meter.create_histogram(
        GenAI.METRIC_OPERATION_DURATION,
        unit="s",
        description="GenAI client operation wall-clock duration",
    )
    _hist_tokens = meter.create_histogram(
        GenAI.METRIC_TOKEN_USAGE,
        unit="{token}",
        description="GenAI token usage per request (input/output)",
    )
    _counter_override = meter.create_counter(
        "rca.validator.override",
        unit="{override}",
        description="Deterministic post-processor overrides of the LLM answer",
    )

    if app is not None:
        _instrument_fastapi(app)


def _install_sdk() -> None:
    global _llm_io_otlp_handler
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry._logs import get_logger_provider, set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.semconv.resource import ResourceAttributes

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_ENDPOINT)
    base = endpoint.rstrip("/") + "/"
    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: _SERVICE_NAME,
            ResourceAttributes.SERVICE_VERSION: os.environ.get(
                "OTEL_SERVICE_VERSION", "1.0.0"
            ),
            "deployment.environment": os.environ.get("DEPLOYMENT_ENV", "local"),
        }
    )

    # Don't clobber a provider already installed by the host process.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "v1/traces"))
        )
        trace.set_tracer_provider(tp)

    if not isinstance(metrics.get_meter_provider(), MeterProvider):
        mp = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=base + "v1/metrics"),
                    export_interval_millis=30_000,
                )
            ],
        )
        metrics.set_meter_provider(mp)

    # OTLP log export (Python stdlib logging -> OTel logs pipeline -> Loki)
    if not isinstance(get_logger_provider(), LoggerProvider):
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=base + "v1/logs"))
        )
        set_logger_provider(lp)
    _llm_io_otlp_handler = LoggingHandler(
        level=logging.INFO,
        logger_provider=get_logger_provider(),
    )


def _instrument_fastapi(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # pragma: no cover - instrumentation is best-effort
        pass


def get_tracer() -> trace.Tracer:
    """Return the platform tracer (configuring telemetry lazily if needed)."""
    if _tracer is None:
        setup_telemetry()
    return _tracer  # type: ignore[return-value]


def record_llm_metrics(
    *,
    duration_s: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    model: Optional[str] = None,
) -> None:
    """Record GenAI client duration + token-usage histograms (best-effort)."""
    if _hist_duration is None:
        return
    req_model = model or os.environ.get("LLM_MODEL", "qwen-14b")
    sys_name = "qwen" if "qwen" in req_model.lower() else "llm"
    base_attrs = {GenAI.SYSTEM: sys_name, GenAI.REQUEST_MODEL: req_model}
    if duration_s is not None:
        _hist_duration.record(duration_s, attributes=base_attrs)
    if input_tokens is not None and input_tokens >= 0:
        _hist_tokens.record(
            input_tokens, attributes={**base_attrs, GenAI.TOKEN_TYPE: "input"}
        )
    if output_tokens is not None and output_tokens >= 0:
        _hist_tokens.record(
            output_tokens, attributes={**base_attrs, GenAI.TOKEN_TYPE: "output"}
        )


def record_override(dimension: str, rule: Optional[str]) -> None:
    """Count one deterministic override of the LLM answer (best-effort)."""
    if _counter_override is None:
        return
    _counter_override.add(
        1, attributes={"rca.dimension": dimension, "rca.rule": rule or "unknown"}
    )
