"""OpenTelemetry instrumentation for test-base evaluation runs."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Iterator
from urllib.parse import urlsplit

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


_LOGGER = logging.getLogger(__name__)
_METRICS: dict[str, tuple[str, str]] = {
    "evaluation_run_state": (
        "Evaluation run state: 0=running, 1=scoring, 2=complete, 3=failed",
        "",
    ),
    "evaluation_run_started_timestamp_seconds": ("Evaluation start time", "s"),
    "evaluation_run_last_update_timestamp_seconds": (
        "Last evaluation progress update",
        "s",
    ),
    "evaluation_elapsed_seconds": ("Evaluation elapsed time", "s"),
    "evaluation_cases_expected": ("Expected evaluation cases", "{case}"),
    "evaluation_cases_completed": ("Completed evaluation cases", "{case}"),
    "evaluation_cases_failed": ("Failed evaluation cases", "{case}"),
    "evaluation_cases_in_progress": (
        "Evaluation cases currently in progress",
        "{case}",
    ),
    "evaluation_current_case_index": ("Current evaluation case index", "{case}"),
    "evaluation_diagnostics_expected": (
        "Expected case-method diagnostics",
        "{diagnostic}",
    ),
    "evaluation_diagnostics_completed": (
        "Completed case-method diagnostics",
        "{diagnostic}",
    ),
    "evaluation_completion_ratio": ("Evaluation completion ratio", "1"),
    "evaluation_cases_scored": ("Cases included in final scoring", "{case}"),
    "evaluation_service_correct": ("Cases with the correct service", "{case}"),
    "evaluation_fault_type_correct": ("Cases with the correct fault type", "{case}"),
    "evaluation_joint_correct": (
        "Cases with both the correct service and fault type",
        "{case}",
    ),
    "evaluation_service_accuracy": ("Service accuracy", "1"),
    "evaluation_fault_type_accuracy": ("Fault type accuracy", "1"),
    "evaluation_joint_accuracy": ("Joint service and fault type accuracy", "1"),
    "evaluation_retrieval_cases": ("Cases evaluated for retrieval", "{case}"),
    "evaluation_retrieval_hit_cases": (
        "Cases with a relevant result in top 3",
        "{case}",
    ),
    "evaluation_retrieval_hit_rate": ("Retrieval hit rate at 3", "1"),
    "evaluation_joint_correct_given_retrieval": (
        "Jointly correct diagnoses when retrieval was relevant",
        "{case}",
    ),
    "evaluation_joint_accuracy_given_retrieval": (
        "Joint accuracy when retrieval was relevant",
        "1",
    ),
    "evaluation_model_calls_total": (
        "Model requests made during evaluation",
        "{request}",
    ),
    "evaluation_tokens_total": ("Tokens used during evaluation", "{token}"),
    "evaluation_measured_token_calls": (
        "Requests with measured token usage",
        "{request}",
    ),
    "evaluation_estimated_token_calls": (
        "Requests with estimated token usage",
        "{request}",
    ),
    "evaluation_mean_request_duration_seconds": ("Mean API request duration", "s"),
    "evaluation_mean_response_duration_seconds": ("Mean model response duration", "s"),
    "evaluation_cases_by_service": ("Cases grouped by expected service", "{case}"),
    "evaluation_service_correct_by_service": (
        "Correct service diagnoses by expected service",
        "{case}",
    ),
    "evaluation_cases_by_fault_type": (
        "Cases grouped by expected fault type",
        "{case}",
    ),
    "evaluation_fault_type_correct_by_fault_type": (
        "Correct fault diagnoses by expected fault type",
        "{case}",
    ),
    "evaluation_cases_by_service_fault": (
        "Cases grouped by expected service and fault",
        "{case}",
    ),
    "evaluation_joint_correct_by_service_fault": (
        "Jointly correct diagnoses by expected service and fault",
        "{case}",
    ),
}

_PROGRESS_METRICS = (
    "evaluation_run_state",
    "evaluation_run_started_timestamp_seconds",
    "evaluation_run_last_update_timestamp_seconds",
    "evaluation_elapsed_seconds",
    "evaluation_cases_expected",
    "evaluation_cases_completed",
    "evaluation_cases_failed",
    "evaluation_cases_in_progress",
    "evaluation_current_case_index",
    "evaluation_diagnostics_expected",
    "evaluation_diagnostics_completed",
    "evaluation_completion_ratio",
)

MetricValue = tuple[float, Mapping[str, str]]


class EvaluationTelemetry:
    def __init__(
        self,
        *,
        evaluation_id: str,
        retriever_version: str,
        expected_cases: int,
        expected_diagnostics: int,
        started_at: float,
        meter: Meter,
        tracer: Tracer,
        meter_provider: MeterProvider,
        tracer_provider: TracerProvider,
    ) -> None:
        self.evaluation_id = evaluation_id
        self.expected_cases = expected_cases
        self.expected_diagnostics = expected_diagnostics
        self.started_at = started_at
        self._meter = meter
        self._tracer = tracer
        self._meter_provider = meter_provider
        self._tracer_provider = tracer_provider
        self._lock = threading.Lock()
        self._attributes = {
            "evaluation_id": evaluation_id,
            "retriever_version": retriever_version,
        }
        self._values: dict[str, list[MetricValue]] = {
            metric_name: [] for metric_name in _METRICS
        }

        for metric_name, (description, unit) in _METRICS.items():
            meter.create_observable_gauge(
                metric_name,
                callbacks=[self._callback(metric_name)],
                description=description,
                unit=unit,
            )

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    def _callback(
        self, metric_name: str
    ) -> Callable[[CallbackOptions], Iterable[Observation]]:
        def observe(_: CallbackOptions) -> Iterable[Observation]:
            with self._lock:
                values = tuple(self._values[metric_name])
            return [
                Observation(value, attributes=dict(attributes))
                for value, attributes in values
            ]

        return observe

    def _set_gauge(
        self,
        metric_name: str,
        value: float | int | None,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        if metric_name not in self._values:
            raise KeyError(f"Unknown evaluation metric: {metric_name}")
        values: list[MetricValue] = []
        if value is not None:
            values.append(
                (
                    float(value),
                    {**self._attributes, **(attributes or {})},
                )
            )
        with self._lock:
            self._values[metric_name] = values

    def _set_series(
        self,
        metric_name: str,
        series: Iterable[tuple[float | int, Mapping[str, str]]],
    ) -> None:
        if metric_name not in self._values:
            raise KeyError(f"Unknown evaluation metric: {metric_name}")
        values = [
            (
                float(value),
                {**self._attributes, **attributes},
            )
            for value, attributes in series
        ]
        with self._lock:
            self._values[metric_name] = values

    def update_progress(self, progress: Mapping[str, int | float]) -> None:
        for metric_name in _PROGRESS_METRICS:
            if metric_name in progress:
                self._set_gauge(metric_name, progress[metric_name])

    def set_scoring(self) -> None:
        self._set_gauge("evaluation_run_state", 1)
        self._set_gauge("evaluation_run_last_update_timestamp_seconds", _now())

    def record_report(self, report: Mapping[str, Any]) -> None:
        per_run = report["per_run"]
        retrieval = report["retrieval"]
        for strategy, summary in report["diagnosis_summary"].items():
            attributes = {"strategy": str(strategy)}
            self._set_gauge(
                "evaluation_cases_scored", summary["denominator"], attributes
            )
            for source, metric_name in (
                ("service_correct", "evaluation_service_correct"),
                ("fault_type_correct", "evaluation_fault_type_correct"),
                ("jointly_correct", "evaluation_joint_correct"),
                ("service_accuracy", "evaluation_service_accuracy"),
                ("fault_type_accuracy", "evaluation_fault_type_accuracy"),
                ("joint_accuracy", "evaluation_joint_accuracy"),
            ):
                self._set_gauge(metric_name, summary.get(source), attributes)

            performance = report["performance"].get(strategy, {})
            for source, metric_name in (
                ("total_model_calls", "evaluation_model_calls_total"),
                ("total_tokens", "evaluation_tokens_total"),
                ("measured_token_usage_calls", "evaluation_measured_token_calls"),
                ("estimated_token_usage_calls", "evaluation_estimated_token_calls"),
            ):
                self._set_gauge(metric_name, performance.get(source), attributes)
            for source, metric_name in (
                (
                    "mean_request_duration_ms",
                    "evaluation_mean_request_duration_seconds",
                ),
                (
                    "mean_response_duration_ms",
                    "evaluation_mean_response_duration_seconds",
                ),
            ):
                value = performance.get(source)
                self._set_gauge(
                    metric_name,
                    float(value) / 1000 if value is not None else None,
                    attributes,
                )

            self._set_gauge(
                "evaluation_retrieval_cases",
                retrieval.get("test_case_denominator"),
                attributes,
            )
            self._set_gauge(
                "evaluation_retrieval_hit_cases",
                retrieval.get("hit_at_3_cases"),
                attributes,
            )
            self._set_gauge(
                "evaluation_retrieval_hit_rate",
                retrieval.get("hit_at_3"),
                attributes,
            )

            if strategy == "exploratory_rag":
                relevance = report["diagnosis_relevance"]["with_relevant_retrieval"]
                self._set_gauge(
                    "evaluation_joint_correct_given_retrieval",
                    relevance.get("jointly_correct"),
                    attributes,
                )
                self._set_gauge(
                    "evaluation_joint_accuracy_given_retrieval",
                    relevance.get("joint_accuracy"),
                    attributes,
                )

            if strategy == "exploratory_rag":
                self._record_breakdowns(
                    per_run, report["by_service"], strategy, attributes
                )

    def _record_breakdowns(
        self,
        per_run: Sequence[Mapping[str, Any]],
        by_service: Mapping[str, Mapping[str, Any]],
        strategy: str,
        attributes: Mapping[str, str],
    ) -> None:
        self._set_series(
            "evaluation_cases_by_service",
            (
                (summary["denominator"], {**attributes, "service": service})
                for service, summary in by_service.items()
            ),
        )
        self._set_series(
            "evaluation_service_correct_by_service",
            (
                (summary["service_correct"], {**attributes, "service": service})
                for service, summary in by_service.items()
            ),
        )

        by_fault_type: dict[str, dict[str, int]] = {}
        by_service_fault: dict[tuple[str, str], dict[str, int]] = {}
        for row in per_run:
            service = str(row["service"])
            fault_type = str(row["fault_type"])
            scores = row["methods"][strategy]
            by_fault_type.setdefault(
                fault_type, {"denominator": 0, "fault_type_correct": 0}
            )
            by_fault_type[fault_type]["denominator"] += 1
            by_fault_type[fault_type]["fault_type_correct"] += int(
                bool(scores["fault_type_correct"])
            )
            pair = (service, fault_type)
            by_service_fault.setdefault(pair, {"denominator": 0, "joint_correct": 0})
            by_service_fault[pair]["denominator"] += 1
            by_service_fault[pair]["joint_correct"] += int(
                bool(scores["joint_correct"])
            )

        self._set_series(
            "evaluation_cases_by_fault_type",
            (
                (summary["denominator"], {**attributes, "fault_type": fault_type})
                for fault_type, summary in by_fault_type.items()
            ),
        )
        self._set_series(
            "evaluation_fault_type_correct_by_fault_type",
            (
                (
                    summary["fault_type_correct"],
                    {**attributes, "fault_type": fault_type},
                )
                for fault_type, summary in by_fault_type.items()
            ),
        )
        self._set_series(
            "evaluation_cases_by_service_fault",
            (
                (
                    summary["denominator"],
                    {
                        **attributes,
                        "service": service,
                        "fault_type": fault_type,
                    },
                )
                for (service, fault_type), summary in by_service_fault.items()
            ),
        )
        self._set_series(
            "evaluation_joint_correct_by_service_fault",
            (
                (
                    summary["joint_correct"],
                    {
                        **attributes,
                        "service": service,
                        "fault_type": fault_type,
                    },
                )
                for (service, fault_type), summary in by_service_fault.items()
            ),
        )

    @contextmanager
    def case_span(
        self, *, case_id: str, run_id: str, index: int, strategy: str
    ) -> Iterator[Span]:
        with self._tracer.start_as_current_span(
            "evaluation.case",
            attributes={
                "evaluation.id": self.evaluation_id,
                "evaluation.case_id": case_id,
                "evaluation.run_id": run_id,
                "evaluation.case_index": index,
                "evaluation.strategy": strategy,
            },
        ) as span:
            span.set_attribute(
                "evaluation.retriever_version",
                self._attributes["retriever_version"],
            )
            yield span

    def complete(self, elapsed_seconds: float | None = None) -> None:
        now = _now()
        self._set_gauge("evaluation_run_state", 2)
        self._set_gauge("evaluation_run_last_update_timestamp_seconds", now)
        self._set_gauge(
            "evaluation_elapsed_seconds",
            elapsed_seconds if elapsed_seconds is not None else now - self.started_at,
        )
        self._set_gauge("evaluation_cases_in_progress", 0)

    def fail(self) -> None:
        now = _now()
        self._set_gauge("evaluation_run_state", 3)
        self._set_gauge("evaluation_run_last_update_timestamp_seconds", now)
        self._set_gauge("evaluation_elapsed_seconds", now - self.started_at)

    def flush_and_shutdown(self) -> None:
        if not self._meter_provider.force_flush(timeout_millis=10000):
            _LOGGER.warning(
                "OpenTelemetry metric flush timed out for %s", self.evaluation_id
            )
        if not self._tracer_provider.force_flush(timeout_millis=10000):
            _LOGGER.warning(
                "OpenTelemetry trace flush timed out for %s", self.evaluation_id
            )
        self._meter_provider.shutdown()
        self._tracer_provider.shutdown()


def create_evaluation_telemetry(
    *,
    evaluation_id: str,
    retriever_version: str,
    expected_cases: int,
    expected_diagnostics: int,
    started_at: float | None = None,
) -> EvaluationTelemetry | None:
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        _LOGGER.warning(
            "OpenTelemetry SDK is disabled; evaluation metrics will not be exported"
        )
        return None

    endpoint = (
        os.environ.get("EVALUATION_OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or "http://127.0.0.1:30318"
    ).rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid OTLP endpoint: {endpoint}")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=1.5):
            pass
    except OSError as exc:
        _LOGGER.warning(
            "OpenTelemetry collector is unreachable at %s; evaluation continues without Grafana telemetry: %s",
            endpoint,
            exc,
        )
        return None

    resource = Resource.create(
        {
            "service.name": "evaluation-runner",
            "service.namespace": "sock-shop",
        }
    )
    metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
    metric_interval = int(os.environ.get("EVALUATION_OTEL_EXPORT_INTERVAL_MS", "5000"))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=metric_interval,
            )
        ],
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    meter = meter_provider.get_meter("sockshop.evaluation")
    tracer = tracer_provider.get_tracer("sockshop.evaluation")
    telemetry = EvaluationTelemetry(
        evaluation_id=evaluation_id,
        retriever_version=retriever_version,
        expected_cases=expected_cases,
        expected_diagnostics=expected_diagnostics,
        started_at=started_at if started_at is not None else _now(),
        meter=meter,
        tracer=tracer,
        meter_provider=meter_provider,
        tracer_provider=tracer_provider,
    )
    telemetry.update_progress(
        {
            "evaluation_run_state": 0,
            "evaluation_run_started_timestamp_seconds": telemetry.started_at,
            "evaluation_run_last_update_timestamp_seconds": telemetry.started_at,
            "evaluation_elapsed_seconds": 0,
            "evaluation_cases_expected": expected_cases,
            "evaluation_cases_completed": 0,
            "evaluation_cases_failed": 0,
            "evaluation_cases_in_progress": 0,
            "evaluation_current_case_index": 0,
            "evaluation_diagnostics_expected": expected_diagnostics,
            "evaluation_diagnostics_completed": 0,
            "evaluation_completion_ratio": 0,
        }
    )
    return telemetry


def _now() -> float:
    return time.time()
