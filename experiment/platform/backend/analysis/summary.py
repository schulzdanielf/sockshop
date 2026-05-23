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
        names = [c.get("service") for c in cascade[:3] if isinstance(c, dict) and c.get("service")]
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
        lines.append("\n## Per-phase metrics")
        for metric_id, phase_stats in list(phase_metrics.items())[:8]:
            if isinstance(phase_stats, dict):
                lines.append(_phase_line(metric_id, phase_stats))

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
            lines.append(
                f"- [{rca.get('confidence')}] {rca.get('hypothesis')}"
            )

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
            err_edges = sum(1 for e in edges if isinstance(e, dict) and e.get("error_count", 0) > 0)
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
