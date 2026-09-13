"""ServiceLocalizerValidator — rule-based post-processor for the *where*.

Companion to ``fault_category_validator``: the fault validator owns the
"why" (which fault category), this one owns the "where" (which service
was the root cause). It runs **after** the fault validator so it can
anchor its signal selection on the (possibly already corrected)
``fault_category``.

Why a separate module
---------------------
Service localisation is a different problem from cause classification.
For each fault category there is exactly one *unambiguous* metric whose
top label is the root-cause service:

* ``memory-exhaustion`` → service with the largest ``oom_killed`` Δ.
  If no OOM Δ but mem_sat>90 fires, fall back to the top memory
  saturator.
* ``cpu-exhaustion`` → service with the largest ``cpu_throttled`` Δ
  (cgroup-level, exact). Fall back to ``cpu_saturation_pct`` top.
* ``pod-failure`` → service with the largest ``|pod_restarts_total|``
  Δ (absolute, to catch both restart-in-place and pod replacement).

These anchors are *causally* the right ones because each chaos type
*directly* perturbs that exact metric on the target pod; the
classification LLM is free to wander into downstream blast-radius
services (cf. run-26a89230f1f9: shipping was pod-deleted, but the LLM
picked ``carts`` because carts had the highest CPU during the cascade).

Safety guards
-------------
* The validator only overrides ``rca`` when there is a **single
  dominant** anchor label. If the anchor metric returns ties or no
  top row at all, we keep the LLM's verdict.
* Only fires for the three fault categories above. Any other
  ``fault_category`` (e.g. ``unknown``, ``parse error``, free-text
  the LLM invented) → no override.
* Records the decision under ``analysis.validator_meta.localizer``
  with the same shape as the fault validator's ``validator_meta``
  (fired, rule, original_rca, new_rca, evidence, reason).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Minimal absolute signal to claim a dominant anchor. Below this we
# decline to override — better to keep the LLM than guess from noise.
_MIN_DOMINANT_ABS_DELTA = 0.5
_MIN_DOMINANT_FAULT_MEAN_PCT = 50.0

# Tie tolerance: if the runner-up's absolute signal is within this
# fraction of the leader's, we treat the result as ambiguous.
_TIE_TOLERANCE = 0.25

# OOM-restart fingerprint thresholds (mirrors fault_category_validator).
# Used as a third fallback in _anchor_memory when oom_killed is absent
# and memory_saturation_pct fault_mean is below the 50 % floor.
_OOM_RESTART_DELTA_MIN = 0.15
_OOM_MEM_DROP_MIN = 5.0


def _normalise_category(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _top_rows(hotspots: Dict[str, Any], metric_id: str) -> List[Dict[str, Any]]:
    entry = hotspots.get(metric_id) or {}
    rows = entry.get("top") or []
    return [r for r in rows if isinstance(r, dict) and r.get("label")]


def _rank_by(
    rows: List[Dict[str, Any]],
    field: str,
    *,
    use_abs: bool,
) -> List[Tuple[str, float, Dict[str, Any]]]:
    """Return ``[(label, score, row), ...]`` sorted by score desc."""
    scored: List[Tuple[str, float, Dict[str, Any]]] = []
    for r in rows:
        try:
            v = float(r.get(field) or 0.0)
        except (TypeError, ValueError):
            continue
        if use_abs:
            v = abs(v)
        label = str(r.get("label"))
        scored.append((label, v, r))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def _pick_dominant(
    ranked: List[Tuple[str, float, Dict[str, Any]]],
    *,
    floor: float,
) -> Optional[Tuple[str, float, Dict[str, Any]]]:
    """Return the leader if it is dominant; else ``None``.

    "Dominant" = leader's score is above ``floor`` AND (the runner-up
    has a strictly smaller label OR its score is below
    ``leader * (1 - _TIE_TOLERANCE)``). The same label appearing twice
    in a row (different metrics merged earlier) is collapsed.
    """
    # Collapse duplicate labels keeping highest score (defensive — top
    # already deduped, but the hotspot computation could in theory emit
    # two pods aggregated to the same service).
    best_per_label: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    for label, score, row in ranked:
        prev = best_per_label.get(label)
        if prev is None or score > prev[0]:
            best_per_label[label] = (score, row)
    collapsed = sorted(
        ((label, score, row) for label, (score, row) in best_per_label.items()),
        key=lambda t: t[1],
        reverse=True,
    )

    if not collapsed:
        return None
    leader_label, leader_score, leader_row = collapsed[0]
    if leader_score < floor:
        return None
    if len(collapsed) >= 2:
        runner_up_score = collapsed[1][1]
        if runner_up_score >= leader_score * (1 - _TIE_TOLERANCE):
            return None  # ambiguous tie
    return leader_label, leader_score, leader_row


def _evidence(metric_id: str, score: float, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "metric": metric_id,
        "label": row.get("label"),
        "score": round(score, 4),
        "baseline_mean": row.get("baseline_mean"),
        "fault_mean": row.get("fault_mean"),
        "delta_abs": row.get("delta_abs"),
    }


# ── Per-category anchors ────────────────────────────────────────────────
def _anchor_memory(
    hotspots: Dict[str, Any],
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Locate the service responsible for memory exhaustion.

    Primary: largest ``oom_killed`` Δ (the OOM gauge transition).
    Fallback 1: largest ``memory_saturation_pct`` fault_mean above 50 %.
    Fallback 2: OOM-restart fingerprint — top ``pod_restarts`` service
    whose ``memory_saturation_pct`` also dropped (pod restarted after
    OOM kill, resetting working-set to near-zero). Used when
    ``oom_killed`` was not collected in older runs.
    """
    oom_rows = _top_rows(hotspots, "oom_killed")
    ranked = _rank_by(oom_rows, "delta_abs", use_abs=True)
    pick = _pick_dominant(ranked, floor=0.4)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("oom_killed", score, row)]

    mem_rows = _top_rows(hotspots, "memory_saturation_pct")
    ranked = _rank_by(mem_rows, "fault_mean", use_abs=False)
    pick = _pick_dominant(ranked, floor=_MIN_DOMINANT_FAULT_MEAN_PCT)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("memory_saturation_pct", score, row)]

    # OOM-restart fingerprint: pod_restarts top service + memory drop.
    restart_rows = _top_rows(hotspots, "pod_restarts")
    if restart_rows:
        top_r = restart_rows[0]
        try:
            r_delta = float(top_r.get("delta_abs") or 0.0)
        except (TypeError, ValueError):
            r_delta = 0.0
        if r_delta > _OOM_RESTART_DELTA_MIN:
            mem_by_label = {r.get("label"): r for r in mem_rows}
            mem_row = mem_by_label.get(top_r.get("label")) or {}
            try:
                m_delta = float(mem_row.get("delta_abs") or 0.0)
            except (TypeError, ValueError):
                m_delta = 0.0
            if m_delta < -_OOM_MEM_DROP_MIN:
                return str(top_r["label"]), [_evidence("pod_restarts", r_delta, top_r)]

    return None


