"""Baselines para localização de falha (WHERE) — o piso científico.

Antes de alegar que o GAT aprende algo, ele precisa bater estes baselines
nos mesmos splits. Todos operam sobre o grafo ``gat_graph.json`` (nós com
features log-ratio; ``None`` = sem dado).

* **B0 — Random:** score uniforme por nó. Piso absoluto.
* **B1 — Max-anomaly:** score = maior |log-ratio| entre as golden metrics
  do nó (imputação None→0). Heurística sem grafo — o teto a bater.
* **B2 — Anomaly + difusão 1-hop:** B1 suavizado pelos vizinhos na
  topologia. Testa se a **estrutura** do grafo ajuda (mini-preview do GAT).

Nota: PageRank estrutural puro é inútil aqui — a topologia é idêntica em
todos os runs, então daria o mesmo ranking sempre. B2 usa as features do
run, por isso varia por run.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

# Métricas de nó consideradas "anomalia" (as golden metrics em log-ratio).
_ANOMALY_KEYS = ("traffic_lr", "latency_p95_lr", "error_rate_lr")


def _node_anomaly_vector(graph: Dict) -> np.ndarray:
    """|log-ratio| máximo entre as golden metrics, por nó. None→0 (neutro)."""
    n = len(graph["nodes"])
    out = np.zeros(n, dtype=float)
    for node in graph["nodes"]:
        idx = node["idx"]
        feats = node["features"]
        vals = []
        for k in _ANOMALY_KEYS:
            v = feats.get(k)
            if v is not None:
                vals.append(abs(float(v)))
        out[idx] = max(vals) if vals else 0.0
    return out


def b0_random(graph: Dict, *, seed: int = 0) -> np.ndarray:
    """Score uniforme aleatório por nó. Determinístico por (seed, run_id)."""
    run_id = graph.get("run_id", "")
    local_seed = (seed * 1_000_003 + hash(run_id)) % (2**32)
    rng = np.random.default_rng(local_seed)
    return rng.random(len(graph["nodes"]))


def b1_max_anomaly(graph: Dict) -> np.ndarray:
    """Score = maior |log-ratio| entre as golden metrics do nó."""
    return _node_anomaly_vector(graph)


def _adjacency(graph: Dict) -> np.ndarray:
    """Matriz de adjacência não-direcionada (topologia do grafo)."""
    n = len(graph["nodes"])
    adj = np.zeros((n, n), dtype=float)
    for e in graph["edges"]:
        adj[e["src"], e["dst"]] = 1.0
        adj[e["dst"], e["src"]] = 1.0
    return adj


def b2_anomaly_diffused(graph: Dict, *, alpha: float = 0.5) -> np.ndarray:
    """B1 suavizado por 1 hop: score_i = (1-α)·a_i + α·média(vizinhos).

    Testa se propagar a anomalia pela topologia ajuda a localizar a causa
    raiz (a anomalia do serviço-alvo "irradia" para os vizinhos, então o
    alvo real tende a ter vizinhos também anômalos).
    """
    a = _node_anomaly_vector(graph)
    adj = _adjacency(graph)
    deg = adj.sum(axis=1)
    # Nó sem vizinhos não tem o que difundir → mantém o próprio valor
    # (a média dos vizinhos cai para o próprio score), evitando diluí-lo.
    neigh_mean = a.copy()
    nz = deg > 0
    neigh_mean[nz] = (adj @ a)[nz] / deg[nz]
    return (1 - alpha) * a + alpha * neigh_mean


BASELINES = {
    "B0_random": b0_random,
    "B1_max_anomaly": b1_max_anomaly,
    "B2_anomaly_diffused": b2_anomaly_diffused,
}
