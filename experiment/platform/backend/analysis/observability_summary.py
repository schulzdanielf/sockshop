"""Deterministic, model-facing summary of one observability window."""

from __future__ import annotations

from typing import Any, Dict


def _metric_coverage(metrics_raw: Dict[str, Any]) -> Dict[str, Any]:
    data = metrics_raw.get("data", {})
    if not isinstance(data, dict):
        return {"query_count": 0, "empty_queries": []}

    empty_queries = []
    for metric_id, payload in data.items():
        result = payload.get("data", {}).get("result", []) if isinstance(payload, dict) else []
        if not isinstance(result, list) or not result:
            empty_queries.append(str(metric_id))

    return {
        "query_count": len(data),
        "empty_queries": empty_queries,
        "window_start": metrics_raw.get("window_start"),
        "window_end": metrics_raw.get("window_end"),
    }


def build_observability_summary(
    *,
    run_id: str,
    experiment_id: str,
    observation_window: Dict[str, Any],
    metrics_raw: Dict[str, Any],
    metrics_summary: Dict[str, Any],
    traces_raw: Dict[str, Any],
    traces_summary: Dict[str, Any],
    features: Dict[str, Any],
    artifact_paths: Dict[str, str],
) -> Dict[str, Any]:
    """Build a deterministic context without adding an RCA conclusion."""
    trace_collection = traces_raw.get("collection", {})
    if not isinstance(trace_collection, dict):
        trace_collection = {}

    return {
        "schema_version": "1",
        "run_id": run_id,
        "experiment_id": experiment_id,
        "observation_window": observation_window,
        "sources": {
            "metrics": {
                "provider": "prometheus-via-mcp",
                "coverage": _metric_coverage(metrics_raw),
                "summary": metrics_summary.get("metrics", {}),
                "phase_metrics": features.get("phase_metrics", {}),
                "metric_hotspots": features.get("metric_hotspots", {}),
            },
            "traces": {
                "provider": "tempo-via-mcp",
                "window_start": traces_raw.get("window_start"),
                "window_end": traces_raw.get("window_end"),
                "summary": traces_summary,
                "collection": trace_collection,
                "affected_services": features.get("affected_services", []),
                "top_failure_signatures": features.get(
                    "top_failure_signatures", []
                ),
            },
        },
        "observed": {
            "verdict_signal": features.get("verdict"),
            "slo_violations": features.get("slo_violations", []),
            "recovery_time_seconds": features.get("recovery_time_seconds"),
        },
        "gaps": {
            "empty_metric_queries": _metric_coverage(metrics_raw)["empty_queries"],
            "trace_empty_reason": trace_collection.get("empty_reason"),
            "trace_search_error": trace_collection.get("search_error"),
        },
        "artifacts": artifact_paths,
    }