def _anchor_cpu(
    hotspots: Dict[str, Any],
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Locate the service responsible for CPU exhaustion.

    Primary: largest ``cpu_throttled`` Δ — cgroup-level signal, fires
    only on pods that *actually* hit their CPU limit, so it ignores
    downstream services that merely got busy.
    Fallback: largest ``cpu_saturation_pct`` fault_mean above 50 %.
    """
    thr_rows = _top_rows(hotspots, "cpu_throttled")
    ranked = _rank_by(thr_rows, "delta_abs", use_abs=True)
    pick = _pick_dominant(ranked, floor=0.10)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("cpu_throttled", score, row)]

    sat_rows = _top_rows(hotspots, "cpu_saturation_pct")
    ranked = _rank_by(sat_rows, "fault_mean", use_abs=False)
    pick = _pick_dominant(ranked, floor=_MIN_DOMINANT_FAULT_MEAN_PCT)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("cpu_saturation_pct", score, row)]
    return None


def _anchor_pod_failure(
    hotspots: Dict[str, Any],
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Locate the service whose pod was killed / restarted.

    Anchor: largest ``|pod_restarts_total| Δ|``. Positive Δ catches
    in-place restarts; negative Δ catches pod replacement (counter
    reset on new pod — same signature exploited by the fault
    validator).
    """
    rows = _top_rows(hotspots, "pod_restarts_total")
    ranked = _rank_by(rows, "delta_abs", use_abs=True)
    pick = _pick_dominant(ranked, floor=_MIN_DOMINANT_ABS_DELTA)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("pod_restarts_total", score, row)]
    return None


