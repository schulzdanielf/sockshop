"""MCP Server for observability - Prometheus, Loki, Metrics and KPIs."""

import json
from typing import Any, Dict

from config import settings
from fastmcp import FastMCP
from investigators import check_oom_kills as _inv_check_oom_kills
from investigators import check_pod_restarts as _inv_check_pod_restarts
from investigators import check_http_error_rate_top as _inv_http_error_rate_top
from investigators import check_request_latency_top as _inv_request_latency_top
from investigators import get_cpu_saturation_top as _inv_cpu_top
from investigators import get_memory_saturation_top as _inv_memory_top
from loki_client import LokiClient
from prometheus_client import PrometheusClient
from service_graph import diff_graph_summaries, summarize_trace_graph
from tempo_client import TempoClient
from trace_analyzer import extract_features
from trace_summarizer import summarize_trace

from metrics import get_golden_metrics_dict, get_kpis_dict

# Initialize FastMCP server
mcp = FastMCP("observability-server")

# Initialize clients
prometheus = None
loki = None
tempo = None


def init_clients():
    """Initialize Prometheus, Loki and Tempo clients."""
    global prometheus, loki, tempo
    prometheus = PrometheusClient()
    loki = LokiClient()
    tempo = TempoClient()


# ============================================================================
# PROMETHEUS TOOLS
# ============================================================================


@mcp.tool()
def prometheus_instant_query(query: str) -> str:
    """Execute an instant query against Prometheus.

    Args:
        query: PromQL query string (e.g., 'up{job="prometheus"}')

    Returns:
        JSON string with query results
    """
    if not prometheus:
        init_clients()
    result = prometheus.query(query)
    return json.dumps(result, indent=2)


@mcp.tool()
def prometheus_range_query(
    query: str, start: str = None, end: str = None, step: str = "1m"
) -> str:
    """Execute a range query against Prometheus.

    Args:
        query: PromQL query string
        start: Start time (ISO format or duration). Defaults to 1 hour ago
        end: End time (ISO format or duration). Defaults to now
        step: Query resolution step (default: 1m)

    Returns:
        JSON string with time series data
    """
    if not prometheus:
        init_clients()
    result = prometheus.query_range(query, start, end, step)
    return json.dumps(result, indent=2)


@mcp.tool()
def prometheus_get_metrics() -> str:
    """Get list of available metrics in Prometheus.

    Returns:
        JSON string with metric names
    """
    if not prometheus:
        init_clients()
    result = prometheus.get_metrics()
    return json.dumps(result, indent=2)


@mcp.tool()
def prometheus_get_series(match: str) -> str:
    """Get time series matching a pattern.

    Args:
        match: Series matcher pattern (e.g., '{job="prometheus"}')

    Returns:
        JSON string with matching series
    """
    if not prometheus:
        init_clients()
    result = prometheus.get_series(match)
    return json.dumps(result, indent=2)


# ============================================================================
# LOKI TOOLS
# ============================================================================


@mcp.tool()
def loki_query(query: str, limit: int = 1000) -> str:
    """Execute an instant query against Loki.

    Args:
        query: LogQL query string (e.g., '{job="varlogs"}')
        limit: Maximum number of log lines (default: 1000)

    Returns:
        JSON string with log results
    """
    if not loki:
        init_clients()
    result = loki.query(query, limit)
    return json.dumps(result, indent=2)


@mcp.tool()
def loki_range_query(
    query: str, start: str = None, end: str = None, limit: int = 1000
) -> str:
    """Execute a range query against Loki.

    Args:
        query: LogQL query string
        start: Start time (Unix timestamp in nanoseconds). Defaults to 1 hour ago
        end: End time (Unix timestamp in nanoseconds). Defaults to now
        limit: Maximum number of log lines (default: 1000)

    Returns:
        JSON string with log results
    """
    if not loki:
        init_clients()
    result = loki.query_range(query, start, end, limit)
    return json.dumps(result, indent=2)


@mcp.tool()
def loki_get_labels() -> str:
    """Get available label names in Loki.

    Returns:
        JSON string with label names
    """
    if not loki:
        init_clients()
    result = loki.get_labels()
    return json.dumps(result, indent=2)


