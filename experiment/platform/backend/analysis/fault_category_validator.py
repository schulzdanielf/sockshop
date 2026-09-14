"""FaultCategoryValidator — rule-based post-processor for the LLM verdict.

Motivation
----------
On the memory-hog eval, the hybrid RAG pipeline reaches **100 % top-1
service accuracy** (the *where*) but only **17–50 % fault-category
accuracy** (the *why*). The pattern of confusion is consistent with a
small model that has been given enough context to localise the bad
service but not enough discriminative *cause* signals — confidence is
high in both correct and incorrect category answers, so the LLM itself
cannot self-correct.

This validator runs **after** the LLM produces its parsed verdict. It
looks at a handful of unambiguous evidence channels already captured
in ``features["metric_hotspots"]`` — OOMKilled gauges, restart-counter
deltas, CPU / memory saturation percentages — and overrides ONLY
``fault_category`` (never ``rca``, never ``confidence``, never anything
else) when the evidence is strong and unambiguous.

Design rules
------------
* **Scope.** Override at most one field (``fault_category``). Leave the
  LLM's free-form reasoning intact so we can later audit *why* the
  model picked the wrong category.
* **Evidence-driven.** Rules trigger on per-label hotspot rows already
  computed by ``MCPPrometheusMetricsPlugin.per_label_hotspots`` —
  values are window-bounded (baseline vs fault phase), so we never
  pick up signals from earlier experiments.
* **Conflict aware.** If more than one category fires (e.g. CPU
  saturation AND OOMKill in the same window) we record the conflict
  in ``validator_meta`` and **do not override** — better to keep the
  LLM's guess than to introduce noise.
* **Always traceable.** The output ``analysis`` dict gains a
  ``validator_meta`` block with: ``fired`` (bool), ``rule`` (str or
  None), ``original_fault_category``, ``new_fault_category``,
  ``evidence`` (the supporting rows), ``conflict`` (None or list of
  rules that all matched).
* **Idempotent.** Calling the validator twice on its own output is a
  no-op (we read the original from ``validator_meta`` if present).

Vocabulary
----------
Output fault categories match the harness ground truth:
``memory-exhaustion``, ``cpu-exhaustion``, ``pod-failure``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ── Thresholds ──────────────────────────────────────────────────────────
# All thresholds are conservative: we'd rather miss a correction than
# overwrite a correct LLM verdict. Tune via observation on evaluation.csv.

# OOMKilled is a 0/1 gauge per container. A baseline→fault Δ > 0.4
# means at least one container actually transitioned (an already-1
# baseline would show Δ ≈ 0; a pure-noise series caps well below 0.4).
_OOM_DELTA_MIN = 0.4

# Pod restart counter (cumulative). A baseline-to-fault Δ ≥ 1 restart
# is a strong signal; we use 0.5 to tolerate fractional means while
# still rejecting noise.
_RESTART_DELTA_MIN = 0.5

# CPU saturation > 80 % of limit during fault phase is the empirical
# floor for a chaos-induced CPU exhaustion in this cluster.
_CPU_SAT_FAULT_MIN = 80.0

# CPU throttled seconds-per-second — non-zero throttling during fault
# is a direct cgroup-level signal that the pod hit its CPU limit.
_CPU_THROTTLE_DELTA_MIN = 0.10

# Memory saturation > 90 % of limit signals imminent / actual OOM
# pressure even when the OOMKilled gauge hasn't fired yet (e.g. the
# kernel reclaim kept up just enough to avoid kill).
_MEM_SAT_FAULT_MIN = 90.0

# Network issues are usually observable as a rise in request latency, not
# only as a rise in error_count. A service whose p95 latency rises above
# 300ms during the fault window is a strong network-latency indicator.
_LATENCY_FAULT_MIN = 0.30
_LATENCY_DELTA_MIN = 0.10

# Explicit packet-drop signal for network-loss when cAdvisor exposes it.
_NETWORK_DROP_DELTA_MIN = 0.01

# HTTP failures usually show up as a jump in 5xx / request error rate.
# The threshold is intentionally modest to avoid overfitting to a single
# run while still catching the kinds of failures seen in the experiment.
_HTTP_ERROR_RATE_MIN = 0.05
_HTTP_ERROR_DELTA_MIN = 0.02

# OOM-restart fingerprint — fallback when ``oom_killed`` was not
# collected (older runs). Signature: the pod restarts repeatedly (OOM
# kill) while its memory saturation *drops* (each restart resets the
# working set to near-zero, pulling the fault-phase mean below baseline).
# Two thresholds: (a) pod_restarts rate delta must be positive and
# above the noise floor; (b) the same service's memory_saturation_pct
# must have dropped by at least this many percentage points.
_OOM_RESTART_DELTA_MIN = 0.15
_OOM_MEM_DROP_MIN = 5.0


# ── Helpers ─────────────────────────────────────────────────────────────
def _top_rows(hotspots: Dict[str, Any], metric_id: str) -> List[Dict[str, Any]]:
    entry = hotspots.get(metric_id) or {}
    rows = entry.get("top") or []
    return [r for r in rows if isinstance(r, dict)]


def _max_delta(rows: List[Dict[str, Any]]) -> float:
    """Largest absolute delta across rows. ``0.0`` if no row qualifies.

    Uses ``abs(delta_abs)`` so this also catches **monotonic counters
    that decrease** within the window. That happens specifically when a
    pod is deleted and replaced: the old pod's high counter disappears
    from the series and the new pod starts at zero, so the per-service
    mean (after pod→service normalisation) *drops*. A negative delta on
    a cumulative restart counter is therefore the signature of a pod
    replacement — exactly the chaos we want to flag as pod-failure.
    """
    best = 0.0
    for r in rows:
        try:
            v = abs(float(r.get("delta_abs") or 0.0))
        except (TypeError, ValueError):
            continue
        if v > best:
            best = v
    return best


def _max_fault_mean(rows: List[Dict[str, Any]]) -> float:
    """Largest fault-phase mean across rows. ``0.0`` if no row qualifies."""
    best = 0.0
    for r in rows:
        try:
            v = float(r.get("fault_mean") or 0.0)
        except (TypeError, ValueError):
            continue
        if v > best:
            best = v
    return best


def _summarise_evidence(
    metric_id: str,
    rows: List[Dict[str, Any]],
    *,
    keep: int = 2,
) -> List[Dict[str, Any]]:
    """Compact rows for inclusion in ``validator_meta`` (avoid bloat)."""
    out: List[Dict[str, Any]] = []
    for r in rows[:keep]:
        out.append(
            {
                "metric": metric_id,
                "label": r.get("label"),
                "baseline_mean": r.get("baseline_mean"),
                "fault_mean": r.get("fault_mean"),
                "delta_abs": r.get("delta_abs"),
            }
        )
    return out


# ── Rules ───────────────────────────────────────────────────────────────
def _check_memory_exhaustion(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Fire on OOMKilled transition, sustained memory pressure > 90 %,
    or OOM-restart fingerprint (restarts + memory drop, for runs where
    ``oom_killed`` was not collected)."""
    # Primary: oom_killed gauge transition.
    oom = _top_rows(hotspots, "oom_killed")
    if _max_delta(oom) > _OOM_DELTA_MIN:
        return True, _summarise_evidence("oom_killed", oom)

    # Primary: sustained memory pressure.
    mem = _top_rows(hotspots, "memory_saturation_pct")
    if _max_fault_mean(mem) > _MEM_SAT_FAULT_MIN:
        return True, _summarise_evidence("memory_saturation_pct", mem)

    # Fallback: OOM-restart fingerprint (``oom_killed`` absent).
    # Condition: pod_restarts rate delta > floor (OOM kills the pod, it
    # restarts) AND the same service's memory_saturation_pct delta is
    # negative (each restart resets working-set, lowering fault-phase mean).
    restarts = _top_rows(hotspots, "pod_restarts")
    if restarts:
        top_r = restarts[0]
        try:
            r_delta = float(top_r.get("delta_abs") or 0.0)
        except (TypeError, ValueError):
            r_delta = 0.0
        if r_delta > _OOM_RESTART_DELTA_MIN:
            mem_by_label = {r.get("label"): r for r in mem}
            mem_row = mem_by_label.get(top_r.get("label")) or {}
            try:
                m_delta = float(mem_row.get("delta_abs") or 0.0)
            except (TypeError, ValueError):
                m_delta = 0.0
            if m_delta < -_OOM_MEM_DROP_MIN:
                evidence = _summarise_evidence(
                    "pod_restarts", [top_r]
                ) + _summarise_evidence("memory_saturation_pct", [mem_row])
                return True, evidence

    return False, []


