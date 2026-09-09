"""Lightweight similarity ranking for RAG retrieval over past runs.

Two modes are supported:

* ``tags`` — deterministic Jaccard over the controlled‑vocabulary tag
  set (chaos_type, affected_services, violations, recovery bucket).
* ``embedding`` — cosine similarity over L2‑normalized vectors produced
  by :mod:`analysis.embeddings`.

The interface is intentionally simple so we can later plug in any other
vector provider behind the same call.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


def _strip_identity_tags(tags: Iterable[str]) -> List[str]:
    """Drop tags whose value carries the identity of a specific service.

    ``svc:<name>``, ``cascade:<s1>-><s2>`` and similar tags would leak
    ground-truth into the retrieval score when the target is a held-out
    test run: a Jaccard match on ``svc:user`` immediately pulls every
    training run that touched ``user`` to the top, regardless of whether
    the underlying phenomenon is similar.

    We therefore exclude identity-carrying tags from similarity scoring.
    Phenomenon-describing tags (``chaos:*``, ``verdict:*``, ``recovery:*``,
    ``shape:*``, ``violation:*``, ``edges:*``) are kept.
    """
    out: List[str] = []
    for t in tags or []:
        if not isinstance(t, str):
            continue
        prefix = t.split(":", 1)[0]
        if prefix in {"svc", "cascade"}:
            continue
        out.append(t)
    return out


def score_similarity(
    query_tags: Iterable[str],
    candidate_tags: Iterable[str],
    *,
    verdict_bonus: float = 0.1,
) -> float:
    q = set(_strip_identity_tags(query_tags))
    c = set(_strip_identity_tags(candidate_tags))
    if not q or not c:
        return 0.0
    inter = q & c
    union = q | c
    jaccard = len(inter) / len(union) if union else 0.0
    bonus = 0.0
    q_verdict = next((t for t in q if t.startswith("verdict:")), None)
    c_verdict = next((t for t in c if t.startswith("verdict:")), None)
    if q_verdict and c_verdict and q_verdict == c_verdict:
        bonus = verdict_bonus
    return round(min(1.0, jaccard + bonus), 4)


def rank_similar_runs(
    query_tags: Iterable[str],
    candidates: List[Dict[str, Any]],
    *,
    limit: int = 5,
    exclude_run_id: str | None = None,
    min_score: float = 0.05,
) -> List[Dict[str, Any]]:
    """Return candidates ordered by similarity score.

    Each candidate must have ``run_id`` and ``tags`` (list[str]).
    The returned items get a ``score`` field added.
    """
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for cand in candidates:
        if exclude_run_id and cand.get("run_id") == exclude_run_id:
            continue
        score = score_similarity(query_tags, cand.get("tags") or [])
        if score < min_score:
            continue
        scored.append((score, cand))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for score, cand in scored[:limit]:
        item = dict(cand)
        item["score"] = score
        out.append(item)
    return out


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for already‑normalized vectors falls back to dot."""
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


