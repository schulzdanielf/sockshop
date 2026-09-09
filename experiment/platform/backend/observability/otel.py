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

import os
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


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def capture_content() -> bool:
    """Whether prompt/response text may be attached to spans (opt-in)."""
    return _truthy(os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"))


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
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
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
    model: str = "qwen-14b",
) -> None:
    """Record GenAI client duration + token-usage histograms (best-effort)."""
    if _hist_duration is None:
        return
    base_attrs = {GenAI.SYSTEM: "qwen", GenAI.REQUEST_MODEL: model}
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
