"""Historical lifecycle summaries and explicit telemetry gaps for RCA."""

from datetime import datetime
import json
import math
import re
from typing import Any


def termination_query(namespace: str, services: list[str]) -> str:
    pods = "|".join(
        re.escape(service) + r"(-[a-z0-9]+)*" for service in sorted(set(services))
    )
    pod_selector = json.dumps(f"^({pods})$")
    return (
        '{__name__=~"kube_pod_container_status_last_terminated_(reason|exitcode|timestamp)",'
        f'namespace="{namespace}",pod=~{pod_selector}}}'
    )


def termination_summary(
    raw: dict[str, Any],
    phases: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if raw.get("status") != "success":
        return {
            "status": "tool_error",
            "error": raw.get("error") or "Invalid Prometheus response",
        }
    data = raw.get("data")
    series = data.get("result") if isinstance(data, dict) else None
    if not isinstance(series, list):
        return {
            "status": "tool_error",
            "error": "Missing historical termination series",
        }
    parsed_series = []
    for item in series:
        if not isinstance(item, dict):
            return {
                "status": "tool_error",
                "error": "Invalid historical termination series",
            }
        labels = item.get("metric") or {}
        points = []
        for t, v in item.get("values", []):
            timestamp, value = float(t), float(v)
            if not math.isfinite(timestamp) or not math.isfinite(value):
                return {
                    "status": "tool_error",
                    "error": "Non-finite historical termination sample",
                }
            points.append((timestamp, value))
        points.sort(key=lambda point: point[0])
        parsed_series.append((labels, points))

    phase_bounds = {
        phase: (
            datetime.fromisoformat(window["start"].replace("Z", "+00:00")).timestamp(),
            datetime.fromisoformat(window["end"].replace("Z", "+00:00")).timestamp(),
        )
        for phase, window in phases.items()
    }
    summaries = {}
    for phase, (start, end) in phase_bounds.items():
        rows = []
        sample_count = 0
        for labels, all_points in parsed_series:
            points = [
                (timestamp, value)
                for timestamp, value in all_points
                if start <= timestamp <= end
            ]
            if not points:
                continue
            sample_count += len(points)
            rows.append(
                {
                    **{
                        key: labels[key]
                        for key in ("namespace", "pod", "container", "uid", "reason")
                        if key in labels
                    },
                    "metric": labels.get("__name__"),
                    "sampled_at": points[-1][0],
                    "first_value": points[0][1],
                    "last_value": points[-1][1],
                    "min": min(value for _, value in points),
                    "max": max(value for _, value in points),
                }
            )

        event_records = {}
        for labels, points in parsed_series:
            if not str(labels.get("__name__") or "").endswith("_timestamp"):
                continue
            identity = tuple(
                str(labels.get(key) or "")
                for key in ("namespace", "pod", "container", "uid")
            )
            for observed_at, terminated_at in points:
                if not start <= terminated_at <= end:
                    continue
                key = (*identity, terminated_at)
                event_records.setdefault(key, (labels, observed_at, terminated_at))

        termination_events = []
        for key, (labels, observed_at, terminated_at) in event_records.items():
            identity = key[:4]
            has_uid = bool(identity[3])
            reasons = []
            exit_codes = []
            if has_uid:
                for state_labels, state_points in parsed_series:
                    state_identity = tuple(
                        str(state_labels.get(name) or "")
                        for name in ("namespace", "pod", "container", "uid")
                    )
                    if state_identity != identity:
                        continue
                    sampled_values = [
                        value
                        for timestamp, value in state_points
                        if timestamp == observed_at
                    ]
                    metric_name = str(state_labels.get("__name__") or "")
                    if (
                        metric_name.endswith("_reason")
                        and sampled_values
                        and sampled_values[-1] > 0
                    ):
                        reasons.append(str(state_labels.get("reason") or "unknown"))
                    elif metric_name.endswith("_exitcode") and sampled_values:
                        exit_codes.append(sampled_values[-1])
            termination_events.append(
                {
                    **{
                        name: labels[name]
                        for name in ("namespace", "pod", "container", "uid")
                        if name in labels
                    },
                    "observed_at": observed_at,
                    "terminated_at": terminated_at,
                    "reason": sorted(set(reasons)) or ["unknown"],
                    "exit_code": exit_codes[-1] if exit_codes else None,
                }
            )
        summaries[phase] = {
            "coverage": {
                "sample_count": sample_count,
                "series_count": len(rows),
                "event_count": len(termination_events),
            },
            "termination_states": rows[:24],
            "termination_events": termination_events[:24],
            "omitted_event_count": max(0, len(termination_events) - 24),
            "omitted_series_count": max(0, len(rows) - 24),
        }
    collected_metrics = sorted({
        str(labels.get("__name__")) for labels, points in parsed_series if points
    })
    missing_metrics = [
        f"kube_pod_container_status_last_terminated_{suffix}"
        for suffix in ("timestamp", "reason", "exitcode")
        if f"kube_pod_container_status_last_terminated_{suffix}" not in collected_metrics
    ]
    return {
        "status": (
            "ok"
            if any(p["coverage"]["sample_count"] for p in summaries.values())
            else "no_data"
        ),
        "purpose": "historical_container_termination",
        "collected_metrics": collected_metrics,
        "missing_metrics": missing_metrics,
        "event_time_coverage": "unavailable" if any(
            name.endswith("_timestamp") for name in missing_metrics
        ) else "available",
        "phases": summaries,
        "limitations": [
            "Uma terminacao so e atribuida a fase quando seu timestamp cai na janela; o instante observado pode vir de um scrape posterior.",
            "Reason e exit code sao correlacionados apenas quando as series compartilham UID e timestamp de scrape.",
            "Metricas de terminacao continuam na familia metrics, nao eventos independentes.",
            "Eventos historicos de probes/eviction e logs do runtime nao foram coletados.",
            *[f"Metrica ausente na janela consultada: {name}" for name in missing_metrics],
        ],
    }


def phase_series_summary(
    raw: dict[str, Any],
    phases: dict[str, dict[str, str]],
    *,
    purpose: str,
    unit: str,
    service: str,
) -> dict[str, Any]:
    if raw.get("status") != "success" or not isinstance(raw.get("data"), dict):
        return {"status": "tool_error", "error": raw.get("error") or "Invalid Prometheus response"}
    series = raw["data"].get("result")
    if not isinstance(series, list):
        return {"status": "tool_error", "error": "Missing Prometheus range series"}
    summaries = {}
    for phase, window in phases.items():
        start = datetime.fromisoformat(window["start"].replace("Z", "+00:00")).timestamp()
        end = datetime.fromisoformat(window["end"].replace("Z", "+00:00")).timestamp()
        rows = []
        for item in series:
            if not isinstance(item, dict) or not isinstance(item.get("metric"), dict):
                return {"status": "tool_error", "error": "Invalid Prometheus series"}
            points = [(float(t), float(v)) for t, v in item.get("values", [])]
            if any(not math.isfinite(t) or not math.isfinite(v) for t, v in points):
                return {"status": "tool_error", "error": "Non-finite Prometheus sample"}
            values = [v for t, v in points if start <= t <= end]
            if values:
                rows.append({
                    "labels": item["metric"], "service": service,
                    "sample_count": len(values), "mean": sum(values) / len(values),
                    "min": min(values), "max": max(values),
                })
        summaries[phase] = {
            "status": "ok" if rows else "no_data",
            "coverage": {"sample_count": sum(row["sample_count"] for row in rows),
                         "series_count": len(rows)},
            "series": rows,
        }
    return {
        "status": "ok" if any(phase["series"] for phase in summaries.values()) else "no_data",
        "purpose": purpose, "unit": unit, "service": service,
        "windows": phases, "phases": summaries,
        "limitations": [
            "rate([1m]) inclui amostras anteriores ao inicio da fase; media temporal, nao contagem de eventos.",
            "Serie ausente ou NaN nao demonstra ausencia de falha.",
        ],
    }


def coverage_report(
    calls: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_family: dict[str, dict[str, Any]] = {}
    for call in calls:
        family = call["family"]
        record = by_family.setdefault(family, {
            "queries": 0, "fault_observations": 0, "errors": 0,
            "truncated_queries": 0, "services": {},
            "statuses": {"ok": 0, "partial": 0, "no_data": 0, "tool_error": 0},
        })
        record["queries"] += 1
        record["fault_observations"] += call["fault_observation_count"]
        status = call.get("status")
        if status in record["statuses"]:
            record["statuses"][status] += 1
        record["errors"] += call["status"] == "tool_error"
        phases = (call["result"].get("phases") or {})
        record["truncated_queries"] += any(
            (p.get("coverage") or {}).get("truncated")
            for p in phases.values() if isinstance(p, dict)
        )
        if call.get("service"):
            record["services"].setdefault(call["service"], {})[call.get("phase") or "all"] = call["status"]
    trace_probes = [
        item for item in diagnostics or []
        if item.get("tool") == "tempo_search_traces"
    ]
    return {
        "families": by_family,
        "trace_search_probes": {
            "attempted": len(trace_probes),
            "traces_present": sum(
                item.get("status") == "traces_present" for item in trace_probes
            ),
            "empty_windows": sum(
                item.get("status") == "no_traces_in_window" for item in trace_probes
            ),
            "tool_errors": sum(
                item.get("status") == "tool_error" for item in trace_probes
            ),
            "services": {
                item["service"]: item.get("status")
                for item in trace_probes
                if isinstance(item.get("service"), str)
            },
        },
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
