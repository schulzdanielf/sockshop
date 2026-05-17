"""Trace summarizer: formats extracted features into a compact summary and optionally calls the local LLM."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from config import settings


# ---------------------------------------------------------------------------
# Compact text formatter
# ---------------------------------------------------------------------------

def format_trace_summary(features: Dict) -> str:
    """Format extracted trace features into a compact human-readable summary."""
    if "error" in features:
        return f"Error extracting features: {features['error']}"

    dur = features['trace_duration_ms']
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────
    err_svcs = len({e["service"] for e in features.get("error_spans", [])})
    err_tag = f"  {err_svcs} service(s) with errors" if err_svcs else ""
    lines.append(f"TRACE  {dur}ms  {features['span_count']} spans{err_tag}")

    # ── Critical path (semantic, skip pure internal groups) ─────────────
    cp = [s for s in features.get("critical_path", []) if s.get("semantic") != "INTERNAL_GROUP"]
    if cp:
        lines.append("")
        lines.append("CRITICAL PATH")
        for s in cp:
            err = " ✗" if s.get("is_error") else ""
            lines.append(f"  {s['service']}.{s['name']}  {s['duration_ms']}ms{err}")

    # ── Top bottlenecks — exclusive latency only ─────────────────────────
    hot = [s for s in features.get("hot_spans", []) if s["exclusive_duration_ms"] > 1][:4]
    if hot:
        lines.append("")
        lines.append("BOTTLENECKS  (exclusive ms — actual CPU/IO time)")
        for s in hot:
            lines.append(f"  {s['service']}.{s['name']}  {s['exclusive_duration_ms']}ms")

    # ── Failure signatures (deduplicated, no redundant error span list) ──
    sigs = features.get("failure_signatures", [])
    if sigs:
        lines.append("")
        lines.append("FAILURES")
        for sig in sigs:
            ep = f" @ {sig['endpoint']}" if sig.get("endpoint") else ""
            # Show unique affected operations, trimmed
            ops = sorted({o.split(".")[-1] for o in sig["operations"]})
            ops_str = ", ".join(ops[:3]) + ("…" if len(ops) > 3 else "")
            lines.append(f"  [{sig['signature']}]{ep}  x{sig['occurrence_count']}  {ops_str}")

    # ── Dependency graph — deduplicated by (from, to) service pair ───────
    dep = features.get("dependency_map", [])
    if dep:
        lines.append("")
        lines.append("DEPENDENCIES")
        seen: set = set()
        for edge in dep:
            src = edge["from"]
            # Use last path segment as target label to keep it short
            raw_to = edge["to"]
            to = raw_to.split("/")[0].split(":")[0]  # strip port + path
            if not to:
                continue
            key = (src, to, bool(edge.get("is_error")))
            if key in seen:
                continue
            seen.add(key)
            err = " [FAILED]" if edge.get("is_error") else ""
            lines.append(f"  {src} → {to}{err}")

    # ── Service latency — one line per service ───────────────────────────
    svc_lat = features.get("service_latency", [])
    if svc_lat:
        lines.append("")
        lines.append("SERVICE LATENCY  (exclusive)")
        for svc in svc_lat:
            lines.append(
                f"  {svc['service']}  total={svc['total_exclusive_ms']}ms"
                f"  max={svc['max_exclusive_ms']}ms  spans={svc['span_count']}"
            )

    # ── Fanout anomalies — only non-middleware patterns ──────────────────
    anomalies = [
        p for p in features.get("fanout_patterns", [])
        if p["pattern"] != "N+1_REPEATED_CALL" or "middleware" not in p.get("operation", "").lower()
    ][:3]
    if anomalies:
        lines.append("")
        lines.append("CALL ANOMALIES")
        for p in anomalies:
            lines.append(f"  [{p['pattern']}] {p['description']}")

    # ── RCA — HIGH confidence first, evidence condensed ─────────────────
    hyps = sorted(
        features.get("rca_hypotheses", []),
        key=lambda h: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(h["confidence"], 3),
    )
    if hyps:
        lines.append("")
        lines.append("RCA")
        for h in hyps:
            lines.append(f"  [{h['confidence']}] {h['hypothesis']}")
            for action in h.get("suggested_actions", [])[:2]:
                lines.append(f"    → {action}")

    return "\n".join(lines)




# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------

def _build_llm_prompt(summary: str) -> str:
    return f"""You are an expert SRE analyzing a distributed microservices trace.

The trace summary below already contains pre-computed analysis:
- Semantic critical path (internal instrumentation spans are compressed)
- Exclusive latency per span (time excluding children — no double counting)
- Failure signatures with semantic types (e.g. NETWORK_CONNECTION_REFUSED)
- Dependency graph edges inferred from OpenTelemetry attributes
- Fanout/call anomaly patterns (N+1, retry storm, excessive fanout)
- Auto-generated RCA hypotheses with confidence levels

TRACE SUMMARY:
{summary}

Based on this structured trace data, provide a concise SRE incident analysis:

1. **Root cause** — What is the most likely root cause? Reference the failure signature, the exclusive latency concentration, and the dependency graph.
2. **Impact scope** — Which services and operations are affected and why?
3. **Confidence assessment** — How certain are you? What would increase confidence?
4. **Immediate actions** — 2-3 concrete steps to fix or mitigate right now.
5. **Long-term improvements** — Architecture or instrumentation changes to prevent recurrence.

Be direct. Use the pre-computed hypotheses as a starting point but add your own reasoning.
Do not repeat raw numbers already visible in the summary. Focus on causality and actionability."""


def analyze_with_llm(summary: str, max_new_tokens: int = 512) -> Optional[str]:
    """Send trace summary to the local Qwen model and return its analysis.

    Returns None if the LLM is not available or not configured.
    """
    llm_url = getattr(settings, "llm_url", None)
    if not llm_url:
        return None

    prompt = _build_llm_prompt(summary)

    try:
        response = httpx.post(
            llm_url,
            json={"prompt": prompt, "max_new_tokens": max_new_tokens},
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("response", "")
    except Exception as e:
        return f"(LLM unavailable: {e})"


def summarize_trace(features: Dict, use_llm: bool = True, max_new_tokens: int = 512) -> Dict:
    """Return a dict with the formatted summary and optional LLM analysis.

    Args:
        features: Output of trace_analyzer.extract_features().
        use_llm: Whether to call the local LLM for analysis.
        max_new_tokens: Max tokens for LLM response.

    Returns:
        Dict with keys: summary (str), llm_analysis (str or None).
    """
    summary_text = format_trace_summary(features)
    llm_analysis: Optional[str] = None

    if use_llm:
        llm_analysis = analyze_with_llm(summary_text, max_new_tokens=max_new_tokens)

    return {
        "summary": summary_text,
        "llm_analysis": llm_analysis,
    }
