"""Coleta métricas Istio (service mesh) do Prometheus para edge features.

O Istio (sidecar Envoy) expõe ``istio_requests_total`` e
``istio_request_duration_milliseconds_{sum,count}`` com labels
``source_workload``/``destination_workload`` — arestas cliente→servidor reais
que os traces não capturavam (propagação W3C quebrada).

Este módulo monta range queries Prometheus por janela do run e devolve o
payload ``edge_metrics`` consumido pelo ``graph_builder`` (formato
``{"data": {"result": [...]}}`` por métrica — o mesmo shape do metrics_raw).
"""
from __future__ import annotations

from typing import Any, Dict

# Métricas Istio que alimentam as golden metrics por aresta.
ISTIO_EDGE_METRICS = (
    "istio_requests_total",
    "istio_request_duration_milliseconds_sum",
    "istio_request_duration_milliseconds_count",
)

# PromQL de range query por métrica, escopo sock-shop. Mantemos os labels
# source_workload/destination_workload/response_code para o builder.
# Sem rate(): queremos os valores brutos por janela (o builder calcula a razão).
_QUERY_TEMPLATE = (
    'sum by (source_workload, destination_workload, response_code, response_flags, reporter, '
    'destination_workload_namespace) '
    '({metric}{{destination_workload_namespace="{ns}"}})'
)


def build_istio_queries(namespace: str = "sock-shop", step_seconds: int = 15) -> Dict[str, str]:
    """Retorna {metric_name: promql} para as golden metrics por aresta."""
    return {
        m: _QUERY_TEMPLATE.format(metric=m, ns=namespace, step=step_seconds)
        for m in ISTIO_EDGE_METRICS
    }


def collect_istio_edges(
    client: Any,
    start_iso: str,
    end_iso: str,
    step_seconds: int,
    namespace: str = "sock-shop",
) -> Dict[str, Any]:
    """Coleta as métricas Istio por aresta via MCP (prometheus_range_query).

    ``client`` é um ``MCPToolClient``. Retorna o payload ``edge_metrics`` no
    formato consumido pelo graph_builder: ``{metric: {data: {result: [...]}}}``.
    Em falha de qualquer query, retorna dict vazio (best-effort — o engine
    registra o evento e o gate ``edge_features_present`` acusa).
    """
    queries = build_istio_queries(namespace=namespace, step_seconds=step_seconds)
    out: Dict[str, Any] = {}
    for metric, promql in queries.items():
        payload = client.call_tool_json(
            "prometheus_range_query",
            {
                "query": promql,
                "start": start_iso,
                "end": end_iso,
                "step": f"{step_seconds}s",
            },
        )
        if isinstance(payload, dict) and payload.get("data"):
            out[metric] = payload
    out["collection"] = {
        "provider": "prometheus-istio",
        "metrics_requested": list(ISTIO_EDGE_METRICS),
        "metrics_returned": [m for m in ISTIO_EDGE_METRICS if m in out],
        "unknown_source_series": {},
    }
    for metric in ISTIO_EDGE_METRICS:
        results = ((out.get(metric) or {}).get("data") or {}).get("result", [])
        unknown = sum(
            1 for row in results
            if (row.get("metric") or {}).get("source_workload") == "unknown"
        )
        out["collection"]["unknown_source_series"][metric] = {
            "unknown": unknown,
            "total": len(results),
        }
    return out
