from __future__ import annotations

from experiment.platform.backend.engine import OrchestratorEngine


class _Metrics:
    def summarize_per_phase(self, _raw, _phases):
        return {
            "error_rate": {
                "baseline": {"count": 4, "mean": 2.0, "max": 2.0},
                "warmup": {"count": 4, "mean": 2.0, "max": 2.0},
                "fault": {"count": 4, "mean": 7.0, "max": 7.0},
                "post": {"count": 4, "mean": 6.5, "max": 6.5},
            },
            "latency_p95": {
                "baseline": {"count": 4, "mean": 1000.0, "p95": 1000.0},
                "warmup": {"count": 4, "mean": 1000.0, "p95": 1000.0},
                "fault": {"count": 4, "mean": 1700.0, "p95": 1700.0},
                "post": {"count": 4, "mean": 1200.0, "p95": 1200.0},
            },
        }

    def per_label_hotspots(self, _raw, _phases, top_k=3):
        assert top_k == 3
        return {}

    def find_recovery_time(
        self, _raw, metric_id, _baseline, _threshold, _fault_end, _recovery_end, _tolerance
    ):
        return 12.0 if metric_id == "error_rate" else None


class _Traces:
    def aggregate_failures(self, _raw):
        return {}

    def build_propagation_graph(self, _raw):
        return {}


def test_relative_slo_thresholds_and_unrecovered_metric_drive_verdict():
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.metrics = _Metrics()
    engine.traces = _Traces()
    spec = {
        "analysis": {
            "slo": {
                "error_rate_threshold": 5.0,
                "error_rate_relative_factor": 3.0,
                "latency_p95_threshold_ms": 800.0,
                "latency_p95_relative_factor": 1.5,
                "recovery_tolerance_pct": 0.2,
                "max_recovery_seconds": 600,
            }
        },
        "chaos_profile": {"provider": "litmus", "chaos_engine": "carts-cpu-hog"},
    }

    build_run_features = getattr(engine, "_build_run_features")
    features = build_run_features(
        spec,
        metrics_raw={},
        traces_raw={},
        traces_summary={"error_trace_count": 0},
        phases=[],
        fault_end_iso="2026-09-30T00:01:00+00:00",
        recovery_end_iso="2026-09-30T00:04:00+00:00",
    )

    assert features["slo_effective_thresholds"]["error_rate"]["effective"] == 6.0
    assert features["slo_effective_thresholds"]["latency_p95"]["effective"] == 1500.0
    assert {violation["metric"] for violation in features["slo_violations"]} == {
        "error_rate",
        "latency_p95",
    }
    assert features["recovery_by_metric"] == {"error_rate": 12.0, "latency_p95": None}
    assert features["recovery_time_seconds"] is None
    assert features["verdict"] == "degraded_persistent"
