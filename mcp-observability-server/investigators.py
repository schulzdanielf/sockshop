"""High-level *investigator* primitives for the observability MCP server.

These tools wrap raw PromQL queries with a stable, business-oriented API:
instead of asking "give me the PromQL result of X", an agent (or human)
asks "did any pod get OOMKilled between t0 and t1?" and receives a tidy
JSON answer with `found`, the list of pods, evidence values and the
exact query used.

Design goals
------------
* **Window safety.** Every tool takes explicit ``start``/``end`` ISO
  timestamps. Range queries are bounded by those — never use
  ``increase(...[1h])`` or any open-ended lookback, because that would
  smear counters from earlier experiments into the current window.
    Counter-based signals (restart counts) are derived from per-container
    counter increments on a range_query restricted to ``[start, end]``.
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
                points.append((float(p[0]), float(p[1])))
            except (ValueError, TypeError, IndexError):
                continue
        yield labels, points


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
            * ``"mean"`` — average across the window per pod, then average
                pod percentages per service. Use for saturation percentages.
    """
    per_pod: Dict[str, Dict[str, Any]] = {}
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
        else:
            raise ValueError(f"unknown aggregation mode: {mode!r}")
        per_pod[pod] = {
            "pod": pod,
            "service": _pod_to_service(pod),
            "value": round(value, 4),
        }

    per_service: Dict[str, Dict[str, Any]] = {}
    for entry in per_pod.values():
        svc = entry["service"]
        bucket = per_service.setdefault(svc, {"service": svc, "value": 0.0, "pods": []})
        if mode == "max":
            bucket["value"] = max(bucket["value"], entry["value"])
        else:
            bucket["value"] += entry["value"]
        bucket["pods"].append({"pod": entry["pod"], "value": entry["value"]})
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


def _restart_rollup(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Sum reset-aware counter increments by pod and service.

    kube-state-metrics can be scraped through more than one Prometheus job.
    The query selects its canonical job; the check here also guards test or
    nonstandard payloads from counting duplicate scrape sources.
    """
    per_pod: Dict[str, float] = {}
    for labels, points in _iter_series(payload):
        if labels.get("job") not in (None, "kube-state-metrics"):
            continue
        pod = str(labels.get("pod") or "")
        if not pod or pod == "unknown":
            continue
        per_pod[pod] = per_pod.get(pod, 0.0) + _counter_increments(points)

    per_service: Dict[str, Dict[str, Any]] = {}
    for pod, count in per_pod.items():
        service = _pod_to_service(pod)
        row = per_service.setdefault(
            service, {"service": service, "value": 0.0, "pods": []}
        )
        row["value"] += count
        row["pods"].append({"pod": pod, "value": round(count, 4)})
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
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
        }

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
        "found": any(r[max_key] > found_floor for r in rows),
        "count": sum(1 for r in rows if r[max_key] > found_floor),
        "top": rows[:k],
        "query": query,
        "window": {"start": start, "end": end},
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

    Uses ``kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}``,
    a 0/1 gauge. The range query lets us see the 0→1 transition that
    happens at the OOM kill — pods that were already OOMKilled before
    ``start`` would still have value=1 throughout the window, so we
    compare ``max - min`` to confirm the transition actually occurred
    inside the window (and additionally report ``max`` so an agent can
    distinguish "OOMed in this run" from "OOMed earlier and still in
    that state").
    """
    query = (
        "sum by (pod) ("
        "kube_pod_container_status_last_terminated_reason"
        f'{{namespace="{namespace}",reason="OOMKilled"}})'
    )
    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
        }
    transitions = _aggregate_by_service(raw, mode="max_minus_min")
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
        "found": len(transitioned) > 0,
        "count": len(transitioned),
        "top": rows[:5],
        "query": query,
        "window": {"start": start, "end": end},
    }


def check_pod_restarts(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
) -> Dict[str, Any]:
    """Return per-service restart counts *strictly* within ``[start, end]``.

    We avoid ``increase(...[1h])`` because the 1h lookback would include
    restarts from previous experiments. We range-query only the canonical
    kube-state-metrics scrape, then sum reset-aware adjacent counter
    increments per container inside the requested window. This avoids
    treating duplicate scrape-source gaps as counter changes.
    """
    query = (
        "kube_pod_container_status_restarts_total"
        f'{{namespace="{namespace}",job="kube-state-metrics"}}'
    )
    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
        }
    rollup = _restart_rollup(raw)
    # Return all observed services so a targeted question can distinguish a
    # zero count from a service omitted by a top-5 cutoff.
    top = _top_k(rollup, k=len(rollup))
    nonzero = [r for r in top if r["value"] > 0.5]
    return {
        "found": len(nonzero) > 0,
        "count": len(nonzero),
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
    }


def _saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    resource: str,
    namespace: str,
    step: str,
    k: int,
) -> Dict[str, Any]:
    """Common implementation for the CPU / memory saturation top-K tools.

    ``resource`` ∈ {"cpu", "memory"}. CPU saturation is computed against
    ``container_spec_cpu_quota / container_spec_cpu_period`` (i.e. the
    effective vCPU limit). Memory saturation is the working-set as a
    fraction of the container memory limit. Pods without a limit are
    excluded (no saturation signal exists for them).
    """
    if resource == "cpu":
        query = (
            "100 * sum by (pod) ("
            "rate(container_cpu_usage_seconds_total"
            f'{{namespace="{namespace}",cpu="total"}}[1m])'
            ")"
            " / on(pod) group_left() ("
            "sum by (pod) ("
            "container_spec_cpu_quota"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"}}'
            " / container_spec_cpu_period"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"}}'
            ")"
            ")"
        )
    elif resource == "memory":
        query = (
            "100 * sum by (pod) ("
            "container_memory_working_set_bytes"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"}}'
            ")"
            " / on(pod) group_left() ("
            "container_spec_memory_limit_bytes"
            f'{{namespace="{namespace}",job="kubernetes-cadvisor"}} > 0'
            ")"
        )
    else:
        raise ValueError(f"unknown resource: {resource!r}")

    raw = prometheus.query_range(query, start, end, step)
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "found": False,
            "count": 0,
            "top": [],
            "error": raw.get("error") if isinstance(raw, dict) else "no data",
            "query": query,
            "window": {"start": start, "end": end},
        }
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
        "found": any(r["saturation_max_pct"] > 50 for r in rows),
        "count": sum(1 for r in rows if r["saturation_max_pct"] > 50),
        "top": rows[:k],
        "query": query,
        "window": {"start": start, "end": end},
    }


def get_cpu_saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
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
    )


def get_memory_saturation_top(
    prometheus,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    step: str = "15s",
    k: int = 3,
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
