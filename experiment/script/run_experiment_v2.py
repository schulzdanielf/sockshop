#!/usr/bin/env python3
"""Run load experiments with Locust and export MCP-backed metrics to CSV.

What this script does per round:
1. Saves key timestamps (start, warmup start, ready, end)
2. Starts Locust with configurable users/spawn-rate
3. Waits for warmup + experiment duration
4. Pulls Golden Metrics and KPIs through MCP tools
5. Stores all samples with load_state labels in CSV
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import shlex
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client


@dataclass(frozen=True)
class MetricSpec:
    """Maps an experiment output column to a metric/KPI exposed by MCP."""

    column_name: str
    source: str  # "golden" or "kpi"
    aliases: List[str]
    fallback_query: Optional[str] = None


# Services to capture metrics for
SERVICES_TO_CAPTURE = ["front", "carts", "catalogue", "user"]

EXPERIMENT_METRICS: List[MetricSpec] = [
    MetricSpec(
        column_name="trafego",
        source="golden",
        aliases=["Request Rate", "Traffic"],
    ),
    MetricSpec(
        column_name="taxa_erros",
        source="golden",
        aliases=["Error Rate"],
    ),
    MetricSpec(
        column_name="tempo_resposta_medio",
        source="golden",
        aliases=["Latency Mean", "Latency Average", "Response Time Mean", "Response Time Average"],
        fallback_query='sum(rate(request_duration_seconds_sum[5m])) by (name) / sum(rate(request_duration_seconds_count[5m])) by (name)',
    ),
    MetricSpec(
        column_name="tempo_resposta_p95",
        source="golden",
        aliases=["Latency P95"],
    ),
    MetricSpec(
        column_name="saturacao_cpu",
        source="golden",
        aliases=["CPU Usage"],
    ),
    MetricSpec(
        column_name="saturacao_memoria",
        source="golden",
        aliases=["Memory Usage"],
    ),
    MetricSpec(
        column_name="disponibilidade",
        source="kpi",
        aliases=["Service Availability", "Disponibilidade"],
    ),
    MetricSpec(
        column_name="concorrencia",
        source="kpi",
        aliases=["Concurrency", "Concorrencia"],
    ),
]


def utc_iso_from_epoch(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc).isoformat()


def now_epoch() -> float:
    return time.time()


def normalize_service_name(label_value: str) -> Optional[str]:
    """Extract service name from pod label or use name directly.
    
    Examples:
    - 'front' -> 'front'
    - 'front-5f65464684-6fhlt' -> 'front'
    - 'carts-db-xyz' -> 'carts'
    """
    if not isinstance(label_value, str):
        return None
    
    # Try to match known services
    for service in SERVICES_TO_CAPTURE:
        if label_value == service:
            return service
        # Check if it starts with service- (pod name pattern)
        if label_value.startswith(service + "-"):
            return service
    
    return None


def _http_get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_vector_or_scalar(payload: dict) -> Optional[float]:
    if payload.get("status") != "success":
        return None

    data = payload.get("data", {})
    result_type = data.get("resultType")
    result = data.get("result")

    if result_type == "vector":
        if not result:
            return None
        values: List[float] = []
        for item in result:
            value_pair = item.get("value", [None, None])
            try:
                values.append(float(value_pair[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return sum(values) if values else None

    if result_type == "scalar":
        try:
            return float(result[1])
        except (TypeError, ValueError, IndexError):
            return None

    return None


def _decode_json_if_possible(value: Any) -> Any:
    current = value
    for _ in range(3):
        if isinstance(current, str):
            try:
                current = json.loads(current)
                continue
            except json.JSONDecodeError:
                return current
        return current
    return current


def _extract_mcp_payload(raw: Any) -> Any:
    payload = _decode_json_if_possible(raw)
    if not isinstance(payload, dict):
        return payload

    structured = payload.get("structuredContent")
    if structured is not None:
        if isinstance(structured, dict) and "result" in structured:
            return _decode_json_if_possible(structured["result"])
        return _decode_json_if_possible(structured)

    content = payload.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and "text" in first:
            return _decode_json_if_possible(first["text"])

    return payload


class MCPToolClient:
    """Simple MCP client for calling tools over SSE from sync code."""

    def __init__(self, sse_url: str, timeout_seconds: int):
        self.sse_url = sse_url
        self.timeout_seconds = timeout_seconds

    def assert_sse_connectivity(self) -> None:
        parsed = urllib.parse.urlparse(self.sse_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=self.timeout_seconds):
            pass

        req = urllib.request.Request(
            self.sse_url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            if response.status >= 400:
                raise RuntimeError(f"SSE endpoint retornou status HTTP {response.status}")

    async def _call_tool_async(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        async with sse_client(
            self.sse_url,
            timeout=self.timeout_seconds,
            sse_read_timeout=max(20, self.timeout_seconds * 2),
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                return result.model_dump() if hasattr(result, "model_dump") else result

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        return asyncio.run(self._call_tool_async(name, arguments))

    def call_tool_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        raw = self.call_tool(name, arguments)
        return _extract_mcp_payload(raw)


def _metric_catalog_index(items: List[Dict[str, Any]], key_field: str) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in items:
        name = item.get(key_field)
        if isinstance(name, str):
            indexed[name.lower()] = item
    return indexed


def load_metric_queries_from_mcp(client: MCPToolClient) -> Dict[str, str]:
    golden_raw = client.call_tool_json("get_golden_metrics")
    kpi_raw = client.call_tool_json("get_kpis")

    golden_items = golden_raw.get("metrics", []) if isinstance(golden_raw, dict) else []
    kpi_items = kpi_raw.get("kpis", []) if isinstance(kpi_raw, dict) else []

    golden_by_name = _metric_catalog_index(golden_items, "name")
    kpi_by_name = _metric_catalog_index(kpi_items, "name")

    resolved: Dict[str, str] = {}
    missing: List[str] = []

    for spec in EXPERIMENT_METRICS:
        source_index = golden_by_name if spec.source == "golden" else kpi_by_name
        found_query: Optional[str] = None

        for alias in spec.aliases:
            record = source_index.get(alias.lower())
            if isinstance(record, dict) and isinstance(record.get("query"), str):
                found_query = record["query"]
                break

        if not found_query and spec.fallback_query:
            found_query = spec.fallback_query

        if not found_query:
            missing.append(spec.column_name)
            continue

        resolved[spec.column_name] = found_query

    if missing:
        raise RuntimeError(
            "Metricas nao encontradas no MCP: "
            f"{', '.join(missing)}. "
            "Valide aliases em EXPERIMENT_METRICS e o catalogo retornado por get_golden_metrics/get_kpis."
        )

    return resolved


def query_mcp_instant(client: MCPToolClient, promql: str) -> Optional[float]:
    payload = client.call_tool_json("prometheus_instant_query", {"query": promql})
    if not isinstance(payload, dict):
        return None
    return _parse_vector_or_scalar(payload)


def query_mcp_range_by_service(
    client: MCPToolClient,
    promql: str,
    start_epoch: float,
    end_epoch: float,
    step_seconds: int,
) -> Dict[int, Dict[str, Optional[float]]]:
    """Query Prometheus and extract metrics by service.
    
    Returns: Dict[timestamp, Dict[service_name, value]]
    """
    payload = client.call_tool_json(
        "prometheus_range_query",
        {
            "query": promql,
            "start": utc_iso_from_epoch(start_epoch),
            "end": utc_iso_from_epoch(end_epoch),
            "step": f"{step_seconds}s",
        },
    )

    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {}

    result_type = payload.get("data", {}).get("resultType")
    result = payload.get("data", {}).get("result", [])
    if result_type != "matrix" or not isinstance(result, list):
        return {}

    # Group by service, then by timestamp
    by_service_ts: Dict[int, Dict[str, float]] = {}
    
    for series in result:
        # Extract service name from metric labels (name or pod)
        metric_labels = series.get("metric", {})
        service_name = metric_labels.get("name") or metric_labels.get("pod")
        
        normalized_service = normalize_service_name(service_name) if service_name else None
        if not normalized_service:
            continue
        
        values = series.get("values", [])
        for item in values:
            try:
                ts = int(float(item[0]))
                val = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            
            if ts not in by_service_ts:
                by_service_ts[ts] = {}
            # Store value for this service (overwrite if duplicate, or sum if needed)
            by_service_ts[ts][normalized_service] = val
    
    # Build output with all services for each timestamp
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for ts in range(int(start_epoch), int(end_epoch) + 1, step_seconds):
        out[ts] = {}
        for service in SERVICES_TO_CAPTURE:
            out[ts][service] = by_service_ts.get(ts, {}).get(service)
    
    return out


def query_mcp_range(
    client: MCPToolClient,
    promql: str,
    start_epoch: float,
    end_epoch: float,
    step_seconds: int,
) -> Dict[int, Optional[float]]:
    """Legacy: Query and sum all series (kept for backward compatibility)."""
    payload = client.call_tool_json(
        "prometheus_range_query",
        {
            "query": promql,
            "start": utc_iso_from_epoch(start_epoch),
            "end": utc_iso_from_epoch(end_epoch),
            "step": f"{step_seconds}s",
        },
    )

    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {}

    result_type = payload.get("data", {}).get("resultType")
    result = payload.get("data", {}).get("result", [])
    if result_type != "matrix" or not isinstance(result, list):
        return {}

    merged: Dict[int, float] = {}
    for series in result:
        values = series.get("values", [])
        for item in values:
            try:
                ts = int(float(item[0]))
                val = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            merged[ts] = merged.get(ts, 0.0) + val

    out: Dict[int, Optional[float]] = {}
    for ts in range(int(start_epoch), int(end_epoch) + 1, step_seconds):
        out[ts] = merged.get(ts)
    return out


def run_locust_round(args: argparse.Namespace, run_total_seconds: int) -> subprocess.CompletedProcess[str]:
    cmd = shlex.split(args.locust_cmd) + [
        "-f",
        args.locust_file,
        "--host",
        args.locust_host,
        "--headless",
        "--users",
        str(args.users),
        "--spawn-rate",
        str(args.spawn_rate),
        "--run-time",
        f"{run_total_seconds}s",
    ]

    if args.locust_extra_args:
        cmd.extend(args.locust_extra_args.split())

    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=run_total_seconds + args.locust_grace_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Comando do Locust nao encontrado. "
            "Use o comando via Kubernetes: --locust-cmd 'kubectl -n loadtest exec deploy/locust-web -- locust'."
        ) from exc


def state_for_timestamp(
    ts: int,
    disabled_end: float,
    warmup_end: float,
    run_end: float,
) -> str:
    if ts < int(disabled_end):
        return "DESATIVADA"
    if ts < int(warmup_end):
        return "AQUECIMENTO"
    if ts <= int(run_end):
        return "EXECUCAO"
    return "DESATIVADA"


def run(args: argparse.Namespace) -> Path:
    mcp_client = MCPToolClient(args.mcp_sse_url, args.mcp_timeout_seconds)
    mcp_client.assert_sse_connectivity()
    metrics = load_metric_queries_from_mcp(mcp_client)
    metric_names = list(metrics.keys())

    rows: List[Dict[str, object]] = []

    for round_number in range(1, args.rounds + 1):
        round_start = now_epoch()

        if args.disabled_seconds > 0:
            time.sleep(args.disabled_seconds)

        warmup_start = now_epoch()
        warmup_end = warmup_start + args.warmup_seconds
        run_end = warmup_end + args.run_seconds
        locust_total = args.warmup_seconds + args.run_seconds

        locust_result = run_locust_round(args=args, run_total_seconds=locust_total)

        round_end = now_epoch()
        # If locust exits earlier/later for any reason, keep a deterministic data window.
        data_end = min(max(run_end, warmup_start), round_end)

        by_metric: Dict[str, Dict[int, Dict[str, Optional[float]]]] = {}
        for metric_name, promql in metrics.items():
            by_metric[metric_name] = query_mcp_range_by_service(
                client=mcp_client,
                promql=promql,
                start_epoch=round_start,
                end_epoch=data_end,
                step_seconds=args.sample_interval_seconds,
            )

        ts_points = list(range(int(round_start), int(data_end) + 1, args.sample_interval_seconds))
        for ts in ts_points:
            row: Dict[str, object] = {
                "timestamp_utc": utc_iso_from_epoch(ts),
                "round": round_number,
                "load_state": state_for_timestamp(
                    ts=ts,
                    disabled_end=warmup_start,
                    warmup_end=warmup_end,
                    run_end=run_end,
                ),
                "round_start_utc": utc_iso_from_epoch(round_start),
                "warmup_start_utc": utc_iso_from_epoch(warmup_start),
                "ready_utc": utc_iso_from_epoch(warmup_end),
                "round_end_utc": utc_iso_from_epoch(data_end),
                "locust_exit_code": locust_result.returncode,
            }
            for metric_name in metric_names:
                service_values = by_metric.get(metric_name, {}).get(ts, {})
                for service in SERVICES_TO_CAPTURE:
                    col_name = f"{metric_name}_{service}"
                    row[col_name] = service_values.get(service)
            rows.append(row)

        ready_row: Dict[str, object] = {
            "timestamp_utc": utc_iso_from_epoch(warmup_end),
            "round": round_number,
            "load_state": "PRONTA",
            "round_start_utc": utc_iso_from_epoch(round_start),
            "warmup_start_utc": utc_iso_from_epoch(warmup_start),
            "ready_utc": utc_iso_from_epoch(warmup_end),
            "round_end_utc": utc_iso_from_epoch(data_end),
            "locust_exit_code": locust_result.returncode,
        }
        for metric_name, promql in metrics.items():
            instant_value = query_mcp_instant(mcp_client, promql)
            # For ready_row, just store the aggregated value (or None)
            ready_row[metric_name] = instant_value
        rows.append(ready_row)

        if args.fail_on_locust_error and locust_result.returncode != 0:
            raise RuntimeError(
                "Locust failed in round "
                f"{round_number} with exit code {locust_result.returncode}\n"
                f"stdout:\n{locust_result.stdout}\n"
                f"stderr:\n{locust_result.stderr}"
            )

    out_path = Path(args.output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build dynamic headers: base columns + per-metric-per-service columns
    base_headers = [
        "timestamp_utc",
        "round",
        "load_state",
        "round_start_utc",
        "warmup_start_utc",
        "ready_utc",
        "round_end_utc",
        "locust_exit_code",
    ]
    
    # Add aggregate columns (for ready_row compatibility)
    aggregate_headers = metric_names
    
    # Add per-service columns
    service_headers = [
        f"{metric_name}_{service}"
        for metric_name in metric_names
        for service in SERVICES_TO_CAPTURE
    ]
    
    headers = base_headers + aggregate_headers + service_headers

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run multi-round locust experiments and export MCP-backed metrics to CSV"
    )
    parser.add_argument("--mcp-sse-url", default="http://127.0.0.1:18080/sse", help="MCP SSE endpoint URL")
    parser.add_argument("--mcp-timeout-seconds", type=int, default=10, help="MCP connection/tool timeout")
    parser.add_argument("--output-csv", default="experiment/data/experiment.csv", help="Output CSV path")

    parser.add_argument("--rounds", type=int, default=3, help="Number of rounds")
    parser.add_argument("--disabled-seconds", type=int, default=0, help="Idle time before warmup")
    parser.add_argument("--warmup-seconds", type=int, default=60, help="Warmup duration")
    parser.add_argument("--run-seconds", type=int, default=300, help="Experiment duration after warmup")
    parser.add_argument("--sample-interval-seconds", type=int, default=15, help="Prometheus range step")

    parser.add_argument(
        "--locust-cmd",
        default="kubectl -n loadtest exec deploy/locust-web -- locust",
        help="Comando para executar o Locust via Kubernetes (default: deploy/locust-web)",
    )
    parser.add_argument("--locust-file", default="deploy/kubernetes/manifests-loadtest/locust.py", help="Path to locustfile.py")
    parser.add_argument("--locust-host", default="http://front-end", help="Target host for load")
    parser.add_argument("--users", type=int, default=20, help="Concurrent users")
    parser.add_argument("--spawn-rate", type=float, default=5.0, help="User growth rate")
    parser.add_argument("--locust-web-host", default="0.0.0.0", help="Locust Web UI host")
    parser.add_argument("--locust-web-port", type=int, default=8089, help="Locust Web UI port")
    parser.add_argument(
        "--locust-autoquit-seconds",
        type=int,
        default=1,
        help="Seconds to wait before Locust exits after run-time in Web UI mode",
    )
    parser.add_argument("--locust-extra-args", default="", help="Extra locust CLI args")
    parser.add_argument("--locust-grace-seconds", type=int, default=30, help="Extra timeout buffer")
    parser.add_argument(
        "--fail-on-locust-error",
        action="store_true",
        help="Stop script if locust exits with non-zero code",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    output = run(args)
    print(f"CSV gerado em: {output}")


if __name__ == "__main__":
    main()
