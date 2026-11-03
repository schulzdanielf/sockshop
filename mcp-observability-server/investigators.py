"""High-level *investigator* primitives for the observability MCP server.

These tools wrap raw PromQL queries with a stable, business-oriented API:
instead of asking "give me the PromQL result of X", an agent (or human)
asks "did any pod get OOMKilled between t0 and t1?" and receives a tidy
JSON answer with `found`, the list of pods, evidence values and the
exact query used.

Design goals
------------
* **Window safety.** Every tool takes explicit ``start``/``end`` ISO
  timestamps. Counter queries may pad the range to obtain scrapes on both
  sides, but only count increments whose observed scrape interval overlaps
  the requested window. This avoids unbounded lookbacks while preserving
  the uncertainty between scrapes.
* **Vocabulary parity.** Pod labels are normalised to the parent
  deployment / service name (``front-end-6489c74749-9jdrg`` →
  ``front-end``) so callers can reason at the service level. The raw
  pod name is kept in ``pods_detail`` for traceability.
* **Schema for small LLMs.** Output is always a flat dict with
  ``found`` (bool), ``count`` (int), ``top`` (list ordered by impact),
  ``query`` (str), ``window`` (dict). The shape never changes based
  on whether something was found — small models stall on conditional
  schemas.
* **Cheap to compose.** Tools never call other tools. A future
  ``FaultCategoryValidator`` agent will call 2–3 of these in sequence
  with simple boolean reasoning, without needing to read PromQL.
"""
from __future__ import annotations

import re
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

# ── Pod-name → service-name normalisation ───────────────────────────────
# Strip the last two hyphen-delimited segments when they look like a
# k8s ReplicaSet+Pod hash suffix (5-10 lowercase alnum chars). Mirrors
# ``MCPPrometheusMetricsPlugin._pod_to_service`` in the platform so the
# agent and the engine speak the same service vocabulary.
_HASH_RE = re.compile(r"^[a-z0-9]{4,10}$")
SERVICE_LABEL_KEYS = ("destination_workload", "name", "service", "source_workload")


def _pod_to_service(pod_name: str) -> str:
    if not pod_name or "-" not in pod_name:
        return pod_name
    parts = pod_name.rsplit("-", 2)
    if (
        len(parts) == 3
        and len(parts[1]) >= 5
        and _HASH_RE.match(parts[1])
        and _HASH_RE.match(parts[2])
    ):
        return parts[0]
    return pod_name


# ── Range-query helpers ─────────────────────────────────────────────────
def _iter_series(payload: Any):
    """Yield (labels_dict, [(ts, value), ...]) for each series."""
    if not isinstance(payload, dict):
        return
    result = payload.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return
    for series in result:
        if not isinstance(series, dict):
            continue
        labels = series.get("metric", {}) or {}
        points: List[tuple] = []
        for p in series.get("values", []) or []:
            try:
                timestamp, value = float(p[0]), float(p[1])
                if math.isfinite(timestamp) and math.isfinite(value):
                    points.append((timestamp, value))
            except (ValueError, TypeError, IndexError):
                continue
        yield labels, points


def _coverage_summary(payload: Any) -> Dict[str, int]:
    series_count = 0
    sample_count = 0
    for _, points in _iter_series(payload):
        if points:
            series_count += 1
            sample_count += len(points)
    return {"series_count": series_count, "sample_count": sample_count}


