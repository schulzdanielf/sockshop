"""Métricas de ranking para localização de falha (WHERE) + bootstrap CI.

Um "scorer" recebe um grafo (dict) e retorna um score por nó (maior =
mais provável de ser a causa raiz). As métricas comparam o ranking
induzido contra o índice do nó rotulado (ground truth).

Métricas (H001): **top-3** (primária), top-1 e MRR (secundárias).
Todas com intervalo de confiança 95% via bootstrap — obrigatório dado o
tamanho pequeno dos test sets (22–58).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Um scorer: (graph_dict) -> np.ndarray de shape [n_nodes] com scores.
Scorer = Callable[[Dict], np.ndarray]


def _label_index(graph: Dict) -> Optional[int]:
    return (graph.get("label") or {}).get("root_service_idx")


def _rank_of_label(scores: np.ndarray, label_idx: int) -> int:
    """Posição (1-based) do nó rotulado no ranking decrescente de scores.

    Empates resolvidos de forma conservadora: o rank do label é
    ``1 + (nº de nós com score ESTRITAMENTE maior)``. Isso evita inflar a
    métrica quando o scorer dá o mesmo score a vários nós.
    """
    label_score = scores[label_idx]
    strictly_greater = int(np.sum(scores > label_score))
    return strictly_greater + 1


def evaluate_scorer(
    graphs: Sequence[Dict],
    scorer: Scorer,
) -> Dict[str, float]:
    """Top-1, top-3 e MRR de um scorer sobre uma lista de grafos.

    Grafos sem label (root_service_idx None) são ignorados.
    """
    ranks: List[int] = []
    for g in graphs:
        label_idx = _label_index(g)
        if label_idx is None:
            continue
        scores = np.asarray(scorer(g), dtype=float)
        ranks.append(_rank_of_label(scores, label_idx))

    if not ranks:
        return {"n": 0, "top1": float("nan"), "top3": float("nan"), "mrr": float("nan")}

    ranks_arr = np.asarray(ranks)
    return {
        "n": len(ranks),
        "top1": float(np.mean(ranks_arr == 1)),
        "top3": float(np.mean(ranks_arr <= 3)),
        "mrr": float(np.mean(1.0 / ranks_arr)),
    }


def bootstrap_ci(
    graphs: Sequence[Dict],
    scorer: Scorer,
    *,
    metric: str = "top3",
    n_resamples: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
) -> Tuple[float, float, float]:
    """Intervalo de confiança de uma métrica via bootstrap de runs.

    Retorna ``(ponto, lo, hi)`` para o percentil ``[alpha/2, 1-alpha/2]``.
    Reamostra os **runs** (não os nós), preservando a unidade experimental.
    """
    per_run: List[float] = []
    for g in graphs:
        label_idx = _label_index(g)
        if label_idx is None:
            continue
        scores = np.asarray(scorer(g), dtype=float)
        rank = _rank_of_label(scores, label_idx)
        if metric == "top1":
            per_run.append(1.0 if rank == 1 else 0.0)
        elif metric == "top3":
            per_run.append(1.0 if rank <= 3 else 0.0)
        elif metric == "mrr":
            per_run.append(1.0 / rank)
        else:
            raise ValueError(f"métrica desconhecida: {metric}")

    if not per_run:
        return float("nan"), float("nan"), float("nan")

    arr = np.asarray(per_run)
    point = float(np.mean(arr))

    rng = np.random.default_rng(seed)
    n = len(arr)
    means = np.empty(n_resamples)
    for i in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        means[i] = np.mean(arr[idx])
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return point, lo, hi