@mcp.tool()
def loki_get_label_values(label: str) -> str:
    """Get available values for a label in Loki.

    Args:
        label: Label name (e.g., 'job', 'pod', 'namespace')

    Returns:
        JSON string with label values
    """
    if not loki:
        init_clients()
    result = loki.get_label_values(label)
    return json.dumps(result, indent=2)


# ============================================================================
# TEMPO TOOLS
# ============================================================================


@mcp.tool()
def tempo_search_traces(
    query: str = None,
    start: str = None,
    end: str = None,
    limit: int = 20,
    min_duration_ms: int = None,
    max_duration_ms: int = None,
    service_name: str = None,
) -> str:
    """Search traces in Tempo.

    Args:
        query: Optional TraceQL query (e.g., '{ status = error }').
        start: Start time in RFC3339 format. Defaults to 1 hour ago.
        end: End time in RFC3339 format. Defaults to now.
        limit: Maximum number of traces to return (default: 20).
        min_duration_ms: Optional minimum trace duration in milliseconds.
        max_duration_ms: Optional maximum trace duration in milliseconds.
        service_name: Optional service name filter.

    Returns:
        JSON string with Tempo trace search results.
    """
    if not tempo:
        init_clients()
    result = tempo.search_traces(
        query=query,
        start=start,
        end=end,
        limit=limit,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        service_name=service_name,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def tempo_get_trace(trace_id: str) -> str:
    """Get a full trace from Tempo by trace ID.

    Args:
        trace_id: Trace ID to fetch.

    Returns:
        JSON string with full trace data.
    """
    if not tempo:
        init_clients()
    result = tempo.get_trace(trace_id)
    return json.dumps(result, indent=2)


@mcp.tool()
def tempo_analyze_trace(trace_id: str) -> str:
    """Analyze a trace and extract observability features.

    Extracts from the raw OTLP trace:
    - trace_duration_ms : total trace wall-clock duration
    - span_count        : total number of spans
    - critical_path     : dominant execution path (highest accumulated latency)
    - hot_spans         : top-10 spans ranked by individual duration
    - error_spans       : spans with error status and their messages
    - fanout            : spans with the most direct children (parallelism hotspots)
    - dependency_map    : unique service-to-service call edges
    - service_latency   : per-service total / avg / max duration aggregation

    Args:
        trace_id: Trace ID to fetch and analyze.

    Returns:
        JSON string with extracted features.
    """
    if not tempo:
        init_clients()
    trace_data = tempo.get_trace(trace_id)
    if trace_data.get("status") == "error":
        return json.dumps(trace_data, indent=2)
    features = extract_features(trace_data)
    return json.dumps(features, indent=2)


@mcp.tool()
def tempo_summarize_trace(
    trace_id: str, use_llm: bool = True, max_new_tokens: int = 512
) -> str:
    """Fetch a trace, extract features, and produce a compact human-readable summary.

    When use_llm=True the summary is also sent to the local Qwen model which returns
    a root-cause analysis and prioritised improvement recommendations.

    The summary format is:
        TRACE SUMMARY
        Trace duration: Xs
        Critical path: service.span → ...
        Top bottlenecks: ...
        Errors: ...
        Fanout detection: ...
        Dependency graph: ...
        Service latency aggregation: ...

    Args:
        trace_id: Trace ID to summarize.
        use_llm: Whether to call the local LLM for analysis (default: True).
        max_new_tokens: Max tokens for LLM response (default: 512).

    Returns:
        JSON string with keys 'summary' (compact text) and 'llm_analysis' (LLM output or null).
    """
    if not tempo:
        init_clients()
    trace_data = tempo.get_trace(trace_id)
    if trace_data.get("status") == "error":
        return json.dumps(trace_data, indent=2)
    features = extract_features(trace_data)
    result = summarize_trace(features, use_llm=use_llm, max_new_tokens=max_new_tokens)
    return json.dumps(result, indent=2)


@mcp.tool()
def tempo_search_and_analyze(
    query: str = None,
    service_name: str = None,
    start: str = None,
    end: str = None,
    limit: int = 5,
    min_duration_ms: int = None,
    max_duration_ms: int = None,
) -> str:
    """Search traces and return extracted features for each result.

    Combines tempo_search_traces + tempo_analyze_trace in a single call.
    Useful for comparing multiple traces or quickly spotting anomalies.

    Args:
        query: TraceQL query (e.g., '{ status = error }').
        service_name: Filter by service name.
        start: Start time in RFC3339 format.
        end: End time in RFC3339 format.
        limit: Maximum number of traces to analyze (default: 5).
        min_duration_ms: Minimum trace duration filter in ms.
        max_duration_ms: Maximum trace duration filter in ms.

    Returns:
        JSON string with list of {trace_id, features} objects.
    """
    if not tempo:
        init_clients()

    search_result = tempo.search_traces(
        query=query,
        service_name=service_name,
        start=start,
        end=end,
        limit=limit,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
    )

    if search_result.get("status") == "error":
        return json.dumps(search_result, indent=2)

    traces = search_result.get("traces", [])
    analyzed = []

    for t in traces:
        trace_id = t.get("traceID") or t.get("traceId", "")
        if not trace_id:
            continue
        trace_data = tempo.get_trace(trace_id)
        features = (
            extract_features(trace_data)
            if trace_data.get("status") != "error"
            else {"error": trace_data.get("error")}
        )
        analyzed.append({"trace_id": trace_id, "features": features})

    return json.dumps(analyzed, indent=2)


def _tempo_analyzed_traces(
    query: str = None,
    service_name: str = None,
    start: str = None,
    end: str = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Fetch and analyze traces for a bounded Tempo window."""
    if not tempo:
        init_clients()
    search = tempo.search_traces(
        query=query,
        service_name=service_name,
        start=start,
        end=end,
        limit=limit,
    )
    if search.get("status") == "error":
        return search

    analyzed = []
    for item in search.get("traces", []) or []:
        trace_id = item.get("traceID") or item.get("traceId")
        if not trace_id:
            continue
        trace_data = tempo.get_trace(trace_id)
        features = (
            extract_features(trace_data)
            if trace_data.get("status") != "error"
            else {"error": trace_data.get("error")}
        )
        analyzed.append(
            {
                "trace_id": trace_id,
                "root_service": item.get("rootServiceName"),
                "root_trace_name": item.get("rootTraceName"),
                "start_time_unix_nano": item.get("startTimeUnixNano"),
                "duration_ms": item.get("durationMs"),
                "features": features,
            }
        )
    return {"traces": analyzed, "search_count": len(search.get("traces", []) or [])}


@mcp.tool()
def tempo_service_graph_window(
    start: str,
    end: str,
    query: str = None,
    namespace: str = "sock-shop",
    service_name: str = None,
    limit: int = 50,
    phase: str = "window",
    top_k: int = 5,
) -> str:
    """Capture a compact service graph summary for a bounded time window.

    The summary is model-oriented: top nodes/edges by errors and latency,
    first degraded services, and a suspected propagation path. It deliberately
    avoids raw trace payloads to keep the context small.

    Args:
        start: RFC3339 start of the window.
        end: RFC3339 end of the window.
        query: Optional TraceQL query. Defaults to a namespace-scoped query.
        namespace: Kubernetes namespace used when query is omitted.
        service_name: Optional service filter (avoid during RCA evals).
        limit: Max traces to inspect (default 50).
        phase: Human label for the window (baseline/fault/post).
        top_k: Number of ranked nodes/edges to include.
    """
    trace_query = query or (
        f'{{ resource.k8s.namespace.name = "{namespace}" && name !~ ".*health.*" '
        '&& name !~ ".*metrics.*" }'
    )
    window = _tempo_analyzed_traces(
        query=trace_query,
        service_name=service_name,
        start=start,
        end=end,
        limit=limit,
    )
    if window.get("status") == "error":
        return json.dumps(window, indent=2)
    summary = summarize_trace_graph(
        window.get("traces") or [],
        window_start=start,
        window_end=end,
        phase=phase,
        top_k=top_k,
    )
    summary["query"] = trace_query
    summary["limit"] = limit
    summary["search_count"] = window.get("search_count")
    return json.dumps(summary, indent=2)


@mcp.tool()
def tempo_service_graph_phases(
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    post_start: str = None,
    post_end: str = None,
    query: str = None,
    namespace: str = "sock-shop",
    limit: int = 50,
    top_k: int = 5,
) -> str:
    """Capture baseline/fault[/post] windows and return a compact graph delta.

    This is the preferred experiment-facing tool: it compares healthy vs
    degraded topology/latency/error behaviour and returns only the changes most
    likely to help identify where the fault propagated.
    """
    trace_query = query or (
        f'{{ resource.k8s.namespace.name = "{namespace}" && name !~ ".*health.*" '
        '&& name !~ ".*metrics.*" }'
    )
    baseline_raw = _tempo_analyzed_traces(
        query=trace_query, start=baseline_start, end=baseline_end, limit=limit
    )
    fault_raw = _tempo_analyzed_traces(
        query=trace_query, start=fault_start, end=fault_end, limit=limit
    )
    if baseline_raw.get("status") == "error":
        return json.dumps(baseline_raw, indent=2)
    if fault_raw.get("status") == "error":
        return json.dumps(fault_raw, indent=2)

    baseline = summarize_trace_graph(
        baseline_raw.get("traces") or [],
        window_start=baseline_start,
        window_end=baseline_end,
        phase="baseline",
        top_k=top_k,
    )
    fault = summarize_trace_graph(
        fault_raw.get("traces") or [],
        window_start=fault_start,
        window_end=fault_end,
        phase="fault",
        top_k=top_k,
    )
    post = None
    if post_start and post_end:
        post_raw = _tempo_analyzed_traces(
            query=trace_query, start=post_start, end=post_end, limit=limit
        )
        if post_raw.get("status") == "error":
            return json.dumps(post_raw, indent=2)
        post = summarize_trace_graph(
            post_raw.get("traces") or [],
            window_start=post_start,
            window_end=post_end,
            phase="post",
            top_k=top_k,
        )

    delta = diff_graph_summaries(baseline, fault, recovery=post, top_k=top_k)
    result = {
        "query": trace_query,
        "namespace": namespace,
        "limit": limit,
        "phases": {"baseline": baseline, "fault": fault, "post": post},
        "delta": delta,
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def tempo_graph_diff_from_summaries(
    baseline: Dict[str, Any],
    fault: Dict[str, Any],
    post: Dict[str, Any] = None,
    top_k: int = 5,
) -> str:
    """Compute a graph delta from previously captured window summaries.

    Useful when the caller already stored phase summaries and wants to avoid
    querying Tempo again.
    """
    return json.dumps(
        diff_graph_summaries(baseline, fault, recovery=post, top_k=top_k),
        indent=2,
    )


# ============================================================================
# GOLDEN METRICS TOOLS
# ============================================================================


@mcp.tool()
def get_golden_metrics() -> str:
    """Get list of golden metrics (RED method).

    Returns the golden metrics definitions including:
    - Request Rate: Number of requests per second
    - Error Rate: Proportion of requests that result in error
    - Latency P95 and P99: Percentiles of request duration
    - CPU and Memory Usage: Resource consumption
    - Disk Write Throughput: Filesystem write rate per pod
    - MySQL/MongoDB/Redis: Database throughput and latency indicators

    Returns:
        JSON string with golden metrics definitions
    """
    result = get_golden_metrics_dict()
    return json.dumps(result, indent=2)


# ============================================================================
# HIGH-LEVEL INVESTIGATOR TOOLS
# ============================================================================


@mcp.tool()
def check_http_error_rate_top(
    start: str,
    end: str,
    step: str = "15s",
    k: int = 5,
) -> str:
    """Return top services by HTTP 5xx error rate in the given window.

    This is a high-level RCA-oriented tool meant for agents. It hides PromQL and
    returns a stable JSON contract with found/count/top/query/window.
    """
    if not prometheus:
        init_clients()
    result = _inv_http_error_rate_top(
        prometheus,
        start=start,
        end=end,
        step=step,
        k=k,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def check_request_latency_top(
    start: str,
    end: str,
    step: str = "15s",
    k: int = 5,
) -> str:
    """Return top services by request latency P95 in the given window."""
    if not prometheus:
        init_clients()
    result = _inv_request_latency_top(
        prometheus,
        start=start,
        end=end,
        step=step,
        k=k,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def query_golden_metric(metric_name: str) -> str:
    """Query a specific golden metric from Prometheus.

    Args:
        metric_name: Name of the golden metric (e.g., 'Request Rate', 'Latency P95', 'MySQL Query Rate', 'MongoDB Ops Rate')

    Returns:
        JSON string with metric values
    """
    if not prometheus:
        init_clients()

    metrics_dict = get_golden_metrics_dict()
    metric = None

    for m in metrics_dict["metrics"]:
        if m["name"].lower() == metric_name.lower():
            metric = m
            break

    if not metric:
        return json.dumps({"error": f"Golden metric '{metric_name}' not found"})

    result = prometheus.query(metric["query"])
    result["metric_info"] = {
        "name": metric["name"],
        "description": metric["description"],
        "unit": metric["unit"],
    }
    return json.dumps(result, indent=2)


# ============================================================================
# KPI TOOLS
# ============================================================================


@mcp.tool()
def get_kpis() -> str:
    """Get list of application KPIs.

    Returns KPI definitions including:
    - Service Availability
    - Mean Time To Recovery (MTTR)
    - Error Budget Consumption
    - Cache Hit Rate
    - Queue Depth
    - Database Connection Pool Utilization
    - Concurrency

    Returns:
        JSON string with KPI definitions
    """
    result = get_kpis_dict()
    return json.dumps(result, indent=2)


@mcp.tool()
def query_kpi(kpi_name: str) -> str:
    """Query a specific KPI from Prometheus.

    Args:
        kpi_name: Name of the KPI (e.g., 'Service Availability', 'Cache Hit Rate', 'Concurrency')

    Returns:
        JSON string with KPI values
    """
    if not prometheus:
        init_clients()

    kpis_dict = get_kpis_dict()
    kpi = None

    for k in kpis_dict["kpis"]:
        if k["name"].lower() == kpi_name.lower():
            kpi = k
            break

    if not kpi:
        return json.dumps({"error": f"KPI '{kpi_name}' not found"})

    result = prometheus.query(kpi["query"])
    result["kpi_info"] = {
        "name": kpi["name"],
        "description": kpi["description"],
        "threshold": kpi["threshold"],
        "alert_condition": kpi["alert_condition"],
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def query_all_kpis() -> str:
    """Query all KPIs from Prometheus.

    Returns:
        JSON string with all KPI values
    """
    if not prometheus:
        init_clients()

    kpis_dict = get_kpis_dict()
    results = {"kpis_status": []}

    for kpi in kpis_dict["kpis"]:
        query_result = prometheus.query(kpi["query"])
        results["kpis_status"].append(
            {
                "name": kpi["name"],
                "description": kpi["description"],
                "threshold": kpi["threshold"],
                "alert_condition": kpi["alert_condition"],
                "current_value": query_result.get("data", {}).get("result", []),
            }
        )

    return json.dumps(results, indent=2)


# ============================================================================
# INVESTIGATOR TOOLS (high-level, window-scoped, agent-friendly)
# ============================================================================
# These wrap raw PromQL with stable JSON contracts so a small LLM (or a
# step in an agentic pipeline) can ask "did any pod get OOMKilled between
# t0 and t1?" without writing PromQL. Every tool takes explicit
# start/end ISO timestamps — never an open-ended `increase(...[1h])` —
# so signals are strictly scoped to the caller's window and never leak
# across experiments. Implementation: investigators.py.


@mcp.tool()
def check_oom_kills(
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
) -> str:
    """Return services with OOMKilled containers within ``[start, end]``.

    Args:
        start: ISO timestamp (inclusive) of the investigation window start.
        end: ISO timestamp (inclusive) of the investigation window end.
        namespace: Kubernetes namespace to inspect.
        step: Range-query resolution (default 15s).

    Returns:
        JSON object with keys ``found`` (bool), ``count`` (int),
        ``top`` (list of {service, transitioned_in_window, delta,
        max_in_window, pods}), ``query``, ``window``.
        ``transitioned_in_window=True`` means the OOMKilled gauge went
        from 0 to 1 inside the window (i.e. the OOM happened *during*
        this run, not before).
    """
    if not prometheus:
        init_clients()
    result = _inv_check_oom_kills(
        prometheus,
        start=start,
        end=end,
        namespace=namespace,
        step=step,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def check_pod_restarts(
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
) -> str:
    """Return services that experienced pod restarts within ``[start, end]``.

    Computed as ``max(counter) - min(counter)`` on a range-query
    bounded by the window — so an experiment of 6 minutes returns
    restarts in those 6 minutes regardless of any earlier OOMs.

    Args:
        start: ISO timestamp (inclusive) of the investigation window start.
        end: ISO timestamp (inclusive) of the investigation window end.
        namespace: Kubernetes namespace to inspect.
        step: Range-query resolution (default 15s).

    Returns:
        JSON object with keys ``found`` (bool), ``count`` (int),
        ``top`` (list of {service, restarts_in_window, pods}),
        ``query``, ``window``.
    """
    if not prometheus:
        init_clients()
    result = _inv_check_pod_restarts(
        prometheus,
        start=start,
        end=end,
        namespace=namespace,
        step=step,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def get_cpu_saturation_top(
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
) -> str:
    """Top-K services by CPU saturation (% of limit) within the window.

    Saturation is ``rate(container_cpu_usage_seconds_total) /
    (cpu_quota / cpu_period)``. Reports both ``saturation_mean_pct``
    (typical) and ``saturation_max_pct`` (peak) so an agent can tell
    sustained pressure from short spikes. Pods without a CPU limit
    are excluded — no saturation signal exists for them.

    Args:
        start: ISO timestamp (inclusive) of the window start.
        end: ISO timestamp (inclusive) of the window end.
        namespace: Kubernetes namespace to inspect.
        step: Range-query resolution (default 15s).
        k: Number of top services to return (default 3).
    """
    if not prometheus:
        init_clients()
    result = _inv_cpu_top(
        prometheus,
        start=start,
        end=end,
        namespace=namespace,
        step=step,
        k=k,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def get_memory_saturation_top(
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
) -> str:
    """Top-K services by memory saturation (% of limit) within the window.

    Saturation is ``container_memory_working_set_bytes /
    container_spec_memory_limit_bytes``. Pods without a memory limit
    are excluded (no saturation signal exists for them).

    Args:
        start: ISO timestamp (inclusive) of the window start.
        end: ISO timestamp (inclusive) of the window end.
        namespace: Kubernetes namespace to inspect.
        step: Range-query resolution (default 15s).
        k: Number of top services to return (default 3).
    """
    if not prometheus:
        init_clients()
    result = _inv_memory_top(
        prometheus,
        start=start,
        end=end,
        namespace=namespace,
        step=step,
        k=k,
    )
    return json.dumps(result, indent=2)


# ============================================================================
# UTILITY TOOLS
# ============================================================================


@mcp.tool()
def health_check() -> str:
    """Check health status of Prometheus, Loki and Tempo services.

    Returns:
        JSON string with health status
    """
    if not prometheus:
        init_clients()
    if not loki:
        init_clients()
    if not tempo:
        init_clients()

    health_status = {"status": "healthy", "services": {}}

    # Check Prometheus
    try:
        result = prometheus.query("up{job='prometheus'}")
        health_status["services"]["prometheus"] = (
            "ok" if result.get("status") == "success" else "error"
        )
    except Exception as e:
        health_status["services"]["prometheus"] = f"error: {str(e)}"

    # Check Loki
    try:
        result = loki.get_labels()
        health_status["services"]["loki"] = (
            "ok" if result.get("status") == "success" else "error"
        )
    except Exception as e:
        health_status["services"]["loki"] = f"error: {str(e)}"

    # Check Tempo
    try:
        result = tempo.search_traces(limit=1)
        health_status["services"]["tempo"] = (
            "ok" if result.get("status") != "error" else "error"
        )
    except Exception as e:
        health_status["services"]["tempo"] = f"error: {str(e)}"

    return json.dumps(health_status, indent=2)


if __name__ == "__main__":
    init_clients()
    mcp.run(
        transport="sse",
        host=settings.host,
        port=settings.port,
    )
