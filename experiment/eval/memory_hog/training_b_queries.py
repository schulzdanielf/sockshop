"""Read-only historical queries and lossless diagnostic facts for B rebuilding."""

from __future__ import annotations

import copy
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from experiment.platform.backend.agent_console import (
    _rca_normalize_loki_range_result,
    _rca_normalize_trace_result,
    _rca_tool_digest,
)
from experiment.platform.backend.analysis.telemetry_coverage import (
    phase_series_summary,
    termination_summary,
)
from experiment.platform.backend.evidence_projection import compact_evidence_for_model
from experiment.platform.backend.rca_knowledge import (
    Check,
    canonical,
    prepare_check,
    write_once,
)
from experiment.platform.backend.rca_skill import (
    TOOL_ERRORS,
    _evidence_source_provenance,
    _observation_count,
    _sanitize,
)

HISTORICAL_QUERY_TIMEOUT_SECONDS = 240


def summarize_result(
    tool: str, raw: dict[str, Any], arguments: dict[str, Any],
    windows: dict[str, Any], *, service: str | None = None,
    phase: str | None = None, candidate: dict[str, Any] | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    if tool == "prometheus_range_query":
        if "last_terminated_" in arguments.get("query", ""):
            return termination_summary(raw, windows)
        purposes = {
            "http": ("http_status_rates", "req/s"),
            "network": ("network_edge_latency", "ms"),
            "cpu_throttling": ("cpu_throttling", "%"),
            "gc": ("gc_collection_time", "s/s"),
        }
        purpose, unit = purposes.get(kind or "", ("historical_range", "unspecified"))
        return phase_series_summary(
            raw, windows, purpose=purpose, unit=unit,
            service=service if kind == "http" else (service or "scope"),
        )
    if tool == "loki_range_query" and "phases" not in raw:
        normalized = _rca_normalize_loki_range_result(
            raw, service=service or "scope", namespace="sock-shop",
            fault_window=windows[phase or "fault"], line_limit=150,
        )
        if phase and "fault" in normalized.get("phases", {}):
            normalized["phases"] = {phase: normalized["phases"]["fault"]}
        return _rca_tool_digest(tool, normalized)
    if tool == "tempo_analyze_trace":
        return _rca_tool_digest(
            tool, _rca_normalize_trace_result(raw, candidate or {})
        )
    if tool == "tempo_service_graph_phases":
        summary = _rca_tool_digest(tool, raw)
        summary["phases"] = summary.pop("phase_observations", {})
        summary["status"] = (
            "ok" if any(
                (phase or {}).get("trace_count", 0) > 0
                for phase in summary["phases"].values()
            ) else "no_data"
        )
        return summary
    if tool == "get_run_golden_signals":
        # Preserve every service, precision, unit and phase rather than top-four ranks.
        return {
            "status": raw.get("status"),
            "phases": raw.get("phases"),
            "metrics": {
                name: {
                    key: copy.deepcopy(metric[key])
                    for key in ("status", "unit", "phases", "top", "top_delta")
                    if key in metric
                }
                for name, metric in raw.get("metrics", {}).items()
            },
        }
    return _rca_tool_digest(tool, raw)


def evidence_context(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return compact_evidence_for_model(evidence, char_budget=72_000)


class HistoricalQueries:
    def __init__(
        self, client: Any, windows: dict[str, Any], directory: Path,
    ) -> None:
        self.client = client
        self.windows = {
            phase: windows[phase] for phase in ("baseline", "fault", "post")
        }
        self.directory = directory
        self.tools = {tool["name"]: tool for tool in client.list_tools()}
        self.evidence: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def query(
        self, tool: str, arguments: dict[str, Any], family: str, *,
        service: str | None = None, phase: str | None = None,
        candidate: dict[str, Any] | None = None, kind: str | None = None,
    ) -> dict[str, Any]:
        if tool not in self.tools:
            raise ValueError(f"Historical tool unavailable: {tool}")
        started = datetime.now().astimezone().isoformat()
        clock = time.monotonic()
        identifier = f"N{len(self.evidence) + 1:03d}"
        transport_attempts = []
        for attempt in range(2):
            try:
                raw = self.client.call_tool_json(
                    tool, arguments, timeout_seconds=HISTORICAL_QUERY_TIMEOUT_SECONDS
                )
                break
            except TOOL_ERRORS as exc:
                failure = {
                    "attempt": attempt + 1, "tool": tool, "arguments": arguments,
                    "error": str(exc), "status": "collection_error",
                    "recorded_at": datetime.now().astimezone().isoformat(),
                }
                transport_attempts.append(failure)
                write_once(self.directory / f"{identifier}-attempt-{attempt + 1}-error.json", failure)
                if attempt:
                    raise
        if not isinstance(raw, dict):
            raise ValueError(f"{tool}: historical MCP response is not an object")
        if raw.get("isError") or raw.get("status") in {"error", "tool_error"}:
            raise ValueError(f"{tool}: {raw.get('error') or 'historical query failed'}")
        raw = _sanitize(raw)
        payload = {
            "tool": tool, "arguments": arguments, "result": raw,
            "started_at": started,
            "completed_at": datetime.now().astimezone().isoformat(),
            "duration_ms": round((time.monotonic() - clock) * 1000),
            "transport_attempts": transport_attempts,
        }
        result_path = self.directory / f"{identifier}.json"
        write_once(result_path, payload)
        result_sha256 = hashlib.sha256(result_path.read_bytes()).hexdigest()
        result = summarize_result(
            tool, raw, arguments, self.windows, service=service, phase=phase,
            candidate=candidate, kind=kind,
        )
        result = _sanitize(result)
        if result.get("status") in {"tool_error", "error"}:
            count = 0
            fault_count = 0
        else:
            count = _observation_count(tool, result)
            fault_count = _observation_count(tool, result, "fault")
        if tool == "tempo_search_traces":
            count = len(raw.get("traces") or [])
            fault_count = count if phase == "fault" else 0
            result["status"] = "ok" if count else "no_data"
        if kind in {"cpu", "memory"}:
            count = int((raw.get("coverage") or {}).get("sample_count") or 0)
            if not count:
                count = sum(int(row.get("sample_count") or 0) for row in raw.get("top", []))
            fault_count = count if phase == "fault" else 0
        if tool == "tempo_analyze_trace":
            fault_count = count if (candidate or {}).get("phase") == "fault" else 0
        if tool == "check_pod_restarts":
            fault_count = count if phase == "fault" else 0
        item = {
            "id": identifier, "tool": tool, "family": family, "source": "history",
            "kind": kind,
            "source_provenance": _evidence_source_provenance(
                tool, family, kind, result
            ),
            "status": result.get("status", "ok" if count else "no_data"),
            "observation_count": count, "fault_observation_count": fault_count,
            "arguments": arguments, "digest": result, "service": service,
            "phase": phase, "result_ref": str(result_path),
            "result_sha256": result_sha256,
        }
        self.evidence.append(item)
        self.calls.append({
            **payload, "result": result, "evidence_id": identifier,
            "service": service, "phase": phase, "kind": kind,
        })
        return item

    def triage(self) -> None:
        phases = self.windows
        golden_args = {
            f"{phase}_{edge}": phases[phase][edge]
            for phase in ("baseline", "fault") for edge in ("start", "end")
        }
        self.query(
            "get_run_golden_signals",
            {**golden_args, "namespace": "sock-shop", "step": "15s", "k": 20},
            "metrics",
        )
        self.query(
            "check_pod_restarts",
            {**phases["fault"], "namespace": "sock-shop", "step": "15s"},
            "metrics", phase="fault", kind="lifecycle",
        )

    def collect_coverage(self) -> dict[str, Any]:
        records = []
        for phase, window in self.windows.items():
            for tool, family, arguments in (
                ("prometheus_range_query", "metrics", {
                    **window, "query": 'sum(up{kubernetes_namespace="sock-shop"})',
                    "step": "15s",
                }),
                ("loki_range_query", "logs", {
                    **window, "query": '{namespace="sock-shop"}', "limit": 150,
                }),
                ("tempo_search_traces", "traces", {
                    **window, "query": '{ resource.k8s.namespace.name = "sock-shop" }',
                    "limit": 20,
                }),
            ):
                item = self.query(tool, arguments, family, phase=phase)
                records.append({
                    key: item[key] for key in (
                        "id", "tool", "family", "phase", "arguments", "status",
                        "observation_count", "result_ref", "result_sha256",
                    )
                })
        manifest = {
            "status": "complete", "queries": records,
            "query_count": len(records),
            "no_data_count": sum(row["status"] == "no_data" for row in records),
        }
        write_once(self.directory / "coverage.json", manifest)
        return manifest

    def execute(self, check: Check, scope: set[str]) -> dict[str, Any]:
        tool, arguments, family, candidate = prepare_check(
            check, "sock-shop", self.windows, scope, self.calls, self.tools
        )
        return self.query(
            tool, arguments, family, service=check.service, phase=check.phase,
            candidate=candidate, kind=check.kind,
        )

    def index(self) -> dict[str, dict[str, Any]]:
        return {
            item["id"]: {
                **item, "evidence_id": item["id"],
                "phases": [
                    phase for phase in ("baseline", "fault", "post")
                    if phase in (item["digest"].get("phases") or {})
                ] or ([item["phase"]] if item["phase"] else ["baseline", "fault"]),
                "usable": item["status"] in {"ok", "partial", "success"}
                and item["fault_observation_count"] > 0,
                "text": canonical(item["digest"]),
                "result": item["digest"],
            }
            for item in self.evidence
        }
