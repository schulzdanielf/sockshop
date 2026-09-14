"""Tempo trace provider backed by the MCP server.

``MCPTempoTracesPlugin`` implements :class:`ports.TraceProviderPort`,
fetching distributed traces from Tempo via the MCP observability server
and reducing them to per-service latency/error summaries.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List

from .mcp_client import MCPToolClient


class MCPTempoTracesPlugin:
    def validate(self, config: Dict[str, Any]) -> None:
        if config.get("provider") not in {"mcp-tempo", "tempo"}:
            raise ValueError(
                "observability.traces.provider must be 'mcp-tempo' or 'tempo'"
            )
        if not config.get("mcp_sse_url"):
            raise ValueError("observability.traces.mcp_sse_url is required")

    def _client(self, config: Dict[str, Any]) -> MCPToolClient:
        return MCPToolClient(
            sse_url=str(config["mcp_sse_url"]),
            timeout_seconds=int(config.get("timeout_seconds", 20)),
        )

    def collect_window(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        start_iso: str,
        end_iso: str,
    ) -> Dict[str, Any]:
        client = self._client(config)
        search = client.call_tool_json(
            "tempo_search_traces",
            {
                "query": config.get("query"),
                "service_name": config.get("service_name"),
                "start": start_iso,
                "end": end_iso,
                "limit": int(config.get("limit", 10)),
            },
        )

        traces = search.get("traces", []) if isinstance(search, dict) else []
        analyzed: List[Dict[str, Any]] = []
        for item in traces:
            trace_id = item.get("traceID") or item.get("traceId")
            if not trace_id:
                continue
            features = client.call_tool_json(
                "tempo_analyze_trace", {"trace_id": trace_id}
            )
            summary = client.call_tool_json(
                "tempo_summarize_trace",
                {
                    "trace_id": trace_id,
                    "use_llm": bool(config.get("use_llm", True)),
                    "max_new_tokens": int(config.get("max_new_tokens", 256)),
                },
            )
            # Preserve search-level metadata so downstream analyses
            # (propagation graph, cascade order) can sort by trace start.
            analyzed.append(
                {
                    "trace_id": trace_id,
                    "root_service": item.get("rootServiceName"),
                    "root_trace_name": item.get("rootTraceName"),
                    "start_time_unix_nano": item.get("startTimeUnixNano"),
                    "duration_ms": item.get("durationMs"),
                    "features": features,
                    "summary": summary,
                }
            )

        return {
            "window_start": start_iso,
            "window_end": end_iso,
            "traces": analyzed,
            "count": len(analyzed),
        }

    def summarize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        traces = raw.get("traces", []) if isinstance(raw, dict) else []
        durations: List[float] = []
        errors = 0

        for t in traces:
            features = t.get("features", {}) if isinstance(t, dict) else {}
            if isinstance(features, dict):
                dur = features.get("trace_duration_ms")
                if isinstance(dur, (int, float)):
                    durations.append(float(dur))
                if features.get("error_spans"):
                    errors += 1

        summary: Dict[str, Any] = {
            "trace_count": len(traces),
            "error_trace_count": errors,
        }
        if durations:
            summary["duration_ms"] = {
                "min": min(durations),
                "max": max(durations),
                "mean": statistics.fmean(durations),
                "p95": sorted(durations)[int(0.95 * (len(durations) - 1))],
            }

        return summary

    def aggregate_failures(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Deduplicate failure_signatures across traces and rank by impact.

        Returns:
          {
            top_failure_signatures: [{signature, endpoint, affected_service,
                                       occurrence_count, max_duration_ms,
                                       trace_ids: [...up to 3]}],
            affected_services: [str, ...] (services with errors or hot spans),
            rca_hypotheses: [{hypothesis, confidence, count}]  # top high-confidence
          }
        """
        traces = raw.get("traces", []) if isinstance(raw, dict) else []

        sig_buckets: Dict[tuple, Dict[str, Any]] = {}
        affected: Dict[str, int] = {}
        rca_buckets: Dict[tuple, Dict[str, Any]] = {}

        for t in traces:
            trace_id = t.get("trace_id") if isinstance(t, dict) else None
            features = t.get("features", {}) if isinstance(t, dict) else {}
            if not isinstance(features, dict):
                continue

            for fs in features.get("failure_signatures") or []:
                if not isinstance(fs, dict):
                    continue
                key = (
                    fs.get("signature") or "UNKNOWN",
                    fs.get("endpoint") or "",
                    fs.get("affected_service") or "",
                )
                b = sig_buckets.setdefault(
                    key,
                    {
                        "signature": key[0],
                        "endpoint": key[1],
                        "affected_service": key[2],
                        "occurrence_count": 0,
                        "max_duration_ms": 0.0,
                        "trace_ids": [],
                    },
                )
                b["occurrence_count"] += int(fs.get("occurrence_count", 1))
                dur = float(fs.get("max_duration_ms", 0) or 0)
                if dur > b["max_duration_ms"]:
                    b["max_duration_ms"] = dur
                if (
                    trace_id
                    and trace_id not in b["trace_ids"]
                    and len(b["trace_ids"]) < 3
                ):
                    b["trace_ids"].append(trace_id)
                if key[2]:
                    affected[key[2]] = affected.get(key[2], 0) + 1

            for es in features.get("error_spans") or []:
                if isinstance(es, dict):
                    svc = es.get("service")
                    if svc:
                        affected[svc] = affected.get(svc, 0) + 1

            for hs in (features.get("hot_spans") or [])[:3]:
                if isinstance(hs, dict):
                    svc = hs.get("service")
                    if svc:
                        affected[svc] = affected.get(svc, 0) + 1

            for rca in features.get("rca_hypotheses") or []:
                if not isinstance(rca, dict):
                    continue
                key = (rca.get("hypothesis") or "", rca.get("confidence") or "LOW")
                b = rca_buckets.setdefault(
                    key,
                    {"hypothesis": key[0], "confidence": key[1], "count": 0},
                )
                b["count"] += 1

        top_sigs = sorted(
            sig_buckets.values(),
            key=lambda x: (x["occurrence_count"], x["max_duration_ms"]),
            reverse=True,
        )[:5]
        top_rca = sorted(
            (r for r in rca_buckets.values() if r["confidence"] in {"HIGH", "MEDIUM"}),
            key=lambda x: (x["confidence"] == "HIGH", x["count"]),
            reverse=True,
        )[:3]
        services = sorted(affected.items(), key=lambda kv: kv[1], reverse=True)

        return {
            "top_failure_signatures": top_sigs,
            "affected_services": [s for s, _ in services[:5]],
            "rca_hypotheses": top_rca,
        }

    # ------------------------------------------------------------------
    # Phase F1 — propagation graph for trace-aware retrieval
    # ------------------------------------------------------------------
    def build_propagation_graph(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate per-trace ``dependency_map`` into a service graph.

        Each edge is annotated with the number of traces that exercised it
        and whether at least one of those traces marked the edge as
        erroring. ``cascade_order`` is the list of services that first
        showed up in an error trace, ordered by trace ``startTimeUnixNano``
        — i.e. the temporal sequence in which the failure propagated.

        Returns a JSON-serialisable dict with the shape::

            {
              "nodes":  [str, ...],
              "edges":  [{"from": str, "to": str,
                          "trace_count": int, "error_count": int}, ...],
              "cascade_order": [{"service": str, "t_offset_s": float}, ...],
              "edge_count":       int,
              "error_edge_count": int,
              "trace_count":      int,
            }
        """
        traces = raw.get("traces", []) if isinstance(raw, dict) else []

        nodes: set[str] = set()
        edge_buckets: Dict[tuple, Dict[str, Any]] = {}
        first_error_ts: Dict[str, float] = {}
        earliest_trace_ts: float | None = None

        for t in traces:
            if not isinstance(t, dict):
                continue

            # Trace start time (nanoseconds string from Tempo).
            ts_raw = t.get("start_time_unix_nano")
            ts_s: float | None = None
            if ts_raw is not None:
                try:
                    ts_s = float(int(ts_raw)) / 1e9
                except (ValueError, TypeError):
                    ts_s = None
            if ts_s is not None:
                earliest_trace_ts = (
                    ts_s if earliest_trace_ts is None else min(earliest_trace_ts, ts_s)
                )

            features = t.get("features", {}) if isinstance(t, dict) else {}
            if not isinstance(features, dict):
                continue

            dep_map = features.get("dependency_map") or []
            trace_had_error = bool(features.get("error_spans"))

            for edge in dep_map:
                if not isinstance(edge, dict):
                    continue
                src = edge.get("from")
                dst = edge.get("to")
                if not src or not dst:
                    continue
                nodes.add(src)
                nodes.add(dst)
                key = (src, dst)
                b = edge_buckets.setdefault(
                    key,
                    {
                        "from": src,
                        "to": dst,
                        "trace_count": 0,
                        "error_count": 0,
                    },
                )
                b["trace_count"] += 1
                if trace_had_error or edge.get("is_error"):
                    b["error_count"] += 1

            # Cascade order: first time a service showed up in an error trace.
            if trace_had_error and ts_s is not None:
                for es in features.get("error_spans") or []:
                    if not isinstance(es, dict):
                        continue
                    svc = es.get("service")
                    if not svc:
                        continue
                    nodes.add(svc)
                    prev = first_error_ts.get(svc)
                    if prev is None or ts_s < prev:
                        first_error_ts[svc] = ts_s

        cascade_order: List[Dict[str, Any]] = []
        if first_error_ts:
            base = (
                earliest_trace_ts
                if earliest_trace_ts is not None
                else min(first_error_ts.values())
            )
            for svc, ts in sorted(first_error_ts.items(), key=lambda kv: kv[1]):
                cascade_order.append(
                    {
                        "service": svc,
                        "t_offset_s": round(ts - base, 3),
                    }
                )

        edges = sorted(
            edge_buckets.values(),
            key=lambda e: (e["error_count"], e["trace_count"]),
            reverse=True,
        )
        return {
            "nodes": sorted(nodes),
            "edges": edges,
            "cascade_order": cascade_order,
            "edge_count": len(edges),
            "error_edge_count": sum(1 for e in edges if e["error_count"] > 0),
            "trace_count": len(traces),
        }
