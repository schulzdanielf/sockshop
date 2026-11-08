import time

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from experiment.eval.memory_hog.evaluation_telemetry import EvaluationTelemetry
from experiment.eval.memory_hog.runner import ApiClient


def _telemetry():
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    tracer_provider = TracerProvider()
    exporter = InMemorySpanExporter()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = EvaluationTelemetry(
        evaluation_id="test-evaluation",
        retriever_version="v6",
        expected_cases=2,
        expected_diagnostics=2,
        started_at=time.time(),
        meter=meter_provider.get_meter("test"),
        tracer=tracer_provider.get_tracer("test"),
        meter_provider=meter_provider,
        tracer_provider=tracer_provider,
    )
    return telemetry, reader, meter_provider, tracer_provider, exporter


def test_evaluation_report_exports_counts_breakdowns_and_token_usage():
    telemetry, reader, meter_provider, tracer_provider, _ = _telemetry()
    report = {
        "diagnosis_summary": {
            "exploratory_rag": {
                "denominator": 2,
                "service_correct": 1,
                "service_accuracy": 0.5,
                "fault_type_correct": 2,
                "fault_type_accuracy": 1.0,
                "jointly_correct": 1,
                "joint_accuracy": 0.5,
            }
        },
        "performance": {
            "exploratory_rag": {
                "total_model_calls": 4,
                "total_tokens": 500,
                "measured_token_usage_calls": 1,
                "estimated_token_usage_calls": 3,
                "mean_request_duration_ms": 1500,
                "mean_response_duration_ms": 1200,
            }
        },
        "retrieval": {
            "test_case_denominator": 2,
            "hit_at_3_cases": 1,
            "hit_at_3": 0.5,
        },
        "diagnosis_relevance": {
            "with_relevant_retrieval": {
                "jointly_correct": 1,
                "joint_accuracy": 1.0,
            }
        },
        "by_service": {
            "orders": {"denominator": 1, "service_correct": 1},
            "catalogue": {"denominator": 1, "service_correct": 0},
        },
        "per_run": [
            {
                "service": "orders",
                "fault_type": "cpu-hog",
                "methods": {
                    "exploratory_rag": {
                        "fault_type_correct": True,
                        "joint_correct": True,
                    }
                },
            },
            {
                "service": "catalogue",
                "fault_type": "memory-hog",
                "methods": {
                    "exploratory_rag": {
                        "fault_type_correct": True,
                        "joint_correct": False,
                    }
                },
            },
        ],
    }

    telemetry.update_progress(
        {
            "evaluation_cases_expected": 2,
            "evaluation_cases_completed": 1,
            "evaluation_completion_ratio": 0.5,
        }
    )
    telemetry.record_report(report)
    assert meter_provider.force_flush()

    metrics = {
        metric.name: metric.data.data_points
        for resource_metrics in reader.get_metrics_data().resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert metrics["evaluation_cases_completed"][0].value == 1
    assert metrics["evaluation_completion_ratio"][0].value == 0.5
    assert metrics["evaluation_joint_correct"][0].value == 1
    assert metrics["evaluation_tokens_total"][0].value == 500
    assert metrics["evaluation_mean_request_duration_seconds"][0].value == 1.5
    orders = next(
        point
        for point in metrics["evaluation_cases_by_service"]
        if point.attributes["service"] == "orders"
    )
    assert orders.value == 1

    meter_provider.shutdown()
    tracer_provider.shutdown()


def test_case_and_api_spans_share_trace_context(monkeypatch):
    telemetry, _, meter_provider, tracer_provider, exporter = _telemetry()
    request_headers = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        request_headers.update(dict(request.header_items()))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = ApiClient("http://api.example", tracer=telemetry.tracer)
    with telemetry.case_span(
        case_id="case-1",
        run_id="run-1",
        index=1,
        strategy="exploratory_rag",
    ):
        assert api.get("/api/runs/run-1/skill-rag") == {"ok": True}

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].context.trace_id == spans[1].context.trace_id
    case_span = next(span for span in spans if span.name == "evaluation.case")
    client_span = next(span for span in spans if span.name.startswith("HTTP GET"))
    assert case_span.attributes["evaluation.id"] == "test-evaluation"
    assert client_span.attributes["http.route"] == "/api/runs/{run_id}/skill-rag"
    assert any(key.lower() == "traceparent" for key in request_headers)

    meter_provider.shutdown()
    tracer_provider.shutdown()
