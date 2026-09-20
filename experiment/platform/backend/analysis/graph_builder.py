"""Graph builder para o GAT (Opção D — telemetria com o que já existe).

Constrói, por run, o grafo que alimenta o GAT:

* **Nós** = serviços da Sock Shop. Features de nó = as 3 golden metrics
  (request rate, latência, % erro) por serviço, codificadas como
  **log-ratio** vs baseline, + ``span_count`` (confiança da janela).
  Fonte: ``metrics_raw`` (séries por serviço no label ``name``).
* **Arestas** = topologia estática conhecida da Sock Shop, bidirecionais
  com marcação de direção. Features de aresta: as golden metrics
  **``user → X``** do Prometheus (quando existem) + neutras nas internas.
* **Sem IDs de serviço** nos tensores (indutividade para o split S2);
  nomes ficam no JSON só para auditoria humana.

Decisões registradas em ``research/diario/2026-09-19-avaliacao-arestas.md``
e ``research/hipoteses/H001-gat-localizacao-falhas.md``.

O módulo é puro (sem I/O de rede): o chamador fornece ``metrics_raw``
(e opcionalmente as métricas de aresta) e as janelas temporais.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "1.0"
GRAPH_SCHEMA_VERSION = "gat-v1"

# ── Topologia estática da Sock Shop (Opção D) ───────────────────────────
# Arestas reais de chamada (caller → callee), da arquitetura documentada.
# O load generator (Locust) chama o front-end; internas conforme o fluxo
# de e-commerce. Como a propagação W3C está quebrada (ver diário), estas
# são as arestas *conhecidas*, não observadas por trace.
STATIC_TOPOLOGY: List[Tuple[str, str]] = [
    ("front-end", "catalogue"),
    ("front-end", "carts"),
    ("front-end", "orders"),
    ("front-end", "user"),
    ("orders", "payment"),
    ("orders", "shipping"),
    ("shipping", "queue-master"),
    ("carts", "carts-db"),
    ("catalogue", "catalogue-db"),
    ("orders", "orders-db"),
    ("user", "user-db"),
]

# Golden metrics por nó, no label `name` do metrics_raw.
NODE_METRICS = ("traffic", "latency_p95", "error_rate")

# Epsilon do log-ratio: resolve baseline de erro ≈ 0.
EPSILON = 1e-6

# Serviços "core" (excluem os *-db, que são backing services). O nó-alvo
# do ground truth sempre é core; DBs entram no grafo como contexto.
CORE_SERVICES = (
    "front-end",
    "catalogue",
    "carts",
    "orders",
    "payment",
    "shipping",
    "user",
    "queue-master",
)


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _series_by_service(metrics_raw: Dict[str, Any], metric_id: str) -> Dict[str, List[Tuple[float, float]]]:
    """Retorna {service: [(ts, value), ...]} para uma métrica, ordenado por ts."""
    out: Dict[str, List[Tuple[float, float]]] = {}
    data = metrics_raw.get("data") or {}
    result = ((data.get(metric_id) or {}).get("data") or {}).get("result") or []
    for series in result:
        name = (series.get("metric") or {}).get("name")
        if not name:
            continue
        points: List[Tuple[float, float]] = []
        for ts, val in series.get("values") or []:
            try:
                points.append((float(ts), float(val)))
            except (TypeError, ValueError):
                continue
        points.sort(key=lambda p: p[0])
        if points:
            out[name] = points
    return out


def _window_mean(points: List[Tuple[float, float]], start: float, end: float) -> Tuple[Optional[float], int]:
    """Média dos pontos dentro de [start, end]. Retorna (mean, count)."""
    vals = [v for ts, v in points if start <= ts <= end]
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def _log_ratio(period_mean: Optional[float], baseline_mean: Optional[float]) -> Optional[float]:
    """log((period+ε)/(baseline+ε)). None se algum lado não tem dado.

    Sanitiza NaN/Inf (bug histórico: divisão por zero em latência) —
    retorna None em vez de propagar valor não finito.
    """
    if period_mean is None or baseline_mean is None:
        return None
    for v in (period_mean, baseline_mean):
        if v != v or v in (float("inf"), float("-inf")):
            return None
    ratio = (period_mean + EPSILON) / (baseline_mean + EPSILON)
    if ratio <= 0:
        return None
    out = math.log(ratio)
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def build_gat_graph(
    *,
    run_id: str,
    metrics_raw: Dict[str, Any],
    baseline_window: Tuple[str, str],
    fault_window: Tuple[str, str],
    ground_truth_service: Optional[str],
    ground_truth_fault_category: Optional[str],
    is_training: bool = True,
    edge_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Constrói o grafo GAT de um run (Opção D).

    Parameters
    ----------
    metrics_raw:
        Conteúdo do artefato ``metrics_raw.json`` (séries por serviço).
    baseline_window / fault_window:
        ``(start_iso, end_iso)`` das janelas. A fault window já deve ser a
        **janela efetiva encurtada** (ver C3 / decisão de 2026-09-19).
    edge_metrics:
        Opcional. Séries ``traces_service_graph_*`` do Prometheus-v3 para
        as arestas ``user → X``. Quando ausente, arestas ficam neutras e
        ``edge_source`` = ``"static"``.

    Returns
    -------
    O documento ``gat_graph.json`` (schema versionado, autocontido).
    """
    base_start, base_end = _parse_ts(baseline_window[0]), _parse_ts(baseline_window[1])
    fault_start, fault_end = _parse_ts(fault_window[0]), _parse_ts(fault_window[1])

    # ── Nós ────────────────────────────────────────────────────────────
    services: List[str] = sorted(set(CORE_SERVICES))
    node_index = {svc: i for i, svc in enumerate(services)}

    nodes: List[Dict[str, Any]] = []
    for svc in services:
        features: Dict[str, Optional[float]] = {}
        total_count = 0
        for metric_id in NODE_METRICS:
            series = _series_by_service(metrics_raw, metric_id)
            points = series.get(svc) or []
            period_mean, period_count = _window_mean(points, fault_start, fault_end)
            base_mean, _ = _window_mean(points, base_start, base_end)
            features[f"{metric_id}_lr"] = _log_ratio(period_mean, base_mean)
            total_count += period_count
        # count como feature auxiliar de confiança (janelas curtas → esparso)
        features["span_count"] = float(total_count)
        nodes.append({"idx": node_index[svc], "service": svc, "features": features})

    # ── Arestas (topologia estática, bidirecionais) ─────────────────────
    # edge_source reflete a melhor evidência disponível neste run:
    #   "user_only" se há métricas user→X no Prometheus-v3; senão "static".
    edge_source = "user_only" if edge_metrics else "static"
    edges: List[Dict[str, Any]] = []
    for src_name, dst_name in STATIC_TOPOLOGY:
        # só cria aresta se ambos os nós existem no grafo (core services)
        if src_name not in node_index or dst_name not in node_index:
            continue
        edge_feats = _edge_features(edge_metrics, src_name, dst_name, fault_start, fault_end, base_start, base_end)
        # aresta real (caller → callee)
        edges.append({
            "src": node_index[src_name],
            "dst": node_index[dst_name],
            "direction": "real",
            "features": edge_feats,
        })
        # aresta reversa (para message passing bidirecional)
        edges.append({
            "src": node_index[dst_name],
            "dst": node_index[src_name],
            "direction": "reverse",
            "features": edge_feats,
        })

    # ── Label ──────────────────────────────────────────────────────────
    label = {
        "root_service_idx": node_index.get(ground_truth_service) if ground_truth_service else None,
        "root_service": ground_truth_service,
        "fault_category": ground_truth_fault_category,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "windows": {
            "baseline": {"start": baseline_window[0], "end": baseline_window[1]},
            "fault": {"start": fault_window[0], "end": fault_window[1]},
        },
        "nodes": nodes,
        "edges": edges,
        "feature_spec": {
            "node": [f"{m}_lr" for m in NODE_METRICS] + ["span_count"],
            "edge": ["req_rate_lr", "latency_lr", "error_lr", "call_count"],
            "epsilon": EPSILON,
            "encoding": "log-ratio",
        },
        "edge_source": edge_source,
        "label": label,
        "is_training": is_training,
    }


