"""Golden metrics and KPIs definitions."""
from dataclasses import dataclass
from typing import Any, Dict, List

from http_metrics import golden_http_query


@dataclass
class GoldenMetric:
    """Golden metric definition."""

    name: str
    query: str
    description: str
    unit: str


@dataclass
class KPI:
    """KPI definition."""

    name: str
    query: str
    description: str
    threshold: str
    alert_condition: str


# Golden Metrics - RED method (Rate, Errors, Duration)
GOLDEN_METRICS: List[GoldenMetric] = [
    GoldenMetric(
        name="Request Rate",
        query=golden_http_query("request_rate"),
        description="Number of requests per second",
        unit="req/s",
    ),
    GoldenMetric(
        name="Error Rate",
        query=golden_http_query("error_rate"),
        description="Proportion of requests that result in error",
        unit="%",
    ),
    GoldenMetric(
        name="Latency P95",
        query=golden_http_query("latency_p95"),
        description="95th percentile of request duration",
        unit="s",
    ),
    GoldenMetric(
        name="Latency P99",
        query=golden_http_query("latency_p99"),
        description="99th percentile of request duration",
        unit="s",
    ),
    GoldenMetric(
        name="CPU Usage",
        query='sum(rate(container_cpu_usage_seconds_total{namespace="sock-shop"}[1m])) by (pod)',
        description="CPU usage per container",
        unit="cores",
    ),
    GoldenMetric(
        name="Memory Usage",
        query='sum(container_memory_usage_bytes{namespace="sock-shop"}) by (pod)',
        description="Memory usage per container",
        unit="bytes",
    ),
    GoldenMetric(
        name="Disk Write Throughput",
        query='sum by(pod) (rate(container_fs_writes_bytes_total{namespace="sock-shop"}[1m]))',
        description="Disk write throughput per pod",
        unit="bytes/s",
    ),
    GoldenMetric(
        name="MySQL Query Rate",
        query="rate(mysql_global_status_queries[1m])",
        description="MySQL queries processed per second",
        unit="queries/s",
    ),
    GoldenMetric(
        name="MongoDB Ops Rate",
        query="sum(rate(mongodb_ss_opLatencies_ops[1m])) by (name, op_type)",
        description="MongoDB operation count rate by service and operation type",
        unit="ops/s",
    ),
    GoldenMetric(
        name="MongoDB Avg Op Latency",
        query="sum(rate(mongodb_ss_opLatencies_latency[1m])) by (name, op_type) / sum(rate(mongodb_ss_opLatencies_ops[1m])) by (name, op_type)",
        description="Average MongoDB operation latency by service and operation type",
        unit="latency/op",
    ),
    GoldenMetric(
        name="Redis Commands Rate",
        query='rate(redis_commands_processed_total{job="kubernetes-service-endpoints"}[1m])',
        description="Redis commands processed per second",
        unit="commands/s",
    ),
]

# KPIs - Application specific
KPIS: List[KPI] = [
    KPI(
        name="Service Availability",
        query=f"100 - ({golden_http_query('error_rate')})",
        description="Percentage of successful requests",
        threshold=">= 99.9%",
        alert_condition="< 99",
    ),
    KPI(
        name="Mean Time To Recovery (MTTR)",
        query="avg(up_duration_seconds) by (service)",
        description="Average time to recover from failures",
        threshold="< 5min",
        alert_condition="> 300",
    ),
    KPI(
        name="Error Budget Consumption",
        query=f"({golden_http_query('error_rate', lookback='1h')}) / (100 * 0.001)",
        description="Percentage of error budget consumed",
        threshold="< 10%",
        alert_condition="> 50",
    ),
    KPI(
        name="Cache Hit Rate",
        query="sum(rate(cache_hits_total[1m])) / (sum(rate(cache_hits_total[1m])) + sum(rate(cache_misses_total[1m]))) * 100",
        description="Percentage of cache hits",
        threshold="> 80%",
        alert_condition="< 60",
    ),
    KPI(
        name="Queue Depth",
        query="avg(queue_depth) by (queue_name)",
        description="Average number of messages in queue",
        threshold="< 1000",
        alert_condition="> 5000",
    ),
    KPI(
        name="Database Connection Pool Utilization",
        query="sum(db_connections_active) / sum(db_connections_max) * 100",
        description="Percentage of database connections in use",
        threshold="< 80%",
        alert_condition="> 90",
    ),
    KPI(
        name="Concurrency",
        query=f"({golden_http_query('request_rate')}) * ({golden_http_query('latency_p95')})",
        description="Estimated concurrent requests using request rate multiplied by P95 latency",
        threshold="< team-defined limit",
        alert_condition="> team-defined limit",
    ),
]


def get_golden_metrics_dict() -> Dict[str, Any]:
    """Get golden metrics as dictionary."""
    return {
        "metrics": [
            {
                "name": m.name,
                "query": m.query,
                "description": m.description,
                "unit": m.unit,
            }
            for m in GOLDEN_METRICS
        ]
    }


def get_kpis_dict() -> Dict[str, Any]:
    """Get KPIs as dictionary."""
    return {
        "kpis": [
            {
                "name": k.name,
                "query": k.query,
                "description": k.description,
                "threshold": k.threshold,
                "alert_condition": k.alert_condition,
            }
            for k in KPIS
        ]
    }
