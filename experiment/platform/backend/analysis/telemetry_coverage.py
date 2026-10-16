"""Historical lifecycle summaries and explicit telemetry gaps for RCA."""

from datetime import datetime
import math
from typing import Any


def termination_summary(
    raw: dict[str, Any], phases: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if raw.get("status") != "success":
        return {"status": "tool_error", "error": raw.get("error") or "Invalid Prometheus response"}
    data = raw.get("data")
    series = data.get("result") if isinstance(data, dict) else None
    if not isinstance(series, list):
        return {"status": "tool_error", "error": "Missing historical termination series"}
    summaries = {}
    for phase, window in phases.items():
        start = datetime.fromisoformat(window["start"].replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(window["end"].replace("Z", "+00:00")).timestamp()
        rows = []
        sample_count = 0
        for item in series:
            if not isinstance(item, dict):
                return {"status": "tool_error", "error": "Invalid historical termination series"}
            labels = item.get("metric") or {}
            points = []
            for t, v in item.get("values", []):
                timestamp, value = float(t), float(v)
                if not math.isfinite(timestamp) or not math.isfinite(value):
                    return {"status": "tool_error", "error": "Non-finite historical termination sample"}
                if start <= timestamp <= end:
                    points.append((timestamp, value))
            if not points:
                continue
            sample_count += len(points)
            rows.append({
                **{key: labels[key] for key in ("pod", "container", "uid", "reason") if key in labels},
                "metric": labels.get("__name__"),
                "first_value": points[0][1],
                "last_value": points[-1][1],
                "min": min(v for _, v in points),
                "max": max(v for _, v in points),
            })
        summaries[phase] = {
            "coverage": {"sample_count": sample_count, "series_count": len(rows)},
            "termination_states": rows[:24],
            "omitted_series_count": max(0, len(rows) - 24),
        }
    return {
        "status": "ok" if any(p["coverage"]["sample_count"] for p in summaries.values()) else "no_data",
        "purpose": "historical_container_termination",
        "phases": summaries,
        "limitations": [
            "Ultima terminacao pode preceder a janela; valores amostrados nao comprovam novo evento.",
            "Metricas de terminacao continuam na familia metrics, nao eventos independentes.",
            "Eventos historicos de probes/eviction e logs do runtime nao foram coletados.",
        ],
    }


def coverage_report(calls: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, dict[str, Any]] = {}
    for call in calls:
        family = call["family"]
        record = by_family.setdefault(family, {
            "queries": 0, "fault_observations": 0, "errors": 0,
            "truncated_queries": 0, "services": {},
        })
        record["queries"] += 1
        record["fault_observations"] += call["fault_observation_count"]
        record["errors"] += call["status"] == "tool_error"
        phases = (call["result"].get("phases") or {})
        record["truncated_queries"] += any(
            (p.get("coverage") or {}).get("truncated")
            for p in phases.values() if isinstance(p, dict)
        )
        if call.get("service"):
            record["services"].setdefault(call["service"], {})[call.get("phase") or "all"] = call["status"]
    return {
        "families": by_family,
        "missing_fault_families": [
            family for family in ("metrics", "logs", "traces")
            if by_family.get(family, {}).get("fault_observations", 0) == 0
        ],
        "uncollected_sources": ["historical_kubernetes_events", "container_runtime_logs", "probe_history"],
        "interpretation": (
            "Cobertura e diagnostico de disponibilidade, nao evidencia de mecanismo causal. "
            "Contagens acumuladas por consulta podem sobrepor eventos; nao sao eventos unicos."
        ),
    }
