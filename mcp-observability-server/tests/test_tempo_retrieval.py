"""Tests for trace collection coverage metadata."""

import json
import importlib

mcp_main = importlib.import_module("main")


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
