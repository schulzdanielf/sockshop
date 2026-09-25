#!/usr/bin/env python3
"""Backfill de métricas Prometheus/Istio para o dataset GAT.

Reconsulta o Prometheus-v3 para janelas históricas ainda retidas no TSDB,
reconstrói os grafos GAT com as queries Istio atuais e atualiza
``out/dataset.jsonl``. Runs sem dados no Prometheus são preservados e
registrados no relatório.

Uso
---
    python -m experiment.eval.gat.backfill_prometheus_metrics --dry-run
    python -m experiment.eval.gat.backfill_prometheus_metrics --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.analysis.graph_builder import build_gat_graph  # noqa: E402

SCHEMA_VERSION = "1.0"
DEFAULT_PROM_URL = "http://127.0.0.1:9091"
DEFAULT_NAMESPACE = "sock-shop"
DEFAULT_DATASET = HERE / "out" / "dataset.jsonl"
DEFAULT_REPORT = HERE / "out" / "prometheus_backfill_report.json"

NODE_QUERIES = {
    "traffic": (
        'sum(rate(istio_requests_total{destination_workload_namespace="{namespace}",'
        'destination_workload!="unknown"}[5m])) by (destination_workload)'
    ),
    "error_rate": (
        '100 * ('
        'sum(rate(istio_requests_total{destination_workload_namespace="{namespace}",'
        'destination_workload!="unknown",response_code=~"5.."}[5m])) by (destination_workload)'
        ' or on(destination_workload) 0 * '
        'sum(rate(istio_requests_total{destination_workload_namespace="{namespace}",'
        'destination_workload!="unknown"}[5m])) by (destination_workload)'
        ') / clamp_min(sum(rate(istio_requests_total{destination_workload_namespace="{namespace}",'
        'destination_workload!="unknown"}[5m])) by (destination_workload), 0.0001)'
    ),
    "latency_p95": (
        'histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{'
        'destination_workload_namespace="{namespace}",destination_workload!="unknown"}'
        '[5m])) by (le,destination_workload))'
    ),
}

EDGE_QUERIES = {
    metric: (
        'sum by (source_workload, destination_workload, response_code, reporter, '
        'destination_workload_namespace) '
        f'({metric}{{destination_workload_namespace="{{namespace}}"}})'
    )
    for metric in (
        "istio_requests_total",
        "istio_request_duration_milliseconds_sum",
        "istio_request_duration_milliseconds_count",
    )
}


def _parse_time(value: str) -> datetime:
    """Parse ISO/RFC3339 timestamps and normalise to UTC-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_dataset(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_dataset(path: Path, graphs: Iterable[Dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(g, ensure_ascii=False) for g in graphs) + "\n",
        encoding="utf-8",
    )


def _prom_query_range(
    prom_url: str,
    query: str,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> Dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "start": f"{start.timestamp():.3f}",
            "end": f"{end.timestamp():.3f}",
            "step": str(step_seconds),
        }
    )
    url = f"{prom_url.rstrip('/')}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(str(payload.get("error") or payload))
    return payload


