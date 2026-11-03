"""Window-scoped service graph extraction from Tempo traces.

This module builds a compact, model-oriented representation of service-to-service
behaviour for a bounded time window. It is designed for chaos/RCA experiments:
rather than sending raw graph frames to an LLM, it returns only the deltas and
ranked evidence needed to reason about where a fault propagated.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _percentile(values: List[float], q: float) -> Optional[float]:
    """Return percentile q in [0,1] from a non-empty numeric list."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    idx = int(round(q * (len(ordered) - 1)))
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def _mean(values: List[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(float(value), 3) if isinstance(value, (int, float)) else None


def _edge_key(
    source: Optional[str], target: Optional[str]
) -> Optional[Tuple[str, str]]:
    if not source or not target or source == target:
        return None
    if source.isdigit() or target.isdigit():
        return None
    if source == "unknown" or target == "unknown":
        return None
    return source, target


def _service_label_from_endpoint(endpoint: Optional[str]) -> Optional[str]:
    """Best-effort endpoint → service label normalisation.

    OTel endpoints can be service names, hostnames, IPs with ports, URLs, or
    route-like labels such as ``orders/orders``. The Sock Shop traces normally
    expose Kubernetes service names, which are the labels we want to compare
    against metrics/validators.
    """
    if not endpoint:
        return None
    value = str(endpoint).strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    value = value.split(":", 1)[0]
    # Common K8s FQDN suffixes: service.namespace.svc.cluster.local
    if ".svc.cluster.local" in value:
        value = value.split(".svc.cluster.local", 1)[0]
    if "." in value:
        value = value.split(".", 1)[0]
    return value or None


def _parse_span_attributes(span: Dict[str, Any]) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for attr in span.get("attributes", []) or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        value = attr.get("value")
        if isinstance(value, dict):
            for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if kind in value:
                    attrs[str(key)] = str(value[kind])
                    break
    return attrs


def _span_is_error(span: Dict[str, Any], attrs: Dict[str, str]) -> bool:
    status = span.get("status") or {}
    code = status.get("code")
    if code in (2, "2", "STATUS_CODE_ERROR", "error"):
        return True
    http_status = attrs.get("http.status_code") or attrs.get(
        "http.response.status_code"
    )
    if http_status:
        try:
            return int(http_status) >= 400
        except (TypeError, ValueError):
            return False
    return False


def _span_duration_ms(span: Dict[str, Any]) -> float:
    try:
        start = int(span.get("startTimeUnixNano", 0))
        end = int(span.get("endTimeUnixNano", 0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (end - start) / 1_000_000)


def _extract_trace_spans(trace_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    spans: List[Dict[str, Any]] = []
    for batch in trace_data.get("batches", []) or []:
        if not isinstance(batch, dict):
            continue
        service = "unknown"
        resource_attrs = batch.get("resource", {}).get("attributes", []) or []
        for attr in resource_attrs:
            if attr.get("key") == "service.name":
                value = attr.get("value", {})
                service = str(
                    value.get("stringValue")
                    or value.get("intValue")
                    or value.get("doubleValue")
                    or service
                )
                break
        for scope in batch.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                if not isinstance(span, dict):
                    continue
                attrs = _parse_span_attributes(span)
                spans.append(
                    {
                        "span_id": span.get("spanId"),
                        "parent_span_id": span.get("parentSpanId"),
                        "service": service,
                        "name": span.get("name") or "unknown",
                        "start_ns": int(span.get("startTimeUnixNano", 0) or 0),
                        "duration_ms": _span_duration_ms(span),
                        "is_error": _span_is_error(span, attrs),
                        "attrs": attrs,
                    }
                )
    return spans


def _infer_edge_targets(span: Dict[str, Any]) -> List[str]:
    """Infer possible target services from span attributes/name."""
    attrs = span.get("attrs") or {}
    candidates = [
        attrs.get("peer.service"),
        attrs.get("rpc.service"),
        attrs.get("server.address"),
        attrs.get("net.peer.name"),
        attrs.get("http.host"),
        attrs.get("url.full"),
        attrs.get("http.url"),
    ]
    targets: List[str] = []
    for candidate in candidates:
        service = _service_label_from_endpoint(candidate)
        if service and service != span.get("service") and service not in targets:
            targets.append(service)
    return targets


def summarize_trace_graph(
    analyzed_traces: Iterable[Dict[str, Any]],
    *,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
    phase: str = "window",
    top_k: int = 5,
) -> Dict[str, Any]:
    """Aggregate analyzed traces into a compact service graph summary.

    ``analyzed_traces`` entries should look like the plugin's payload:
    ``{trace_id, root_service, start_time_unix_nano, features: {...}}``.
    The function tolerates missing features and endpoint-level targets.
    """
    node_spans: Dict[str, List[Dict[str, Any]]] = {}
    edge_spans: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    first_error_ns: Dict[str, int] = {}
    first_slow_ns: Dict[str, int] = {}
    trace_count = 0
    error_trace_count = 0

    for trace in analyzed_traces or []:
        if not isinstance(trace, dict):
            continue
        trace_count += 1
        features = trace.get("features") or {}
        if not isinstance(features, dict):
            features = {}
        trace_start_ns = trace.get("start_time_unix_nano")
        try:
            trace_start_ns = int(trace_start_ns) if trace_start_ns is not None else None
        except (TypeError, ValueError):
            trace_start_ns = None

        spans = _extract_trace_spans({"batches": features.get("batches") or []})
        if not spans:
            # Fall back to feature-derived maps when raw batches are not embedded.
            for svc_lat in features.get("service_latency") or []:
                service = svc_lat.get("service")
                if not service:
                    continue
                node_spans.setdefault(service, []).append(
                    {
                        "service": service,
                        "name": "feature_service_latency",
                        "duration_ms": float(svc_lat.get("total_inclusive_ms") or 0.0),
                        "is_error": False,
                        "start_ns": trace_start_ns or 0,
                        "attrs": {},
                    }
                )
            for edge in features.get("dependency_map") or []:
                key = _edge_key(edge.get("from"), edge.get("to"))
                if key:
                    edge_spans.setdefault(key, []).append(
                        {
                            "service": key[0],
                            "name": edge.get("operation") or "dependency",
                            "duration_ms": float(edge.get("duration_ms") or 0.0),
                            "is_error": bool(edge.get("is_error")),
                            "start_ns": trace_start_ns or 0,
                            "attrs": {},
                        }
                    )
        else:
            span_map = {s.get("span_id"): s for s in spans if s.get("span_id")}
            for span in spans:
                node_spans.setdefault(span["service"], []).append(span)
                parent = span_map.get(span.get("parent_span_id"))
                if parent:
                    key = _edge_key(parent.get("service"), span.get("service"))
                    if key:
                        edge_spans.setdefault(key, []).append(span)
                for target in _infer_edge_targets(span):
                    key = _edge_key(span.get("service"), target)
                    if key:
                        edge_spans.setdefault(key, []).append(span)

        trace_has_error = bool(features.get("error_spans")) or any(
            s.get("is_error") for s in spans
        )
        if trace_has_error:
            error_trace_count += 1
        if trace_has_error and trace_start_ns is not None:
            for err in features.get("error_spans") or []:
                service = err.get("service")
                if service:
                    first_error_ns[service] = min(
                        first_error_ns.get(service, trace_start_ns), trace_start_ns
                    )
        duration_ms = features.get("trace_duration_ms")
        trace_duration = duration_ms
        if not isinstance(trace_duration, (int, float)):
            trace_duration = trace.get("duration_ms")
        if not isinstance(trace_duration, (int, float)) and spans:
            trace_duration = max(float(s.get("duration_ms") or 0.0) for s in spans)
        if isinstance(trace_duration, (int, float)) and trace_duration >= 1000:
            root = trace.get("root_service")
            if root and trace_start_ns is not None:
                first_slow_ns[root] = min(
                    first_slow_ns.get(root, trace_start_ns), trace_start_ns
                )

    nodes: List[Dict[str, Any]] = []
    for service, spans in node_spans.items():
        durations = [float(s.get("duration_ms") or 0.0) for s in spans]
        errors = sum(1 for s in spans if s.get("is_error"))
        nodes.append(
            {
                "service": service,
                "span_count": len(spans),
                "error_span_count": errors,
                "error_rate": round(errors / len(spans), 4) if spans else 0.0,
                "latency_mean_ms": _mean(durations),
                "latency_p50_ms": _round_or_none(_percentile(durations, 0.50)),
                "latency_p95_ms": _round_or_none(_percentile(durations, 0.95)),
                "latency_max_ms": _round_or_none(max(durations) if durations else None),
            }
        )
    nodes.sort(
        key=lambda item: (
            item["error_span_count"],
            item["latency_p95_ms"] or 0.0,
            item["span_count"],
        ),
        reverse=True,
    )

    edges: List[Dict[str, Any]] = []
    for (source, target), spans in edge_spans.items():
        durations = [float(s.get("duration_ms") or 0.0) for s in spans]
        errors = sum(1 for s in spans if s.get("is_error"))
        edges.append(
            {
                "from": source,
                "to": target,
                "span_count": len(spans),
                "error_span_count": errors,
                "error_rate": round(errors / len(spans), 4) if spans else 0.0,
                "latency_mean_ms": _mean(durations),
                "latency_p50_ms": _round_or_none(_percentile(durations, 0.50)),
                "latency_p95_ms": _round_or_none(_percentile(durations, 0.95)),
                "latency_max_ms": _round_or_none(max(durations) if durations else None),
            }
        )
    edges.sort(
        key=lambda item: (
            item["error_span_count"],
            item["latency_p95_ms"] or 0.0,
            item["span_count"],
        ),
        reverse=True,
    )

    base_ns_values = list(first_error_ns.values()) + list(first_slow_ns.values())
    base_ns = min(base_ns_values) if base_ns_values else None
    cascade = []
    for service, ts in sorted(first_error_ns.items(), key=lambda kv: kv[1]):
        cascade.append(
            {
                "service": service,
                "signal": "error",
                "t_offset_s": round((ts - base_ns) / 1e9, 3) if base_ns else 0.0,
            }
        )
    for service, ts in sorted(first_slow_ns.items(), key=lambda kv: kv[1]):
        if service not in first_error_ns:
            cascade.append(
                {
                    "service": service,
                    "signal": "latency",
                    "t_offset_s": round((ts - base_ns) / 1e9, 3) if base_ns else 0.0,
                }
            )

    suspected_path: List[str] = []
    if edges:
        first_edge = edges[0]
        suspected_path = [first_edge["from"], first_edge["to"]]
        outgoing = {e["from"]: e for e in edges}
        cursor = first_edge["to"]
        guard = 0
        while cursor in outgoing and guard < 6:
            nxt = outgoing[cursor]["to"]
            if nxt in suspected_path:
                break
            suspected_path.append(nxt)
            cursor = nxt
            guard += 1

    return {
        "phase": phase,
        "window": {"start": window_start, "end": window_end},
        "trace_count": trace_count,
        "error_trace_count": error_trace_count,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "error_edge_count": sum(1 for e in edges if e["error_span_count"] > 0),
        "top_nodes": nodes[:top_k],
        "top_edges": edges[:top_k],
        "cascade_order": cascade[:top_k],
        "suspected_propagation_path": suspected_path[:6],
    }


def select_slow_trace_candidates(
    baseline_traces: Iterable[Dict[str, Any]],
    fault_traces: Iterable[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Select error-free fault traces that exceed the same operation's baseline P95."""
    def duration_ms(trace: Dict[str, Any]) -> Optional[float]:
        features = trace.get("features") or {}
        if not isinstance(features, dict):
            return None
        value = features.get("trace_duration_ms")
        if not isinstance(value, (int, float)):
            value = trace.get("duration_ms")
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    baseline_durations: Dict[Tuple[str, str], List[float]] = {}
    for trace in baseline_traces or []:
        if not isinstance(trace, dict):
            continue
        key = (trace.get("root_service"), trace.get("root_trace_name"))
        if not all(isinstance(value, str) and value for value in key):
            continue
        duration = duration_ms(trace)
        if duration is not None:
            baseline_durations.setdefault(key, []).append(duration)

    candidates = []
    for trace in fault_traces or []:
        if not isinstance(trace, dict):
            continue
        features = trace.get("features") or {}
        if not isinstance(features, dict):
            continue
        if features.get("error_spans") or features.get("failure_signatures"):
            continue
        trace_id = trace.get("trace_id")
        root_service = trace.get("root_service")
        root_trace_name = trace.get("root_trace_name")
        if not isinstance(trace_id, str) or not trace_id:
            continue
        key = (root_service, root_trace_name)
        if key not in baseline_durations:
            continue
        duration = duration_ms(trace)
        if duration is None:
            continue
        baseline_p95 = _percentile(baseline_durations[key], 0.95)
        if baseline_p95 is None or duration <= baseline_p95:
            continue

        spans = features.get("hot_spans") or features.get("critical_path") or []
        operations = [
            f"{span['service']}.{span['name']}"
            for span in spans
            if isinstance(span, dict)
            and isinstance(span.get("service"), str)
            and isinstance(span.get("name"), str)
        ][:3]
        candidates.append(
            {
                "trace_id": trace_id,
                "signature": "LATENCY_REGRESSION",
                "affected_service": root_service,
                "endpoint": root_trace_name,
                "operations": operations,
                "start_time_unix_nano": trace.get("start_time_unix_nano"),
                "fault_duration_ms": round(duration, 3),
                "baseline_p95_ms": round(baseline_p95, 3),
                "latency_delta_ms": round(duration - baseline_p95, 3),
                "baseline_sample_count": len(baseline_durations[key]),
            }
        )

    candidates.sort(
        key=lambda item: (
            -item["latency_delta_ms"],
            -item["fault_duration_ms"],
            item["trace_id"],
        )
    )
    return candidates[:max(0, limit)]


def _index_edges(summary: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {
        (e.get("from"), e.get("to")): e
        for e in summary.get("top_edges", [])
        if isinstance(e, dict) and e.get("from") and e.get("to")
    }


def _index_nodes(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        n.get("service"): n
        for n in summary.get("top_nodes", [])
        if isinstance(n, dict) and n.get("service")
    }


def diff_graph_summaries(
    baseline: Dict[str, Any],
    fault: Dict[str, Any],
    *,
    recovery: Optional[Dict[str, Any]] = None,
    top_k: int = 5,
) -> Dict[str, Any]:
    """Compute a compact baseline→fault (and optional fault→post) graph delta."""
    base_edges = _index_edges(baseline)
    fault_edges = _index_edges(fault)
    base_nodes = _index_nodes(baseline)
    fault_nodes = _index_nodes(fault)

    edge_deltas = []
    for key in sorted(set(base_edges) | set(fault_edges)):
        before = base_edges.get(key, {})
        after = fault_edges.get(key, {})
        source, target = key
        edge_deltas.append(
            {
                "from": source,
                "to": target,
                "span_count_delta": int(after.get("span_count", 0))
                - int(before.get("span_count", 0)),
                "error_rate_delta": round(
                    float(after.get("error_rate") or 0.0)
                    - float(before.get("error_rate") or 0.0),
                    4,
                ),
                "latency_p95_ms_delta": round(
                    float(after.get("latency_p95_ms") or 0.0)
                    - float(before.get("latency_p95_ms") or 0.0),
                    3,
                ),
                "latency_mean_ms_delta": round(
                    float(after.get("latency_mean_ms") or 0.0)
                    - float(before.get("latency_mean_ms") or 0.0),
                    3,
                ),
                "appeared_in_fault": key not in base_edges and key in fault_edges,
                "missing_in_fault": key in base_edges and key not in fault_edges,
            }
        )
    edge_deltas.sort(
        key=lambda item: (
            abs(item["error_rate_delta"]),
            abs(item["latency_p95_ms_delta"]),
            abs(item["span_count_delta"]),
        ),
        reverse=True,
    )

    node_deltas = []
    for service in sorted(set(base_nodes) | set(fault_nodes)):
        before = base_nodes.get(service, {})
        after = fault_nodes.get(service, {})
        node_deltas.append(
            {
                "service": service,
                "span_count_delta": int(after.get("span_count", 0))
                - int(before.get("span_count", 0)),
                "error_rate_delta": round(
                    float(after.get("error_rate") or 0.0)
                    - float(before.get("error_rate") or 0.0),
                    4,
                ),
                "latency_p95_ms_delta": round(
                    float(after.get("latency_p95_ms") or 0.0)
                    - float(before.get("latency_p95_ms") or 0.0),
                    3,
                ),
                "appeared_in_fault": service not in base_nodes
                and service in fault_nodes,
                "missing_in_fault": service in base_nodes
                and service not in fault_nodes,
            }
        )
    node_deltas.sort(
        key=lambda item: (
            abs(item["error_rate_delta"]),
            abs(item["latency_p95_ms_delta"]),
            abs(item["span_count_delta"]),
        ),
        reverse=True,
    )

    new_error_edges = [
        e
        for e in edge_deltas
        if e["error_rate_delta"] > 0
        or (
            e["appeared_in_fault"]
            and (e["latency_p95_ms_delta"] > 0 or e["error_rate_delta"] >= 0)
        )
    ]
    missing_edges = [e for e in edge_deltas if e["missing_in_fault"]]
    first_signals = fault.get("cascade_order") or []

    recovery_summary = None
    if recovery:
        rec_edges = _index_edges(recovery)
        persistent = []
        recovered = []
        for item in new_error_edges:
            key = (item["from"], item["to"])
            rec = rec_edges.get(key)
            if rec is None:
                continue
            if (
                float(rec.get("error_rate") or 0.0) > 0
                or float(rec.get("latency_p95_ms") or 0.0)
                >= float(fault_edges.get(key, {}).get("latency_p95_ms") or 0.0) * 0.8
            ):
                persistent.append(key)
            else:
                recovered.append(key)
        recovery_summary = {
            "persistent_edges": [{"from": a, "to": b} for a, b in persistent[:top_k]],
            "recovered_edges": [{"from": a, "to": b} for a, b in recovered[:top_k]],
        }

    return {
        "baseline_window": baseline.get("window"),
        "fault_window": fault.get("window"),
        "post_window": recovery.get("window") if recovery else None,
        "top_node_deltas": node_deltas[:top_k],
        "top_edge_deltas": edge_deltas[:top_k],
        "new_error_edges": new_error_edges[:top_k],
        "missing_edges_during_fault": missing_edges[:top_k],
        "first_degraded_services": first_signals[:top_k],
        "suspected_propagation_path": fault.get("suspected_propagation_path") or [],
        "recovery": recovery_summary,
    }