def _check_cpu_exhaustion(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Fire on sustained CPU saturation > 80 % or CPU throttling > 0.1 s/s.

    Suppressed when an OOMKill is also present (``oom_killed`` gauge or
    the OOM-restart fingerprint): kernel memory reclaim drives transient
    CPU pressure (and throttling) on the same pod, so a coincident CPU
    spike is downstream of the OOM — not an independent CPU exhaustion.
    The memory rule wins in that case.
    """
    # Suppress on oom_killed gauge.
    oom = _top_rows(hotspots, "oom_killed")
    if _max_delta(oom) > _OOM_DELTA_MIN:
        return False, []

    # Suppress on OOM-restart fingerprint (even when oom_killed absent).
    restarts = _top_rows(hotspots, "pod_restarts")
    if restarts:
        top_r = restarts[0]
        try:
            r_delta = float(top_r.get("delta_abs") or 0.0)
        except (TypeError, ValueError):
            r_delta = 0.0
        if r_delta > _OOM_RESTART_DELTA_MIN:
            mem = _top_rows(hotspots, "memory_saturation_pct")
            mem_by_label = {r.get("label"): r for r in mem}
            mem_row = mem_by_label.get(top_r.get("label")) or {}
            try:
                m_delta = float(mem_row.get("delta_abs") or 0.0)
            except (TypeError, ValueError):
                m_delta = 0.0
            if m_delta < -_OOM_MEM_DROP_MIN:
                return False, []

    evidence: List[Dict[str, Any]] = []
    sat = _top_rows(hotspots, "cpu_saturation_pct")
    if _max_fault_mean(sat) > _CPU_SAT_FAULT_MIN:
        evidence.extend(_summarise_evidence("cpu_saturation_pct", sat))
    throttle = _top_rows(hotspots, "cpu_throttled")
    if _max_delta(throttle) > _CPU_THROTTLE_DELTA_MIN:
        evidence.extend(_summarise_evidence("cpu_throttled", throttle))
    return bool(evidence), evidence


def _check_pod_failure(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Fire on a restart-counter excursion, with NO concurrent OOM signal.

    Two failure modes both trigger the rule:

    * **Restart in place** — the same pod restarts (e.g. liveness probe
      kill), so the cumulative restart counter *increases*. Positive
      ``delta_abs``.
    * **Pod replacement** — the pod is deleted (litmus pod-delete
      chaos), a fresh pod with a new name takes over with counter
      starting at zero. After ``_pod_to_service`` aggregation, the
      per-service mean *drops* relative to baseline. Negative
      ``delta_abs`` (caught by ``_max_delta``'s ``abs``).

    The OOM guard remains: OOM-driven restarts are owned by the
    memory rule, not by this one.
    """
    restarts = _top_rows(hotspots, "pod_restarts_total")
    if _max_delta(restarts) <= _RESTART_DELTA_MIN:
        return False, []
    oom = _top_rows(hotspots, "oom_killed")
    if _max_delta(oom) > _OOM_DELTA_MIN:
        return False, []
    evidence = _summarise_evidence("pod_restarts_total", restarts)
    return True, evidence


def _check_network_latency(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Network-latency faults appear as a sustained latency spike.

    This is intentionally conservative: it only fires when a service's
    p95/p99 latency rises materially above the baseline and there is no
    stronger memory/CPU/OOM signal competing for the same run.
    """
    latency_rows = _top_rows(hotspots, "latency_p95")
    latency_rows += _top_rows(hotspots, "latency_p99")
    if not latency_rows:
        return False, []

    fault_mean = _max_fault_mean(latency_rows)
    delta = _max_delta(latency_rows)
    if fault_mean <= _LATENCY_FAULT_MIN and delta <= _LATENCY_DELTA_MIN:
        return False, []

    evidence = _summarise_evidence("latency_p95", latency_rows)
    return True, evidence


def _check_network_loss(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Detect packet-loss faults from interface packet-drop counters."""
    rows = _top_rows(hotspots, "network_receive_dropped_rate")
    rows += _top_rows(hotspots, "network_transmit_dropped_rate")
    if not rows:
        return False, []
    if _max_delta(rows) <= _NETWORK_DROP_DELTA_MIN:
        return False, []
    return True, _summarise_evidence("network_receive_dropped_rate", rows)


def _check_http_error(
    hotspots: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """HTTP faults are usually the result of elevated error-rate or 5xx.

    We treat a significant increase in per-service error_rate as the
    discriminative signal for http-error, even when the overall run also
    shows network latency.
    """
    err_rows = _top_rows(hotspots, "error_rate")
    if not err_rows:
        return False, []

    fault_mean = _max_fault_mean(err_rows)
    delta = _max_delta(err_rows)
    if fault_mean <= _HTTP_ERROR_RATE_MIN and delta <= _HTTP_ERROR_DELTA_MIN:
        return False, []

    evidence = _summarise_evidence("error_rate", err_rows)
    return True, evidence


# ── Public API ──────────────────────────────────────────────────────────
def validate_fault_category(
    analysis: Dict[str, Any],
    features: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return ``analysis`` augmented with a validator decision.

    Parameters
    ----------
    analysis:
        The parsed LLM verdict (output of ``parse_verdict_response``
        merged with metadata in ``api.run_llm_analysis``). Mutated and
        returned for convenience.
    features:
        The per-run L1 features dict (output of
        ``storage.get_run_features``). Must contain ``metric_hotspots``
        in the format produced by ``MCPPrometheusMetricsPlugin``.

    Returns
    -------
    The same ``analysis`` dict with two changes:

    * ``analysis["fault_category"]`` may be overridden when a single
      rule fires unambiguously.
    * ``analysis["validator_meta"]`` is always populated with the
      decision trace, so callers can audit the validator post-hoc.
    """
    # Preserve the *original* LLM answer across idempotent calls. If the
    # validator already ran (e.g. cached re-fetch), use the recorded
    # original; otherwise capture it now.
    prior_meta = analysis.get("validator_meta") or {}
    original_fault = prior_meta.get("original_fault_category")
    if original_fault is None:
        original_fault = analysis.get("fault_category")

    hotspots: Dict[str, Any] = {}
    if isinstance(features, dict):
        raw = features.get("metric_hotspots")
        if isinstance(raw, dict):
            hotspots = raw

    rule_results: List[Tuple[str, List[Dict[str, Any]]]] = []
    for rule_name, checker in (
        ("memory-exhaustion", _check_memory_exhaustion),
        ("cpu-exhaustion", _check_cpu_exhaustion),
        ("pod-failure", _check_pod_failure),
        ("network-loss", _check_network_loss),
        ("network-latency", _check_network_latency),
        ("http-error", _check_http_error),
    ):
        fired, evidence = checker(hotspots)
        if fired:
            rule_results.append((rule_name, evidence))

    meta: Dict[str, Any] = {
        "fired": False,
        "rule": None,
        "original_fault_category": original_fault,
        "new_fault_category": original_fault,
        "evidence": [],
        "conflict": None,
        "thresholds": {
            "oom_delta_min": _OOM_DELTA_MIN,
            "restart_delta_min": _RESTART_DELTA_MIN,
            "cpu_sat_fault_min": _CPU_SAT_FAULT_MIN,
            "cpu_throttle_delta_min": _CPU_THROTTLE_DELTA_MIN,
            "mem_sat_fault_min": _MEM_SAT_FAULT_MIN,
            "latency_fault_min": _LATENCY_FAULT_MIN,
            "latency_delta_min": _LATENCY_DELTA_MIN,
            "network_drop_delta_min": _NETWORK_DROP_DELTA_MIN,
            "http_error_rate_min": _HTTP_ERROR_RATE_MIN,
            "http_error_delta_min": _HTTP_ERROR_DELTA_MIN,
            "oom_restart_delta_min": _OOM_RESTART_DELTA_MIN,
            "oom_mem_drop_min": _OOM_MEM_DROP_MIN,
        },
    }

    if not rule_results:
        # No discriminative signal — keep the LLM verdict, record absence.
        meta["reason"] = "no rule matched (insufficient or absent signals)"
        analysis["validator_meta"] = meta
        return analysis

    if len(rule_results) > 1:
        # Multiple categories matched — too ambiguous to safely override.
        # The LLM's guess is at least informed by neighbours; ours would
        # be arbitrary.
        meta["conflict"] = [name for name, _ in rule_results]
        meta["evidence"] = [ev for _, evs in rule_results for ev in evs]
        meta["reason"] = "multiple categories matched — keeping LLM verdict"
        analysis["validator_meta"] = meta
        return analysis

    # Exactly one rule fired — override.
    rule_name, evidence = rule_results[0]
    meta["fired"] = True
    meta["rule"] = rule_name
    meta["new_fault_category"] = rule_name
    meta["evidence"] = evidence
    meta["reason"] = (
        f"single rule matched ({rule_name}); overriding LLM verdict "
        f"{original_fault!r} → {rule_name!r}"
    )
    analysis["fault_category"] = rule_name
    analysis["validator_meta"] = meta
    return analysis
