"""Phase F1 — Temporal feature engineering.

Derives per-metric temporal descriptors from raw Prometheus range-query
points produced by :class:`MCPPrometheusMetricsPlugin`. The features are
designed to capture *how* a failure unfolds (and recovers), independent
of the absolute magnitude already covered by per-phase aggregates.

Features per metric (when computable):

* ``time_to_first_violation_s`` — seconds between fault start and the
  first point that crosses the SLO threshold.
* ``time_to_peak_s`` — seconds between fault start and the peak value
  observed inside the fault window.
* ``peak_value`` — absolute peak value during the fault window.
* ``max_abs_derivative`` — largest |dv/dt| between consecutive points
  in the fault window (units per second). A proxy for "violence".
* ``coefficient_of_variation_fault`` — stddev/mean during the fault
  window (≈ stability indicator).
* ``recovery_shape`` — one of ``step``, ``exponential``, ``oscillating``,
  ``persistent``, ``unknown``. Computed on the post-fault window using
  sign changes of the first derivative, the fraction of time above the
  recovery target, and how quickly the series collapses back to baseline.

A run-level ``overall_recovery_shape`` is also returned, taken from the
"worst" metric (priority: persistent > oscillating > exponential > step
> unknown).

All functions are pure and depend only on the standard library, so the
engine can call them in best-effort mode without extra dependencies.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SHAPE_PRIORITY = {
    "persistent": 4,
    "oscillating": 3,
    "exponential": 2,
    "step": 1,
    "unknown": 0,
}


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _iter_points(payload: Any) -> Iterable[Tuple[float, float]]:
    """Yield (timestamp_seconds, value) tuples from a Prom range-query payload.

    Flattens across all series so the temporal view is per metric_id, not
    per label combination. Series ordering across labels is preserved
    after a stable sort by timestamp performed by the caller.
    """
    if not isinstance(payload, dict):
        return
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return
    for series in result:
        if not isinstance(series, dict):
            continue
        for point in series.get("values", []):
            try:
                yield float(point[0]), float(point[1])
            except (ValueError, TypeError, IndexError):
                continue


def _bounds(phases: List[Tuple[str, str, str]]) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for name, start, end in phases:
        try:
            out[name] = (_parse_iso(start), _parse_iso(end))
        except Exception:  # pragma: no cover - malformed phase bounds
            continue
    return out


def _window_points(payload: Any, lo: float, hi: float) -> List[Tuple[float, float]]:
    pts = [(ts, v) for ts, v in _iter_points(payload) if lo <= ts <= hi]
    pts.sort(key=lambda p: p[0])
    return pts


def _max_abs_derivative(points: List[Tuple[float, float]]) -> Optional[float]:
    if len(points) < 2:
        return None
    best = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        d = abs((v1 - v0) / dt)
        if d > best:
            best = d
    return best


def _coef_of_variation(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return None
    try:
        sd = statistics.pstdev(values)
    except statistics.StatisticsError:
        return None
    return sd / abs(mean)


def _sign_changes(points: List[Tuple[float, float]]) -> int:
    if len(points) < 3:
        return 0
    derivs: List[float] = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        derivs.append(v1 - v0)
    changes = 0
    prev = 0
    for d in derivs:
        s = 1 if d > 0 else (-1 if d < 0 else 0)
        if s != 0 and prev != 0 and s != prev:
            changes += 1
        if s != 0:
            prev = s
    return changes


def _classify_recovery_shape(
    post_points: List[Tuple[float, float]],
    *,
    target: Optional[float],
    baseline_mean: Optional[float],
) -> str:
    """Heuristic classifier for the recovery curve.

    Inputs are the post-fault window points (sorted by timestamp).
    """
    if len(post_points) < 3 or target is None:
        return "unknown"

    values = [v for _, v in post_points]
    span = post_points[-1][0] - post_points[0][0]
    if span <= 0:
        return "unknown"

    over = [v for v in values if v > target]
    over_frac = len(over) / len(values)

    if over_frac >= 0.8:
        return "persistent"

    sign_changes = _sign_changes(post_points)
    # "Many" sign changes scaled to the series length.
    osc_threshold = max(3, len(post_points) // 5)
    if sign_changes >= osc_threshold and over_frac > 0.2:
        return "oscillating"

    # First-quartile already under target → step recovery.
    q1_idx = max(1, len(post_points) // 4)
    early_under = all(v <= target for _, v in post_points[:q1_idx])
    if early_under:
        return "step"

    # Mostly monotonic decay with a final value near baseline or target.
    if sign_changes <= 1:
        last = values[-1]
        if last <= target:
            return "exponential"
        if baseline_mean is not None and baseline_mean != 0:
            ratio = abs(last) / abs(baseline_mean)
            if ratio <= 1.5:
                return "exponential"

    return "unknown"


def _time_to_first_violation(
    points: List[Tuple[float, float]],
    threshold: Optional[float],
    fault_start: float,
) -> Optional[float]:
    if threshold is None:
        return None
    for ts, v in points:
        if v > threshold:
            return max(0.0, ts - fault_start)
    return None


def _time_to_peak(
    points: List[Tuple[float, float]],
    fault_start: float,
) -> Tuple[Optional[float], Optional[float]]:
    if not points:
        return None, None
    peak_ts, peak_val = max(points, key=lambda p: p[1])
    return max(0.0, peak_ts - fault_start), peak_val


def _recovery_target(
    metric_id: str,
    baseline_mean: Optional[float],
    slo_thresholds: Optional[Dict[str, float]],
    tolerance_pct: float,
) -> Optional[float]:
    """Same convention used by ``find_recovery_time``.

    ``max(baseline_mean*(1+tol), slo_threshold)``; falls back to either
    side if the other is missing.
    """
    slo_thresholds = slo_thresholds or {}
    slo_val: Optional[float] = None
    if metric_id == "error_rate":
        slo_val = slo_thresholds.get("error_rate")
    elif metric_id == "latency_p95":
        slo_val = slo_thresholds.get("latency_p95_ms")

    if baseline_mean is None and slo_val is None:
        return None
    if baseline_mean is None:
        return float(slo_val)  # type: ignore[arg-type]
    if slo_val is None:
        return float(baseline_mean) * (1.0 + tolerance_pct)
    return max(float(baseline_mean) * (1.0 + tolerance_pct), float(slo_val))


def compute_temporal_features(
    metrics_raw: Dict[str, Any],
    phases: List[Tuple[str, str, str]],
    *,
    baseline_means: Optional[Dict[str, Optional[float]]] = None,
    slo_thresholds: Optional[Dict[str, float]] = None,
    tolerance_pct: float = 0.20,
) -> Dict[str, Any]:
    """Compute per-metric temporal features and an overall recovery shape.

    Parameters
    ----------
    metrics_raw : dict
        Raw Prometheus payload as returned by ``collect_window``.
    phases : list[(name, start_iso, end_iso)]
        Same phase list passed to ``summarize_per_phase``.
    baseline_means : dict
        Per-metric baseline means (used to pick recovery target). Already
        computed inside the engine; passed in to avoid recomputation.
    slo_thresholds : dict
        Output of ``Engine._slo_thresholds`` (provides recovery target).
    tolerance_pct : float
        Same recovery tolerance used by ``find_recovery_time``.
    """
    bounds = _bounds(phases)
    fault = bounds.get("fault")
    post = bounds.get("post")
    if fault is None:
        return {"by_metric": {}, "overall_recovery_shape": "unknown"}

    fault_lo, fault_hi = fault
    data = metrics_raw.get("data", {}) if isinstance(metrics_raw, dict) else {}
    baseline_means = baseline_means or {}

    by_metric: Dict[str, Any] = {}
    shapes: List[str] = []

    for metric_id, payload in data.items():
        fault_pts = _window_points(payload, fault_lo, fault_hi)
        if not fault_pts:
            by_metric[str(metric_id)] = {}
            continue

        # Threshold preference: SLO threshold if known, else baseline*tol.
        slo = slo_thresholds or {}
        if metric_id == "error_rate":
            violation_threshold: Optional[float] = slo.get("error_rate")
        elif metric_id == "latency_p95":
            violation_threshold = slo.get("latency_p95_ms")
        else:
            base = baseline_means.get(metric_id)
            violation_threshold = (
                float(base) * (1.0 + tolerance_pct) if base is not None else None
            )

        time_violation = _time_to_first_violation(
            fault_pts, violation_threshold, fault_lo
        )
        time_peak, peak_value = _time_to_peak(fault_pts, fault_lo)
        max_deriv = _max_abs_derivative(fault_pts)
        cv = _coef_of_variation([v for _, v in fault_pts])

        post_pts = _window_points(payload, post[0], post[1]) if post else []
        target = _recovery_target(
            metric_id,
            baseline_means.get(metric_id),
            slo_thresholds,
            tolerance_pct,
        )
        shape = _classify_recovery_shape(
            post_pts,
            target=target,
            baseline_mean=baseline_means.get(metric_id),
        )
        shapes.append(shape)

        by_metric[str(metric_id)] = {
            "time_to_first_violation_s": (
                round(time_violation, 2) if time_violation is not None else None
            ),
            "time_to_peak_s": round(time_peak, 2) if time_peak is not None else None,
            "peak_value": round(peak_value, 4) if peak_value is not None else None,
            "max_abs_derivative": round(max_deriv, 6)
            if max_deriv is not None
            else None,
            "coefficient_of_variation_fault": (
                round(cv, 4) if cv is not None else None
            ),
            "recovery_shape": shape,
        }

    overall = "unknown"
    if shapes:
        overall = max(shapes, key=lambda s: _SHAPE_PRIORITY.get(s, 0))

    return {"by_metric": by_metric, "overall_recovery_shape": overall}


__all__ = ["compute_temporal_features"]