def _aggregate_by_service(
    payload: Any,
    *,
    mode: str,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate Prometheus range-query points into per-service rollups.

    ``mode`` selects the reduction:
      * ``"max_minus_min"`` — for monotonic counters; result is
        ``max(values) - min(values)`` per series, then summed per
        service. Use for restart counts.
      * ``"max"`` — for 0/1 gauges where any transition to 1 inside
        the window counts; result is ``max(values)`` per series, then
        ``max`` per service. Use for the OOMKilled gauge.
      * ``"rising"`` — count observed low-to-high gauge transitions.
            * ``"mean"`` — average across the window per pod, then average
                pod percentages per service. Use for saturation percentages.
    """
    per_pod: Dict[Any, Dict[str, Any]] = {}
    for labels, points in _iter_series(payload):
        if not points:
            continue
        pod = str(labels.get("pod") or next((labels[k] for k in SERVICE_LABEL_KEYS if labels.get(k)), ""))
        if not pod or pod == "unknown":
            continue
        values = [v for _, v in points]
        if mode == "max_minus_min":
            value = max(values) - min(values)
        elif mode == "max":
            value = max(values)
        elif mode == "mean":
            value = sum(values) / len(values)
        elif mode == "rising":
            ordered = sorted(points)
            value = sum(previous < 0.5 <= current for (_, previous), (_, current)
                        in zip(ordered, ordered[1:]))
        else:
            raise ValueError(f"unknown aggregation mode: {mode!r}")
        identity = (
            (pod, labels.get("container"), labels.get("uid"))
            if mode in ("rising", "max") else pod
        )
        per_pod[identity] = {
            "pod": pod,
            "service": _pod_to_service(pod),
            "value": round(value, 4),
            **({"container": labels["container"]} if labels.get("container") else {}),
            **({"uid": labels["uid"]} if labels.get("uid") else {}),
        }

    per_service: Dict[str, Dict[str, Any]] = {}
    for entry in per_pod.values():
        svc = entry["service"]
        bucket = per_service.setdefault(svc, {"service": svc, "value": 0.0, "pods": []})
        if mode == "max":
            bucket["value"] = max(bucket["value"], entry["value"])
        else:
            bucket["value"] += entry["value"]
        bucket["pods"].append({k: v for k, v in entry.items() if k != "service"})
    if mode == "mean":
        for bucket in per_service.values():
            bucket["value"] /= len(bucket["pods"])
    return per_service


def _counter_increments(points: List[tuple]) -> float:
    """Count increments of one monotonic counter over the supplied window."""
    ordered = sorted(points, key=lambda point: point[0])
    increments = 0.0
    for (_, previous), (_, current) in zip(ordered, ordered[1:]):
        if current >= previous:
            increments += current - previous
        else:
            increments += current
    return increments


def _sample_time_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


_PROMETHEUS_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_PROMETHEUS_DURATION_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


def _prometheus_duration_seconds(value: str) -> float:
    parts = list(_PROMETHEUS_DURATION_PART.finditer(value))
    if not parts or "".join(part.group(0) for part in parts) != value:
        raise ValueError("step must be a positive Prometheus duration")
    seconds = sum(
        float(part.group(1)) * _PROMETHEUS_DURATION_SECONDS[part.group(2)]
        for part in parts
    )
    if seconds <= 0:
        raise ValueError("step must be a positive Prometheus duration")
    return seconds


def _iso_timestamp_seconds(value: str, name: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).timestamp()


def _restart_container_samples(payload: Any) -> Dict[tuple, Dict[float, float]]:
    container_samples: Dict[tuple, Dict[float, float]] = {}
    for labels, points in _iter_series(payload):
        if labels.get("job") not in (None, "kube-state-metrics"):
            continue
        pod = str(labels.get("pod") or "")
        if not pod or pod == "unknown":
            continue
        container = str(labels.get("container") or "unknown")
        uid = str(labels.get("uid") or "")
        samples = container_samples.setdefault((pod, container, uid), {})
        for timestamp, value in points:
            if value >= 0:
                samples[timestamp] = max(value, samples.get(timestamp, value))
    return {
        identity: samples
        for identity, samples in container_samples.items()
        if samples
    }


def _restart_coverage(
    container_samples: Dict[tuple, Dict[float, float]],
    window_start: float,
    window_end: float,
) -> Dict[str, int]:
    bracketed = sum(
        min(samples) <= window_start and max(samples) >= window_end
        for samples in container_samples.values()
    )
    series_count = len(container_samples)
    return {
        "series_count": series_count,
        "sample_count": sum(len(samples) for samples in container_samples.values()),
        "window_bracketed_series_count": bracketed,
        "window_unbracketed_series_count": series_count - bracketed,
        "window_sample_count": sum(
            window_start <= timestamp <= window_end
            for samples in container_samples.values() for timestamp in samples
        ),
    }


def _restart_rollup(
    container_samples: Dict[tuple, Dict[float, float]],
    window_start: float,
    window_end: float,
    timestamp_kind: str = "scrape",
) -> Dict[str, Dict[str, Any]]:
    """Sum reset-aware counter increments by pod and service.

    kube-state-metrics can be scraped through more than one Prometheus job.
    The query selects its canonical job; the check here also guards test or
    nonstandard payloads from counting duplicate scrape sources.
    """
    per_service: Dict[str, Dict[str, Any]] = {}
    for (pod, container, uid), samples in container_samples.items():
        points = sorted(samples.items())
        count = 0.0
        service = _pod_to_service(pod)
        row = per_service.setdefault(
            service, {"service": service, "value": 0.0, "pods": []}
        )
        observations = []
        for (previous_time, previous), (current_time, current) in zip(
            points, points[1:]
        ):
            if current_time < window_start or previous_time >= window_end:
                continue
            delta = current - previous if current >= previous else current
            if delta <= 0:
                continue
            count += delta
            observations.append(
                {
                    "observed_from": _sample_time_iso(previous_time),
                    "observed_to": _sample_time_iso(current_time),
                    "restart_count_delta": round(delta, 4),
                    "counter_reset": current < previous,
                    "timestamp_precision": (
                        "between_scrapes" if timestamp_kind == "scrape"
                        else "between_evaluations"
                    ),
                    "window_attribution": (
                        "within_window" if previous_time >= window_start
                        and current_time <= window_end else "overlap_only"
                    ),
                }
            )
        row["value"] += count
        row["pods"].append(
            {
                "pod": pod,
                "container": container,
                "uid": uid or None,
                "value": round(count, 4),
                "restart_observations": observations[:20],
                "omitted_restart_observation_count": max(0, len(observations) - 20),
            }
        )
    return per_service


def _top_k(rollup: Dict[str, Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    rows = list(rollup.values())
    rows.sort(key=lambda r: r["value"], reverse=True)
    for r in rows:
        r["value"] = round(r["value"], 4)
    return rows[:k]


def _service_level_series_top(
    prometheus,
    *,
    query: str,
    start: str,
    end: str,
    step: str,
    k: int,
    mean_key: str,
    max_key: str,
    found_floor: float,
) -> Dict[str, Any]:
    """Query a service-level time series and return mean/max top-K rows."""
    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
            "coverage": {"series_count": 0, "sample_count": 0},
        }

    coverage = _coverage_summary(raw)
    means = _aggregate_by_service(raw, mode="mean")
    maxes = _aggregate_by_service(raw, mode="max")
    rows: List[Dict[str, Any]] = []
    for svc, m in means.items():
        rows.append(
            {
                "service": svc,
                mean_key: round(m["value"], 4),
                max_key: round(maxes.get(svc, {}).get("value", 0.0), 4),
                "pods": m.get("pods", []),
            }
        )
    rows.sort(key=lambda r: r[max_key], reverse=True)
    return {
        "status": "ok" if coverage["series_count"] else "no_data",
        "found": any(r[max_key] > found_floor for r in rows),
        "count": sum(1 for r in rows if r[max_key] > found_floor),
        "signal_detected": any(r[max_key] > found_floor for r in rows),
        "top": rows[:k],
        "query": query,
        "window": {"start": start, "end": end},
        "coverage": coverage,
    }


# ── Public investigator queries ─────────────────────────────────────────
def check_oom_kills(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
) -> Dict[str, Any]:
    """Return per-service evidence of OOMKills *within* ``[start, end]``.

    Uses the last-termination reason gauge. Only observed rising transitions
    are counted; a falling edge or initial high state is not a new OOM.
    Without termination timestamps this remains sampled state, not proof
    of the event time or of its cause.
    """
    query = (
        "kube_pod_container_status_last_terminated_reason"
        f'{{namespace="{namespace}",reason="OOMKilled",job="kube-state-metrics"}}'
    )
    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
            "coverage": {"series_count": 0, "sample_count": 0},
        }
    coverage = _coverage_summary(raw)
    transitions = _aggregate_by_service(raw, mode="rising")
    still_high = _aggregate_by_service(raw, mode="max")
    rows: List[Dict[str, Any]] = []
    for svc, t in transitions.items():
        rows.append(
            {
                "service": svc,
                "transitioned_in_window": t["value"] > 0.4,
                "delta": round(t["value"], 4),
                "max_in_window": round(still_high.get(svc, {}).get("value", 0.0), 4),
                "pods": t.get("pods", []),
            }
        )
    rows.sort(key=lambda r: r["delta"], reverse=True)
    transitioned = [r for r in rows if r["transitioned_in_window"]]
    return {
        "status": "ok" if coverage["series_count"] else "no_data",
        "found": len(transitioned) > 0,
        "count": len(transitioned),
        "signal_detected": len(transitioned) > 0,
        "top": rows[:5],
        "query": query,
        "window": {"start": start, "end": end},
        "coverage": coverage,
        "limitations": [
            "Last-termination reason is sampled metrics state, not an exact event timestamp.",
            "A missing or initially high OOM reason cannot exclude or date an OOM.",
        ],
    }


def check_pod_restarts(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    history_pad: str = "1m",
) -> Dict[str, Any]:
    """Return counter increments observed across ``[start, end]``.

    Raw scrape collection is padded by at least one minute on both sides,
    independently of query evaluation step. An increment is
    included when its between-scrape interval overlaps the requested window;
    the exact event time remains uncertain at scrape resolution.
    """
    window_start = _iso_timestamp_seconds(start, "start")
    window_end = _iso_timestamp_seconds(end, "end")
    if window_start > window_end:
        raise ValueError("start must not be after end")
    step_seconds = _prometheus_duration_seconds(step)
    pad_seconds = max(step_seconds, _prometheus_duration_seconds(history_pad))
    query_start = _sample_time_iso(window_start - pad_seconds)
    query_end = _sample_time_iso(window_end + pad_seconds)
    query = (
        "kube_pod_container_status_restarts_total"
        f'{{namespace="{namespace}",job="kube-state-metrics"}}'
    )
    raw_query = getattr(prometheus, "query_raw_range", None)
    timestamp_kind = "scrape" if callable(raw_query) else "evaluation"
    raw = (
        raw_query(query, query_start, query_end) if callable(raw_query)
        else prometheus.query_range(query, query_start, query_end, step)
    )
    collection = {
        "sample_timestamp_kind": timestamp_kind,
        "history_pad": history_pad,
        "history_pad_seconds": pad_seconds,
        "collection_query": (
            f"{query}[{math.ceil((window_end - window_start + 2 * pad_seconds) * 1000)}ms]"
            if timestamp_kind == "scrape" else query
        ),
        "collection_evaluation_time": query_end if timestamp_kind == "scrape" else None,
        "telemetry_family": "metrics",
        "limitations": [
            "Counter increments overlapping the window are candidates, not exactly timed events.",
            "Padding samples are not fault-phase observations or independent telemetry.",
            "Missing series or insufficient bracketing cannot exclude a restart.",
            "Observed services are only those with retrieved counters, not a service inventory.",
        ],
    }
    if timestamp_kind == "evaluation":
        collection["limitations"].append(
            "Legacy range-query timestamps are evaluations; lookback may repeat older scrapes."
        )
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
            "query_window": {"start": query_start, "end": query_end},
            **collection,
            "observed_services": [],
            "raw_coverage": {"series_count": 0, "sample_count": 0},
            "coverage": {
                "series_count": 0,
                "sample_count": 0,
                "window_bracketed_series_count": 0,
                "window_unbracketed_series_count": 0,
                "window_sample_count": 0,
            },
        }
    container_samples = _restart_container_samples(raw)
    coverage = _restart_coverage(container_samples, window_start, window_end)
    rollup = _restart_rollup(
        container_samples, window_start, window_end, timestamp_kind
    )
    # Return all observed services so a targeted question can distinguish a
    # zero count from a service omitted by a top-5 cutoff.
    top = _top_k(rollup, k=len(rollup))
    nonzero = [r for r in top if r["value"] > 0.5]
    status = (
        "no_data"
        if coverage["series_count"] == 0
        else "ok"
        if coverage["window_unbracketed_series_count"] == 0
        else "partial"
    )
    return {
        "status": status,
        "found": len(nonzero) > 0,
        "count": len(nonzero),
        "signal_detected": len(nonzero) > 0,
        "top": [
            {
                "service": r["service"],
                "restarts_in_window": int(round(r["value"])),
                "pods": r["pods"],
            }
            for r in top
        ],
        "query": query,
        "window": {"start": start, "end": end},
        "query_window": {"start": query_start, "end": query_end},
        "coverage": coverage,
        "raw_coverage": _coverage_summary(raw),
        "observed_services": sorted(rollup),
        **collection,
    }


def check_service_readiness(
    prometheus,
    *,
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    post_start: str = "",
    post_end: str = "",
    namespace: str = "sock-shop",
    step: str = "15s",
) -> Dict[str, Any]:
    """Compare per-service pod readiness across bounded run phases."""
    query = (
        "kube_pod_status_ready"
        f'{{namespace="{namespace}",condition="true",job="kube-state-metrics"}}'
    )
    windows = {
        "baseline": (baseline_start, baseline_end),
        "fault": (fault_start, fault_end),
    }
    if post_start and post_end:
        windows["post"] = (post_start, post_end)

    phases: Dict[str, Any] = {}
    for phase, (start, end) in windows.items():
        raw = prometheus.query_range(query, start, end, step)
        if not isinstance(raw, dict) or raw.get("status") == "error":
            phases[phase] = {
                "status": "tool_error",
                "error": raw.get("error") if isinstance(raw, dict) else "no data",
                "coverage": {"series_count": 0, "sample_count": 0},
                "services": [],
            }
            continue

        coverage = _coverage_summary(raw)
        pods: Dict[str, Dict[str, Any]] = {}
        for labels, points in _iter_series(raw):
            pod = str(labels.get("pod") or "")
            if not pod or not points:
                continue
            service = _pod_to_service(pod)
            ready_pct = 100.0 * sum(value >= 0.5 for _, value in points) / len(points)
            ordered_points = sorted(set(points), key=lambda point: point[0])
            transitions = []
            for (previous_time, previous), (current_time, current) in zip(
                ordered_points, ordered_points[1:]
            ):
                if (previous >= 0.5) == (current >= 0.5):
                    continue
                transitions.append(
                    {
                        "transition": (
                            "ready_to_not_ready" if previous >= 0.5 else "not_ready_to_ready"
                        ),
                        "observed_from": _sample_time_iso(previous_time),
                        "observed_to": _sample_time_iso(current_time),
                        "timestamp_precision": "between_scrapes",
                    }
                )
            row = pods.setdefault(service, {"service": service, "pods": []})
            row["pods"].append(
                {
                    "pod": pod,
                    "ready_pct": round(ready_pct, 2),
                    "sample_count": len(points),
                    "transitions": transitions[:20],
                    "omitted_transition_count": max(0, len(transitions) - 20),
                }
            )

        services = []
        for row in pods.values():
            pod_rows = row["pods"]
            services.append(
                {
                    "service": row["service"],
                    "pod_count": len(pod_rows),
                    "ready_mean_pct": round(
                        sum(pod["ready_pct"] for pod in pod_rows) / len(pod_rows), 2
                    ),
                    "pods": pod_rows,
                }
            )
        services.sort(key=lambda row: (row["ready_mean_pct"], row["service"]))
        phases[phase] = {
            "status": "ok" if coverage["series_count"] else "no_data",
            "coverage": coverage,
            "services": services,
            "window": {"start": start, "end": end},
        }

    phase_statuses = [phase["status"] for phase in phases.values()]
    if all(status == "ok" for status in phase_statuses):
        status = "ok"
    elif any(status == "ok" for status in phase_statuses):
        status = "partial"
    elif any(status == "tool_error" for status in phase_statuses):
        status = "tool_error"
    else:
        status = "no_data"
    return {"status": status, "query": query, "step": step, "phases": phases}


def _saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    resource: str,
    namespace: str,
    step: str,
    k: int,
    service: str | None = None,
) -> Dict[str, Any]:
    """Common implementation for the CPU / memory saturation top-K tools.

    ``resource`` ∈ {"cpu", "memory"}. CPU saturation is computed against
    ``container_spec_cpu_quota / container_spec_cpu_period`` (i.e. the
    effective vCPU limit). Memory saturation is the working-set as a
    fraction of the container memory limit. Pods without a limit are
    excluded (no saturation signal exists for them).
    """
    pod_filter = ""
    if service is not None:
        if not re.fullmatch(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", service):
            raise ValueError("invalid service name")
        pod_filter = f',pod=~"^{service}(-[a-z0-9]{{5,}}-[a-z0-9]+|-[0-9]+)?$"'
    if resource == "cpu":
        query = (
            "100 * sum by (pod) ("
            "rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}",cpu="total",job="kubernetes-cadvisor"{pod_filter}}}[1m])'
            ")"
            " / on(pod) group_left() ("
            "sum by (pod) ("
            "(container_spec_cpu_quota"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"{pod_filter}}} > 0)'
            " / container_spec_cpu_period"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"{pod_filter}}}'
            ")"
            ")"
        )
    elif resource == "memory":
        query = (
            "100 * sum by (pod) ("
            "container_memory_working_set_bytes"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"{pod_filter}}}'
            ")"
            " / on(pod) group_left() ("
            "sum by (pod) (container_spec_memory_limit_bytes"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"{pod_filter}}}) > 0'
            ")"
        )
    else:
        raise ValueError(f"unknown resource: {resource!r}")

    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
            "coverage": {"series_count": 0, "sample_count": 0},
        }
    coverage = _coverage_summary(raw)
    # We use ``mean`` for "typical" saturation and ``max`` to flag spikes.
    means = _aggregate_by_service(raw, mode="mean")
    maxes = _aggregate_by_service(raw, mode="max")
    rows: List[Dict[str, Any]] = []
    for svc, m in means.items():
        rows.append(
            {
                "service": svc,
                "saturation_mean_pct": round(m["value"], 2),
                "saturation_max_pct": round(maxes.get(svc, {}).get("value", 0.0), 2),
                "pods": m.get("pods", []),
            }
        )
    rows.sort(key=lambda r: r["saturation_max_pct"], reverse=True)
    return {
        "status": "ok" if coverage["series_count"] else "no_data",
        "found": any(r["saturation_max_pct"] > 50 for r in rows),
        "count": sum(1 for r in rows if r["saturation_max_pct"] > 50),
        "signal_detected": any(r["saturation_max_pct"] > 50 for r in rows),
        "top": rows[:k],
        "query": query,
        "window": {"start": start, "end": end},
        "coverage": coverage,
    }


def get_cpu_saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
    service: str | None = None,
) -> Dict[str, Any]:
    """Top-K services by CPU saturation (% of limit) in ``[start, end]``."""
    return _saturation_top(
        prometheus,
        start=start,
        end=end,
        resource="cpu",
        namespace=namespace,
        step=step,
        k=k,
        service=service,
    )


def get_memory_saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
    service: str | None = None,
) -> Dict[str, Any]:
    """Top-K services by memory saturation (% of limit) in ``[start, end]``."""
    return _saturation_top(
        prometheus,
        start=start,
        end=end,
        resource="memory",
        namespace=namespace,
        step=step,
        k=k,
        service=service,
    )


def check_http_error_rate_top(
    prometheus,
    *,
    start: str,
    end: str,
    step: str = "15s",
    k: int = 5,
) -> Dict[str, Any]:
    """Top-K services by HTTP 5xx error rate inside ``[start, end]``.

    Uses Istio/Envoy request counters so services without app-level Prometheus
    instrumentation are covered. The series is grouped by destination workload,
    which maps directly to the service receiving the HTTP request.
    """
    query = (
        '100 * ('
        'sum by (destination_workload) (rate(istio_requests_total{'
        'destination_workload_namespace="sock-shop",destination_workload!="unknown",'
        'response_code=~"5.."}[1m])) '
        'or on(destination_workload) 0 * sum by (destination_workload) '
        '(rate(istio_requests_total{destination_workload_namespace="sock-shop",'
        'destination_workload!="unknown"}[1m]))'
        ') / clamp_min(sum by (destination_workload) (rate(istio_requests_total{'
        'destination_workload_namespace="sock-shop",destination_workload!="unknown"}[1m])), 0.0001)'
    )
    return _service_level_series_top(
        prometheus,
        query=query,
        start=start,
        end=end,
        step=step,
        k=k,
        mean_key="error_rate_mean_pct",
        max_key="error_rate_max_pct",
        found_floor=1.0,
    )


def check_request_latency_top(
    prometheus,
    *,
    start: str,
    end: str,
    step: str = "15s",
    k: int = 5,
) -> Dict[str, Any]:
    """Top-K services by request latency P95 inside ``[start, end]``.

    This is the main high-level HTTP-path symptom a small RCA agent should look
    at before diving into raw traces or logs.
    """
    query = (
        'histogram_quantile(0.95, '
        'sum(rate(istio_request_duration_milliseconds_bucket{'
        'destination_workload_namespace="sock-shop",destination_workload!="unknown"}'
        '[1m])) by (le, destination_workload)'
        ') / 1000'
    )
    return _service_level_series_top(
        prometheus,
        query=query,
        start=start,
        end=end,
        step=step,
        k=k,
        mean_key="latency_p95_mean_seconds",
        max_key="latency_p95_max_seconds",
        found_floor=0.25,
    )


def _golden_metric_query(metric: str, namespace: str) -> str:
    escaped_namespace = namespace.replace("\\", "\\\\").replace('"', '\\"')
    if metric == "request_rate":
        return (
            "sum by (destination_workload) (rate(istio_requests_total{"
            f'destination_workload_namespace="{escaped_namespace}",'
            'destination_workload!="unknown"}[1m]))'
        )
    if metric == "error_rate":
        return (
            "100 * ("
            "sum by (destination_workload) (rate(istio_requests_total{"
            f'destination_workload_namespace="{escaped_namespace}",'
            'destination_workload!="unknown",response_code=~"5.."}[1m])) '
            "or on(destination_workload) 0 * sum by (destination_workload) "
            "(rate(istio_requests_total{"
            f'destination_workload_namespace="{escaped_namespace}",'
            'destination_workload!="unknown"}[1m]))'
            ") / clamp_min(sum by (destination_workload) (rate(istio_requests_total{"
            f'destination_workload_namespace="{escaped_namespace}",'
            'destination_workload!="unknown"}[1m])), 0.0001)'
        )
    if metric == "latency_p95":
        return (
            "histogram_quantile(0.95, sum(rate("
            "istio_request_duration_milliseconds_bucket{"
            f'destination_workload_namespace="{escaped_namespace}",'
            'destination_workload!="unknown"}[1m])) '
            "by (le, destination_workload)) / 1000"
        )
    if metric == "cpu_saturation_pct":
        return (
            "100 * sum by (pod) (rate(container_cpu_usage_seconds_total"
            f'{{namespace="{escaped_namespace}",cpu="total",job="kubernetes-cadvisor"}}[1m]))'
            " / on(pod) group_left() (sum by (pod) ((container_spec_cpu_quota"
            f'{{namespace="{escaped_namespace}",job="kubernetes-cadvisor"}} > 0)'
            " / container_spec_cpu_period"
            f'{{namespace="{escaped_namespace}",job="kubernetes-cadvisor"}}))'
        )
    if metric == "memory_saturation_pct":
        return (
            "100 * sum by (pod) (container_memory_working_set_bytes"
            f'{{namespace="{escaped_namespace}",job="kubernetes-cadvisor"}})'
            " / on(pod) group_left() (sum by (pod) (container_spec_memory_limit_bytes"
            f'{{namespace="{escaped_namespace}",job="kubernetes-cadvisor"}} > 0))'
        )
    raise ValueError(f"unknown golden metric: {metric!r}")


def _golden_phase_values(
    prometheus,
    *,
    query: str,
    start: str,
    end: str,
    step: str,
) -> Dict[str, Any]:
    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "error": raw.get("error") if isinstance(raw, dict) else "invalid response",
            "series_count": 0,
            "sample_count": 0,
            "services": {},
        }

    sample_count = 0
    series_count = 0
    for _, points in _iter_series(raw):
        if points:
            series_count += 1
            sample_count += len(points)

    if not series_count:
        return {
            "status": "no_data",
            "series_count": 0,
            "sample_count": 0,
            "services": {},
        }

    means = _aggregate_by_service(raw, mode="mean")
    maxes = _aggregate_by_service(raw, mode="max")
    services = {
        service: {
            "mean": means[service]["value"],
            "max": maxes.get(service, {}).get("value", 0.0),
        }
        for service in means
    }
    return {
        "status": "ok",
        "series_count": series_count,
        "sample_count": sample_count,
        "services": services,
    }


def get_run_golden_signals(
    prometheus,
    *,
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 10,
) -> Dict[str, Any]:
    """Compare RED and resource-saturation signals across run phases."""
    windows = {
        "baseline": {"start": baseline_start, "end": baseline_end},
        "fault": {"start": fault_start, "end": fault_end},
    }
    definitions = {
        "request_rate": "req/s",
        "error_rate": "%",
        "latency_p95": "s",
        "cpu_saturation_pct": "%",
        "memory_saturation_pct": "%",
    }
    metrics: Dict[str, Any] = {}
    for metric, unit in definitions.items():
        query = _golden_metric_query(metric, namespace)
        phases = {
            phase: _golden_phase_values(
                prometheus,
                query=query,
                start=window["start"],
                end=window["end"],
                step=step,
            )
            for phase, window in windows.items()
        }
        baseline = phases["baseline"]
        fault = phases["fault"]
        if "tool_error" in {baseline["status"], fault["status"]}:
            status = "tool_error"
        elif baseline["status"] == "no_data" and fault["status"] == "no_data":
            status = "no_data"
        elif baseline["status"] != "ok" or fault["status"] != "ok":
            status = "partial"
        else:
            status = "ok"

        rows = []
        for service in set(baseline["services"]) | set(fault["services"]):
            before = baseline["services"].get(service)
            during = fault["services"].get(service)
            delta = round(during["mean"] - before["mean"], 4) if before and during else None
            delta_pct = (
                round(100.0 * delta / abs(before["mean"]), 2)
                if delta is not None and before["mean"] != 0
                else None
            )
            rows.append(
                {
                    "service": service,
                    "baseline_mean": before["mean"] if before else None,
                    "fault_mean": during["mean"] if during else None,
                    "baseline_max": before["max"] if before else None,
                    "fault_max": during["max"] if during else None,
                    "delta_abs": delta,
                    "delta_pct": delta_pct,
                }
            )
        rows.sort(
            key=lambda row: abs(row["delta_abs"]) if row["delta_abs"] is not None else -1,
            reverse=True,
        )
        metrics[metric] = {
            "status": status,
            "signal_detected": any(
                row["delta_abs"] is not None and abs(row["delta_abs"]) > 0.0001
                for row in rows
            ),
            "unit": unit,
            "query": query,
            "phases": {
                phase: {
                    "window": windows[phase],
                    "status": result["status"],
                    "series_count": result["series_count"],
                    "sample_count": result["sample_count"],
                    **({"error": result["error"]} if result.get("error") else {}),
                }
                for phase, result in phases.items()
            },
            "top": rows[:k],
        }

    metric_statuses = {result["status"] for result in metrics.values()}
    status = (
        "ok" if metric_statuses == {"ok"}
        else "tool_error" if "tool_error" in metric_statuses
        else "partial" if "ok" in metric_statuses or "partial" in metric_statuses
        else "no_data"
    )
    return {
        "status": status,
        "namespace": namespace,
        "step": step,
        "phases": windows,
        "metrics": metrics,
    }


def check_network_packet_drops(
    prometheus,
    *,
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    step: str = "15s",
    k: int = 5,
) -> Dict[str, Any]:
    """Compare node-interface packet-drop rates across baseline and fault windows."""
    windows = {
        "baseline": {"start": baseline_start, "end": baseline_end},
        "fault": {"start": fault_start, "end": fault_end},
    }
    definitions = {
        "network_receive_dropped_rate": "container_network_receive_packets_dropped_total",
        "network_transmit_dropped_rate": "container_network_transmit_packets_dropped_total",
    }
    metrics: Dict[str, Any] = {}
    for metric, counter in definitions.items():
        query = (
            f'sum by (interface) (rate({counter}'
            '{job="kubernetes-cadvisor"}[30s]))'
        )

        phases = {}
        for phase, window in windows.items():
            raw = prometheus.query_range(
                query,
                start=window["start"],
                end=window["end"],
                step=step,
            )
            if not isinstance(raw, dict) or raw.get("status") == "error":
                phases[phase] = {
                    "status": "tool_error",
                    "error": raw.get("error") if isinstance(raw, dict) else "invalid response",
                    "series_count": 0,
                    "sample_count": 0,
                    "interfaces": {},
                }
                continue

            grouped: Dict[str, List[float]] = {}
            series_count = 0
            sample_count = 0
            for labels, points in _iter_series(raw):
                if not points:
                    continue
                interface = str(
                    labels.get("interface")
                    or labels.get("device")
                    or labels.get("instance")
                    or "unlabeled"
                )
                values = [value for _, value in points]
                grouped.setdefault(interface, []).extend(values)
                series_count += 1
                sample_count += len(values)

            phases[phase] = {
                "status": "ok" if series_count else "no_data",
                "series_count": series_count,
                "sample_count": sample_count,
                "interfaces": {
                    interface: {
                        "mean": sum(values) / len(values),
                        "max": max(values),
                    }
                    for interface, values in grouped.items()
                },
            }

        baseline = phases["baseline"]
        fault = phases["fault"]
        if "tool_error" in {baseline["status"], fault["status"]}:
            status = "tool_error"
        elif baseline["status"] == "no_data" and fault["status"] == "no_data":
            status = "no_data"
        elif baseline["status"] != "ok" or fault["status"] != "ok":
            status = "partial"
        else:
            status = "ok"

        rows = []
        for interface in set(baseline["interfaces"]) | set(fault["interfaces"]):
            before = baseline["interfaces"].get(interface)
            during = fault["interfaces"].get(interface)
            delta = round(during["mean"] - before["mean"], 4) if before and during else None
            rows.append(
                {
                    "interface": interface,
                    "baseline_mean": before["mean"] if before else None,
                    "fault_mean": during["mean"] if during else None,
                    "fault_max": during["max"] if during else None,
                    "delta_abs": delta,
                }
            )
        rows.sort(
            key=lambda row: row["delta_abs"] if row["delta_abs"] is not None else -1,
            reverse=True,
        )
        metrics[metric] = {
            "status": status,
            "unit": "packets/s",
            "signal_detected": any(
                row["delta_abs"] is not None and row["delta_abs"] > 0.01
                for row in rows
            ),
            "phases": {
                phase: {
                    "sample_count": result["sample_count"],
                    "series_count": result["series_count"],
                    "status": result["status"],
                }
                for phase, result in phases.items()
            },
            "top": rows[:k],
        }

    statuses = {metric["status"] for metric in metrics.values()}
    status = (
        "ok" if statuses == {"ok"}
        else "tool_error" if "tool_error" in statuses
        else "partial" if "ok" in statuses or "partial" in statuses
        else "no_data"
    )
    return {
        "status": status,
        "found": any(metric["signal_detected"] for metric in metrics.values()),
        "step": step,
        "phases": windows,
        "interpretation_limit": "node-interface evidence does not identify an application service or prove root cause by itself",
        "metrics": metrics,
    }
