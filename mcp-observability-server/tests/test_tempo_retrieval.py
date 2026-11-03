"""Tests for trace collection coverage metadata."""

import json
import importlib

import pytest

mcp_main = importlib.import_module("main")


@pytest.mark.parametrize(
    ("tool_name", "investigator_name"),
    [
        ("get_cpu_saturation_top", "_inv_cpu_top"),
        ("get_memory_saturation_top", "_inv_memory_top"),
    ],
)
def test_saturation_tools_forward_service_scope(monkeypatch, tool_name, investigator_name):
    received = {}

    def investigator(_prometheus, **kwargs):
        received.update(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(mcp_main, "prometheus", object())
    monkeypatch.setattr(mcp_main, investigator_name, investigator)

    result = getattr(mcp_main, tool_name).fn(
        start="2026-10-16T10:01:00Z",
        end="2026-10-16T10:04:00Z",
        service="orders",
    )

    assert json.loads(result) == {"status": "success"}
    assert received["service"] == "orders"


def test_tempo_analyzed_traces_reports_missing_ids_and_fetch_errors(monkeypatch):
    class _Tempo:
        def search_traces(self, **_kwargs):
            return {
                "traces": [
                    {"traceID": "trace-1"},
                    {"rootTraceName": "trace-without-id"},
                ]
            }

        def get_trace(self, _trace_id):
            return {"status": "error", "error": "temporary read failure"}

    monkeypatch.setattr(mcp_main, "tempo", _Tempo())

    result = getattr(mcp_main, "_tempo_analyzed_traces")(start="start", end="end")

    assert result["collection"] == {
        "search_count": 2,
        "trace_id_count": 1,
        "missing_trace_id_count": 1,
        "trace_fetch_error_count": 1,
        "analyzed_count": 0,
    }


def test_tempo_phase_graph_keeps_collection_metadata(monkeypatch):
    collection = {
        "search_count": 15,
        "trace_id_count": 14,
        "missing_trace_id_count": 1,
        "trace_fetch_error_count": 2,
        "analyzed_count": 12,
    }
    monkeypatch.setattr(
        mcp_main,
        "_tempo_analyzed_traces",
        lambda **_kwargs: {"traces": [], "search_count": 15, "collection": collection},
    )

    result = json.loads(
        mcp_main.tempo_service_graph_phases.fn(
            baseline_start="b0",
            baseline_end="b1",
            fault_start="f0",
            fault_end="f1",
        )
    )

    assert result["phases"]["baseline"]["collection"] == collection
    assert result["phases"]["fault"]["collection"] == collection


def test_tempo_phase_graph_preserves_bounded_failure_signatures(monkeypatch):
    def _phase(**kwargs):
        phase = "fault" if kwargs["start"] == "f0" else "baseline"
        return {
            "traces": [
                {
                    "trace_id": f"trace-{phase}",
                    "start_time_unix_nano": "1760000000000000000",
                    "features": {
                        "failure_signatures": [
                            {
                                "signature": "NETWORK_TIMEOUT",
                                "endpoint": "catalogue:80",
                                "affected_service": "user",
                                "operations": ["user.GET /"],
                                "occurrence_count": 2,
                                "max_duration_ms": 1200.0,
                            }
                        ]
                    },
                }
            ],
            "search_count": 1,
            "collection": {"analyzed_count": 1},
        }

    monkeypatch.setattr(mcp_main, "_tempo_analyzed_traces", _phase)

    result = json.loads(
        mcp_main.tempo_service_graph_phases.fn(
            baseline_start="b0",
            baseline_end="b1",
            fault_start="f0",
            fault_end="f1",
        )
    )

    fault_signatures = result["phases"]["fault"]["failure_signatures"]
    assert fault_signatures == [
        {
            "trace_id": "trace-fault",
            "start_time_unix_nano": "1760000000000000000",
            "signature": "NETWORK_TIMEOUT",
            "endpoint": "catalogue:80",
            "affected_service": "user",
            "operations": ["user.GET /"],
            "occurrence_count": 2,
            "max_duration_ms": 1200.0,
        }
    ]


def test_tempo_phase_graph_selects_slow_traces_without_error_spans(monkeypatch):
    def _phase(**kwargs):
        is_fault = kwargs["start"] == "f0"
        duration = 900.0 if is_fault else 100.0
        phase = "fault" if is_fault else "baseline"
        return {
            "traces": [{
                "trace_id": "a" * 32 if is_fault else "b" * 32,
                "root_service": "orders",
                "root_trace_name": "GET /orders",
                "start_time_unix_nano": "1760000000000000000",
                "duration_ms": duration,
                "features": {
                    "trace_duration_ms": duration,
                    "error_spans": [],
                    "hot_spans": [{
                        "service": "payment",
                        "name": "POST /charge",
                        "exclusive_duration_ms": duration,
                    }],
                },
            }],
            "search_count": 1,
            "collection": {"analyzed_count": 1},
        }

    monkeypatch.setattr(mcp_main, "_tempo_analyzed_traces", _phase)

    result = json.loads(
        mcp_main.tempo_service_graph_phases.fn(
            baseline_start="b0",
            baseline_end="b1",
            fault_start="f0",
            fault_end="f1",
        )
    )

    assert result["phases"]["fault"]["failure_signatures"] == []
    assert result["phases"]["fault"]["latency_candidates"] == [{
        "trace_id": "a" * 32,
        "signature": "LATENCY_REGRESSION",
        "affected_service": "orders",
        "endpoint": "GET /orders",
        "operations": ["payment.POST /charge"],
        "start_time_unix_nano": "1760000000000000000",
        "fault_duration_ms": 900.0,
        "baseline_p95_ms": 100.0,
        "latency_delta_ms": 800.0,
        "baseline_sample_count": 1,
    }]
