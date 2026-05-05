"""MCP Server for observability - Prometheus, Loki, Metrics and KPIs."""
import json
from typing import Any
from fastmcp import FastMCP
from prometheus_client import PrometheusClient
from loki_client import LokiClient
from metrics import get_golden_metrics_dict, get_kpis_dict
from config import settings

# Initialize FastMCP server
mcp = FastMCP("observability-server")

# Initialize clients
prometheus = None
loki = None


def init_clients():
    """Initialize Prometheus and Loki clients."""
    global prometheus, loki
    prometheus = PrometheusClient()
    loki = LokiClient()


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
    query: str,
    start: str = None,
    end: str = None,
    step: str = "1m"
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
    query: str,
    start: str = None,
    end: str = None,
    limit: int = 1000
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
    
    Returns:
        JSON string with golden metrics definitions
    """
    result = get_golden_metrics_dict()
    return json.dumps(result, indent=2)


@mcp.tool()
def query_golden_metric(metric_name: str) -> str:
    """Query a specific golden metric from Prometheus.
    
    Args:
        metric_name: Name of the golden metric (e.g., 'Request Rate', 'Error Rate', 'Latency P95')
    
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
        "unit": metric["unit"]
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
    
    Returns:
        JSON string with KPI definitions
    """
    result = get_kpis_dict()
    return json.dumps(result, indent=2)


@mcp.tool()
def query_kpi(kpi_name: str) -> str:
    """Query a specific KPI from Prometheus.
    
    Args:
        kpi_name: Name of the KPI (e.g., 'Service Availability', 'Cache Hit Rate')
    
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
        "alert_condition": kpi["alert_condition"]
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
        results["kpis_status"].append({
            "name": kpi["name"],
            "description": kpi["description"],
            "threshold": kpi["threshold"],
            "alert_condition": kpi["alert_condition"],
            "current_value": query_result.get("data", {}).get("result", [])
        })
    
    return json.dumps(results, indent=2)


# ============================================================================
# UTILITY TOOLS
# ============================================================================

@mcp.tool()
def health_check() -> str:
    """Check health status of Prometheus and Loki services.
    
    Returns:
        JSON string with health status
    """
    if not prometheus:
        init_clients()
    if not loki:
        init_clients()
    
    health_status = {
        "status": "healthy",
        "services": {}
    }
    
    # Check Prometheus
    try:
        result = prometheus.query("up{job='prometheus'}")
        health_status["services"]["prometheus"] = "ok" if result.get("status") == "success" else "error"
    except Exception as e:
        health_status["services"]["prometheus"] = f"error: {str(e)}"
    
    # Check Loki
    try:
        result = loki.get_labels()
        health_status["services"]["loki"] = "ok" if result.get("status") == "success" else "error"
    except Exception as e:
        health_status["services"]["loki"] = f"error: {str(e)}"
    
    return json.dumps(health_status, indent=2)


if __name__ == "__main__":
    init_clients()
    mcp.run(
        transport="sse",
        host=settings.host,
        port=settings.port,
    )
