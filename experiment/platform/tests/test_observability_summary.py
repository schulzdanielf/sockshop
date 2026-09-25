from experiment.platform.backend.analysis.observability_summary import (
    build_observability_summary,
)


def test_observability_summary_preserves_gaps_without_inventing_rca():
    result = build_observability_summary(
        run_id="run-1",
        experiment_id="exp-1",
        observation_window={"window": {"start": "s", "end": "e"}},
        metrics_raw={
            "window_start": "s",
            "window_end": "e",
            "data": {
                "error_rate": {"data": {"result": []}},
                "latency_p95": {"data": {"result": [{"values": [[1, "2"]]}]}},
            },
        },
        metrics_summary={"metrics": {"latency_p95": {"count": 1}}},
        traces_raw={
            "window_start": "s",
            "window_end": "e",
            "collection": {"empty_reason": "no_traces_in_window"},
        },
        traces_summary={"trace_count": 0},
        features={
            "phase_metrics": {"error_rate": {"baseline": {"count": 0}}},
            "metric_hotspots": {},
            "affected_services": [],
            "top_failure_signatures": [],
            "rca_hypotheses": [{"hypothesis": "must not be copied"}],
            "slo_violations": [],
        },
        artifact_paths={"metrics_raw": "metrics_raw.json"},
    )

    assert result["gaps"]["empty_metric_queries"] == ["error_rate"]
    assert result["gaps"]["trace_empty_reason"] == "no_traces_in_window"
    assert "rca_hypotheses" not in result
    assert result["artifacts"]["metrics_raw"] == "metrics_raw.json"