# --------------------------------------------------------------------------
# Phase F1 — trace-aware graph similarity + hybrid ranker
# --------------------------------------------------------------------------
def _jaccard(a: Iterable[Any], b: Iterable[Any]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _cascade_overlap(
    qc: List[Dict[str, Any]], cc: List[Dict[str, Any]]
) -> Tuple[float, List[str]]:
    """Order-aware similarity between two cascade sequences.

    Returns ``(score, overlap_services)`` where ``score`` blends the
    fraction of services in common (size overlap) with the fraction of
    common pairs whose relative ordering matches (Kendall-tau style).
    """
    q_names = [c.get("service") for c in qc if isinstance(c, dict) and c.get("service")]
    c_names = [c.get("service") for c in cc if isinstance(c, dict) and c.get("service")]
    if not q_names or not c_names:
        return 0.0, []
    c_index = {s: i for i, s in enumerate(c_names)}
    common = [s for s in q_names if s in c_index]
    if not common:
        return 0.0, []
    overlap_norm = len(common) / max(len(q_names), len(c_names))
    if len(common) < 2:
        return round(overlap_norm, 4), common
    ranks = [c_index[s] for s in common]
    pairs = 0
    concordant = 0
    for i in range(len(ranks)):
        for j in range(i + 1, len(ranks)):
            pairs += 1
            if ranks[i] < ranks[j]:
                concordant += 1
    order_score = concordant / pairs if pairs else 1.0
    return round((overlap_norm + order_score) / 2.0, 4), common


def score_graph_similarity(
    query_graph: Optional[Dict[str, Any]],
    candidate_graph: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute a multi-component similarity between two propagation graphs.

    Components (all in ``[0, 1]``):

    * ``nodes_jaccard`` — Jaccard over the service node sets.
    * ``edges_jaccard`` — Jaccard over the (from,to) edge sets.
    * ``cascade_overlap`` — order-aware overlap of cascade sequences.

    The final ``score`` is the mean of the three components, weighted
    equally. ``overlap_services`` lists the services that appeared in
    both cascades (useful for explainability).
    """
    if not isinstance(query_graph, dict) or not isinstance(candidate_graph, dict):
        return {
            "nodes_jaccard": 0.0,
            "edges_jaccard": 0.0,
            "cascade_overlap": 0.0,
            "overlap_services": [],
            "score": 0.0,
        }
    q_nodes = query_graph.get("nodes") or []
    c_nodes = candidate_graph.get("nodes") or []
    q_edges = [
        (e.get("from"), e.get("to"))
        for e in (query_graph.get("edges") or [])
        if isinstance(e, dict)
    ]
    c_edges = [
        (e.get("from"), e.get("to"))
        for e in (candidate_graph.get("edges") or [])
        if isinstance(e, dict)
    ]

    nodes_j = _jaccard(q_nodes, c_nodes)
    edges_j = _jaccard(q_edges, c_edges)
    cascade_s, overlap = _cascade_overlap(
        query_graph.get("cascade_order") or [],
        candidate_graph.get("cascade_order") or [],
    )
    score = round((nodes_j + edges_j + cascade_s) / 3.0, 4)
    return {
        "nodes_jaccard": round(nodes_j, 4),
        "edges_jaccard": round(edges_j, 4),
        "cascade_overlap": cascade_s,
        "overlap_services": overlap,
        "score": score,
    }


def rank_hybrid(
    query_vec: Optional[np.ndarray],
    query_graph: Optional[Dict[str, Any]],
    candidates: Sequence[Dict[str, Any]],
    *,
    weights: Optional[Dict[str, float]] = None,
    limit: int = 5,
    exclude_run_id: Optional[str] = None,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """Blend semantic (embedding cosine) and structural (graph) similarity.

    Each candidate must carry ``vector`` (np.ndarray) and ``graph``
    (propagation-graph dict). Missing components contribute 0 to that
    sub-score and the weights are renormalised over the available
    components so a candidate is never penalised for a partial signal.
    """
    weights = weights or {"semantic": 0.6, "graph": 0.4}
    w_sem = float(weights.get("semantic", 0.0))
    w_gra = float(weights.get("graph", 0.0))
    total_w = w_sem + w_gra
    if total_w <= 0:
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for cand in candidates:
        if exclude_run_id and cand.get("run_id") == exclude_run_id:
            continue

        sem_score: Optional[float] = None
        if query_vec is not None and query_vec.size > 0:
            vec = cand.get("vector")
            if isinstance(vec, np.ndarray) and vec.shape == query_vec.shape:
                sem_score = cosine_similarity(query_vec, vec)

        gra_detail = score_graph_similarity(query_graph, cand.get("graph"))
        gra_score = gra_detail["score"] if (cand.get("graph") and query_graph) else None

        # Renormalise weights over available components.
        parts: List[Tuple[float, float]] = []
        if sem_score is not None:
            parts.append((sem_score, w_sem))
        if gra_score is not None:
            parts.append((gra_score, w_gra))
        if not parts:
            continue
        local_total = sum(w for _, w in parts)
        if local_total <= 0:
            continue
        score = sum(s * w for s, w in parts) / local_total

        if score < min_score:
            continue

        item = {k: v for k, v in cand.items() if k not in ("vector", "graph")}
        item["score"] = round(score, 4)
        item["semantic_score"] = round(sem_score, 4) if sem_score is not None else None
        item["graph_score"] = round(gra_score, 4) if gra_score is not None else None
        item["cascade_overlap_services"] = gra_detail.get("overlap_services", [])
        item["graph_detail"] = {
            k: gra_detail[k]
            for k in ("nodes_jaccard", "edges_jaccard", "cascade_overlap")
            if k in gra_detail
        }
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def rank_by_embedding(
    query_vec: np.ndarray,
    candidates: Sequence[Dict[str, Any]],
    *,
    limit: int = 5,
    exclude_run_id: Optional[str] = None,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """Rank candidates by cosine similarity.

    Each candidate must contain a ``vector`` field (``np.ndarray``).
    Returns a copy of each accepted candidate with ``score`` added and
    without the heavy ``vector`` field.
    """
    if query_vec is None or query_vec.size == 0:
        return []
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for cand in candidates:
        if exclude_run_id and cand.get("run_id") == exclude_run_id:
            continue
        vec = cand.get("vector")
        if (
            vec is None
            or not isinstance(vec, np.ndarray)
            or vec.shape != query_vec.shape
        ):
            continue
        score = cosine_similarity(query_vec, vec)
        if score < min_score:
            continue
        scored.append((score, cand))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for score, cand in scored[:limit]:
        item = {k: v for k, v in cand.items() if k != "vector"}
        item["score"] = round(score, 4)
        out.append(item)
    return out
