"""Read-back validation for telemetry streamed to historical observability."""

from __future__ import annotations

from typing import Any


_REQUIRED_TOOLS = {
    "get_run_golden_signals",
    "loki_range_query",
    "tempo_search_traces",
}


def _payload_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "tool returned a non-object response"
    error = payload.get("error")
    if isinstance(error, str) and error:
        return error[:300]
    if payload.get("status") in {"error", "failed"}:
        return f"tool returned status {payload['status']}"
    return None


def _sample_count(payload: Any, phase_name: str) -> int:
    if not isinstance(payload, dict):
        return 0
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return 0
    total = 0
    for metric in metrics.values():
        if not isinstance(metric, dict):
            continue
        phases = metric.get("phases")
        phase = phases.get(phase_name) if isinstance(phases, dict) else None
        if not isinstance(phase, dict):
            continue
        count = phase.get("sample_count")
        if not isinstance(count, int) or isinstance(count, bool):
            coverage = phase.get("coverage")
            count = coverage.get("sample_count") if isinstance(coverage, dict) else 0
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            total += count
    return total


def _log_entry_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    streams = data.get("result") if isinstance(data, dict) else None
    if isinstance(streams, list):
        return sum(
            len(stream.get("values") or [])
            for stream in streams
            if isinstance(stream, dict)
        )
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        count = coverage.get("lines_returned")
        return count if isinstance(count, int) and not isinstance(count, bool) else 0
    return 0


def validate_history_stream(
    client: Any,
    *,
    phases: dict[str, dict[str, str]],
    namespace: str,
    service: str,
) -> dict[str, Any]:
    """Confirm historical read-back for metrics, logs, and traces in a run window."""
    baseline = phases.get("baseline")
    fault = phases.get("fault")
    if not isinstance(baseline, dict) or not isinstance(fault, dict):
        raise ValueError("dual-write validation requires baseline and fault windows")
    baseline_start = baseline.get("start")
    baseline_end = baseline.get("end")
    fault_start = fault.get("start")
    fault_end = fault.get("end")
    if not all((baseline_start, baseline_end, fault_start, fault_end)):
        raise ValueError("dual-write validation received incomplete phase bounds")

    tools = {
        tool.get("name")
        for tool in client.list_tools()
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    missing_tools = sorted(_REQUIRED_TOOLS - tools)
    if missing_tools:
        return {
            "status": "failed",
            "error": f"historical MCP is missing required tools: {missing_tools}",
            "telemetry": {},
        }

    window_start = baseline_start
    window_end = (phases.get("post") or {}).get("end") or fault_end
    metric_arguments = {
        "baseline_start": baseline_start,
        "baseline_end": baseline_end,
        "fault_start": fault_start,
        "fault_end": fault_end,
        "namespace": namespace,
        "step": "15s",
        "k": 10,
    }
    target_log_arguments = {
        "query": f'{{namespace="{namespace}",container="{service}"}}',
        "start": window_start,
        "end": window_end,
        "limit": 20,
    }
    trace_arguments = {
        "service_name": service,
        "start": window_start,
        "end": window_end,
        "limit": 1,
    }
    metrics = client.call_tool_json(
        "get_run_golden_signals",
        metric_arguments,
        timeout_seconds=30,
    )
    logs = client.call_tool_json(
        "loki_range_query",
        target_log_arguments,
        timeout_seconds=30,
    )
    traces = client.call_tool_json(
        "tempo_search_traces",
        trace_arguments,
        timeout_seconds=30,
    )

    baseline_samples = _sample_count(metrics, "baseline")
    fault_samples = _sample_count(metrics, "fault")
    target_log_entries = _log_entry_count(logs)
    log_entries = target_log_entries
    log_scope = "target_container"
    log_error = _payload_error(logs)
    log_queries = [target_log_arguments]
    if target_log_entries == 0 and log_error is None:
        namespace_log_arguments = {
            "query": f'{{namespace="{namespace}"}}',
            "start": window_start,
            "end": window_end,
            "limit": 20,
        }
        logs = client.call_tool_json(
            "loki_range_query",
            namespace_log_arguments,
            timeout_seconds=30,
        )
        log_entries = _log_entry_count(logs)
        log_error = _payload_error(logs)
        log_scope = "namespace"
        log_queries.append(namespace_log_arguments)
    trace_rows = traces.get("traces") if isinstance(traces, dict) else None
    trace_count = len(trace_rows) if isinstance(trace_rows, list) else 0
    metric_error = _payload_error(metrics)
    trace_error = _payload_error(traces)
    telemetry = {
        "metrics": {
            "status": (
                "complete"
                if baseline_samples and fault_samples and metric_error is None
                else "pending"
            ),
            "baseline_samples": baseline_samples,
            "fault_samples": fault_samples,
            "error": metric_error,
        },
        "logs": {
            "status": "complete" if log_entries and log_error is None else "pending",
            "entries_returned": log_entries,
            "target_entries_returned": target_log_entries,
            "validation_scope": log_scope,
            "error": log_error,
        },
        "traces": {
            "status": "complete" if trace_count and trace_error is None else "pending",
            "traces_returned": trace_count,
            "error": trace_error,
        },
    }
    complete = all(item["status"] == "complete" for item in telemetry.values())
    return {
        "status": "complete" if complete else "pending",
        "mode": "dual_write",
        "source": "historical_mcp",
        "window": {"start": window_start, "end": window_end},
        "namespace": namespace,
        "service": service,
        "telemetry": telemetry,
        "source_counts": {
            "metrics": baseline_samples + fault_samples,
            "logs": log_entries,
            "traces": trace_count,
        },
        "queries": [
            {"tool": "get_run_golden_signals", "arguments": metric_arguments},
            *(
                {"tool": "loki_range_query", "arguments": arguments}
                for arguments in log_queries
            ),
            {"tool": "tempo_search_traces", "arguments": trace_arguments},
        ],
    }
