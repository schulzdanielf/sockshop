"""Small, auditable MCP investigations scoped to one observation window."""

from __future__ import annotations

from typing import Any, Dict


def _summarize_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"series_count": 0, "sample_count": 0, "error": "invalid_payload"}

    error = payload.get("error")
    data = payload.get("data", {})
    result = data.get("result", []) if isinstance(data, dict) else []
    if not isinstance(result, list):
        result = []

    sample_count = 0
    series = []
    for item in result:
        if not isinstance(item, dict):
            continue
        values = item.get("values", [])
        if not isinstance(values, list):
            values = []
        sample_count += len(values)
        labels = item.get("metric", {})
        series.append(
            {
                "labels": labels if isinstance(labels, dict) else {},
                "sample_count": len(values),
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
            }
        )

    return {
        "series_count": len(result),
        "sample_count": sample_count,
        "series": series,
        "error": error,
    }


def investigate_metric_window(
    client: Any,
    *,
    run_id: str,
    window_start: str,
    window_end: str,
    query: str,
    step_seconds: int,
) -> Dict[str, Any]:
    """Run one explicit-window MCP query and return evidence plus audit data."""
    if not window_start or not window_end or window_end < window_start:
        raise ValueError("invalid observation window")
    if not query:
        raise ValueError("query is required")
    if step_seconds <= 0:
        raise ValueError("step_seconds must be positive")

    arguments = {
        "query": query,
        "start": window_start,
        "end": window_end,
        "step": f"{step_seconds}s",
    }
    payload = client.call_tool_json("prometheus_range_query", arguments)
    return {
        "schema_version": "1",
        "run_id": run_id,
        "tool": "prometheus_range_query",
        "arguments": arguments,
        "window": {"start": window_start, "end": window_end},
        "evidence": _summarize_payload(payload),
    }
