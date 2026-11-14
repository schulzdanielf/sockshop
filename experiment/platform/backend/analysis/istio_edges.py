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

import json
import re
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
HTTP_EDGE_LABELS = (
    "source_workload,source_workload_namespace,destination_workload,"
    "destination_workload_namespace,destination_service_name,destination_service,"
    "response_code,response_flags,reporter"
)


def istio_expression(
    metric: str,
    namespace: str,
    *,
    service: str | None = None,
    reporter: str | None = None,
    source_workload_unknown: bool = False,
    codes: str | None = None,
    lookback: str | None = None,
    normalize_destination: bool = False,
) -> str:
    ns = json.dumps(namespace)
    extra = ""
    if reporter is not None:
        extra += f",reporter={json.dumps(reporter)}"
        if reporter == "source":
            extra += ',source_workload!="unknown"'
    if source_workload_unknown:
        if reporter == "source":
            raise ValueError("source_workload_unknown cannot be combined with source reporter")
        extra += ',source_workload="unknown"'
    if codes is not None:
        extra += f",response_code=~{json.dumps(codes)}"
    known = f'destination_workload_namespace={ns},destination_workload!="unknown"'
    if service is not None:
        known += f",destination_workload={json.dumps(service)}"
    host = re.escape(service) if service is not None else "[^.]+"
    fqdn = host + r"\." + re.escape(namespace) + r"\.svc(\.cluster\.local)?"
    unknown = (
        'destination_workload="unknown",'
        f"destination_service=~{json.dumps(fqdn)}"
    )
    if service is not None:
        unknown += f",destination_service_name={json.dumps(service)}"
    expressions = []
    for selector in (known, unknown):
        expression = f"{metric}{{{selector}{extra}}}"
        if lookback is not None:
            expression = f"rate({expression}[{lookback}])"
        expressions.append(expression)
    if normalize_destination:
        # Preserve the proxy's original identity before deriving the intended service.
        expressions[1] = (
            f'label_replace(label_replace({expressions[1]},'
            '"original_destination_workload","$1","destination_workload","(.*)"),'
            '"destination_workload","$1","destination_service_name","(.+)")'
        )
    return f"({expressions[0]} or {expressions[1]})"


def http_status_query(namespace: str, service: str | None = None) -> str:
    expression = istio_expression(
        "istio_requests_total", namespace, service=service, lookback="1m"
    )
    return f"sum by ({HTTP_EDGE_LABELS}) ({expression})"


def http_client_expression(
    metric: str, namespace: str, *, codes: str | None = None, lookback: str = "1m"
) -> str:
    source = istio_expression(
        metric, namespace, reporter="source", codes=codes, lookback=lookback,
        normalize_destination=True,
    )
    selector = (
        f'destination_workload_namespace={json.dumps(namespace)},'
        'destination_workload!="unknown",source_workload="unknown",reporter="destination"'
    )
    if codes is not None:
        selector += f",response_code=~{json.dumps(codes)}"
    return f"({source} or rate({metric}{{{selector}}}[{lookback}]))"


def http_golden_query(metric: str, namespace: str, lookback: str = "1m") -> str:
    traffic = f"sum by (destination_workload) ({http_client_expression('istio_requests_total', namespace, lookback=lookback)})"
    if metric == "request_rate":
        return traffic
    if metric == "error_rate":
        errors = f"sum by (destination_workload) ({http_client_expression('istio_requests_total', namespace, codes='5..', lookback=lookback)})"
        return f"100 * ({errors} or on(destination_workload) 0 * {traffic}) / clamp_min({traffic}, 0.0001)"
    quantiles = {"latency_p50": 0.50, "latency_p95": 0.95, "latency_p99": 0.99}
    expression = http_client_expression(
        "istio_request_duration_milliseconds_bucket", namespace, lookback=lookback
    )
    return f"histogram_quantile({quantiles[metric]}, sum by (le, destination_workload) ({expression}))"


def resolve_istio_destination(labels: dict[str, Any], namespace: str) -> str | None:
    workload = labels.get("destination_workload")
    if isinstance(workload, str) and workload and workload != "unknown":
        if labels.get("destination_workload_namespace", namespace) == namespace:
            return workload
        return None
    service = labels.get("destination_service_name")
    if not isinstance(service, str) or not re.fullmatch(r"[a-z0-9][-a-z0-9]*", service):
        return None
    if labels.get("destination_service") in {
        f"{service}.{namespace}.svc", f"{service}.{namespace}.svc.cluster.local",
    }:
        return service
    return None


def build_istio_queries(namespace: str = "sock-shop", step_seconds: int = 15) -> Dict[str, str]:
    """Retorna {metric_name: promql} para as golden metrics por aresta."""
    return {
        m: f"sum by ({HTTP_EDGE_LABELS}) ({istio_expression(m, namespace)})"
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
