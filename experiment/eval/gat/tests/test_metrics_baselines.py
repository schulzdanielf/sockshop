"""Testes de métricas de ranking e baselines."""
from __future__ import annotations

import numpy as np

from experiment.eval.gat.baselines import (
    b1_max_anomaly,
    b2_anomaly_diffused,
    _node_anomaly_vector,
)
from experiment.eval.gat.metrics import (
    _rank_of_label,
    bootstrap_ci,
    evaluate_scorer,
)


def _graph(anomalies, label_idx, edges=None):
    """Grafo sintético: anomalies = lista de |log-ratio| máximo por nó."""
    nodes = []
    for i, a in enumerate(anomalies):
        nodes.append({
            "idx": i,
            "service": f"svc{i}",
            "features": {"traffic_lr": a, "latency_p95_lr": None, "error_rate_lr": None},
        })
    return {
        "run_id": f"run-{label_idx}-{len(anomalies)}",
        "nodes": nodes,
        "edges": edges or [],
        "label": {"root_service_idx": label_idx},
    }


# ── _rank_of_label ──────────────────────────────────────────────────────
def test_rank_perfect():
    scores = np.array([0.1, 0.9, 0.2])
    assert _rank_of_label(scores, 1) == 1


def test_rank_worst():
    scores = np.array([0.9, 0.8, 0.1])
    assert _rank_of_label(scores, 2) == 3


def test_rank_ties_conservative():
    # todos empatados → rank do label = 1 + (0 estritamente maiores) = 1,
    # mas o teste conservador conta só ESTRITAMENTE maiores
    scores = np.array([0.5, 0.5, 0.5])
    assert _rank_of_label(scores, 0) == 1
    # um estritamente maior → rank 2
    scores = np.array([0.9, 0.5, 0.5])
    assert _rank_of_label(scores, 1) == 2


# ── evaluate_scorer ─────────────────────────────────────────────────────
def test_evaluate_perfect_scorer():
    graphs = [_graph([0.1, 0.9, 0.2], 1), _graph([0.9, 0.1, 0.2], 0)]
    # scorer que retorna a anomalia → sempre acerta top-1
    res = evaluate_scorer(graphs, b1_max_anomaly)
    assert res["top1"] == 1.0
    assert res["top3"] == 1.0
    assert res["mrr"] == 1.0


def test_evaluate_ignores_unlabeled():
    g = _graph([0.1, 0.9], 0)
    g["label"]["root_service_idx"] = None
    res = evaluate_scorer([g], b1_max_anomaly)
    assert res["n"] == 0


# ── baselines ───────────────────────────────────────────────────────────
def test_b1_picks_max_anomaly():
    g = _graph([0.1, 0.2, 0.95], 2)
    scores = b1_max_anomaly(g)
    assert int(np.argmax(scores)) == 2


def test_node_anomaly_imputes_none():
    # nó com todas as métricas None → anomalia 0
    g = {
        "nodes": [
            {"idx": 0, "service": "a", "features": {"traffic_lr": None, "latency_p95_lr": None, "error_rate_lr": None}},
            {"idx": 1, "service": "b", "features": {"traffic_lr": 0.5, "latency_p95_lr": None, "error_rate_lr": None}},
        ],
        "edges": [],
    }
    a = _node_anomaly_vector(g)
    assert a[0] == 0.0
    assert a[1] == 0.5


def test_b2_diffusion_uses_topology():
    # nó alvo (2) anômalo + vizinho (1) via aresta → difusão eleva o vizinho
    edges = [{"src": 1, "dst": 2}, {"src": 2, "dst": 1}]
    g = _graph([0.0, 0.0, 1.0], 2, edges=edges)
    diffused = b2_anomaly_diffused(g, alpha=0.5)
    # o nó 1 (vizinho do anômalo) deve ganhar score > 0
    assert diffused[1] > 0.0
    # o nó 0 (isolado) permanece 0
    assert diffused[0] == 0.0


def test_b2_no_edges_equals_b1():
    g = _graph([0.1, 0.9, 0.2], 1)  # sem arestas
    b1 = b1_max_anomaly(g)
    b2 = b2_anomaly_diffused(g)
    assert np.allclose(b1, b2)


# ── bootstrap_ci ────────────────────────────────────────────────────────
def test_bootstrap_ci_bounds():
    graphs = [_graph([0.1, 0.9, 0.2], 1) for _ in range(20)]
    point, lo, hi = bootstrap_ci(graphs, b1_max_anomaly, metric="top3", n_resamples=200)
    assert point == 1.0  # sempre acerta
    assert 0.0 <= lo <= point <= hi <= 1.0


def test_bootstrap_ci_deterministic():
    graphs = [_graph([0.5, 0.9, 0.2], 1) for _ in range(10)]
    r1 = bootstrap_ci(graphs, b1_max_anomaly, metric="top1", seed=7, n_resamples=100)
    r2 = bootstrap_ci(graphs, b1_max_anomaly, metric="top1", seed=7, n_resamples=100)
    assert r1 == r2  # mesma seed → mesmo resultado
