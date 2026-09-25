"""Testes da adaptação do graph_builder ao service mesh Istio (edge features)."""
from __future__ import annotations

from experiment.platform.backend.analysis.graph_builder import (
    _detect_edge_source,
    _edge_features,
    build_gat_graph,
)

BASELINE = ("2026-09-16T10:00:00+00:00", "2026-09-16T10:05:00+00:00")
FAULT = ("2026-09-16T10:05:00+00:00", "2026-09-16T10:07:00+00:00")


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _istio_series(src, dst, code, base_val, fault_val):
    b0, b1 = _ts(BASELINE[0]), _ts(BASELINE[1])
    f0, f1 = _ts(FAULT[0]), _ts(FAULT[1])
    values = [[b0 + i * 15, str(base_val)] for i in range(20)]
    values += [[f0 + i * 15, str(fault_val)] for i in range(8)]
    return {"metric": {
        "source_workload": src, "destination_workload": dst,
        "response_code": code, "destination_workload_namespace": "sock-shop",
    }, "values": values}


def make_istio_metrics():
    """Prometheus-v3 payload com aresta front-end→catalogue: latência sobe 4x no fault."""
    return {
        "istio_requests_total": {"data": {"result": [
            _istio_series("front-end", "catalogue", "200", 5.0, 5.0),
            _istio_series("front-end", "catalogue", "500", 0.0, 0.5),
        ]}},
        "istio_request_duration_milliseconds_sum": {"data": {"result": [
            _istio_series("front-end", "catalogue", "200", 500.0, 2000.0),
        ]}},
        "istio_request_duration_milliseconds_count": {"data": {"result": [
            _istio_series("front-end", "catalogue", "200", 5.0, 5.0),
        ]}},
    }


def make_metrics_raw_flat():
    """metrics_raw com baseline == fault (sem mudança) para todos os nós."""
    from experiment.platform.backend.analysis.graph_builder import CORE_SERVICES
    b0, b1 = _ts(BASELINE[0]), _ts(BASELINE[1])
    f0, f1 = _ts(FAULT[0]), _ts(FAULT[1])
    pts = [(b0 + i * 15, 10.0) for i in range(20)] + [(f0 + i * 15, 10.0) for i in range(8)]
    data = {}
    for mid in ("traffic", "latency_p95", "error_rate"):
        series = [{"metric": {"name": svc}, "values": [[t, str(v)] for t, v in pts]} for svc in CORE_SERVICES]
        data[mid] = {"data": {"result": series}}
    return {"window_start": BASELINE[0], "window_end": FAULT[1], "data": data}


def test_detect_edge_source():
    assert _detect_edge_source({"istio_requests_total": {}}) == "full"
    assert _detect_edge_source({"traces_service_graph_request_total": {}}) == "user_only"


def test_edge_features_istio_latency_ratio():
    em = make_istio_metrics()
    feats = _edge_features(em, "front-end", "catalogue", _ts(FAULT[0]), _ts(FAULT[1]), _ts(BASELINE[0]), _ts(BASELINE[1]))
    # latência média: baseline=500/5=100ms, fault=2000/5=400ms → lr ≈ log(4) > 0
    assert feats["latency_lr"] is not None and feats["latency_lr"] > 0.9
    # request rate estável → lr ≈ 0
    assert feats["req_rate_lr"] is not None and abs(feats["req_rate_lr"]) < 0.2
    # erro: baseline 0/5=0, fault 0.5/5=0.1 → sobe → lr > 0
    assert feats["error_lr"] is not None and feats["error_lr"] > 0
    assert feats["call_count"] is not None and feats["call_count"] > 0


def test_edge_features_istio_missing_edge_is_neutral():
    em = make_istio_metrics()
    feats = _edge_features(em, "orders", "payment", _ts(FAULT[0]), _ts(FAULT[1]), _ts(BASELINE[0]), _ts(BASELINE[1]))
    # aresta orders→payment não existe no payload → todas None
    assert feats["req_rate_lr"] is None
    assert feats["latency_lr"] is None


def test_build_gat_graph_edge_source_full():
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw_flat(),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="catalogue",
        ground_truth_fault_category="cpu-hog",
        edge_metrics=make_istio_metrics(),
    )
    assert g["edge_source"] == "full"
    # a aresta front-end→catalogue deve carregar features reais
    fe = next(e for e in g["edges"]
              if g["nodes"][e["src"]]["service"] == "front-end"
              and g["nodes"][e["dst"]]["service"] == "catalogue"
              and e["direction"] == "real")
    assert fe["features"]["latency_lr"] is not None and fe["features"]["latency_lr"] > 0.9
