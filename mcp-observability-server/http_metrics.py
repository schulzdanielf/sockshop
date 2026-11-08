"""HTTP triage for the independently deployed MCP server."""

import json
import re


def _client_rate(
    metric: str, namespace: str, codes: str | None = None, lookback: str = "1m"
) -> str:
    ns = json.dumps(namespace)
    extra = f",response_code=~{json.dumps(codes)}" if codes else ""
    host = json.dumps(r"[^.]+\." + re.escape(namespace) + r"\.svc(\.cluster\.local)?")
    known = (
        f"rate({metric}{{destination_workload_namespace={ns},"
        f'destination_workload!="unknown",source_workload!="unknown",reporter="source"{extra}}}[{lookback}])'
    )
    unknown = (
        f"rate({metric}{{destination_workload=\"unknown\","
        f'destination_service=~{host},source_workload!="unknown",reporter="source"{extra}}}[{lookback}])'
    )
    unknown = (
        f'label_replace(label_replace({unknown},'
        '"original_destination_workload","$1","destination_workload","(.*)"),'
        '"destination_workload","$1","destination_service_name","(.+)")'
    )
    inbound = (
        f"rate({metric}{{destination_workload_namespace={ns},"
        'destination_workload!="unknown",source_workload="unknown",'
        f'reporter="destination"{extra}}}[{lookback}])'
    )
    return f"(({known} or {unknown}) or {inbound})"


def golden_http_query(
    metric: str, namespace: str = "sock-shop", lookback: str = "1m"
) -> str:
    traffic = f"sum by (destination_workload) ({_client_rate('istio_requests_total', namespace, lookback=lookback)})"
    if metric == "request_rate":
        return traffic
    if metric == "error_rate":
        errors = f"sum by (destination_workload) ({_client_rate('istio_requests_total', namespace, '5..', lookback)})"
        return f"100 * ({errors} or on(destination_workload) 0 * {traffic}) / clamp_min({traffic}, 0.0001)"
    quantiles = {"latency_p95": 0.95, "latency_p99": 0.99}
    rate = _client_rate("istio_request_duration_milliseconds_bucket", namespace, lookback=lookback)
    return f"histogram_quantile({quantiles[metric]}, sum by (le, destination_workload) ({rate})) / 1000"