def _prom_status(prom_url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for endpoint in ("runtimeinfo", "tsdb"):
        url = f"{prom_url.rstrip('/')}/api/v1/status/{endpoint}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            out[endpoint] = payload.get("data") if payload.get("status") == "success" else payload
        except Exception as exc:  # pragma: no cover - diagnostic best effort
            out[endpoint] = {"error": str(exc)}
    return out


def _collect_metrics(
    prom_url: str,
    namespace: str,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    metrics_raw = {"window_start": _iso_utc(start), "window_end": _iso_utc(end), "data": {}}
    for metric_id, template in NODE_QUERIES.items():
        metrics_raw["data"][metric_id] = _prom_query_range(
            prom_url,
            template.replace("{namespace}", namespace),
            start,
            end,
            step_seconds,
        )

    edge_metrics: Dict[str, Any] = {}
    for metric_id, template in EDGE_QUERIES.items():
        edge_metrics[metric_id] = _prom_query_range(
            prom_url,
            template.replace("{namespace}", namespace),
            start,
            end,
            step_seconds,
        )
    return metrics_raw, edge_metrics


def _series_services(payload: Dict[str, Any]) -> List[str]:
    services = set()
    result = ((payload.get("data") or {}).get("result") or [])
    for series in result:
        labels = series.get("metric") or {}
        svc = labels.get("destination_workload") or labels.get("name")
        if svc and svc != "unknown":
            services.add(str(svc))
    return sorted(services)


def _result_count(payload: Dict[str, Any]) -> int:
    return len(((payload.get("data") or {}).get("result") or []))


def _merge_preserved_metadata(new_graph: Dict[str, Any], old_graph: Dict[str, Any]) -> Dict[str, Any]:
    for key in (
        "chaos_type_raw",
        "chaos_type_normalized",
        "fault_window_meta",
        "splits",
    ):
        if key in old_graph:
            new_graph[key] = old_graph[key]
    return new_graph


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{path.name}.pre-prometheus-backfill-{stamp}"


def backfill(
    *,
    dataset_path: Path,
    report_path: Path,
    prom_url: str,
    namespace: str,
    step_seconds: int,
    apply: bool,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    graphs = _load_dataset(dataset_path)
    updated_graphs: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    counts: Counter[str] = Counter()
    service_coverage: Counter[str] = Counter()

    for graph in graphs[: limit or None]:
        run_id = str(graph.get("run_id"))
        try:
            baseline = graph["windows"]["baseline"]
            fault = graph["windows"]["fault"]
            start = _parse_time(baseline["start"])
            end = _parse_time(fault["end"])
            baseline_window = (baseline["start"], baseline["end"])
            fault_window = (fault["start"], fault["end"])

            metrics_raw, edge_metrics = _collect_metrics(
                prom_url, namespace, start, end, step_seconds
            )
            traffic_services = _series_services(metrics_raw["data"]["traffic"])
            latency_services = _series_services(metrics_raw["data"]["latency_p95"])
            error_services = _series_services(metrics_raw["data"]["error_rate"])
            if not traffic_services or not latency_services or not error_services:
                counts["empty_node_metrics"] += 1
                updated_graphs.append(graph)
                rows.append(
                    {
                        "run_id": run_id,
                        "status": "skipped",
                        "reason": "empty_node_metrics",
                        "traffic_services": traffic_services,
                        "latency_services": latency_services,
                        "error_services": error_services,
                    }
                )
                continue

            label = graph.get("label") or {}
            rebuilt = build_gat_graph(
                run_id=run_id,
                metrics_raw=metrics_raw,
                baseline_window=baseline_window,
                fault_window=fault_window,
                ground_truth_service=label.get("root_service"),
                ground_truth_fault_category=label.get("fault_category"),
                is_training=bool(graph.get("is_training", True)),
                edge_metrics=edge_metrics,
            )
            rebuilt = _merge_preserved_metadata(rebuilt, graph)
            updated_graphs.append(rebuilt)
            counts["updated"] += 1
            for svc in traffic_services:
                service_coverage[svc] += 1
            rows.append(
                {
                    "run_id": run_id,
                    "status": "updated",
                    "window_start_utc": _iso_utc(start),
                    "window_end_utc": _iso_utc(end),
                    "traffic_services": traffic_services,
                    "latency_services": latency_services,
                    "error_services": error_services,
                    "edge_source": rebuilt.get("edge_source"),
                    "edge_series": {k: _result_count(v) for k, v in edge_metrics.items()},
                }
            )
        except Exception as exc:
            counts["query_or_rebuild_failed"] += 1
            updated_graphs.append(graph)
            rows.append({"run_id": run_id, "status": "skipped", "reason": str(exc)[:500]})

    if limit:
        updated_graphs.extend(graphs[limit:])

    counts["preserved"] = len(graphs) - counts["updated"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "dataset_path": str(dataset_path),
        "apply": apply,
        "prom_url": prom_url,
        "namespace": namespace,
        "step_seconds": step_seconds,
        "timezone_check": {
            "platform_timestamps": "ISO-8601 with UTC offset (+00:00/Z)",
            "prometheus_query_range": "Unix epoch seconds in UTC",
            "adjustment_applied": False,
        },
        "prometheus_status": _prom_status(prom_url),
        "summary": dict(counts),
        "service_coverage_updated_runs": dict(sorted(service_coverage.items())),
        "runs": rows,
    }

    if apply:
        backup = _backup_path(dataset_path)
        shutil.copy2(dataset_path, backup)
        _write_dataset(dataset_path, updated_graphs)
        report["backup_path"] = str(backup)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Prometheus/Istio metrics into GAT dataset.jsonl")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--prom-url", default=DEFAULT_PROM_URL)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--step-seconds", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None, help="debug: process only first N graphs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = backfill(
        dataset_path=Path(args.dataset),
        report_path=Path(args.report),
        prom_url=args.prom_url,
        namespace=args.namespace,
        step_seconds=args.step_seconds,
        apply=bool(args.apply),
        limit=args.limit,
    )
    summary = report["summary"]
    print("Prometheus/Istio backfill")
    print(f"  dataset: {report['dataset_path']}")
    print(f"  apply: {report['apply']}")
    print(f"  updated: {summary.get('updated', 0)}")
    print(f"  preserved: {summary.get('preserved', 0)}")
    if "empty_node_metrics" in summary:
        print(f"  empty_node_metrics: {summary['empty_node_metrics']}")
    if "query_or_rebuild_failed" in summary:
        print(f"  query_or_rebuild_failed: {summary['query_or_rebuild_failed']}")
    print(f"  report: {args.report}")
    if report.get("backup_path"):
        print(f"  backup: {report['backup_path']}")


if __name__ == "__main__":
    main()