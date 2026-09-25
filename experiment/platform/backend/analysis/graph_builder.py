"""Graph builder para o GAT (Opção D — telemetria com o que já existe).

Constrói, por run, o grafo que alimenta o GAT:

* **Nós** = serviços da Sock Shop. Features de nó = as 3 golden metrics
    (request rate, latência, % erro) por serviço, codificadas como
    **log-ratio** vs baseline, + ``span_count`` (confiança da janela).
    Fonte: ``metrics_raw`` (séries por serviço no label ``name`` ou no label
    Istio ``destination_workload``).
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

# Golden metrics por nó, no label `name` ou `destination_workload` do metrics_raw.
NODE_METRICS = ("traffic", "latency_p95", "error_rate")
SERVICE_LABEL_KEYS = ("destination_workload", "name", "service", "source_workload")

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
)


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _series_by_service(metrics_raw: Dict[str, Any], metric_id: str) -> Dict[str, List[Tuple[float, float]]]:
    """Retorna {service: [(ts, value), ...]} para uma métrica, ordenado por ts."""
    out: Dict[str, List[Tuple[float, float]]] = {}
    data = metrics_raw.get("data") or {}
    result = ((data.get(metric_id) or {}).get("data") or {}).get("result") or []
    for series in result:
        labels = series.get("metric") or {}
        name = next((str(labels[k]) for k in SERVICE_LABEL_KEYS if labels.get(k)), "")
        if not name or name == "unknown":
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
    #   "full"      — arestas reais via service mesh Istio (source→destination);
    #   "user_only" — só métricas user→X do traces service-graph;
    #   "static"    — topologia conhecida sem métricas por aresta.
    edge_source = "static"
    if edge_metrics:
        edge_source = _detect_edge_source(edge_metrics)
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


def _detect_edge_source(edge_metrics: Dict[str, Any]) -> str:
    """Identifica a proveniência das métricas de aresta."""
    keys = set(edge_metrics.keys())
    if any(k.startswith("istio_") for k in keys):
        return "full"  # service mesh Istio: arestas reais source→destination
    return "user_only"  # traces service-graph: apenas user→X


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

    Aceita dois formatos de ``edge_metrics``:

    * **Istio** (``edge_source: full``): ``istio_requests_total``,
      ``istio_request_duration_milliseconds_sum`` e erros derivados de
      ``istio_requests_total`` filtrado por ``response_code`` 5xx.
    * **Traces service-graph** (``user_only``): ``traces_service_graph_*`` do
      Prometheus-v3 — na prática só existe para ``user → X``.

    Sem métricas, retorna neutras (None).
    """
    if not edge_metrics:
        return {"req_rate_lr": None, "latency_lr": None, "error_lr": None, "call_count": None}

    source = _detect_edge_source(edge_metrics)
    if source == "full":
        return _edge_features_istio(edge_metrics, src, dst, fault_start, fault_end, base_start, base_end)
    return _edge_features_traces(edge_metrics, src, dst, fault_start, fault_end, base_start, base_end)


def _edge_features_traces(
    edge_metrics: Dict[str, Any],
    src: str,
    dst: str,
    fault_start: float,
    fault_end: float,
    base_start: float,
    base_end: float,
) -> Dict[str, Optional[float]]:
    """Golden metrics da aresta a partir das séries ``traces_service_graph_*``."""

    def _lr(metric_name: str) -> Tuple[Optional[float], int]:
        series = _edge_series(edge_metrics, metric_name, src, dst)
        period_mean, count = _window_mean(series, fault_start, fault_end)
        base_mean, _ = _window_mean(series, base_start, base_end)
        return _log_ratio(period_mean, base_mean), count

    req_lr, req_count = _lr("traces_service_graph_request_total")
    lat_lr, _ = _lr("traces_service_graph_request_client_seconds_sum")
    err_lr, _ = _lr("traces_service_graph_request_failed_total")
    return {
        "req_rate_lr": req_lr,
        "latency_lr": lat_lr,
        "error_lr": err_lr,
        "call_count": float(req_count) if req_count else None,
    }


def _edge_features_istio(
    edge_metrics: Dict[str, Any],
    src: str,
    dst: str,
    fault_start: float,
    fault_end: float,
    base_start: float,
    base_end: float,
) -> Dict[str, Optional[float]]:
    """Golden metrics da aresta a partir das séries Istio.

    Erro% = taxa de 5xx / total por aresta. Latência = sum/count.
    """
    def _window_stats(metric_name: str, only_5xx: bool) -> Tuple[Optional[float], Optional[float], int]:
        """Retorna (fault_mean, baseline_mean, count) já como RATE por segundo."""
        series = _edge_series_istio(edge_metrics, metric_name, src, dst, only_5xx)
        f_mean, f_count = _window_mean(series, fault_start, fault_end)
        b_mean, _ = _window_mean(series, base_start, base_end)
        return f_mean, b_mean, f_count

    # request rate (req/s) — counter → rate implícito via janelas
    req_f, req_b, req_count = _window_stats("istio_requests_total", only_5xx=False)
    err_f, err_b, _ = _window_stats("istio_requests_total", only_5xx=True)
    lat_sum_f, lat_sum_b, _ = _window_stats("istio_request_duration_milliseconds_sum", only_5xx=False)
    lat_cnt_f, lat_cnt_b, _ = _window_stats("istio_request_duration_milliseconds_count", only_5xx=False)

    # latência média = sum/count (ms)
    lat_f = (lat_sum_f / lat_cnt_f) if lat_sum_f and lat_cnt_f else None
    lat_b = (lat_sum_b / lat_cnt_b) if lat_sum_b and lat_cnt_b else None

    # erro% = 5xx / total
    err_rate_f = (err_f / req_f) if (err_f is not None and req_f) else (0.0 if req_f else None)
    err_rate_b = (err_b / req_b) if (err_b is not None and req_b) else (0.0 if req_b else None)

    return {
        "req_rate_lr": _log_ratio(req_f, req_b),
        "latency_lr": _log_ratio(lat_f, lat_b),
        "error_lr": _log_ratio(err_rate_f, err_rate_b),
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


def _edge_series_istio(
    edge_metrics: Dict[str, Any],
    metric_name: str,
    src: str,
    dst: str,
    only_5xx: bool = False,
) -> List[Tuple[float, float]]:
    """Extrai [(ts, value)] de uma série Istio para (source_workload=src, destination_workload=dst).

    Agrega séries que diferem apenas por ``response_code``/``reporter``. Com
    ``only_5xx=True``, soma apenas séries cujo ``response_code`` começa com 5.
    """
    result = ((edge_metrics.get(metric_name) or {}).get("data") or {}).get("result") or []
    by_ts: Dict[float, float] = {}
    for series in result:
        metric = series.get("metric") or {}
        if metric.get("source_workload") != src or metric.get("destination_workload") != dst:
            continue
        if only_5xx:
            code = str(metric.get("response_code") or "")
            if not code.startswith("5"):
                continue
        for ts, val in series.get("values") or []:
            try:
                t, v = float(ts), float(val)
            except (TypeError, ValueError):
                continue
            by_ts[t] = by_ts.get(t, 0.0) + v
    return sorted(by_ts.items())
