"""L2 run summary: compact, RAG-friendly text derived from L1 features.

The output target is ~400-700 tokens so it fits comfortably inside a
4k-context window together with several retrieved neighbours.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "not recovered"
    try:
        return f"{float(value):.0f}s"
    except (TypeError, ValueError):
        return str(value)


def _phase_line(metric_id: str, phase_stats: Dict[str, Any]) -> str:
    """Render a single line: '<metric> baseline=… fault=… post=…'."""
    parts: List[str] = []
    for phase in ("baseline", "fault", "post"):
        stats = phase_stats.get(phase) or {}
        if not stats or stats.get("count", 0) == 0:
            continue
        mean = stats.get("mean")
        p95 = stats.get("p95")
        parts.append(f"{phase}:mean={_fmt_num(mean)} p95={_fmt_num(p95)}")
    if not parts:
        return f"- {metric_id}: no samples"
    return f"- {metric_id}: " + " | ".join(parts)


def _metric_change_score(phase_stats: Dict[str, Any]) -> float:
    """Estimate how discriminative a metric is between baseline and fault."""
    baseline = (phase_stats.get("baseline") or {}).get("mean")
    fault = (phase_stats.get("fault") or {}).get("mean")
    try:
        baseline_value = float(baseline)
        fault_value = float(fault)
    except (TypeError, ValueError):
        return 0.0
    if baseline_value != baseline_value or fault_value != fault_value:
        return 0.0
    delta = abs(fault_value - baseline_value)
    denominator = max(abs(baseline_value), 1e-9)
    return delta / denominator if delta else 0.0


def _select_prompt_metrics(
    phase_metrics: Dict[str, Any],
    violations: List[Dict[str, Any]],
    *,
    max_changed: int = 8,
    max_controls: int = 3,
) -> List[str]:
    """Select discriminative L1 metrics for the compact L2 prompt.

    Raw metrics remain fully persisted. This selector only reduces prompt
    volume: changed metrics and SLO metrics are retained, plus a few stable
    controls that help the model reject competing hypotheses.
    """
    if not phase_metrics:
        return []

    violation_ids = {str(v.get("metric")) for v in violations if v.get("metric")}
    ranked = sorted(
        (
            (_metric_change_score(stats), metric_id)
            for metric_id, stats in phase_metrics.items()
            if isinstance(stats, dict)
        ),
        reverse=True,
    )
    changed = [
        metric_id
        for score, metric_id in ranked
        if score > 0.10 or metric_id in violation_ids
    ][:max_changed]

    controls = [
        metric_id
        for score, metric_id in ranked
        if metric_id not in changed and score <= 0.10
    ][:max_controls]
    return changed + controls


def _render_pod_health(features: Dict[str, Any]) -> List[str]:
    """Convert OOMKilled / pod_restarts_total hotspots into explicit
    English sentences.

    Why a dedicated section instead of leaving the signal in
    ``Resource saturation hotspots``: those rows read as continuous Δs
    (``fault=0.5 baseline=0.0 Δ=+0.50``), which forces the LLM to
    re-interpret what a 0.5 delta on a 0/1 gauge means. A plain
    "Pod X was OOMKilled" line is far less ambiguous for a small model.

    We rely on the per-label hotspot deltas already computed by
    ``MCPPrometheusMetricsPlugin.per_label_hotspots`` (which buckets by
    baseline vs fault phase, scoped to the experiment window — so no
    cross-experiment leakage from earlier OOMs / restarts).
    """
    hotspots = features.get("metric_hotspots") or {}
    out: List[str] = []

    oom = hotspots.get("oom_killed") or {}
    oom_rows = [
        r
        for r in (oom.get("top") or [])
        if isinstance(r, dict) and (r.get("delta_abs") or 0) > 0.4
    ]
    for r in oom_rows:
        label = r.get("label", "unknown")
        # The gauge is 0/1 per container; a delta > 0.4 means the
        # OOMKilled gauge transitioned for at least one container in
        # that pod/service during the fault window.
        out.append(
            f"- **OOMKilled**: container(s) in `{label}` were OOMKilled "
            f"during the run (last-terminated-reason gauge "
            f"baseline={_fmt_num(r.get('baseline_mean'), 2)} → "
            f"fault={_fmt_num(r.get('fault_mean'), 2)})."
        )

    restarts = hotspots.get("pod_restarts_total") or {}
    restart_rows = [
        r
        for r in (restarts.get("top") or [])
        if isinstance(r, dict) and (r.get("delta_abs") or 0) > 0.05
    ]
    for r in restart_rows:
        label = r.get("label", "unknown")
        # delta_abs on a monotonic counter ≈ (mid-fault - mid-baseline);
        # we flag presence of restarts but do not claim an exact count.
        out.append(
            f"- **Restarts**: `{label}` accumulated restarts during the run "
            f"(counter Δ≈{_fmt_num(r.get('delta_abs'), 2)})."
        )

    if not out:
        # Make the absence explicit so the LLM doesn't infer it from
        # silence — useful when classifying pod-failure vs cpu-hog.
        out.append("- No OOMKills or pod restarts observed during the run.")
    return out


def _render_hotspot_lines(
    hotspots: Dict[str, Any],
    metric_ids: Tuple[str, ...],
) -> List[str]:
    """Render top-N hotspots for the given metric ids, in declared order.

    Skips metrics that produced no useful per-label data (single global
    series, query failed, all rows have zero deviation).
    """
    out: List[str] = []
    for metric_id in metric_ids:
        entry = hotspots.get(metric_id)
        if not isinstance(entry, dict):
            continue
        rows = entry.get("top") or []
        if not rows:
            continue
        # Drop rows where there is no signal at all (fault==baseline==0).
        meaningful = [
            r
            for r in rows
            if isinstance(r, dict)
            and (abs(r.get("delta_abs", 0.0)) > 1e-6 or r.get("fault_mean", 0.0) > 1e-6)
        ]
        if not meaningful:
            continue
        for row in meaningful:
            label = row.get("label", "?")
            fault = _fmt_num(row.get("fault_mean"))
            base = _fmt_num(row.get("baseline_mean"))
            delta = _fmt_num(row.get("delta_abs"))
            sign = "+" if (row.get("delta_abs") or 0) >= 0 else ""
            out.append(
                f"- {label:<14} {metric_id:<22} "
                f"fault={fault} baseline={base} Δ={sign}{delta}"
            )
    return out


def build_summary_tags(features: Dict[str, Any]) -> List[str]:
    """Short controlled-vocabulary tags used for retrieval and filtering."""
    tags: List[str] = []
    verdict = features.get("verdict")
    if verdict:
        tags.append(f"verdict:{verdict}")
    chaos = features.get("chaos_type")
    if chaos:
        tags.append(f"chaos:{chaos}")
    for svc in (features.get("affected_services") or [])[:5]:
        tags.append(f"svc:{svc}")
    for v in (features.get("slo_violations") or [])[:5]:
        metric = v.get("metric")
        phase = v.get("phase")
        if metric and phase:
            tags.append(f"violation:{metric}@{phase}")
    network_metric_ids = (
        "network_receive_errors_rate",
        "network_transmit_errors_rate",
        "network_receive_dropped_rate",
        "network_transmit_dropped_rate",
    )
    network_hotspots = features.get("metric_hotspots") or {}
    if any(
        any(
            isinstance(row, dict)
            and (abs(float(row.get("delta_abs") or 0.0)) > 1e-6)
            for row in (network_hotspots.get(metric, {}).get("top") or [])
        )
        for metric in network_metric_ids
        if isinstance(network_hotspots.get(metric), dict)
    ):
        tags.append("network:signal")
    rt = features.get("recovery_time_seconds")
    if rt is None:
        tags.append("recovery:none")
    elif rt <= 60:
        tags.append("recovery:fast")
    elif rt <= 300:
        tags.append("recovery:medium")
    else:
        tags.append("recovery:slow")

    # Phase F1 — propagation graph & temporal dynamics tags.
    graph = features.get("propagation_graph") or {}
    cascade = graph.get("cascade_order") or []
    if cascade:
        names = [
            c.get("service")
            for c in cascade[:3]
            if isinstance(c, dict) and c.get("service")
        ]
        if names:
            tags.append("cascade:" + "->".join(names))
    edge_count = graph.get("edge_count")
    if isinstance(edge_count, int) and edge_count > 0:
        tags.append(f"edges:{edge_count}")

    temporal = features.get("temporal_features") or {}
    shape = temporal.get("overall_recovery_shape")
    if shape and shape != "unknown":
        tags.append(f"shape:{shape}")
    return tags


def build_run_summary_l2(
    run_id: str,
    experiment_id: str,
    features: Dict[str, Any],
    timings: Dict[str, Any] | None = None,
) -> Tuple[str, List[str]]:
    """Return (markdown_summary, tags).

    The summary is deterministic — no LLM call — so it is cheap to
    regenerate and safe to embed in prompts.
    """
    verdict = features.get("verdict") or "unknown"
    chaos = features.get("chaos_type") or "n/a"
    slo = features.get("slo_thresholds") or {}
    violations = features.get("slo_violations") or []
    services = features.get("affected_services") or []
    signatures = features.get("top_failure_signatures") or []
    rcas = features.get("rca_hypotheses") or []
    phase_metrics = features.get("phase_metrics") or {}
    trace_sum = features.get("trace_summary") or {}
    rt = features.get("recovery_time_seconds")
    rt_metric = features.get("recovery_reference_metric")

    timings = timings or {}
    duration = timings.get("experiment_total_duration_seconds")
    fault_dur = timings.get("fault_injection_duration_seconds")

    lines: List[str] = []
    lines.append(f"# Run {run_id} (experiment {experiment_id})")
    lines.append(
        f"Verdict: **{verdict}** | chaos_type: `{chaos}` | "
        f"fault_duration: {_fmt_seconds(fault_dur)} | "
        f"total: {_fmt_seconds(duration)}"
    )

    # SLO line
    lines.append(
        f"SLOs: error_rate<={_fmt_num(slo.get('error_rate'), 3)}, "
        f"p95<={_fmt_num(slo.get('latency_p95_ms'), 0)}ms, "
        f"tolerance={_fmt_num(slo.get('recovery_tolerance_pct'), 2)}, "
        f"max_recovery={_fmt_num(slo.get('max_recovery_seconds'), 0)}s"
    )

    # Violations
    if violations:
        lines.append("\n## SLO violations")
        for v in violations[:10]:
            lines.append(
                f"- [{v.get('phase')}] {v.get('metric')}: "
                f"observed={_fmt_num(v.get('observed'))} "
                f"> threshold={_fmt_num(v.get('threshold'))} "
                f"(Δ={_fmt_num(v.get('delta_pct'), 1)}%)"
            )
    else:
        lines.append("\n## SLO violations\nNone — system stayed within SLOs.")

    # Recovery
    lines.append("\n## Recovery")
    if rt is None:
        lines.append(
            "System did not return to baseline within the post-recovery window."
        )
    else:
        lines.append(
            f"Recovered in {_fmt_seconds(rt)} "
            f"(reference metric: `{rt_metric or 'n/a'}`)."
        )

    # Per-phase metrics
    if phase_metrics:
        selected_metric_ids = _select_prompt_metrics(phase_metrics, violations)
        lines.append("\n## Per-phase metrics")
        lines.append(
            "Only metrics with material fault-vs-baseline change, SLO violations, "
            "or a small negative-control set are shown below; the complete raw "
            "metric collection remains persisted in L1 artifacts."
        )
        network_metric_ids = {
            "network_receive_bytes_rate",
            "network_transmit_bytes_rate",
            "network_receive_errors_rate",
            "network_transmit_errors_rate",
            "network_receive_dropped_rate",
            "network_transmit_dropped_rate",
        }
        for metric_id in selected_metric_ids:
            if metric_id in network_metric_ids:
                continue
            phase_stats = phase_metrics.get(metric_id)
            if isinstance(phase_stats, dict):
                lines.append(_phase_line(metric_id, phase_stats))

        network_metric_ids_ordered = (
            "network_receive_bytes_rate",
            "network_transmit_bytes_rate",
            "network_receive_errors_rate",
            "network_transmit_errors_rate",
            "network_receive_dropped_rate",
            "network_transmit_dropped_rate",
        )
        network_phase_lines = [
            _phase_line(metric_id, phase_metrics[metric_id])
            for metric_id in network_metric_ids_ordered
            if metric_id in selected_metric_ids
            if metric_id in phase_metrics
            and isinstance(phase_metrics[metric_id], dict)
        ]
        if network_phase_lines:
            lines.append("\n## Network phase signals")
            lines.append(
                "Host/interface-level signals from Prometheus; these indicate "
                "network pressure or packet handling anomalies but do not identify "
                "a service by themselves."
            )
            lines.extend(network_phase_lines)

    # ── Service hotspots — per-label localisation signal ─────────────────
    # Two subsections separating SYMPTOM metrics (RED — cascading effects
    # the operator observes) from CAUSAL metrics (resource saturation that
    # points at the resource-exhausted pod). Labels are pre-normalised to
    # the parent service name in ``per_label_hotspots`` so the same pod
    # group is aggregated across replicas.
    hotspots = features.get("metric_hotspots") or {}
    if hotspots:
        _SYMPTOM_METRICS = ("error_rate", "latency_p95", "latency_p99", "traffic")
        _CAUSAL_METRICS = (
            "memory_saturation_pct",
            "cpu_saturation_pct",
            "cpu_throttled",
            "oom_killed",
            "pod_restarts_total",
            "pod_restarts",
            "memory_working_set_bytes",
            "cpu_usage_cores",
        )
        _NETWORK_METRICS = (
            "network_receive_bytes_rate",
            "network_transmit_bytes_rate",
            "network_receive_errors_rate",
            "network_transmit_errors_rate",
            "network_receive_dropped_rate",
            "network_transmit_dropped_rate",
        )
        symptom_lines = _render_hotspot_lines(hotspots, _SYMPTOM_METRICS)
        causal_lines = _render_hotspot_lines(hotspots, _CAUSAL_METRICS)
        network_lines = _render_hotspot_lines(hotspots, _NETWORK_METRICS)
        if symptom_lines or causal_lines:
            lines.append("\n## Service hotspots")
            lines.append(
                "Per-service deviation during the fault phase (fault_mean − "
                "baseline_mean). Symptom hotspots show where errors/latency "
                "manifest (often downstream); causal hotspots show resource "
                "exhaustion (often the root cause)."
            )
            if symptom_lines:
                lines.append("\n### Symptom hotspots (RED — request/error/latency)")
                lines.extend(symptom_lines)
            if causal_lines:
                lines.append("\n### Resource saturation hotspots")
                lines.extend(causal_lines)
            if network_lines:
                lines.append("\n### Network hotspots")
                lines.append(
                    "Interface-level network signals; compare these with RED "
                    "metrics to distinguish network faults from resource exhaustion."
                )
                lines.extend(network_lines)

    # ── Pod health events — discrete counts, not continuous Δs ───────────
    # The hotspot block above is great for continuous metrics (saturation,
    # latency) but reads awkwardly for counters/gauges where what matters
    # is "did this event happen at all, and how many times?". We render
    # OOMKills and restart counts as plain English here so the LLM does
    # not have to translate (fault_mean − baseline_mean) deltas back into
    # event counts.
    pod_health_lines = _render_pod_health(features)
    if pod_health_lines:
        lines.append("\n## Pod health events")
        lines.extend(pod_health_lines)

    # Traces
    lines.append("\n## Traces")
    lines.append(
        f"traces={trace_sum.get('trace_count', 0)}, "
        f"errors={trace_sum.get('error_trace_count', 0)}, "
        f"affected_services=[{', '.join(services) or 'none'}]"
    )

    # Failure signatures
    if signatures:
        lines.append("\n## Top failure signatures")
        for sig in signatures[:5]:
            lines.append(
                f"- {sig.get('signature')} on "
                f"`{sig.get('affected_service') or 'unknown'}` "
                f"(x{sig.get('occurrence_count', 0)}, "
                f"max_duration={_fmt_num(sig.get('max_duration_ms'), 0)}ms)"
            )

    # RCA
    if rcas:
        lines.append("\n## RCA hypotheses")
        for rca in rcas[:3]:
            lines.append(f"- [{rca.get('confidence')}] {rca.get('hypothesis')}")

    # Phase F1 — Temporal dynamics
    temporal = features.get("temporal_features") or {}
    by_metric = temporal.get("by_metric") or {}
    if by_metric:
        lines.append("\n## Temporal dynamics")
        lines.append(
            f"Overall recovery shape: **{temporal.get('overall_recovery_shape', 'unknown')}**"
        )
        for metric_id, t in list(by_metric.items())[:8]:
            if not isinstance(t, dict) or not t:
                continue
            ttfv = t.get("time_to_first_violation_s")
            ttp = t.get("time_to_peak_s")
            md = t.get("max_abs_derivative")
            cv = t.get("coefficient_of_variation_fault")
            shape = t.get("recovery_shape", "unknown")
            lines.append(
                f"- {metric_id}: ttfv={_fmt_seconds(ttfv)} | "
                f"ttp={_fmt_seconds(ttp)} | "
                f"|dv/dt|max={_fmt_num(md, 4)} | "
                f"cv={_fmt_num(cv, 3)} | shape={shape}"
            )

    # Phase F1 — Propagation graph
    graph = features.get("propagation_graph") or {}
    cascade = graph.get("cascade_order") or []
    edges = graph.get("edges") or []
    if cascade or edges:
        lines.append("\n## Propagation graph")
        if cascade:
            chain = " -> ".join(
                f"{c.get('service')}(+{_fmt_num(c.get('t_offset_s'), 2)}s)"
                for c in cascade[:6]
                if isinstance(c, dict)
            )
            lines.append(f"Cascade: {chain}")
        if edges:
            err_edges = sum(
                1 for e in edges if isinstance(e, dict) and e.get("error_count", 0) > 0
            )
            lines.append(
                f"Edges: {len(edges)} total, {err_edges} with errors "
                f"(top {min(5, len(edges))} below)"
            )
            for e in edges[:5]:
                if not isinstance(e, dict):
                    continue
                marker = " ❗" if e.get("error_count", 0) > 0 else ""
                lines.append(
                    f"- {e.get('from')} → {e.get('to')} "
                    f"(traces={e.get('trace_count', 0)}, "
                    f"errors={e.get('error_count', 0)}){marker}"
                )

    text = "\n".join(lines).strip() + "\n"
    return text, build_summary_tags(features)