def _edge_features(
    edge_metrics: Optional[Dict[str, Any]],
    src: str,
    dst: str,
    fault_start: float,
    fault_end: float,
    base_start: float,
    base_end: float,
) -> Dict[str, Optional[float]]:
    """Features de uma aresta A→B.

    Com ``edge_metrics`` (séries ``traces_service_graph_*`` do
    Prometheus-v3), calcula log-ratio das golden metrics da aresta
    ``src → dst`` — que na prática só existe para ``user → X``. Sem elas,
    retorna neutras (None), e o graph builder marca ``edge_source``.
    """
    if not edge_metrics:
        return {"req_rate_lr": None, "latency_lr": None, "error_lr": None, "call_count": None}

    def _metric_lr(metric_name: str) -> Tuple[Optional[float], int]:
        series = _edge_series(edge_metrics, metric_name, src, dst)
        period_mean, count = _window_mean(series, fault_start, fault_end)
        base_mean, _ = _window_mean(series, base_start, base_end)
        return _log_ratio(period_mean, base_mean), count

    req_lr, req_count = _metric_lr("traces_service_graph_request_total")
    lat_lr, _ = _metric_lr("traces_service_graph_request_client_seconds_sum")
    err_lr, _ = _metric_lr("traces_service_graph_request_failed_total")
    return {
        "req_rate_lr": req_lr,
        "latency_lr": lat_lr,
        "error_lr": err_lr,
        "call_count": float(req_count) if req_count else None,
    }


def _edge_series(
    edge_metrics: Dict[str, Any],
    metric_name: str,
    src: str,
    dst: str,
) -> List[Tuple[float, float]]:
    """Extrai [(ts, value)] de uma série service-graph para (client=src, server=dst)."""
    result = ((edge_metrics.get(metric_name) or {}).get("data") or {}).get("result") or []
    points: List[Tuple[float, float]] = []
    for series in result:
        metric = series.get("metric") or {}
        if metric.get("client") == src and metric.get("server") == dst:
            for ts, val in series.get("values") or []:
                try:
                    points.append((float(ts), float(val)))
                except (TypeError, ValueError):
                    continue
    points.sort(key=lambda p: p[0])
    return points
