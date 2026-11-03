"""Prometheus edge-latency queries and phase-aware summaries for RCA."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

EDGE_LATENCY_QUERY_SPECS = (
    {
        "metric_name": "edge_latency_istio_mean_ms",
        "unit": "ms",
        "source_label": "source_workload",
        "target_label": "destination_workload",
        "metric_family": "istio",
    },
    {
        "metric_name": "edge_latency_service_graph_client_p90_s",
        "unit": "s",
        "source_label": "client",
        "target_label": "server",
        "metric_family": "service_graph_client",
    },
    {
        "metric_name": "edge_latency_service_graph_server_p90_s",
        "unit": "s",
        "source_label": "client",
        "target_label": "server",
        "metric_family": "service_graph_server",
    },
)


def _prometheus_regex(values: list[str]) -> str:
    return (
        "^("
        + "|".join(
            re.escape(value).replace(r"\-", "-") for value in sorted(set(values))
        )
        + ")$"
    )


def build_edge_latency_queries(
    namespace: str,
    known_services: list[str],
) -> list[dict[str, str]]:
    """Build fixed-window PromQL for source- and server-side edge latency."""
    namespace_selector = json.dumps(namespace)
    service_selector = json.dumps(_prometheus_regex(known_services))
    istio_labels = (
        f"destination_workload_namespace={namespace_selector},"
        f"destination_workload=~{service_selector},"
        'source_workload!="unknown",reporter="source"'
    )
    istio_sum = (
        "sum by (source_workload, destination_workload) "
        f"(rate(istio_request_duration_milliseconds_sum{{{istio_labels}}}[1m]))"
    )
    istio_count = (
        "sum by (source_workload, destination_workload) "
        f"(rate(istio_request_duration_milliseconds_count{{{istio_labels}}}[1m]))"
    )
    service_graph_labels = f"server=~{service_selector}"
    service_graph_client = (
        "histogram_quantile(0.90, "
        "sum by (le, client, server) "
        "(rate(traces_service_graph_request_client_seconds_bucket{"
        f"{service_graph_labels}}}[1m])))"
    )
    service_graph_server = (
        "histogram_quantile(0.90, "
        "sum by (le, client, server) "
        "(rate(traces_service_graph_request_server_seconds_bucket{"
        f"{service_graph_labels}}}[1m])))"
    )
    queries = {
        "edge_latency_istio_mean_ms": f"{istio_sum} / {istio_count}",
        "edge_latency_service_graph_client_p90_s": service_graph_client,
        "edge_latency_service_graph_server_p90_s": service_graph_server,
    }
    return [
        {**spec, "query": queries[spec["metric_name"]]}
        for spec in EDGE_LATENCY_QUERY_SPECS
    ]


def _empty_phase() -> dict[str, Any]:
    return {
        "status": "no_data",
        "coverage": {"sample_count": 0, "series_count": 0, "edge_count": 0},
    }


def _empty_metric(spec: dict[str, str], status: str) -> dict[str, Any]:
    return {
        "status": status,
        "unit": spec["unit"],
        "phases": {
            phase: {
                "status": "no_data",
                "sample_count": 0,
                "series_count": 0,
            }
            for phase in ("baseline", "fault", "post")
        },
        "top_delta": [],
    }


def normalize_edge_latency_result(
    raw: Any,
    *,
    spec: dict[str, str],
    phases: dict[str, dict[str, str]],
    known_services: list[str],
) -> dict[str, Any]:
    """Summarize edge latency by phase without comparing unrelated edges."""
    empty_phases = {
        phase: _empty_phase()
        for phase in ("baseline", "fault", "post")
        if phase in phases
    }
    metric_name = spec["metric_name"]
    if not isinstance(raw, dict) or raw.get("status") != "success":
        error = raw.get("error") if isinstance(raw, dict) else None
        return {
            "purpose": "edge_latency_triage",
            "status": "tool_error",
            "error": str(error or "Prometheus edge-latency query failed"),
            "phases": empty_phases,
            "metrics": {metric_name: _empty_metric(spec, "tool_error")},
        }

    data = raw.get("data")
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {
            "purpose": "edge_latency_triage",
            "status": "tool_error",
            "error": "Prometheus edge-latency response has no matrix result",
            "phases": empty_phases,
            "metrics": {metric_name: _empty_metric(spec, "tool_error")},
        }

    phase_bounds: list[tuple[str, float, float]] = []
    try:
        for phase in ("baseline", "fault", "post"):
            window = phases.get(phase)
            if not window:
                continue
            start = datetime.fromisoformat(
                window["start"].replace("Z", "+00:00")
            ).timestamp()
            end = datetime.fromisoformat(
                window["end"].replace("Z", "+00:00")
            ).timestamp()
            if start >= end:
                raise ValueError(f"Invalid {phase} time window")
            phase_bounds.append((phase, start, end))
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "purpose": "edge_latency_triage",
            "status": "tool_error",
            "error": f"Invalid edge-latency phase window: {exc}",
            "phases": empty_phases,
            "metrics": {metric_name: _empty_metric(spec, "tool_error")},
        }

    edge_values: dict[str, dict[tuple[str, str], list[float]]] = {
        phase: {} for phase, _, _ in phase_bounds
    }
    phase_series: dict[str, set[tuple[str, str]]] = {
        phase: set() for phase, _, _ in phase_bounds
    }
    malformed_count = 0
    omitted_service_count = 0
    known = set(known_services)
    source_label = spec["source_label"]
    target_label = spec["target_label"]

    for row in rows:
        if not isinstance(row, dict):
            malformed_count += 1
            continue
        labels = row.get("metric")
        if not isinstance(labels, dict):
            malformed_count += 1
            continue
        source = labels.get(source_label)
        target = labels.get(target_label)
        if not isinstance(target, str) or target not in known:
            omitted_service_count += 1
            continue
        if not isinstance(source, str) or not source:
            source = "unknown"
        points = row.get("values")
        if not isinstance(points, list):
            malformed_count += 1
            continue
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                malformed_count += 1
                continue
            try:
                timestamp = float(point[0])
                value = float(point[1])
            except (TypeError, ValueError):
                malformed_count += 1
                continue
            if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
                malformed_count += 1
                continue
            phase_name = next(
                (
                    name
                    for name, start, end in phase_bounds
                    if start <= timestamp <= end
                ),
                None,
            )
            if phase_name is None:
                continue
            edge = (source, target)
            edge_values[phase_name].setdefault(edge, []).append(value)
            phase_series[phase_name].add(edge)

    phase_summaries: dict[str, dict[str, Any]] = {}
    normalized_phases: dict[str, dict[str, Any]] = {}
    for phase, _, _ in phase_bounds:
        sample_count = sum(len(values) for values in edge_values[phase].values())
        series_count = len(phase_series[phase])
        status = "ok" if sample_count else "no_data"
        coverage = {
            "sample_count": sample_count,
            "series_count": series_count,
            "edge_count": series_count,
        }
        phase_summaries[phase] = {
            "status": status,
            "sample_count": sample_count,
            "series_count": series_count,
        }
        normalized_phases[phase] = {"status": status, "coverage": coverage}

    comparable_edges: dict[str, list[dict[str, Any]]] = {}
    baseline_values = edge_values.get("baseline", {})
    fault_values = edge_values.get("fault", {})
    for edge in set(baseline_values) | set(fault_values):
        source, target = edge
        before = baseline_values.get(edge, [])
        during = fault_values.get(edge, [])
        if not before or not during:
            continue
        baseline_mean = sum(before) / len(before)
        fault_mean = sum(during) / len(during)
        delta = fault_mean - baseline_mean
        row = {
            "service": target,
            source_label: source,
            target_label: target,
            "baseline": round(baseline_mean, 6),
            "fault": round(fault_mean, 6),
            "delta": round(delta, 6),
            "delta_pct": (
                round(100 * delta / abs(baseline_mean), 2) if baseline_mean else None
            ),
            "direction": (
                "increase" if delta > 0 else "decrease" if delta < 0 else "unchanged"
            ),
            "baseline_sample_count": len(before),
            "fault_sample_count": len(during),
        }
        comparable_edges.setdefault(target, []).append(row)

    top_delta = []
    for edge_rows in comparable_edges.values():
        top_delta.append(
            max(
                edge_rows,
                key=lambda row: (
                    row["delta"] > 0,
                    row["delta"] if row["delta"] > 0 else abs(row["delta"]),
                ),
            )
        )
    top_delta.sort(key=lambda row: (-abs(row["delta"]), row["service"]))

    has_phase_data = bool(
        phase_summaries.get("baseline", {}).get("sample_count")
        and phase_summaries.get("fault", {}).get("sample_count")
    )
    has_comparable_edges = bool(top_delta)
    if has_phase_data and has_comparable_edges and not malformed_count:
        metric_status = "ok"
    elif (
        any(item["sample_count"] for item in phase_summaries.values())
        or malformed_count
    ):
        metric_status = "partial"
    else:
        metric_status = "no_data"

    limitations = [
        "Amostras representam pontos de avaliacao Prometheus, nao contagens de requisicoes; rate([1m]) pode incluir observacoes anteriores ao inicio da fase."
    ]
    if not has_comparable_edges:
        limitations.append("Sem aresta com amostras comparaveis em baseline e fault.")
    if malformed_count:
        limitations.append(
            f"{malformed_count} series/amostras invalidas foram descartadas."
        )
    if omitted_service_count:
        limitations.append(
            f"{omitted_service_count} series cujo destino nao pertence aos servicos conhecidos foram omitidas."
        )

    return {
        "purpose": "edge_latency_triage",
        "status": (
            "ok"
            if metric_status == "ok"
            else "partial" if metric_status == "partial" else "no_data"
        ),
        "phases": normalized_phases,
        "metrics": {
            metric_name: {
                "status": metric_status,
                "unit": spec["unit"],
                "phases": phase_summaries,
                "top_delta": top_delta,
                "limitations": limitations,
            }
        },
        "limitations": limitations,
    }
