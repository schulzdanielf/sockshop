from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .mcp_client import MCPToolClient


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "p95": sorted(values)[int(0.95 * (len(values) - 1))],
    }


class MCPPrometheusMetricsPlugin:
    def validate(self, config: Dict[str, Any]) -> None:
        if config.get("provider") not in {"mcp-prometheus", "prometheus"}:
            raise ValueError("observability.metrics.provider must be 'mcp-prometheus' or 'prometheus'")
        if not config.get("mcp_sse_url"):
            raise ValueError("observability.metrics.mcp_sse_url is required")

    def _client(self, config: Dict[str, Any]) -> MCPToolClient:
        return MCPToolClient(
            sse_url=str(config["mcp_sse_url"]),
            timeout_seconds=int(config.get("timeout_seconds", 10)),
        )

    def collect_window(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        start_iso: str,
        end_iso: str,
        step_seconds: int,
    ) -> Dict[str, Any]:
        client = self._client(config)
        queries = config.get("queries", [])
        rows: Dict[str, Any] = {}
        for q in queries:
            qid = q.get("id") or q.get("name") or "query"
            promql = q.get("query") or q.get("query_ref")
            if not promql:
                continue
            payload = client.call_tool_json(
                "prometheus_range_query",
                {
                    "query": promql,
                    "start": start_iso,
                    "end": end_iso,
                    "step": f"{step_seconds}s",
                },
            )
            rows[str(qid)] = payload
        return {"window_start": start_iso, "window_end": end_iso, "data": rows}

    def summarize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        data = raw.get("data", {})
        summary: Dict[str, Any] = {"metrics": {}}

        for metric_id, payload in data.items():
            values: List[float] = []
            if isinstance(payload, dict):
                result = payload.get("data", {}).get("result", [])
                for series in result if isinstance(result, list) else []:
                    points = series.get("values", [])
                    for point in points:
                        try:
                            values.append(float(point[1]))
                        except (ValueError, TypeError, IndexError):
                            continue

            summary["metrics"][metric_id] = _stats(values) if values else {"count": 0}

        return summary

    def _iter_points(self, payload: Any):
        """Yield (timestamp_seconds, value, series_labels) for each Prom point."""
        if not isinstance(payload, dict):
            return
        result = payload.get("data", {}).get("result", [])
        for series in result if isinstance(result, list) else []:
            labels = series.get("metric", {}) if isinstance(series, dict) else {}
            for point in series.get("values", []) if isinstance(series, dict) else []:
                try:
                    ts = float(point[0])
                    val = float(point[1])
                except (ValueError, TypeError, IndexError):
                    continue
                yield ts, val, labels

    def summarize_per_phase(
        self,
        raw: Dict[str, Any],
        phases: List[Tuple[str, str, str]],
    ) -> Dict[str, Any]:
        """Split raw range-query points into phase buckets.

        phases: list of (phase_name, start_iso, end_iso).
        Returns: {metric_id: {phase_name: {count,min,max,mean,p95}}}
        """
        data = raw.get("data", {})
        phase_bounds: List[Tuple[str, float, float]] = [
            (name, _parse_iso(s), _parse_iso(e)) for name, s, e in phases
        ]

        out: Dict[str, Dict[str, Any]] = {}
        for metric_id, payload in data.items():
            buckets: Dict[str, List[float]] = {name: [] for name, _, _ in phase_bounds}
            for ts, val, _labels in self._iter_points(payload):
                for name, lo, hi in phase_bounds:
                    if lo <= ts <= hi:
                        buckets[name].append(val)
                        break
            out[str(metric_id)] = {name: _stats(vals) for name, vals in buckets.items()}
        return out

    def find_recovery_time(
        self,
        raw: Dict[str, Any],
        metric_id: str,
        baseline_mean: Optional[float],
        threshold: float,
        fault_end_iso: str,
        recovery_window_end_iso: str,
        tolerance_pct: float = 0.20,
        consecutive_points: int = 2,
    ) -> Optional[float]:
        """Return seconds-since-fault_end when metric returns to baseline ± tolerance.

        Uses max(baseline_mean*(1+tolerance), threshold) as the recovery target.
        Returns None if the metric never recovers within the post window.
        """
        if baseline_mean is None:
            target = threshold
        else:
            target = max(baseline_mean * (1.0 + tolerance_pct), threshold)

        fault_end = _parse_iso(fault_end_iso)
        window_end = _parse_iso(recovery_window_end_iso)

        payload = raw.get("data", {}).get(metric_id)
        if not payload:
            return None

        points = sorted(
            ((ts, val) for ts, val, _ in self._iter_points(payload) if fault_end <= ts <= window_end),
            key=lambda p: p[0],
        )

        streak = 0
        for ts, val in points:
            if val <= target:
                streak += 1
                if streak >= consecutive_points:
                    return ts - fault_end
            else:
                streak = 0
        return None