def _anchor_network_latency(
    hotspots: Dict[str, Any],
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """Network-latency faults are anchored by the service with the largest
    latency increase during the fault window.
    """
    rows = _top_rows(hotspots, "latency_p95") + _top_rows(hotspots, "latency_p99")
    if not rows:
        return None
    ranked = _rank_by(rows, "delta_abs", use_abs=True)
    pick = _pick_dominant(ranked, floor=0.10)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("latency_p95", score, row)]
    return None


def _anchor_http_error(
    hotspots: Dict[str, Any],
) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """HTTP failures are anchored by the service with the largest jump in
    error_rate, i.e. the service producing the dominant 5xx / request error
    signal.
    """
    rows = _top_rows(hotspots, "error_rate")
    if not rows:
        return None
    ranked = _rank_by(rows, "delta_abs", use_abs=True)
    pick = _pick_dominant(ranked, floor=0.02)
    if pick is not None:
        label, score, row = pick
        return label, [_evidence("error_rate", score, row)]
    return None


_ANCHORS = {
    "memory-exhaustion": _anchor_memory,
    "cpu-exhaustion": _anchor_cpu,
    "pod-failure": _anchor_pod_failure,
    "network-latency": _anchor_network_latency,
    "network-loss": _anchor_network_latency,
    "http-error": _anchor_http_error,
}


# ── Public API ──────────────────────────────────────────────────────────
def localize_service(
    analysis: Dict[str, Any],
    features: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Possibly override ``analysis["rca"]`` based on metric anchors.

    Always populates ``analysis["validator_meta"]["localizer"]`` with
    the decision trace (fired, rule, original_rca, new_rca, evidence,
    reason). Never raises.
    """
    parent_meta = analysis.get("validator_meta")
    if not isinstance(parent_meta, dict):
        parent_meta = {}
        analysis["validator_meta"] = parent_meta

    # Preserve original RCA across idempotent calls.
    prior_loc = parent_meta.get("localizer") or {}
    original_rca = prior_loc.get("original_rca")
    if original_rca is None:
        original_rca = analysis.get("rca")

    fault_cat = _normalise_category(analysis.get("fault_category"))
    hotspots: Dict[str, Any] = {}
    if isinstance(features, dict):
        raw = features.get("metric_hotspots")
        if isinstance(raw, dict):
            hotspots = raw

    decision: Dict[str, Any] = {
        "fired": False,
        "rule": None,
        "original_rca": original_rca,
        "new_rca": original_rca,
        "evidence": [],
        "reason": None,
    }

    anchor_fn = _ANCHORS.get(fault_cat)
    if anchor_fn is None:
        decision["reason"] = f"no anchor for fault_category={fault_cat!r}"
        parent_meta["localizer"] = decision
        return analysis

    result = anchor_fn(hotspots)
    if result is None:
        decision["reason"] = (
            f"no anchor for {fault_cat!r}: no dominant signal " "(below threshold or tied)"
        )
        parent_meta["localizer"] = decision
        return analysis

    new_rca, evidence = result
    decision["fired"] = True
    decision["rule"] = f"localize:{fault_cat}"
    decision["new_rca"] = new_rca
    decision["evidence"] = evidence
    if original_rca == new_rca:
        decision["reason"] = f"anchor agrees with LLM ({new_rca!r}); no change"
    else:
        decision["reason"] = (
            f"anchor for {fault_cat!r} points to {new_rca!r}; "
            f"overriding LLM rca {original_rca!r}"
        )
        analysis["rca"] = new_rca

    parent_meta["localizer"] = decision
    return analysis
