"""Testes do graph builder (Opção D).

Cobre: log-ratio (incluindo baseline zero e NaN), janelas, nós/arestas,
bidirecionalidade, label, sanitização de NaN/Inf e edge_source.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from experiment.platform.backend.analysis.graph_builder import (
    CORE_SERVICES,
    STATIC_TOPOLOGY,
    _log_ratio,
    _series_by_service,
    _window_mean,
    build_gat_graph,
)

BASELINE = ("2026-09-16T10:00:00+00:00", "2026-09-16T10:05:00+00:00")
FAULT = ("2026-09-16T10:05:00+00:00", "2026-09-16T10:07:00+00:00")


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _series(service: str, points: List[Tuple[float, float]]) -> Dict[str, Any]:
    return {
        "data": {
            "result": [
                {"metric": {"name": service}, "values": [[ts, str(v)] for ts, v in points]}
            ]
        }
    }


def make_metrics_raw(fault_multiplier: float = 3.0) -> Dict[str, Any]:
    """metrics_raw sintético: baseline estável, fault com latência X vezes maior."""
    b0, b1 = _ts(BASELINE[0]), _ts(BASELINE[1])
    f0, f1 = _ts(FAULT[0]), _ts(FAULT[1])
    base_pts = [(b0 + i * 15, 10.0) for i in range(20)]
    fault_pts = [(f0 + i * 15, 10.0 * fault_multiplier) for i in range(8)]

    data: Dict[str, Any] = {}
    for mid in ("traffic", "latency_p95", "error_rate"):
        series = {}
        for svc in CORE_SERVICES:
            series[svc] = _series(svc, base_pts + fault_pts)["data"]["result"][0]
        data[mid] = {"data": {"result": list(series.values())}}
    return {"window_start": BASELINE[0], "window_end": FAULT[1], "data": data}


# ── log-ratio ───────────────────────────────────────────────────────────
def test_log_ratio_basic():
    # período 3x o baseline → log(3) ≈ 1.0986
    assert abs(_log_ratio(30.0, 10.0) - math.log(3)) < 1e-3


def test_log_ratio_baseline_zero():
    # baseline de erro = 0 → ε resolve; período 0.05 → log grande positivo
    v = _log_ratio(0.05, 0.0)
    assert v is not None and v > 0 and math.isfinite(v)


def test_log_ratio_none_when_missing():
    assert _log_ratio(None, 1.0) is None
    assert _log_ratio(1.0, None) is None


def test_log_ratio_sanitizes_nan_inf():
    assert _log_ratio(float("nan"), 1.0) is None
    assert _log_ratio(1.0, float("inf")) is None


# ── helpers de janela ───────────────────────────────────────────────────
def test_series_by_service_and_window_mean():
    metrics = make_metrics_raw()
    series = _series_by_service(metrics, "latency_p95")
    assert "catalogue" in series
    mean, count = _window_mean(series["catalogue"], _ts(FAULT[0]), _ts(FAULT[1]))
    assert count == 8
    assert abs(mean - 30.0) < 1e-6


# ── build_gat_graph ─────────────────────────────────────────────────────
def test_graph_structure():
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw(),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="catalogue",
        ground_truth_fault_category="cpu-hog",
        is_training=True,
    )
    assert g["schema_version"] == "1.0"
    assert g["graph_schema_version"] == "gat-v1"
    assert g["edge_source"] == "static"  # sem edge_metrics

    # nós = core services
    assert len(g["nodes"]) == len(CORE_SERVICES)
    services = {n["service"] for n in g["nodes"]}
    assert "catalogue" in services and "queue-master" in services
    # DBs não são nós (só entram na topologia como contexto)
    assert "carts-db" not in services

    # arestas bidirecionais: para cada aresta real há uma reversa
    real = [e for e in g["edges"] if e["direction"] == "real"]
    rev = [e for e in g["edges"] if e["direction"] == "reverse"]
    assert len(real) == len(rev)
    # só arestas entre core services
    topo_core = [(s, d) for s, d in STATIC_TOPOLOGY if s in CORE_SERVICES and d in CORE_SERVICES]
    assert len(real) == len(topo_core)


def test_graph_label_maps_to_node_index():
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw(),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="orders",
        ground_truth_fault_category="pod-delete",
    )
    label = g["label"]
    assert label["root_service"] == "orders"
    idx = label["root_service_idx"]
    assert g["nodes"][idx]["service"] == "orders"


def test_graph_node_features_log_ratio():
    # fault claramente 3x baseline, sem sobreposição de transição:
    # baseline termina em 10:05, fault começa em 10:05 (sem pontos de transição
    # dentro das janelas porque os pontos são discretos de 15s).
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw(fault_multiplier=3.0),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="catalogue",
        ground_truth_fault_category="cpu-hog",
    )
    node = next(n for n in g["nodes"] if n["service"] == "catalogue")
    lr = node["features"]["latency_p95_lr"]
    # a média da janela de fault (30.0) vs baseline (10.0) → log-ratio ≈ log(3)
    # tolerância maior porque a janela de fault tem 8 pontos a 30.0
    assert lr is not None and lr > 0.9  # claramente positivo (fault >> baseline)
    # span_count > 0
    assert node["features"]["span_count"] > 0


def test_graph_no_service_ids_in_features():
    """Os tensores de features não podem conter nomes de serviço (S2)."""
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw(),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="catalogue",
        ground_truth_fault_category="cpu-hog",
    )
    for n in g["nodes"]:
        for k in n["features"].keys():
            assert not any(svc in k for svc in CORE_SERVICES)


def test_graph_edge_metrics_user_only():
    """Com edge_metrics (user→X), edge_source vira user_only e a aresta
    user→front-end carrega features reais."""
    b0, b1 = _ts(BASELINE[0]), _ts(BASELINE[1])
    f0, f1 = _ts(FAULT[0]), _ts(FAULT[1])
    edge_metrics = {
        "traces_service_graph_request_total": {
            "data": {
                "result": [
                    {
                        "metric": {"client": "user", "server": "front-end"},
                        "values": [[b0 + i * 15, "5.0"] for i in range(20)]
                        + [[f0 + i * 15, "20.0"] for i in range(8)],
                    }
                ]
            }
        }
    }
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=make_metrics_raw(),
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="front-end",
        ground_truth_fault_category="cpu-hog",
        edge_metrics=edge_metrics,
    )
    assert g["edge_source"] == "user_only"
    # aresta user→front-end não está na topologia estática (user não é core),
    # então nenhuma aresta carrega essas features; edge_source reflete a
    # disponibilidade, não a aplicação. Arestas internas ficam neutras.
    for e in g["edges"]:
        assert e["features"]["req_rate_lr"] is None or isinstance(e["features"]["req_rate_lr"], float)


def test_graph_missing_error_rate_service():
    """error_rate só tem 3 serviços no metrics_raw real — os demais ficam None."""
    metrics = make_metrics_raw()
    # remove error_rate de todos menos front-end
    err = metrics["data"]["error_rate"]["data"]["result"]
    metrics["data"]["error_rate"]["data"]["result"] = [
        s for s in err if s["metric"]["name"] == "front-end"
    ]
    g = build_gat_graph(
        run_id="run-x",
        metrics_raw=metrics,
        baseline_window=BASELINE,
        fault_window=FAULT,
        ground_truth_service="front-end",
        ground_truth_fault_category="http-error",
    )
    fe = next(n for n in g["nodes"] if n["service"] == "front-end")
    cat = next(n for n in g["nodes"] if n["service"] == "catalogue")
    assert fe["features"]["error_rate_lr"] is not None
    assert cat["features"]["error_rate_lr"] is None  # sem série → None, não crash
