"""Testes dos gates de qualidade por run e do gate runner.

Cada gate é testado com bundles sintéticos: caso passa, caso falha,
caso skip (quando aplicável). O runner é testado pela regra de
agregação (usable = nenhuma falha blocking).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from experiment.platform.backend.quality import (
    RunBundle,
    build_run_health,
    run_gates,
)
from experiment.platform.backend.quality.gates import (
    gate_collection_completeness,
    gate_feature_sanity,
    gate_graph_connectivity,
    gate_label_consistency,
    gate_symptom_presence,
    gate_window_sanity,
)

# ── Fixtures de bundle ──────────────────────────────────────────────────

GOOD_TIMINGS = {
    "t_start": "2026-09-01T10:00:00+00:00",
    "t_fault_start": "2026-09-01T10:03:00+00:00",
    "t_fault_end": "2026-09-01T10:05:00+00:00",
    "fault_injection_duration_seconds": 120,
}

GOOD_FEATURES = {
    "verdict": "degraded_recoverable",
    "chaos_type": "memory-hog",
    "ground_truth_service": "catalogue",
    "ground_truth_fault_category": "memory-exhaustion",
    "affected_services": ["catalogue"],
    "phase_metrics": {
        mid: {
            "baseline": {"count": 20, "mean": 1.0},
            "fault": {"count": 20, "mean": 2.0},
        }
        for mid in ("traffic", "error_rate", "latency_p95")
    },
    "metric_hotspots": {
        "memory_saturation_pct": {
            "top": [
                {"label": "catalogue", "baseline_mean": 40.0, "fault_mean": 95.0, "delta_abs": 55.0}
            ]
        }
    },
    "propagation_graph": {
        "nodes": ["front-end", "catalogue"],
        "edges": [{"from": "front-end", "to": "catalogue"}],
        "cascade_order": [{"service": "catalogue"}],
    },
}


def _metric_payload(n_series: int = 3) -> Dict[str, Any]:
    return {
        mid: {"data": {"result": [{"metric": {"name": f"svc-{i}"}} for i in range(n_series)]}}
        for mid in ("traffic", "error_rate", "latency_p95")
    }


def make_bundle(
    *,
    artifacts: Optional[Dict[str, Any]] = None,
    features: Optional[Dict[str, Any]] = None,
    timings: Optional[Dict[str, Any]] = None,
    events: Optional[list] = None,
) -> RunBundle:
    """Bundle sintético saudável por default; cada teste estraga 1 coisa."""
    if artifacts is None:
        artifacts = {
            "metrics_raw": {"window_start": "t0", "window_end": "t1", "data": _metric_payload()},
            "traces_raw": {"window_start": "t0", "window_end": "t1", "traces": [{}], "count": 1},
            "load_summary": {"ok": True},
            "chaos_artifacts": {"ok": True},
        }
    return RunBundle(
        run_id="run-test",
        experiment_id="exp-test",
        status="completed",
        timings=GOOD_TIMINGS if timings is None else timings,
        features=GOOD_FEATURES if features is None else features,
        events=events or [],
        artifact_names=list(artifacts.keys()),
        load_artifact=lambda name: artifacts.get(name),
    )


# ── collection_completeness ─────────────────────────────────────────────
def test_collection_ok():
    r = gate_collection_completeness(make_bundle())
    assert r.status == "pass" and r.blocking


def test_collection_traces_empty():
    """O bug histórico: traces_raw existe mas count=0."""
    b = make_bundle()
    b.load_artifact = lambda name: (
        {"window_start": "t0", "window_end": "t1", "traces": [], "count": 0}
        if name == "traces_raw"
        else {
            "metrics_raw": {"data": _metric_payload()},
            "load_summary": {},
            "chaos_artifacts": {},
        }.get(name)
    )
    r = gate_collection_completeness(b)
    assert r.status == "fail"
    assert r.reason == "artifacts_missing_or_empty"
    assert any(e["artifact"] == "traces_raw" for e in r.evidence["empty"])


def test_collection_metric_without_series():
    b = make_bundle()
    payload = _metric_payload()
    payload["error_rate"] = {"data": {"result": []}}
    artifacts = {
        "metrics_raw": {"data": payload},
        "traces_raw": {"traces": [{}], "count": 1},
        "load_summary": {},
        "chaos_artifacts": {},
    }
    b = make_bundle(artifacts=artifacts)
    r = gate_collection_completeness(b)
    assert r.status == "fail"
    assert "error_rate" in r.evidence["empty"][0]["missing_series"]


# ── window_sanity ───────────────────────────────────────────────────────
def test_window_ok():
    assert gate_window_sanity(make_bundle()).status == "pass"


def test_window_missing_timestamps():
    b = make_bundle(timings={"t_start": "x"})
    r = gate_window_sanity(b)
    assert r.status == "fail" and r.reason == "missing_phase_timestamps"


def test_window_thin_phase():
    feats = dict(GOOD_FEATURES)
    feats["phase_metrics"] = {
        "traffic": {"baseline": {"count": 2}, "fault": {"count": 20}},
        "error_rate": {"baseline": {"count": 20}, "fault": {"count": 20}},
        "latency_p95": {"baseline": {"count": 20}, "fault": {"count": 20}},
    }
    r = gate_window_sanity(make_bundle(features=feats))
    assert r.status == "fail" and r.reason == "insufficient_phase_points"


def test_window_skip_without_phase_metrics():
    feats = dict(GOOD_FEATURES)
    feats["phase_metrics"] = {}
    assert gate_window_sanity(make_bundle(features=feats)).status == "skip"


# ── label_consistency ───────────────────────────────────────────────────
def test_label_ok():
    assert gate_label_consistency(make_bundle()).status == "pass"


def test_label_missing_ground_truth():
    feats = dict(GOOD_FEATURES)
    feats["ground_truth_service"] = None
    r = gate_label_consistency(make_bundle(features=feats))
    assert r.status == "fail" and r.reason == "missing_ground_truth"


def test_label_target_not_observed():
    feats = dict(GOOD_FEATURES)
    feats["ground_truth_service"] = "servico-inexistente"
    r = gate_label_consistency(make_bundle(features=feats))
    assert r.status == "fail" and r.reason == "ground_truth_service_not_observed"


# ── graph_connectivity ──────────────────────────────────────────────────
def test_graph_ok():
    assert gate_graph_connectivity(make_bundle()).status == "pass"


def test_graph_skip_without_graph():
    feats = dict(GOOD_FEATURES)
    feats.pop("propagation_graph")
    assert gate_graph_connectivity(make_bundle(features=feats)).status == "skip"


def test_graph_target_missing():
    feats = dict(GOOD_FEATURES)
    feats["propagation_graph"] = {
        "nodes": ["front-end", "orders"],
        "edges": [{"from": "front-end", "to": "orders"}],
    }
    r = gate_graph_connectivity(make_bundle(features=feats))
    assert r.status == "fail" and r.reason == "ground_truth_not_in_graph"


# ── feature_sanity ──────────────────────────────────────────────────────
def test_feature_sanity_ok():
    assert gate_feature_sanity(make_bundle()).status == "pass"


def test_feature_sanity_nan():
    feats = dict(GOOD_FEATURES)
    feats["metric_hotspots"] = {
        "error_rate": {"top": [{"label": "x", "delta_abs": float("nan")}]}
    }
    r = gate_feature_sanity(make_bundle(features=feats))
    assert r.status == "fail" and r.reason == "non_finite_features"


# ── symptom_presence (informativo, nunca blocking) ──────────────────────
def test_symptom_present():
    r = gate_symptom_presence(make_bundle())
    assert r.status == "pass" and not r.blocking


def test_symptom_weak_signal_not_blocking():
    feats = dict(GOOD_FEATURES)
    feats["metric_hotspots"] = {
        "error_rate": {"top": [{"label": "x", "delta_abs": 0.01}]}
    }
    r = gate_symptom_presence(make_bundle(features=feats))
    assert r.status == "fail"
    assert r.reason == "weak_signal"
    assert not r.blocking  # informativo: nunca torna o run inutilizável


# ── runner (agregação) ──────────────────────────────────────────────────
def test_runner_healthy_run_is_usable():
    q = run_gates(make_bundle())
    assert q["usable"] is True
    assert q["gaps"] == []
    assert len(q["gates"]) >= 6  # catálogo pode crescer
    assert q["schema_version"] == "1.0"


def test_runner_blocking_failure_marks_unusable():
    b = make_bundle(features={})  # sem ground truth → label_consistency falha
    q = run_gates(b)
    assert q["usable"] is False
    assert "missing_ground_truth" in q["gaps"]


def test_runner_weak_signal_does_not_block():
    feats = dict(GOOD_FEATURES)
    feats["metric_hotspots"] = {"error_rate": {"top": [{"label": "x", "delta_abs": 0.0}]}}
    q = run_gates(make_bundle(features=feats))
    assert q["usable"] is True
    assert q["weak_signal"] is True


# ── edge_features_present ───────────────────────────────────────────────
def test_edge_features_skip_without_graph():
    from experiment.platform.backend.quality.gates import gate_edge_features_present
    r = gate_edge_features_present(make_bundle())
    assert r.status == "skip" and r.reason == "no_gat_graph"


def test_edge_features_pass_with_real_edges():
    from experiment.platform.backend.quality.gates import gate_edge_features_present
    graph = {
        "edges": [
            {"src": 0, "dst": 1, "features": {"req_rate_lr": 0.5, "latency_lr": None, "error_lr": None}},
            {"src": 1, "dst": 0, "features": {"req_rate_lr": None, "latency_lr": None, "error_lr": None}},
        ]
    }
    b = make_bundle(artifacts={"gat_graph": graph})
    r = gate_edge_features_present(b)
    assert r.status == "pass"
    assert r.evidence["with_features"] == 1


def test_edge_features_fail_all_neutral():
    from experiment.platform.backend.quality.gates import gate_edge_features_present
    graph = {
        "edges": [
            {"src": 0, "dst": 1, "features": {"req_rate_lr": None, "latency_lr": None, "error_lr": None}},
        ]
    }
    b = make_bundle(artifacts={"gat_graph": graph})
    r = gate_edge_features_present(b)
    assert r.status == "fail" and r.reason == "all_edges_neutral"


def test_edge_features_fail_no_edges():
    from experiment.platform.backend.quality.gates import gate_edge_features_present
    b = make_bundle(artifacts={"gat_graph": {"edges": []}})
    r = gate_edge_features_present(b)
    assert r.status == "fail" and r.reason == "no_edges_in_graph"


# ── run_health (relatório consolidado) ──────────────────────────────────
def test_run_health_structure():
    events = [
        {"ts": "2026-09-01T10:03:00+00:00", "event_type": "fault_started", "payload": {}},
        {"ts": "2026-09-01T10:00:00+00:00", "event_type": "run_started", "payload": {}},
    ]
    b = make_bundle(events=events)
    h = build_run_health(b)
    assert h["schema_version"] == "1.0"
    assert h["run_id"] == "run-test"
    assert h["quality"]["usable"] is True
    # timeline ordenada por ts
    assert [e["event_type"] for e in h["timeline"]] == ["run_started", "fault_started"]
    # checklist de artefatos
    assert "metrics_raw" in h["artifacts"]["present"]
    assert "run_features" in h["artifacts"]["missing"]  # não está no bundle sintético
