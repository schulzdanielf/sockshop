"""Trace feature extractor: critical path, exclusive latency, failure signatures,
dependency map, fanout patterns, semantic compression and RCA hypotheses."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ns_to_ms(value: Any) -> float:
    """Convert nanoseconds (int or string) to milliseconds."""
    try:
        return int(value) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _attr_value(attr: Dict) -> str:
    """Extract the string value from an OTLP attribute dict."""
    v = attr.get("value", {})
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in v:
            return str(v[kind])
    return ""


def _get_attr(span: Dict, key: str) -> Optional[str]:
    """Get a named attribute value directly from a span dict."""
    for attr in span.get("attributes", []):
        if attr.get("key") == key:
            val = _attr_value(attr)
            return val if val else None
    return None


# ---------------------------------------------------------------------------
# Semantic span classification
# ---------------------------------------------------------------------------

_OUTBOUND_ATTR_KEYS = {"http.url", "url.full", "http.target", "peer.service", "rpc.service",
                        "net.peer.name", "net.peer.ip", "server.address"}

def _classify_span(span: Dict) -> str:
    """Return a semantic category string for a span."""
    name = span.get("name", "").lower()
    attr_keys = {a.get("key", "") for a in span.get("attributes", [])}

    if "tcp.connect" in name:
        return "OUTBOUND_TCP"
    if name.startswith("middleware"):
        return "MIDDLEWARE"
    if "request handler" in name:
        return "REQUEST_HANDLER"
    if attr_keys & {"rpc.service", "rpc.method"}:
        return "RPC_CALL"
    if attr_keys & {"http.url", "url.full"} or (
        any(m in name for m in ("get ", "post ", "put ", "delete ", "patch ")) and
        attr_keys & {"http.status_code", "http.method"}
    ):
        return "OUTBOUND_HTTP"
    if attr_keys & _OUTBOUND_ATTR_KEYS:
        return "OUTBOUND_CALL"
    return "INTERNAL"


def _extract_remote_endpoint(span: Dict) -> Optional[str]:
    """Extract the best available remote endpoint label from a span."""
    # Prefer named service
    for key in ("peer.service", "rpc.service"):
        val = _get_attr(span, key)
        if val:
            return val
    # HTTP URL / target
    for key in ("http.url", "url.full"):
        val = _get_attr(span, key)
        if val:
            # strip to host+path
            m = re.match(r"https?://([^/]+)(/[^?]*)?", val)
            return m.group(1) + (m.group(2) or "") if m else val
    # Host
    host = (_get_attr(span, "http.host") or _get_attr(span, "server.address")
            or _get_attr(span, "net.peer.name") or _get_attr(span, "net.peer.ip"))
    port = _get_attr(span, "server.port") or _get_attr(span, "net.peer.port")
    if host:
        return f"{host}:{port}" if port else host
    # Fallback: extract IP:port from span name / status message
    return None


def _endpoint_from_message(message: str) -> Optional[str]:
    """Extract IP:port from an error message string."""
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", message)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Failure signature extraction
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS: List[Tuple[str, str]] = [
    (r"ECONNREFUSED|connection refused", "NETWORK_CONNECTION_REFUSED"),
    (r"ETIMEDOUT|timed? ?out", "NETWORK_TIMEOUT"),
    (r"ENOTFOUND|NXDOMAIN|getaddrinfo", "DNS_RESOLUTION_FAILURE"),
    (r"ECONNRESET|connection reset", "CONNECTION_RESET"),
    (r"ECONNABORTED|aborted", "CONNECTION_ABORTED"),
    (r"certificate|ssl|tls|handshake", "TLS_HANDSHAKE_FAILURE"),
    (r"5\d\d", "HTTP_SERVER_ERROR"),
    (r"4\d\d", "HTTP_CLIENT_ERROR"),
]


def _classify_failure(message: str) -> str:
    for pattern, label in _FAILURE_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return label
    return "UNKNOWN_ERROR"


def _extract_failure_signatures(error_spans: List[Dict]) -> List[Dict]:
    """Group error spans into structured, deduplicated failure signatures."""
    bucket: Dict[Tuple[str, str], Dict] = {}

    for span in error_spans:
        msg = span.get("message", "") or ""
        sig_type = _classify_failure(msg)
        endpoint = _get_attr(span, "peer.service") or _get_attr(span, "net.peer.ip")
        if not endpoint:
            endpoint = _endpoint_from_message(msg) or _extract_remote_endpoint(span) or ""

        key = (sig_type, endpoint)
        if key not in bucket:
            bucket[key] = {
                "signature": sig_type,
                "endpoint": endpoint or None,
                "affected_service": span["service"],
                "operations": set(),
                "count": 0,
                "durations_ms": [],
            }
        entry = bucket[key]
        entry["count"] += 1
        entry["operations"].add(f"{span['service']}.{span['name']}")
        entry["durations_ms"].append(span["duration_ms"])

    result = []
    for entry in bucket.values():
        result.append({
            "signature": entry["signature"],
            "endpoint": entry["endpoint"],
            "affected_service": entry["affected_service"],
            "operations": sorted(entry["operations"]),
            "occurrence_count": entry["count"],
            "max_duration_ms": round(max(entry["durations_ms"]), 2),
        })
    result.sort(key=lambda x: x["max_duration_ms"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Fanout pattern detection
# ---------------------------------------------------------------------------

def _detect_fanout_patterns(
    spans: List[Dict],
    span_map: Dict[str, Dict],
    children: Dict[str, List[str]],
) -> List[Dict]:
    """Detect N+1 calls, retry storms, excessive fanout."""
    patterns: List[Dict] = []

    # N+1: same (service, name) repeated many times
    name_counter = Counter((s["service"], s["name"]) for s in spans)
    for (svc, name), count in name_counter.most_common():
        if count >= 4:
            patterns.append({
                "pattern": "N+1_REPEATED_CALL",
                "service": svc,
                "operation": name,
                "count": count,
                "description": f"{count}x repeated '{svc}.{name}'",
            })

    # Retry storm: multiple error spans with the same name
    error_name_counter = Counter(
        (s["service"], s["name"]) for s in spans if s["is_error"]
    )
    for (svc, name), count in error_name_counter.most_common():
        if count > 1:
            patterns.append({
                "pattern": "RETRY_STORM",
                "service": svc,
                "operation": name,
                "count": count,
                "description": f"{count}x failed call to '{svc}.{name}'",
            })

    # Excessive fanout: single span with many direct children
    for sid, kids in children.items():
        if sid not in span_map or len(kids) <= 5:
            continue
        span = span_map[sid]
        patterns.append({
            "pattern": "EXCESSIVE_FANOUT",
            "service": span["service"],
            "operation": span["name"],
            "count": len(kids),
            "description": f"{len(kids)} direct children from '{span['service']}.{span['name']}'",
        })

    # Downstream amplification: error in parent propagates to all children
    for sid, kids in children.items():
        if sid not in span_map:
            continue
        parent = span_map[sid]
        if parent["is_error"] and len(kids) >= 3:
            child_errors = sum(1 for cid in kids if span_map.get(cid, {}).get("is_error"))
            if child_errors >= 2:
                patterns.append({
                    "pattern": "ERROR_PROPAGATION",
                    "service": parent["service"],
                    "operation": parent["name"],
                    "count": child_errors,
                    "description": f"Error in '{parent['name']}' propagated to {child_errors} children",
                })

    patterns.sort(key=lambda x: x["count"], reverse=True)
    return patterns[:8]


# ---------------------------------------------------------------------------
# RCA hypothesis generation
# ---------------------------------------------------------------------------

def _generate_rca_hypotheses(
    spans: List[Dict],
    failure_signatures: List[Dict],
    critical_path: List[Dict],
    trace_duration_ms: float,
) -> List[Dict]:
    """Generate structured RCA hypotheses with confidence levels."""
    hypotheses: List[Dict] = []

    for sig in failure_signatures:
        ep = sig.get("endpoint") or "unknown endpoint"

        if sig["signature"] == "NETWORK_CONNECTION_REFUSED":
            hypotheses.append({
                "hypothesis": f"Downstream dependency unavailable at {ep}",
                "confidence": "HIGH",
                "evidence": [
                    f"ECONNREFUSED at {ep}",
                    f"Affected service: {sig['affected_service']}",
                    f"Operations: {', '.join(sig['operations'])}",
                    f"Max blocked duration: {sig['max_duration_ms']}ms",
                ],
                "suggested_actions": [
                    f"Verify the service at {ep} is running (kubectl get pods/endpoints)",
                    "Check Kubernetes Service/EndpointSlice for this cluster IP",
                    "Inspect network policies between services",
                    "Add circuit breaker / fallback to prevent cascading failure",
                ],
            })

        elif sig["signature"] == "NETWORK_TIMEOUT":
            hypotheses.append({
                "hypothesis": f"Downstream dependency slow or overloaded ({ep})",
                "confidence": "MEDIUM",
                "evidence": [f"Timeout in {sig['affected_service']}", f"Max latency: {sig['max_duration_ms']}ms"],
                "suggested_actions": [
                    "Review timeout thresholds and add adaptive retries",
                    "Check resource utilization (CPU/memory) of the dependency",
                    "Consider async/non-blocking pattern",
                ],
            })

        elif sig["signature"] == "DNS_RESOLUTION_FAILURE":
            hypotheses.append({
                "hypothesis": "DNS resolution failure for a dependency",
                "confidence": "HIGH",
                "evidence": [f"DNS error in {sig['affected_service']}"],
                "suggested_actions": [
                    "Verify Kubernetes Service name matches the DNS hostname",
                    "Check CoreDNS logs: kubectl logs -n kube-system -l k8s-app=kube-dns",
                ],
            })

    # tcp.connect dominates exclusive latency
    tcp_error_spans = [
        s for s in spans
        if "tcp.connect" in s.get("name", "").lower()
        and s.get("exclusive_duration_ms", 0) > trace_duration_ms * 0.4
    ]
    if tcp_error_spans:
        worst = max(tcp_error_spans, key=lambda s: s.get("exclusive_duration_ms", 0))
        pct = round(worst["exclusive_duration_ms"] / trace_duration_ms * 100)
        hypotheses.append({
            "hypothesis": f"Connection establishment accounts for {pct}% of trace latency",
            "confidence": "HIGH",
            "evidence": [
                f"{worst['service']}.{worst['name']} exclusive latency: {worst['exclusive_duration_ms']}ms",
                f"{pct}% of total {trace_duration_ms}ms trace duration",
                "TCP connect is on the critical path",
            ],
            "suggested_actions": [
                "Enable HTTP keep-alive / connection pooling in the client",
                "Verify the target IP resolves to the correct service",
                "Check if the dependency is reachable from this pod's network namespace",
            ],
        })

    # All errors in a single service
    error_services = [s["service"] for s in spans if s["is_error"]]
    if error_services and len(set(error_services)) == 1:
        svc = error_services[0]
        hypotheses.append({
            "hypothesis": f"Error isolated to service '{svc}' — upstream services are healthy",
            "confidence": "MEDIUM",
            "evidence": [f"All {len(error_services)} error span(s) belong to {svc}"],
            "suggested_actions": [
                f"Focus investigation on {svc} outbound calls and configuration",
                f"Review {svc} deployment environment variables and secrets",
            ],
        })

    return hypotheses


# ---------------------------------------------------------------------------
# Exclusive latency computation
# ---------------------------------------------------------------------------

def _compute_exclusive_latency(
    spans: List[Dict],
    span_map: Dict[str, Dict],
    children: Dict[str, List[str]],
) -> None:
    """Annotate each span with exclusive_duration_ms (time outside children) in-place."""
    for span in spans:
        sid = span["span_id"]
        kids_total = sum(
            span_map[cid]["duration_ms"]
            for cid in children.get(sid, [])
            if cid in span_map
        )
        span["exclusive_duration_ms"] = round(max(0.0, span["duration_ms"] - kids_total), 2)


# ---------------------------------------------------------------------------
# Semantic critical path compression
# ---------------------------------------------------------------------------

_INTERNAL_CATEGORIES = {"MIDDLEWARE", "REQUEST_HANDLER", "INTERNAL"}

def _compress_critical_path(
    critical_path_ids: List[str],
    span_map: Dict[str, Dict],
) -> List[Dict]:
    """Compress chains of internal instrumentation spans into semantic operations."""
    compressed: List[Dict] = []
    internal_group: List[Dict] = []

    def _flush_internal(group: List[Dict]) -> None:
        if not group:
            return
        total = sum(s["exclusive_duration_ms"] for s in group)
        if total > 0.5:  # only emit if they collectively consume measurable time
            compressed.append({
                "service": group[0]["service"],
                "name": f"[{len(group)} internal spans]",
                "duration_ms": round(sum(s["duration_ms"] for s in group), 2),
                "exclusive_duration_ms": round(total, 2),
                "semantic": "INTERNAL_GROUP",
            })

    for sid in critical_path_ids:
        if sid not in span_map:
            continue
        span = span_map[sid]
        cat = _classify_span(span)

        if cat in _INTERNAL_CATEGORIES:
            internal_group.append(span)
        else:
            _flush_internal(internal_group)
            internal_group = []
            # Build a meaningful semantic label
            if cat == "OUTBOUND_TCP" and span["is_error"]:
                semantic_name = f"connection failure → {_extract_remote_endpoint(span) or span['name']}"
            elif cat in ("OUTBOUND_HTTP", "OUTBOUND_CALL", "OUTBOUND_TCP"):
                ep = _extract_remote_endpoint(span)
                semantic_name = f"outbound call → {ep}" if ep else span["name"]
            elif cat == "RPC_CALL":
                svc = _get_attr(span, "rpc.service") or ""
                method = _get_attr(span, "rpc.method") or span["name"]
                semantic_name = f"rpc → {svc}.{method}" if svc else f"rpc → {method}"
            else:
                semantic_name = span["name"]

            compressed.append({
                "service": span["service"],
                "name": semantic_name,
                "duration_ms": span["duration_ms"],
                "exclusive_duration_ms": span["exclusive_duration_ms"],
                "semantic": cat,
                "is_error": span["is_error"],
            })

    _flush_internal(internal_group)
    return compressed


# ---------------------------------------------------------------------------
# Span parser
# ---------------------------------------------------------------------------

def _parse_spans(trace_data: Dict) -> List[Dict]:
    """Flatten all spans from OTLP trace payload and enrich with service_name + duration_ms."""
    spans: List[Dict] = []

    for batch in trace_data.get("batches", []):
        # Resolve service name from resource attributes
        service_name = "unknown"
        for attr in batch.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                service_name = _attr_value(attr)
                break

        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                start_ns = int(span.get("startTimeUnixNano", 0))
                end_ns = int(span.get("endTimeUnixNano", 0))
                duration_ms = _ns_to_ms(end_ns - start_ns)
                status = span.get("status", {})
                status_code = status.get("code", 0)
                # OTLP JSON encodes the status code as int (2) or string ("STATUS_CODE_ERROR")
                is_error = status_code in (2, "STATUS_CODE_ERROR", "error") or str(status_code) == "2"
                # Fallback: HTTP spans with 4xx/5xx status codes that don't set OTLP status
                if not is_error:
                    for attr in span.get("attributes", []):
                        if attr.get("key") == "http.status_code":
                            try:
                                if int(_attr_value(attr)) >= 400:
                                    is_error = True
                            except (ValueError, TypeError):
                                pass
                            break

                spans.append({
                    "span_id": span.get("spanId", ""),
                    "parent_span_id": span.get("parentSpanId", ""),
                    "name": span.get("name", ""),
                    "service": service_name,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ms": round(duration_ms, 2),
                    "is_error": is_error,
                    "status_message": span.get("status", {}).get("message", ""),
                    "attributes": span.get("attributes", []),
                })

    return spans


def _build_tree(spans: List[Dict]) -> Tuple[Dict[str, List[str]], Optional[str]]:
    """Build parent→children map and find root span_id."""
    children: Dict[str, List[str]] = defaultdict(list)
    ids = {s["span_id"] for s in spans}
    root_id: Optional[str] = None

    for span in spans:
        pid = span["parent_span_id"]
        if not pid or pid not in ids:
            root_id = span["span_id"]
        else:
            children[pid].append(span["span_id"])

    return children, root_id


def _find_critical_path(
    span_id: str,
    span_map: Dict[str, Dict],
    children: Dict[str, List[str]],
) -> List[str]:
    """Return the longest duration path from span_id to a leaf."""
    if not children.get(span_id):
        return [span_id]

    best_path: List[str] = []
    best_duration = -1.0

    for child_id in children[span_id]:
        path = _find_critical_path(child_id, span_map, children)
        duration = sum(span_map[s]["duration_ms"] for s in path if s in span_map)
        if duration > best_duration:
            best_duration = duration
            best_path = path

    return [span_id] + best_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_features(trace_data: Dict) -> Dict:
    """Extract all observability features from an OTLP trace payload.

    Returns a dict with keys:
    - trace_duration_ms
    - span_count
    - critical_path              : semantic-compressed critical path
    - critical_path_raw          : full uncompressed critical path (inclusive latency)
    - hot_spans                  : top-10 by exclusive latency (real bottlenecks)
    - error_spans                : spans with error status
    - failure_signatures         : structured failure patterns with semantic types
    - fanout_patterns            : N+1, retry storm, excessive fanout detections
    - rca_hypotheses             : auto-generated RCA hypotheses with confidence
    - fanout                     : top-5 spans by direct child count (legacy)
    - dependency_map             : inferred service edges (OTel attrs + parent-child)
    - service_latency            : per-service exclusive + inclusive latency
    """
    spans = _parse_spans(trace_data)
    if not spans:
        return {"error": "no spans found in trace"}

    span_map: Dict[str, Dict] = {s["span_id"]: s for s in spans}
    children, root_id = _build_tree(spans)

    # ---- exclusive latency (must run before any ranking) ---------------
    _compute_exclusive_latency(spans, span_map, children)

    # ---- trace duration ------------------------------------------------
    trace_start = min(s["start_ns"] for s in spans)
    trace_end = max(s["end_ns"] for s in spans)
    trace_duration_ms = round(_ns_to_ms(trace_end - trace_start), 2)

    # ---- critical path (semantic compressed) ---------------------------
    critical_path_ids: List[str] = []
    if root_id:
        critical_path_ids = _find_critical_path(root_id, span_map, children)

    critical_path_raw = [
        {
            "service": span_map[sid]["service"],
            "name": span_map[sid]["name"],
            "duration_ms": span_map[sid]["duration_ms"],
            "exclusive_duration_ms": span_map[sid]["exclusive_duration_ms"],
        }
        for sid in critical_path_ids
        if sid in span_map
    ]

    critical_path = _compress_critical_path(critical_path_ids, span_map)

    # ---- hot spans by exclusive latency (real bottlenecks) -------------
    sorted_exclusive = sorted(spans, key=lambda s: s["exclusive_duration_ms"], reverse=True)
    hot_spans = [
        {
            "service": s["service"],
            "name": s["name"],
            "exclusive_duration_ms": s["exclusive_duration_ms"],
            "inclusive_duration_ms": s["duration_ms"],
        }
        for s in sorted_exclusive[:10]
        if s["exclusive_duration_ms"] > 0
    ]

    # ---- error spans ---------------------------------------------------
    error_spans = [
        {
            "service": s["service"],
            "name": s["name"],
            "duration_ms": s["duration_ms"],
            "exclusive_duration_ms": s["exclusive_duration_ms"],
            "message": s["status_message"],
        }
        for s in spans
        if s["is_error"]
    ]

    # ---- failure signatures --------------------------------------------
    failure_signatures = _extract_failure_signatures(error_spans)

    # ---- fanout patterns -----------------------------------------------
    fanout_patterns = _detect_fanout_patterns(spans, span_map, children)

    # ---- legacy fanout (top 5 by child count) --------------------------
    fanout_counts = [
        {
            "service": span_map[sid]["service"],
            "name": span_map[sid]["name"],
            "children_count": len(kids),
        }
        for sid, kids in children.items()
        if sid in span_map
    ]
    fanout_counts.sort(key=lambda x: x["children_count"], reverse=True)
    top_fanout = fanout_counts[:5]

    # ---- dependency map (OTel attributes + parent-child) ---------------
    edges: Set[Tuple[str, str]] = set()
    edge_details: Dict[Tuple[str, str], Dict] = {}

    for span in spans:
        # Cross-service parent-child edge
        pid = span["parent_span_id"]
        if pid and pid in span_map:
            parent_svc = span_map[pid]["service"]
            child_svc = span["service"]
            if parent_svc != child_svc:
                edges.add((parent_svc, child_svc))

        # Outbound call edges via OTel attributes
        cat = _classify_span(span)
        if cat in ("OUTBOUND_HTTP", "OUTBOUND_TCP", "OUTBOUND_CALL", "RPC_CALL"):
            remote = _extract_remote_endpoint(span)
            if remote:
                key = (span["service"], remote)
                if key not in edge_details:
                    edge_details[key] = {
                        "from": span["service"],
                        "to": remote,
                        "operation": span["name"],
                        "is_error": span["is_error"],
                    }
                elif span["is_error"]:
                    edge_details[key]["is_error"] = True

    dependency_map = [{"from": f, "to": t} for f, t in sorted(edges)]
    # Merge OTel-derived edges (may include endpoint-level detail)
    for (src, dst), detail in sorted(edge_details.items()):
        if (src, dst) not in edges:
            dependency_map.append(detail)

    # ---- service latency (exclusive + inclusive) -----------------------
    svc_spans: Dict[str, List[Dict]] = defaultdict(list)
    for span in spans:
        svc_spans[span["service"]].append(span)

    service_latency = []
    for svc, svc_span_list in sorted(svc_spans.items()):
        incl = [s["duration_ms"] for s in svc_span_list]
        excl = [s["exclusive_duration_ms"] for s in svc_span_list]
        service_latency.append({
            "service": svc,
            "span_count": len(incl),
            "total_exclusive_ms": round(sum(excl), 2),
            "total_inclusive_ms": round(sum(incl), 2),
            "avg_ms": round(sum(excl) / len(excl), 2),
            "max_exclusive_ms": round(max(excl), 2),
            "max_inclusive_ms": round(max(incl), 2),
        })
    service_latency.sort(key=lambda x: x["total_exclusive_ms"], reverse=True)

    # ---- RCA hypotheses ------------------------------------------------
    rca_hypotheses = _generate_rca_hypotheses(
        spans, failure_signatures, critical_path, trace_duration_ms
    )

    return {
        "trace_duration_ms": trace_duration_ms,
        "span_count": len(spans),
        "critical_path": critical_path,
        "critical_path_raw": critical_path_raw,
        "hot_spans": hot_spans,
        "error_spans": error_spans,
        "failure_signatures": failure_signatures,
        "fanout_patterns": fanout_patterns,
        "fanout": top_fanout,
        "dependency_map": dependency_map,
        "service_latency": service_latency,
        "rca_hypotheses": rca_hypotheses,
    }
