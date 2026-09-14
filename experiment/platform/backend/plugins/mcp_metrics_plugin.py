"""Prometheus metrics provider backed by the MCP server.

``MCPPrometheusMetricsPlugin`` implements :class:`ports.MetricsProviderPort`,
querying Prometheus through the MCP observability server and summarising
the results (baseline vs. fault statistics) for downstream analysis.
"""

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
            raise ValueError(
                "observability.metrics.provider must be 'mcp-prometheus' or 'prometheus'"
            )
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
            (
                (ts, val)
                for ts, val, _ in self._iter_points(payload)
                if fault_end <= ts <= window_end
            ),
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

    @staticmethod
    def _pod_to_service(pod_name: str) -> str:
        """Strip the ReplicaSet+Pod hash suffix from a k8s pod name.

        Example: 'user-77c77cd7d8-nf5wq' → 'user'
                 'front-end-6489c74749-9jdrg' → 'front-end'
        Hashes are 5-10 chars of [a-z0-9]; the deployment name may itself
        contain hyphens (e.g. 'front-end'), so we strip the LAST TWO
        hyphen-segments rather than splitting on '-'.
        """
        if not pod_name or "-" not in pod_name:
            return pod_name
        parts = pod_name.rsplit("-", 2)
        # Strip suffix only if it really looks like a deployment hash
        # (5-10 lowercase alnum chars). Otherwise return as-is so labels
        # like 'mongodb-exporter' aren't truncated.
        if len(parts) == 3 and len(parts[1]) >= 5 and len(parts[2]) >= 4:
            return parts[0]
        return pod_name

    def per_label_hotspots(
        self,
        raw: Dict[str, Any],
        phases: List[Tuple[str, str, str]],
        *,
        top_k: int = 3,
    ) -> Dict[str, Dict[str, Any]]:
        """Compute per-label fault-vs-baseline deltas for each metric.

        For each metric in *raw*:
          1. Auto-detect the discriminating label key (``name`` for RED
             metrics, ``pod`` for container/saturation metrics).
          2. Bucket points by (label_value, phase).
          3. Compute baseline_mean / fault_mean / fault_p95 per label.
          4. Rank labels by ``fault_mean - baseline_mean`` (descending);
             keep ``top_k``.

        Pod labels are normalised to the parent deployment name so that
        the same logical service is grouped together across replicas.

        Returns
        -------
        ``{metric_id: {"label_kind": "service"|"pod"|"raw",
                       "top": [{label, baseline_mean, fault_mean,
                                fault_p95, delta_abs}, ...]}}``
        """
        data = raw.get("data", {})
        phase_bounds: List[Tuple[str, float, float]] = [
            (name, _parse_iso(s), _parse_iso(e)) for name, s, e in phases
        ]
        # We only care about baseline vs fault here; warmup/post are noise
        # for the localisation signal.
        baseline_window = next(
            ((lo, hi) for n, lo, hi in phase_bounds if n == "baseline"), None
        )
        fault_window = next(
            ((lo, hi) for n, lo, hi in phase_bounds if n == "fault"), None
        )
        if baseline_window is None or fault_window is None:
            return {}

        out: Dict[str, Dict[str, Any]] = {}
        for metric_id, payload in data.items():
            # Group points by (chosen_label, phase) where chosen_label is
            # auto-detected per series from its label set.
            per_label_baseline: Dict[str, List[float]] = {}
            per_label_fault: Dict[str, List[float]] = {}
            label_kind = "raw"

            for ts, val, labels in self._iter_points(payload):
                # Pick the discriminating label: prefer 'name' (RED metrics)
                # then 'pod' (container/saturation). If neither, skip — the
                # metric is already a single global series with nothing to
                # localise.
                if "name" in labels and labels["name"]:
                    key = str(labels["name"])
                    label_kind = "service"
                elif "pod" in labels and labels["pod"]:
                    key = self._pod_to_service(str(labels["pod"]))
                    label_kind = "pod"
                else:
                    continue

                if baseline_window[0] <= ts <= baseline_window[1]:
                    per_label_baseline.setdefault(key, []).append(val)
                elif fault_window[0] <= ts <= fault_window[1]:
                    per_label_fault.setdefault(key, []).append(val)

            if not per_label_fault:
                continue

            rows: List[Dict[str, Any]] = []
            for label, fault_vals in per_label_fault.items():
                base_vals = per_label_baseline.get(label, [])
                fault_mean = statistics.fmean(fault_vals)
                base_mean = statistics.fmean(base_vals) if base_vals else 0.0
                fault_p95 = sorted(fault_vals)[int(0.95 * (len(fault_vals) - 1))]
                rows.append(
                    {
                        "label": label,
                        "baseline_mean": round(base_mean, 4),
                        "fault_mean": round(fault_mean, 4),
                        "fault_p95": round(fault_p95, 4),
                        "delta_abs": round(fault_mean - base_mean, 4),
                    }
                )
            # Rank by absolute fault-vs-baseline deviation (descending) and
            # truncate. Negative deltas (metric DROPPED during fault, e.g.
            # traffic collapse) are still meaningful — keep them by magnitude.
            rows.sort(key=lambda r: abs(r["delta_abs"]), reverse=True)
            out[str(metric_id)] = {
                "label_kind": label_kind,
                "top": rows[:top_k],
            }
        return out
