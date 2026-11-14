"""Progressive Qwen RCA with evidence-family gates, alongside the legacy RCA."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
import yaml
from exceptiongroup import ExceptionGroup
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_console import (
    _mcp_exception_detail,
    _rca_evidence_bundle,
    _rca_next_trace_candidate,
    _rca_normalize_loki_range_result,
    _rca_normalize_trace_result,
    _rca_redact_text,
    _rca_tool_arguments,
    _rca_tool_digest,
    _rca_trace_candidates,
    rca_reference_phases,
)
from .analysis.edge_latency import (
    build_edge_latency_queries,
    normalize_edge_latency_result,
)
from .analysis.istio_edges import http_status_query, resolve_istio_destination
from .analysis.llm_client import LLMClientError, call_llm_tool_messages
from .analysis.system_card import load_system_card, render_system_card
from .analysis.telemetry_coverage import (
    phase_series_summary,
    termination_query,
    termination_summary,
)
from .evidence_projection import EVIDENCE_FORMAT_NOTICE, EvidenceProjectionSession
from .rca_options import MAX_ADDITIONAL_MCP_CHECKS, validate_possible_fault_types
from .resource_signature import (
    ResourceSignaturePolicy,
    resource_signature_metadata,
    resource_signature_text,
)

SKILL_DIR = Path(__file__).resolve().parents[3] / ".github" / "skills" / "rca"
MAX_LOG_SERVICES = 4
MAX_LOG_LINES = 150
MAX_SKILL_MODEL_ATTEMPTS = 3
_RAW_OVERRIDE = object()
TOOL_ERRORS = (
    httpx.HTTPError,
    McpError,
    OSError,
    RuntimeError,
    TimeoutError,
    asyncio.TimeoutError,
    ValueError,
    ExceptionGroup,
)


class SkillRCAError(ValueError):
    pass


class SkillBudgetExceeded(SkillRCAError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CausalHypothesis(StrictModel):
    hypothesis_id: Literal["H1", "H2", "H3", "H4", "H5"]
    service: str | None
    category: str = Field(max_length=80)
    mechanism: str = Field(min_length=1, max_length=600)
    supporting_evidence: list[str] = Field(max_length=10)
    contradicting_evidence: list[str] = Field(max_length=10)
    unresolved_questions: list[str] = Field(max_length=6)


class InvestigationPlan(StrictModel):
    candidates: list[str] = Field(min_length=1, max_length=3)
    hypotheses: list[CausalHypothesis] = Field(min_length=2, max_length=5)
    reasoning: str = Field(min_length=1, max_length=2000)


class SkillAdditionalCheck(StrictModel):
    kind: Literal[
        "logs",
        "traces",
        "trace_detail",
        "cpu",
        "memory",
        "lifecycle",
        "cpu_throttling",
        "gc",
        "http",
        "network",
    ]
    service: str
    phase: Literal["baseline", "fault", "post"]
    hypothesis: str = Field(min_length=1, max_length=600)
    expected_support: str = Field(min_length=1, max_length=600)
    expected_refutation: str = Field(min_length=1, max_length=600)
    patterns: list[str] = Field(default_factory=list, max_length=5)


class SkillAdditionalCheckDecision(StrictModel):
    action: Literal["query", "stop"]
    reason: str = Field(min_length=1, max_length=1000)
    check: SkillAdditionalCheck | None


class Alternative(StrictModel):
    hypothesis: str = Field(min_length=1, max_length=500)
    supporting_evidence: list[str] = Field(max_length=10)
    contradicting_evidence: list[str] = Field(max_length=10)


class RestartAnalysis(StrictModel):
    service: str
    reason_status: Literal["supported", "unknown"]
    reason: str = Field(min_length=1, max_length=500)
    supporting_evidence: list[str] = Field(max_length=10)
    next_check: str = Field(min_length=1, max_length=500)


class Diagnosis(StrictModel):
    status: Literal["confirmed", "inconclusive"]
    primary_service: str | None
    category: str = Field(max_length=80)
    fault_type: str | None = None
    mechanism: str = Field(min_length=1, max_length=800)
    supporting_evidence: list[str] = Field(max_length=20)
    contradicting_evidence: list[str] = Field(max_length=20)
    alternatives: list[Alternative] = Field(min_length=2, max_length=5)
    causal_chain: list[str] = Field(max_length=8)
    restart_analysis: list[RestartAnalysis] = Field(max_length=15)
    limitations: list[str] = Field(max_length=40)
    next_checks: list[str] = Field(max_length=10)


def load_skill() -> tuple[str, dict[str, str], str]:
    paths = [
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "references" / "evidence-contract.md",
        SKILL_DIR / "references" / "causal-checklist.md",
    ]
    texts = [path.read_text(encoding="utf-8") for path in paths]
    parts = texts[0].split("---", 2)
    if len(parts) != 3:
        raise SkillRCAError("Skill RCA is missing YAML frontmatter")
    meta = yaml.safe_load(parts[1])
    if (
        not isinstance(meta, dict)
        or meta.get("name") != "rca"
        or not isinstance(meta.get("description"), str)
        or not meta["description"].strip()
    ):
        raise SkillRCAError("Skill RCA name/description is invalid")
    references = {path.name: text for path, text in zip(paths[1:], texts[1:])}
    digest = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()
    return parts[2].strip(), references, digest


def _json_content(response: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    content = response.get("content")
    if not isinstance(content, str) or response.get("tool_calls"):
        raise SkillRCAError("Qwen must return a JSON object, not tool calls")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)[:-3].strip()
    value = json.loads(text, strict=strict)
    if not isinstance(value, dict):
        raise SkillRCAError("Qwen returned a non-object JSON response")
    return value


def _observation_count(
    name: str, result: dict[str, Any], phase_name: str | None = None
) -> int:
    def selected(phases: dict[str, Any]) -> list[dict[str, Any]]:
        values = [phases.get(phase_name)] if phase_name else phases.values()
        return [value for value in values if isinstance(value, dict)]

    if name == "tempo_search_traces":
        if phase_name and result.get("phase") != phase_name:
            return 0
        return len(result.get("traces") or [])
    if name == "tempo_service_graph_phases":
        return sum(
            int(phase.get("trace_count") or 0)
            for phase in selected(result.get("phases") or {})
        )
    if name == "tempo_analyze_trace":
        return int(result.get("span_count") or 0)
    if name == "history_archive_traces":
        if phase_name:
            phase = (result.get("phases") or {}).get(phase_name) or {}
            return int(phase.get("span_count") or 0)
        return int(result.get("span_count") or 0)
    if name == "loki_range_query":
        return sum(
            int((phase.get("coverage") or {}).get("lines_returned") or 0)
            for phase in selected(result.get("phases") or {})
        )
    if name in {"get_run_golden_signals", "check_network_packet_drops"}:
        return sum(
            int(phase.get("sample_count") or 0)
            for metric in (result.get("metrics") or {}).values()
            if isinstance(metric, dict)
            for phase in selected(metric.get("phases") or {})
        )
    if name == "prometheus_range_query":
        return sum(
            max(
                int((phase.get("coverage") or {}).get("sample_count") or 0),
                int((phase.get("coverage") or {}).get("event_count") or 0),
            )
            for phase in selected(result.get("phases") or {})
        )
    if name == "check_pod_restarts" and phase_name == "fault":
        coverage = result.get("coverage") or {}
        if "window_sample_count" in coverage:
            return int(coverage["window_sample_count"])
    coverage = result.get("coverage") or {}
    if coverage:
        return int(coverage.get("sample_count") or 0)
    return sum(
        int((phase.get("coverage") or {}).get("sample_count") or 0)
        for phase in selected(result.get("phases") or {})
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _rca_redact_text(value, 12000)
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


_KNOWN_METRIC_PROVENANCE = {
    "prometheus_golden_signals",
    "prometheus_kube_state_metrics",
    "prometheus_cadvisor",
    "prometheus_istio",
    "prometheus_jvm",
}
_SPAN_DERIVED_PROVENANCE = {
    "span_derived",
    "otel_span_metrics",
    "tempo_span_metrics",
    "prometheus_span_metrics",
}


def _evidence_source_provenance(
    tool: str,
    family: str,
    kind: str | None = None,
    result: dict[str, Any] | None = None,
) -> str:
    if family == "logs":
        return "loki_logs"
    if family == "traces":
        return "tempo_traces"
    if family != "metrics":
        return "unknown"
    if tool == "get_run_golden_signals":
        return "prometheus_golden_signals"
    if tool in {
        "check_service_readiness",
        "check_oom_kills",
        "check_pod_restarts",
    }:
        return "prometheus_kube_state_metrics"
    if tool in {
        "get_cpu_saturation_top",
        "get_memory_saturation_top",
        "check_network_packet_drops",
    }:
        return "prometheus_cadvisor"
    if tool == "prometheus_range_query":
        purpose_to_kind = {
            "http_status_rates": "http",
            "network_edge_latency": "network",
            "cpu_throttling": "cpu_throttling",
            "gc_collection_time": "gc",
        }
        kind = kind or purpose_to_kind.get((result or {}).get("purpose"))
        return {
            "http": "prometheus_istio",
            "network": "prometheus_istio",
            "lifecycle": "prometheus_kube_state_metrics",
            "cpu_throttling": "prometheus_cadvisor",
            "gc": "prometheus_jvm",
        }.get(kind, "unknown")
    return "unknown"


def _evidence_mentions_service(item: dict[str, Any], service: str) -> bool:
    if item.get("service") == service:
        return True
    content = item.get("text")
    if not isinstance(content, str):
        content = json.dumps(
            {
                key: item[key]
                for key in ("digest", "result", "arguments")
                if key in item
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return bool(
        re.search(
            rf"(?<![a-z0-9-]){re.escape(service.lower())}(?![a-z0-9-])",
            content.lower(),
        )
    )


def _resolved_evidence_provenance(item: dict[str, Any]) -> str:
    family = item.get("family")
    provenance = item.get("source_provenance")
    if not isinstance(provenance, str):
        provenance = _evidence_source_provenance(
            str(item.get("tool") or ""),
            str(family or ""),
            item.get("kind") if isinstance(item.get("kind"), str) else None,
            item.get("digest") if isinstance(item.get("digest"), dict) else None,
        )
    return provenance


def _independent_evidence_family(item: dict[str, Any]) -> str | None:
    if item.get("is_causal_evidence") is False:
        return None
    family = item.get("family")
    provenance = _resolved_evidence_provenance(item)
    if provenance in _SPAN_DERIVED_PROVENANCE:
        return "traces"
    if family == "logs" and provenance == "loki_logs":
        return "logs"
    if family == "traces" and provenance == "tempo_traces":
        return "traces"
    if family == "metrics" and provenance in _KNOWN_METRIC_PROVENANCE:
        return "metrics"
    return None


def _has_unknown_metric_provenance(
    evidence: list[dict[str, Any]], evidence_ids: list[str]
) -> bool:
    selected_ids = set(evidence_ids)
    return any(
        item.get("id") in selected_ids
        and item.get("family") == "metrics"
        and item.get("usable") is not False
        and item.get("status") in {"ok", "partial", "success"}
        and isinstance(item.get("fault_observation_count"), int)
        and item["fault_observation_count"] > 0
        and ("phases" not in item or "fault" in item.get("phases", []))
        and _resolved_evidence_provenance(item) == "unknown"
        for item in evidence
    )


def independent_evidence_families(
    evidence: list[dict[str, Any]],
    evidence_ids: list[str] | None = None,
    *,
    target_service: str | None = None,
) -> set[str]:
    selected_ids = set(evidence_ids) if evidence_ids is not None else None
    families = set()
    for item in evidence:
        if selected_ids is not None and item.get("id") not in selected_ids:
            continue
        fault_count = item.get("fault_observation_count")
        if (
            item.get("usable") is False
            or item.get("is_causal_evidence") is False
            or item.get("status") not in {"ok", "partial", "success"}
            or ("phases" in item and "fault" not in item.get("phases", []))
            or not isinstance(fault_count, int)
            or fault_count <= 0
        ):
            continue
        if target_service and not _evidence_mentions_service(item, target_service):
            continue
        family = _independent_evidence_family(item)
        if family:
            families.add(family)
    return families


def _evidence_result(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result") or item.get("digest")
    if isinstance(result, dict):
        return result
    text = item.get("text")
    if isinstance(text, str):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _http_fault_row_has_positive_rate(row: dict[str, Any]) -> bool:
    for key in ("mean", "max"):
        value = row.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        ):
            return True
    return False


def _fault_phase_result(
    item: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    phases = result.get("phases")
    if isinstance(phases, dict) and isinstance(phases.get("fault"), dict):
        return phases["fault"]
    if item.get("phase") == "fault" or result.get("phase") == "fault":
        return result
    return None


def _http_edge_error_spans(
    phase: dict[str, Any],
    edges: set[tuple[str | None, str]],
) -> bool:
    for span in phase.get("error_spans") or []:
        if not isinstance(span, dict) or not re.fullmatch(
            r"5\d\d", str(span.get("http_status_code", ""))
        ):
            continue
        service = span.get("service")
        remote_service = span.get("remote_service")
        if not remote_service:
            endpoint = span.get("remote_endpoint")
            if isinstance(endpoint, str):
                match = re.match(r"^(?:https?://)?([a-z0-9-]+)", endpoint)
                remote_service = match.group(1) if match else None
        if service and (service, remote_service) in edges:
            return True
        if service and any(target == service for _, target in edges):
            return True
    return False


def _http_edge_error_logs(
    item: dict[str, Any],
    result: dict[str, Any],
    phase: dict[str, Any],
    edges: set[tuple[str | None, str]],
) -> bool:
    source = result.get("service") or item.get("service")
    messages = [
        row.get("message")
        for row in phase.get("sample_lines") or []
        if isinstance(row, dict) and isinstance(row.get("message"), str)
    ]
    for signature in phase.get("signatures") or []:
        if not isinstance(signature, dict):
            continue
        messages.extend(
            example.get("message")
            for example in signature.get("examples") or []
            if isinstance(example, dict) and isinstance(example.get("message"), str)
        )
    for message in messages:
        if not re.search(r"\b5\d\d\b|HttpServerErrorException", message, re.I):
            continue
        for caller, target in edges:
            if source == target:
                return True
            if source != caller:
                continue
            endpoint = re.search(
                rf"(?:https?://)?{re.escape(target)}"
                rf"(?:\.sock-shop\.svc(?:\.cluster\.local)?|[/:])",
                message,
                re.I,
            )
            if endpoint:
                return True
    return False


def http_independent_evidence_families(
    evidence: list[dict[str, Any]],
    evidence_ids: list[str],
    target_service: str,
) -> set[str]:
    selected_ids = set(evidence_ids)
    fault_edges: set[tuple[str | None, str]] = set()

    for item in evidence:
        if (
            item.get("id") not in selected_ids
            or item.get("family") != "metrics"
            or item.get("is_causal_evidence") is False
            or _resolved_evidence_provenance(item) != "prometheus_istio"
            or item.get("status") not in {"ok", "partial", "success"}
            or item.get("usable") is False
            or ("phases" in item and "fault" not in item.get("phases", []))
            or (
                isinstance(item.get("fault_observation_count"), int)
                and item["fault_observation_count"] <= 0
            )
        ):
            continue
        result = _evidence_result(item)
        if (
            result.get("purpose") != "http_status_rates"
            or result.get("service") != target_service
        ):
            continue
        phase = _fault_phase_result(item, result)
        if phase is None:
            continue
        for row in phase.get("series") or []:
            if not _http_fault_row_has_positive_rate(row):
                continue
            labels = row.get("labels") or {}
            response_code = str(labels.get("response_code", ""))
            caller = labels.get("source_workload")
            namespace = (
                labels.get("source_workload_namespace")
                or labels.get("destination_workload_namespace")
                or "sock-shop"
            )
            if (
                re.fullmatch(r"5\d\d", response_code)
                and resolve_istio_destination(labels, namespace) == target_service
            ):
                fault_edges.add(
                    (
                        (
                            caller
                            if isinstance(caller, str) and caller != "unknown"
                            else None
                        ),
                        target_service,
                    )
                )

    families = {"metrics"} if fault_edges else set()
    for item in evidence:
        if (
            item.get("id") not in selected_ids
            or item.get("status") not in {"ok", "partial", "success"}
            or item.get("usable") is False
            or (
                isinstance(item.get("fault_observation_count"), int)
                and item["fault_observation_count"] <= 0
            )
        ):
            continue
        family = _independent_evidence_family(item)
        if family not in {"logs", "traces"} or not independent_evidence_families(
            [item]
        ):
            continue
        result = _evidence_result(item)
        phase = _fault_phase_result(item, result)
        if phase is None:
            continue
        if family == "traces" and _http_edge_error_spans(phase, fault_edges):
            families.add(family)
        elif family == "logs" and _http_edge_error_logs(
            item, result, phase, fault_edges
        ):
            families.add(family)
    return families


def _families(
    ids: list[str],
    evidence: list[dict[str, Any]],
    *,
    target_service: str | None = None,
) -> set[str]:
    return independent_evidence_families(evidence, ids, target_service=target_service)


def _edge_latency_candidates(
    results: list[dict[str, Any]], known_services: set[str]
) -> list[str]:
    candidates: list[str] = []
    for result in results:
        for metric in (result.get("metrics") or {}).values():
            for edge in metric.get("top_delta") or []:
                service = edge.get("service")
                if (
                    service in known_services
                    and edge.get("delta", 0) > 0
                    and service not in candidates
                ):
                    candidates.append(service)
    return candidates


def _edge_trace_search_targets(
    results: list[dict[str, Any]], known_services: set[str]
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    by_edge: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results:
        for metric_name, metric in (result.get("metrics") or {}).items():
            positive_rows = [
                row
                for row in metric.get("top_delta") or []
                if isinstance(row, dict)
                and isinstance(row.get("delta"), (int, float))
                and not isinstance(row.get("delta"), bool)
                and row["delta"] > 0
            ]
            if not positive_rows:
                continue
            row = max(positive_rows, key=lambda item: item["delta"])
            source = row.get("source_workload") or row.get("client")
            destination = (
                row.get("destination_workload")
                or row.get("server")
                or row.get("service")
            )
            if (
                source not in known_services
                or destination not in known_services
                or source == destination
            ):
                continue
            edge = (source, destination)
            existing = by_edge.get(edge)
            profile = {
                "metric": metric_name,
                "unit": metric.get("unit"),
                "baseline": row.get("baseline"),
                "fault": row.get("fault"),
                "delta": row["delta"],
                "baseline_sample_count": row.get("baseline_sample_count"),
                "fault_sample_count": row.get("fault_sample_count"),
            }
            if existing is None:
                existing = {
                    "source_service": source,
                    "destination_service": destination,
                    "metric_profiles": [],
                }
                by_edge[edge] = existing
                targets.append(existing)
            existing["metric_profiles"].append(profile)
    return targets[:3]


def _prepare_skill_additional_check(
    check: SkillAdditionalCheck,
    namespace: str,
    windows: dict[str, dict[str, str]],
    known_services: set[str],
    calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
) -> tuple[
    str,
    dict[str, Any],
    str,
    Callable[[dict[str, Any]], dict[str, Any]] | None,
    dict[str, Any] | None,
]:
    if check.service not in known_services or check.phase not in windows:
        raise SkillRCAError("Additional check is outside the known service/time scope")
    window = windows[check.phase]
    family = "metrics"
    normalize = None
    candidate = None

    if check.kind == "logs":
        name, family = "loki_range_query", "logs"
        query = (
            f'{{namespace="{namespace}",container=~"{check.service}|istio-proxy",'
            f'pod=~"^{re.escape(check.service)}(-[a-z0-9]+)*$"}}'
        )
        if any(not pattern or len(pattern) > 160 for pattern in check.patterns):
            raise SkillRCAError("Additional log patterns must be non-empty and bounded")
        if check.patterns:
            expression = "|".join(re.escape(pattern) for pattern in check.patterns)
            query += " |~ " + json.dumps(expression)
        arguments = {
            "query": query,
            "start": window["start"],
            "end": window["end"],
            "limit": MAX_LOG_LINES,
        }

        def normalize_logs(raw: dict[str, Any]) -> dict[str, Any]:
            normalized = _rca_normalize_loki_range_result(
                raw,
                service=check.service,
                namespace=namespace,
                fault_window=window,
                line_limit=MAX_LOG_LINES,
            )
            phases = normalized.get("phases") or {}
            if "fault" in phases:
                normalized["phases"] = {check.phase: phases["fault"]}
            return normalized

        normalize = normalize_logs
    elif check.kind == "trace_detail":
        name, family = "tempo_analyze_trace", "traces"
        candidate = _rca_next_trace_candidate(calls)
        if candidate is None or candidate.get("phase") != check.phase:
            raise SkillRCAError("No matching current trace candidate is available")
        if candidate.get("service") and candidate["service"] != check.service:
            raise SkillRCAError("Trace candidate belongs to a different service")
        arguments = {"trace_id": candidate["trace_id"]}
        normalize = lambda raw: _rca_normalize_trace_result(raw, candidate)
    elif check.kind == "traces":
        name, family = "tempo_service_graph_phases", "traces"
        arguments = _rca_tool_arguments(
            name,
            {},
            (tools.get(name) or {}).get("inputSchema") or {},
            phases=windows,
            namespace=namespace,
        )
        arguments["query"] = (
            f'{{ resource.k8s.namespace.name = "{namespace}" '
            f'&& resource.service.name = "{check.service}" }}'
        )
    elif check.kind in {"cpu", "memory"}:
        name, family = f"get_{check.kind}_saturation_top", "metrics"
        arguments = {
            "start": window["start"],
            "end": window["end"],
            "namespace": namespace,
            "service": check.service,
            "step": "15s",
            "k": 3,
        }
    elif check.kind in {
        "lifecycle",
        "cpu_throttling",
        "gc",
        "http",
        "network",
    }:
        name, family = "prometheus_range_query", "metrics"
        namespace_selector = json.dumps(namespace)
        workload_selector = json.dumps(check.service)
        pod_selector = json.dumps(f"^{re.escape(check.service)}-[a-z0-9]+-[a-z0-9]+$")
        if check.kind == "lifecycle":
            query = termination_query(namespace, [check.service])
        elif check.kind == "cpu_throttling":
            selector = f"namespace={namespace_selector},pod=~{pod_selector}"
            query = (
                f"100 * sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[1m]))"
                f" / sum(rate(container_cpu_cfs_periods_total{{{selector}}}[1m]))"
            )
        elif check.kind == "gc":
            query = (
                "sum by(gc)(rate(jvm_gc_collection_seconds_sum{"
                f"kubernetes_namespace={namespace_selector},kubernetes_name={workload_selector}"
                "}[1m]))"
            )
        elif check.kind == "http":
            query = http_status_query(namespace, check.service)
        else:
            selector = (
                f"destination_workload_namespace={namespace_selector},"
                f'destination_workload={workload_selector},reporter="source",'
                'source_workload!="unknown"'
            )
            group = "sum by(source_workload,destination_workload)"
            query = (
                f"{group}(rate(istio_request_duration_milliseconds_sum{{{selector}}}[1m]))"
                f" / {group}(rate(istio_request_duration_milliseconds_count{{{selector}}}[1m]))"
            )
        arguments = {
            "query": query,
            "start": windows["baseline"]["start"],
            "end": windows.get("post", windows["fault"])["end"],
            "step": "15s",
        }
        if check.kind == "lifecycle":
            normalize = lambda raw: termination_summary(raw, windows)
        elif check.kind in {"cpu_throttling", "gc", "http", "network"}:
            purposes = {
                "cpu_throttling": ("cpu_throttling", "%"),
                "gc": ("gc_collection_time", "s/s"),
                "http": ("http_status_rates", "req/s"),
                "network": ("network_edge_latency", "ms"),
            }
            purpose, unit = purposes[check.kind]
            normalize = lambda raw: phase_series_summary(
                raw,
                windows,
                purpose=purpose,
                unit=unit,
                service=check.service,
                namespace=namespace,
            )
    else:
        raise SkillRCAError(f"Unsupported additional check kind: {check.kind}")

    if name not in tools:
        raise SkillRCAError(f"Requested MCP tool unavailable: {name}")
    if any(call["tool"] == name and call["arguments"] == arguments for call in calls):
        raise SkillRCAError("Additional check repeats an executed MCP query")
    return name, arguments, family, normalize, candidate


def validate_diagnosis(
    diagnosis: Diagnosis,
    evidence: list[dict[str, Any]],
    known_services: set[str],
    categories: set[str],
    restarted_services: list[str],
    possible_fault_types: list[str] | None = None,
    require_best_guess: bool = False,
) -> Diagnosis:
    if require_best_guess and diagnosis.primary_service not in known_services:
        raise SkillRCAError(
            "Diagnosis must select one known primary service as the best guess"
        )
    if (
        diagnosis.primary_service is not None
        and diagnosis.primary_service not in known_services
    ):
        diagnosis.limitations.append(
            f"Servico primario nao reconhecido: {diagnosis.primary_service}; origem nao validada"
        )
        diagnosis.primary_service = None
        diagnosis.status = "inconclusive"
    if diagnosis.category not in categories | {"unknown"}:
        diagnosis.limitations.append(
            f"Categoria nao reconhecida: {diagnosis.category}; mecanismo nao classificado"
        )
        diagnosis.category = "unknown"
        diagnosis.status = "inconclusive"
    if possible_fault_types and diagnosis.fault_type not in possible_fault_types:
        raise SkillRCAError(
            "Diagnosis did not select one of the supplied benchmark fault types"
        )
    ids = {item["id"] for item in evidence}

    def retain_known_refs(refs: list[str], label: str) -> list[str]:
        invalid = sorted(set(refs) - ids)
        if invalid:
            diagnosis.limitations.append(
                f"{label}: referencias inexistentes removidas ({', '.join(invalid[:5])})"
            )
            diagnosis.status = "inconclusive"
        return [identifier for identifier in refs if identifier in ids]

    diagnosis.supporting_evidence = retain_known_refs(
        diagnosis.supporting_evidence, "Evidencias de suporte"
    )
    diagnosis.contradicting_evidence = retain_known_refs(
        diagnosis.contradicting_evidence, "Evidencias contraditorias"
    )
    overlap = set(diagnosis.supporting_evidence) & set(diagnosis.contradicting_evidence)
    if overlap:
        diagnosis.contradicting_evidence = [
            identifier
            for identifier in diagnosis.contradicting_evidence
            if identifier not in overlap
        ]
        diagnosis.limitations.append(
            "O modelo classificou as mesmas evidencias como favoraveis e contrarias"
        )
        diagnosis.status = "inconclusive"

    for alternative in diagnosis.alternatives:
        alternative.supporting_evidence = retain_known_refs(
            alternative.supporting_evidence, "Suporte de hipotese alternativa"
        )
        alternative.contradicting_evidence = retain_known_refs(
            alternative.contradicting_evidence, "Contradicoes de hipotese alternativa"
        )

    expected_restarts = set(restarted_services)
    restart_by_service: dict[str, RestartAnalysis] = {}
    for restart in diagnosis.restart_analysis:
        if restart.service not in expected_restarts:
            diagnosis.limitations.append(
                f"Restart atribuido a {restart.service} sem evento confirmado; ignorado"
            )
            diagnosis.status = "inconclusive"
            continue
        if restart.service in restart_by_service:
            diagnosis.limitations.append(
                f"Analise de restart duplicada para {restart.service}; mantida a primeira"
            )
            diagnosis.status = "inconclusive"
            continue
        restart.supporting_evidence = retain_known_refs(
            restart.supporting_evidence, f"Suporte do restart de {restart.service}"
        )
        restart_by_service[restart.service] = restart
    for service in sorted(expected_restarts - set(restart_by_service)):
        restart_by_service[service] = RestartAnalysis(
            service=service,
            reason_status="unknown",
            reason="O restart foi observado, mas a resposta nao apresentou um motivo verificavel.",
            supporting_evidence=[],
            next_check=(
                "Consultar eventos Kubernetes, exit code e resultados de probes "
                "imediatamente antes do restart."
            ),
        )
        diagnosis.limitations.append(
            f"{service}: motivo do restart nao explicado pelo modelo"
        )
        diagnosis.status = "inconclusive"
    diagnosis.restart_analysis = [
        restart_by_service[service] for service in sorted(restart_by_service)
    ]

    for restart in diagnosis.restart_analysis:
        if (
            restart.reason_status == "supported"
            and len(
                _families(
                    restart.supporting_evidence,
                    evidence,
                    target_service=restart.service,
                )
            )
            < 2
        ):
            restart.reason_status = "unknown"
            diagnosis.limitations.append(
                f"{restart.service}: motivo do restart sem corroboração independente"
            )
    support_families = (
        http_independent_evidence_families(
            evidence, diagnosis.supporting_evidence, diagnosis.primary_service
        )
        if diagnosis.fault_type == "http-status-code" and diagnosis.primary_service
        else _families(diagnosis.supporting_evidence, evidence)
    )
    gates = []
    if len(support_families) < 2:
        gates.append("Causa sem suporte de duas familias de telemetria independentes")
        if _has_unknown_metric_provenance(evidence, diagnosis.supporting_evidence):
            diagnosis.limitations.append(
                "Proveniencia de uma metrica desconhecida; ela nao foi contada como "
                "corroboracao independente."
            )
    if not diagnosis.primary_service or diagnosis.category == "unknown":
        gates.append("Origem ou mecanismo nao determinados")
    restart = restart_by_service.get(diagnosis.primary_service or "")
    if restart and restart.reason_status != "supported":
        gates.append("Motivo do restart no servico de origem ainda nao explicado")
    if not diagnosis.causal_chain:
        gates.append("Sequencia causal nao estabelecida")
    if gates:
        diagnosis.status = "inconclusive"
        diagnosis.limitations.extend(gates)
    return diagnosis


def answer_skill_rca(
    *,
    mcp_client: Any,
    phases: dict[str, dict[str, str]],
    namespace: str,
    max_additional_checks: int = 0,
    max_model_calls: int | None = None,
    max_total_tokens: int | None = None,
    timeout_seconds: int | None = None,
    window_metadata: dict[str, Any] | None = None,
    possible_fault_types: list[str] | None = None,
    resource_signature_policy: ResourceSignaturePolicy = "none",
    require_best_guess: bool = False,
    archive_traces: dict[str, Any] | None = None,
    evidence_source: Literal["live", "history"] = "live",
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    evidence_projector: EvidenceProjectionSession | None = None,
    llm: Callable[..., dict[str, Any]] = call_llm_tool_messages,
) -> dict[str, Any]:
    skill, references, skill_hash = load_skill()
    possible_fault_types = validate_possible_fault_types(
        possible_fault_types if possible_fault_types is not None else []
    )
    if require_best_guess and not possible_fault_types:
        raise SkillRCAError(
            "Best-guess classification requires benchmark fault-type options"
        )
    if (
        isinstance(max_additional_checks, bool)
        or not isinstance(max_additional_checks, int)
        or not 0 <= max_additional_checks <= MAX_ADDITIONAL_MCP_CHECKS
    ):
        raise SkillRCAError(
            "Additional MCP check budget must be between zero and "
            f"{MAX_ADDITIONAL_MCP_CHECKS}"
        )
    if not re.fullmatch(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", namespace):
        raise SkillRCAError("Invalid namespace")
    card = load_system_card(namespace)
    known = set((card.get("services") or {}).keys())
    allowed_categories = sorted(card.get("allowed_fault_categories") or [])
    if not known or not {"baseline", "fault"}.issubset(phases):
        raise SkillRCAError(
            "Skill RCA requires a system card and baseline/fault windows"
        )
    windows, reference_source = rca_reference_phases(
        phases,
        baseline_source=((window_metadata or {}).get("phase_sources") or {}).get(
            "baseline"
        ),
    )
    signature_prompt = resource_signature_text(resource_signature_policy)
    for window in windows.values():
        start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise SkillRCAError("Skill RCA requires ordered timezone-aware windows")
        if (end - start).total_seconds() > 1800:
            raise SkillRCAError("Each Skill RCA phase is limited to 30 minutes")

    calls: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    limitations: list[str] = []
    model_attempts: list[dict[str, Any]] = []
    model_budget_rejections: list[dict[str, Any]] = []
    budget_limits = (max_model_calls, max_total_tokens, timeout_seconds)
    budget_enabled = any(value is not None for value in budget_limits)
    if budget_enabled and (
        max_model_calls is None or max_total_tokens is None or timeout_seconds is None
    ):
        raise SkillRCAError("Skill model budget limits must be set together")
    if budget_enabled and (
        isinstance(max_model_calls, bool)
        or not isinstance(max_model_calls, int)
        or not 6 <= max_model_calls <= 30
        or isinstance(max_total_tokens, bool)
        or not isinstance(max_total_tokens, int)
        or not 8_000 <= max_total_tokens <= 500_000
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= 1_800
    ):
        raise SkillRCAError("Skill model budget limits are outside the allowed range")

    model_call_limit = max_model_calls if max_model_calls is not None else 0
    token_limit = max_total_tokens if max_total_tokens is not None else 0
    time_limit = timeout_seconds if timeout_seconds is not None else 0
    budget_started = time.monotonic()
    model_calls_used = 0
    model_tokens_used = 0

    def model_stage(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("stage"), str):
                return payload["stage"]
        return "model_call"

    def budgeted_llm(
        messages: list[dict[str, Any]], tools: list, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal model_calls_used, model_tokens_used
        if not budget_enabled:
            return llm(messages, tools, **kwargs)
        if model_calls_used >= model_call_limit:
            raise SkillBudgetExceeded("Skill model call budget exhausted")
        if time.monotonic() - budget_started >= time_limit:
            raise SkillBudgetExceeded("Skill model timeout exhausted")
        estimated_input = (
            len(
                json.dumps(
                    messages,
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            // 4
        )
        max_new_tokens = kwargs.get("max_new_tokens", 4_096)
        if (
            isinstance(max_new_tokens, bool)
            or not isinstance(max_new_tokens, int)
            or max_new_tokens < 0
        ):
            raise SkillRCAError("Model max_new_tokens must be a non-negative integer")
        stage = model_stage(messages)
        estimated_total = model_tokens_used + estimated_input + max_new_tokens
        if estimated_total > token_limit:
            model_budget_rejections.append(
                {
                    "stage": stage,
                    "reason": "token_budget",
                    "tokens_before": model_tokens_used,
                    "estimated_input_tokens": estimated_input,
                    "reserved_output_tokens": max_new_tokens,
                    "estimated_total_tokens": estimated_total,
                    "token_limit": token_limit,
                }
            )
            raise SkillBudgetExceeded("Skill model token budget exhausted")

        attempt = {
            "stage": stage,
            "status": "started",
            "estimated_input_tokens": estimated_input,
            "reserved_output_tokens": max_new_tokens,
            "tokens_before": model_tokens_used,
        }
        model_attempts.append(attempt)
        model_calls_used += 1
        started = time.monotonic()
        returned = False
        try:
            response = llm(messages, tools, **kwargs)
            returned = True
        except (*TOOL_ERRORS, LLMClientError) as exc:
            attempt.update(
                status="error",
                error=_rca_redact_text(_mcp_exception_detail(exc)),
            )
            raise
        finally:
            attempt["duration_ms"] = round((time.monotonic() - started) * 1000)
            if not returned and attempt["status"] == "started":
                attempt["status"] = "error"

        usage_value = response.get("usage")
        if usage_value is not None and not isinstance(usage_value, dict):
            attempt["status"] = "error"
            raise SkillRCAError("Model returned invalid token usage")
        usage = usage_value or {}
        reported_tokens = usage.get("total_tokens")
        if reported_tokens is not None and (
            isinstance(reported_tokens, bool)
            or not isinstance(reported_tokens, int)
            or reported_tokens < 0
        ):
            attempt["status"] = "error"
            raise SkillRCAError("Model returned invalid total_tokens usage")
        provider_tokens = isinstance(reported_tokens, int) and reported_tokens > 0
        used_tokens = (
            reported_tokens
            if provider_tokens
            else estimated_input + len(str(response.get("content", ""))) // 4
        )
        model_tokens_used += used_tokens
        attempt.update(
            status="returned",
            usage=usage,
            total_tokens=used_tokens,
            token_usage="provider" if provider_tokens else "estimated",
            cumulative_tokens=model_tokens_used,
        )
        return response

    model_llm = budgeted_llm if budget_enabled else llm
    evidence_projector = evidence_projector or EvidenceProjectionSession(
        model_llm,
        model=model,
        temperature=temperature,
        seed=seed,
        attempt_log=None if budget_enabled else model_attempts,
    )
    if budget_enabled and evidence_projector.llm is not model_llm:
        evidence_projector.llm = model_llm
        evidence_projector.attempt_log = None
    restarted: list[str] = []
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                skill
                + "\n"
                + render_system_card(namespace)
                + "\n"
                + EVIDENCE_FORMAT_NOTICE
                + "\nReferencias de evidencia são IDs exatos em listas próprias; "
                "nunca acrescente explicações ao ID. Mantenha causa e narrativa "
                "em campos separados."
                + ("\n\n" + signature_prompt if signature_prompt else "")
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "namespace": namespace,
                    "phases": windows,
                    "reference_source": reference_source,
                    "window_metadata": window_metadata or {},
                    "possible_fault_types": possible_fault_types,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]

    def query(
        name: str,
        level: int,
        family: str,
        arguments: dict[str, Any],
        normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        raw_override: Any = _RAW_OVERRIDE,
        *,
        service: str | None = None,
        phase: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if raw_override is not _RAW_OVERRIDE:
            if not isinstance(raw_override, dict):
                result = {
                    "status": "tool_error",
                    "error": "Archive returned a non-object result",
                }
            else:
                result = normalize(raw_override) if normalize else raw_override
        elif name not in tools:
            result = {"status": "tool_error", "error": f"MCP tool unavailable: {name}"}
        else:
            try:
                raw = mcp_client.call_tool_json(name, arguments)
                if not isinstance(raw, dict):
                    raise SkillRCAError("MCP returned a non-object result")
                if raw.get("status") == "error" or raw.get("isError"):
                    result = {
                        "status": "tool_error",
                        "error": _rca_redact_text(raw.get("error", "MCP tool failed")),
                    }
                else:
                    result = normalize(raw) if normalize else raw
            except TOOL_ERRORS as exc:
                result = {"status": "tool_error", "error": _mcp_exception_detail(exc)}
        result = _sanitize(result)
        try:
            count = _observation_count(name, result, phase)
            fault_count = _observation_count(name, result, "fault")
            if kind in {"cpu", "memory"}:
                fault_count = count if phase == "fault" else 0
            if count < 0 or fault_count < 0:
                raise SkillRCAError("MCP observation counts cannot be negative")
        except (ValueError, TypeError, AttributeError) as exc:
            result = {
                "status": "tool_error",
                "error": f"Invalid MCP coverage: {_mcp_exception_detail(exc)}",
            }
            count = fault_count = 0
        status = result.get("status", "ok" if count else "no_data")
        identifier = f"E{len(evidence) + 1:03d}"
        item = {
            "id": identifier,
            "level": level,
            "tool": name,
            "family": family,
            "source_provenance": _evidence_source_provenance(
                name, family, kind, result
            ),
            "status": status,
            "source": (
                evidence_source
                if evidence_source == "history"
                and result.get("evidence_source") in {None, "live"}
                else result.get("evidence_source", evidence_source)
            ),
            "observation_count": count,
            "fault_observation_count": fault_count,
            **({"is_causal_evidence": False} if name == "tempo_search_traces" else {}),
            "arguments": arguments,
            "digest": _rca_tool_digest(name, result),
            **({"service": service} if service else {}),
            **({"phase": phase} if phase else {}),
            **({"kind": kind} if kind else {}),
        }
        evidence.append(item)
        calls.append(
            {
                "tool": name,
                "level": level,
                "arguments": arguments,
                "evidence_id": identifier,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "result": {**result, "status": status},
                **({"service": service} if service else {}),
                **({"phase": phase} if phase else {}),
                **({"kind": kind} if kind else {}),
            }
        )
        if status != "ok":
            limitations.append(f"{item['id']} {name}: {status}")
        for phase_name, phase in (result.get("phases") or {}).items():
            if not isinstance(phase, dict):
                continue
            coverage = phase.get("coverage") or {}
            if coverage.get("truncated"):
                limitations.append(
                    f"{item['id']} {phase_name}: logs no limite da consulta"
                )
            if name == "tempo_service_graph_phases" and phase.get(
                "trace_count", 0
            ) >= arguments.get("limit", 20):
                limitations.append(
                    f"{item['id']} {phase_name}: limite de traces atingido"
                )
        return result

    def model_json(
        stage: str,
        schema: type[StrictModel],
        context: dict[str, Any],
        *,
        allowed_fault_types: list[str] | None = None,
        allowed_services: list[str] | None = None,
        allowed_categories: list[str] | None = None,
        validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        output_schema = schema.model_json_schema()
        if allowed_fault_types:
            field = output_schema.get("properties", {}).get("fault_type")
            if not isinstance(field, dict):
                raise SkillRCAError(f"Schema for {stage} has no fault_type field")
            field.clear()
            field.update({"type": "string", "enum": allowed_fault_types})
            required = output_schema.setdefault("required", [])
            if "fault_type" not in required:
                required.append("fault_type")
        if allowed_services:
            field = output_schema.get("properties", {}).get("primary_service")
            if isinstance(field, dict):
                field.clear()
                field.update({"type": "string", "enum": allowed_services})
                required = output_schema.setdefault("required", [])
                if "primary_service" not in required:
                    required.append("primary_service")

        allowed_evidence_ids = sorted(item["id"] for item in evidence)

        def constrain_schema(node: Any, property_name: str | None = None) -> None:
            if isinstance(node, dict):
                if property_name == "category" and allowed_categories:
                    node["enum"] = allowed_categories
                if property_name == "service" and allowed_services:
                    if isinstance(node.get("anyOf"), list):
                        for option in node["anyOf"]:
                            if option.get("type") == "string":
                                option["enum"] = allowed_services
                    else:
                        node["enum"] = allowed_services
                if (
                    property_name in {"supporting_evidence", "contradicting_evidence"}
                    and node.get("type") == "array"
                ):
                    if allowed_evidence_ids:
                        node.setdefault("items", {})["enum"] = allowed_evidence_ids
                    else:
                        node["maxItems"] = 0
                for key, value in node.items():
                    constrain_schema(value, key)
            elif isinstance(node, list):
                for value in node:
                    constrain_schema(value, property_name)

        constrain_schema(output_schema)
        if schema is SkillAdditionalCheckDecision:
            check_schema = output_schema["$defs"]["SkillAdditionalCheck"]["properties"]
            check_schema["service"]["enum"] = sorted(known)
            check_schema["phase"]["enum"] = sorted(windows)
        if allowed_services:
            candidate_items = (
                output_schema.get("properties", {}).get("candidates") or {}
            ).get("items")
            if isinstance(candidate_items, dict):
                candidate_items["enum"] = allowed_services
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "stage": stage,
                        "context": context,
                        "evidence": evidence_projector.model_view(evidence),
                        "allowed_evidence_ids": (
                            allowed_evidence_ids
                            if schema in {InvestigationPlan, Diagnosis}
                            else None
                        ),
                        "output_schema": output_schema,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
        for attempt in range(MAX_SKILL_MODEL_ATTEMPTS):
            # Each stage already carries its observations and current evidence.
            current_stage = next(
                index
                for index in range(len(messages) - 1, 1, -1)
                if messages[index]["role"] == "user"
                and messages[index]["content"].startswith('{"stage":')
            )
            model_messages = [
                message
                for message in messages[:current_stage]
                if message["role"] in {"user", "system"}
                and not message["content"].startswith(
                    ('{"stage":', "Resposta invalida:")
                )
            ] + messages[current_stage:]
            generation_options: dict[str, Any] = {}
            if model is not None:
                generation_options["model"] = model
            if temperature is not None:
                generation_options["temperature"] = temperature
            if seed is not None:
                generation_options["seed"] = seed
            attempt_index = len(model_attempts)
            response = model_llm(
                model_messages,
                [],
                tool_choice="none",
                parallel_tool_calls=False,
                max_new_tokens=4096,
                chat_template_kwargs={"enable_thinking": False},
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": output_schema,
                    },
                },
                **generation_options,
            )
            call_attempt = (
                model_attempts[attempt_index]
                if budget_enabled and len(model_attempts) > attempt_index
                else None
            )
            try:
                value = _json_content(response)
                validated = schema.model_validate(value).model_dump()
                if validator is not None:
                    validator(validated)
                if (
                    allowed_fault_types
                    and value.get("fault_type") not in allowed_fault_types
                ):
                    raise ValueError(
                        "fault_type must exactly match one of "
                        + ", ".join(allowed_fault_types)
                    )
                if (
                    allowed_services
                    and "primary_service" in schema.model_fields
                    and value.get("primary_service") not in allowed_services
                ):
                    raise ValueError(
                        "primary_service must select one of "
                        + ", ".join(allowed_services)
                    )
            except (ValueError, ValidationError) as exc:
                if call_attempt is not None:
                    call_attempt.update(
                        status="invalid_response",
                        error=_rca_redact_text(exc),
                    )
                else:
                    model_attempts.append(
                        {
                            "stage": stage,
                            "status": "invalid_response",
                            "error": _rca_redact_text(exc),
                        }
                    )
                if attempt + 1 == MAX_SKILL_MODEL_ATTEMPTS:
                    raise SkillRCAError(f"Qwen returned invalid {stage} JSON") from exc
                messages.append(
                    {
                        "role": "user",
                        "content": "Resposta invalida: "
                        + _rca_redact_text(exc)
                        + ". Retorne JSON valido estrito, sem texto antes/depois, "
                        "sem markdown e sem tool calls. Use campos narrativos curtos "
                        "em portugues e escape aspas internas.",
                    }
                )
            else:
                if call_attempt is not None:
                    call_attempt["status"] = "valid_response"
                else:
                    model_attempts.append({"stage": stage, "status": "valid_response"})
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(value, ensure_ascii=False),
                    }
                )
                return value
        raise SkillRCAError("Qwen did not produce a response")

    diagnosis: Diagnosis | None = None
    error = None
    tools: dict[str, dict[str, Any]] = {}
    planning_was_invalid = False
    golden_result: dict[str, Any] = {}
    edge_latency_results: list[dict[str, Any]] = []
    additional_checks: list[dict[str, Any]] = []
    additional_check_error: str | None = None
    additional_check_stop_reason: str | None = None
    try:
        tools = {
            tool["name"]: tool
            for tool in mcp_client.list_tools()
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        messages.append({"role": "user", "content": references["evidence-contract.md"]})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Em HTTP, distinga a aplicacao-alvo do proxy que respondeu. FI em "
                    "response_flags indica fault injection no proxy; destination_workload "
                    "unknown nao elimina a identidade quando o FQDN valida o servico. "
                    "Compare codigo/flags/aresta por reporter e busque logs do chamador "
                    "e seu istio-proxy ou spans CLIENT. Um abort pode impedir o span "
                    "SERVER e o log da aplicacao-alvo. Ausencia desses registros nao "
                    "comprova o abort; exija corroboracao da mesma ocorrencia. "
                    "Complemente com familias http_client_* e http_server_* do SDK, "
                    "por namespace/job e codigo/metodo/rota, separando probes de "
                    "trafego de negocio e respeitando as unidades dos histogramas."
                ),
            }
        )
        for name, level in (
            ("get_run_golden_signals", 1),
            ("check_service_readiness", 2),
            ("check_oom_kills", 2),
            ("check_pod_restarts", 2),
        ):
            arguments = _rca_tool_arguments(
                name,
                {},
                tools.get(name, {}).get("inputSchema") or {},
                phases=windows,
                namespace=namespace,
            )
            result = query(name, level, "metrics", arguments)
            if name == "get_run_golden_signals":
                golden_result = result
            if name == "check_pod_restarts":
                restarted = sorted(
                    {
                        row["service"]
                        for row in result.get("top", [])
                        if row.get("service") in known
                        and row.get("restarts_in_window", 0) > 0
                    }
                )
        if "prometheus_range_query" in tools and known:
            arguments = {
                "query": termination_query(namespace, sorted(known)),
                "start": windows["baseline"]["start"],
                "end": windows.get("post", windows["fault"])["end"],
                "step": "15s",
            }
            query(
                "prometheus_range_query",
                2,
                "metrics",
                arguments,
                lambda raw: termination_summary(raw, windows),
                kind="lifecycle",
            )
        else:
            limitations.append(
                "Historico de terminacao indisponivel: prometheus_range_query ausente ou sem servicos conhecidos"
            )
        if "prometheus_range_query" in tools:
            query_end = windows.get("post", windows["fault"])["end"]
            query(
                "prometheus_range_query",
                1,
                "metrics",
                {
                    "query": http_status_query(namespace),
                    "start": windows["baseline"]["start"],
                    "end": query_end,
                    "step": "15s",
                },
                lambda raw: phase_series_summary(
                    raw,
                    windows,
                    purpose="http_status_rates",
                    unit="req/s",
                    service=None,
                    namespace=namespace,
                ),
                kind="http",
            )
            for spec in build_edge_latency_queries(namespace, sorted(known)):
                arguments = {
                    "query": spec["query"],
                    "start": windows["baseline"]["start"],
                    "end": query_end,
                    "step": "15s",
                }
                edge_latency_results.append(
                    query(
                        "prometheus_range_query",
                        1,
                        "metrics",
                        arguments,
                        lambda raw, query_spec=spec: normalize_edge_latency_result(
                            raw,
                            spec=query_spec,
                            phases=windows,
                            known_services=sorted(known),
                        ),
                        kind="network",
                    )
                )
        else:
            limitations.append(
                "Latencia por aresta indisponivel: prometheus_range_query ausente"
            )
        edge_candidates = _edge_latency_candidates(edge_latency_results, known)

        def validate_plan(value: dict[str, Any]) -> None:
            identifiers = {
                item["id"] for item in evidence if isinstance(item.get("id"), str)
            }
            hypotheses = value.get("hypotheses") or []
            signatures = set()
            hypothesis_ids = set()
            for hypothesis in hypotheses:
                hypothesis_id = hypothesis["hypothesis_id"]
                if hypothesis_id in hypothesis_ids:
                    raise ValueError("plan hypothesis IDs must be unique")
                hypothesis_ids.add(hypothesis_id)
                service = hypothesis.get("service")
                if service is not None and service not in known:
                    raise ValueError(
                        "hypothesis service must be a known service or null"
                    )
                if hypothesis.get("category") not in allowed_categories:
                    raise ValueError(
                        "hypothesis category must be allowed by the system card"
                    )
                references = set(hypothesis.get("supporting_evidence") or []) | set(
                    hypothesis.get("contradicting_evidence") or []
                )
                if references - identifiers:
                    raise ValueError(
                        "hypothesis references must be exact current evidence IDs"
                    )
                signature = (
                    service,
                    hypothesis.get("category"),
                    hypothesis.get("mechanism", "").strip().casefold(),
                )
                if signature in signatures:
                    raise ValueError("plan hypotheses must be causally distinct")
                signatures.add(signature)

        plan = InvestigationPlan.model_validate(
            model_json(
                "plan",
                InvestigationPlan,
                {
                    "restarted_services": restarted,
                    "edge_latency_candidates": edge_candidates,
                    "known_services": sorted(known),
                    "allowed_categories": allowed_categories,
                    "hypothesis_contract": (
                        "Retorne ao menos duas hipóteses causalmente distintas. "
                        "Cada uma deve nomear serviço conhecido ou null, categoria "
                        "permitida, mecanismo verificável, IDs exatos de suporte/"
                        "contradição e perguntas em aberto. Não trate ranking como prova."
                    ),
                },
                allowed_services=sorted(known),
                allowed_categories=allowed_categories,
                validator=validate_plan,
            )
        )
        invalid_candidates = sorted(set(plan.candidates) - known)
        if invalid_candidates:
            limitations.append(
                "Candidatos desconhecidos descartados: "
                + ", ".join(invalid_candidates[:5])
            )
            planning_was_invalid = True
            plan.candidates = [
                candidate for candidate in plan.candidates if candidate in known
            ]
        for candidate in edge_candidates:
            if len(plan.candidates) >= 3:
                break
            if candidate not in plan.candidates:
                plan.candidates.append(candidate)
        if not plan.candidates:
            metric_candidates = list(edge_candidates)
            for metric in (golden_result.get("metrics") or {}).values():
                if not isinstance(metric, dict):
                    continue
                for row in metric.get("top") or []:
                    service = row.get("service") if isinstance(row, dict) else None
                    if service in known and service not in metric_candidates:
                        metric_candidates.append(service)
            plan.candidates = metric_candidates[:3]
            if not plan.candidates:
                limitations.append(
                    "Nenhum candidato de servico validado; logs limitados aos servicos com restart"
                )
        services = list(dict.fromkeys(restarted + plan.candidates))
        for service in services[MAX_LOG_SERVICES:]:
            limitations.append(
                f"{service}: logs nao consultados por limite de servicos"
            )
        for service in services[:MAX_LOG_SERVICES]:
            for phase_name, window in windows.items():
                if phase_name not in {"baseline", "fault", "post"}:
                    continue
                # Keep proxy failures on the same pod, without including app-db pods.
                arguments = {
                    "query": (
                        f'{{namespace="{namespace}",container=~"{service}|istio-proxy",'
                        'pod=~"^'
                        + re.escape(service).replace(r"\-", "-")
                        + '(-[a-z0-9]+)*$"}'
                    ),
                    "start": window["start"],
                    "end": window["end"],
                    "limit": MAX_LOG_LINES,
                }

                def normalize_logs(raw: dict[str, Any]) -> dict[str, Any]:
                    normalized = _rca_normalize_loki_range_result(
                        raw,
                        service=service,
                        namespace=namespace,
                        fault_window=window,
                        line_limit=MAX_LOG_LINES,
                    )
                    logs = normalized.get("phases") or {}
                    if "fault" in logs:
                        coverage = logs["fault"].get("coverage") or {}
                        if coverage.get("lines_returned", 0) >= MAX_LOG_LINES:
                            coverage["truncated"] = True
                        normalized["phases"] = {phase_name: logs["fault"]}
                    return normalized

                query("loki_range_query", 3, "logs", arguments, normalize_logs)
        name = "tempo_service_graph_phases"
        arguments = _rca_tool_arguments(
            name,
            {},
            tools.get(name, {}).get("inputSchema") or {},
            phases=windows,
            namespace=namespace,
        )
        if evidence_source == "history":
            arguments["limit"] = min(int(arguments.get("limit", 20)), 10)
        query(name, 4, "traces", arguments)
        targeted_trace_candidates: list[dict[str, Any]] = []
        trace_scope = set(services[:MAX_LOG_SERVICES])
        all_trace_search_targets = _edge_trace_search_targets(
            edge_latency_results, known
        )
        trace_search_targets = [
            edge
            for edge in all_trace_search_targets
            if edge["source_service"] in trace_scope
            and edge["destination_service"] in trace_scope
        ]
        if len(trace_search_targets) < len(all_trace_search_targets):
            limitations.append(
                "Busca dirigida de traces limitada ao escopo autorizado de servicos"
            )
        eligible_trace_candidates = {
            candidate["trace_id"]: candidate
            for candidate in _rca_trace_candidates(calls)
        }
        unqualified_trace_count = 0
        if trace_search_targets and {
            "tempo_search_traces",
            "tempo_analyze_trace",
        }.issubset(tools):
            for edge in trace_search_targets:
                source = edge["source_service"]
                destination = edge["destination_service"]
                destination_host = re.escape(destination).replace(r"\-", "-")
                service_host = re.escape(namespace).replace(r"\-", "-")
                endpoint_pattern = (
                    rf"http://{destination_host}"
                    rf"(?:\.{service_host}\.svc(?:\.cluster\.local)?"
                    rf"(?::\d+)?|(?::\d+)?)/.*"
                )
                trace_query = (
                    f'{{ resource.k8s.namespace.name = "{namespace}" '
                    f'&& resource.service.name = "{source}" '
                    f"&& kind = client "
                    f"&& span.http.url =~ {json.dumps(endpoint_pattern)} }}"
                )
                search_limit = 5

                def normalize_edge_search(raw: dict[str, Any]) -> dict[str, Any]:
                    traces = raw.get("traces")
                    return {
                        **raw,
                        "phase": "fault",
                        "status": (
                            "ok" if isinstance(traces, list) and traces else "no_data"
                        ),
                    }

                search_result = query(
                    "tempo_search_traces",
                    4,
                    "traces",
                    {
                        "query": trace_query,
                        "service_name": source,
                        "start": windows["fault"]["start"],
                        "end": windows["fault"]["end"],
                        "limit": search_limit,
                    },
                    normalize_edge_search,
                    service=source,
                    phase="fault",
                    kind="network_edge",
                )
                found_traces = search_result.get("traces") or []
                if len(found_traces) >= search_limit:
                    limitations.append(
                        f"Busca dirigida de traces para {source}->{destination} "
                        f"atingiu o limite {search_limit}"
                    )
                for trace in found_traces:
                    if not isinstance(trace, dict):
                        continue
                    trace_id = (
                        trace.get("traceID")
                        or trace.get("traceId")
                        or trace.get("trace_id")
                    )
                    if not isinstance(trace_id, str) or not re.fullmatch(
                        r"[0-9a-fA-F]{16,64}", trace_id
                    ):
                        unqualified_trace_count += 1
                        continue
                    eligible_candidate = eligible_trace_candidates.get(trace_id)
                    if eligible_candidate is None:
                        unqualified_trace_count += 1
                        continue
                    if any(
                        item["trace_id"] == trace_id
                        for item in targeted_trace_candidates
                    ):
                        continue
                    targeted_trace_candidates.append(
                        {
                            **eligible_candidate,
                            "trace_id": trace_id,
                            "phase": "fault",
                            "source_service": source,
                            "destination_service": destination,
                            "metric_profiles": edge["metric_profiles"],
                        }
                    )
        elif trace_search_targets:
            limitations.append(
                "Busca dirigida de spans CLIENT indisponivel; ferramenta Tempo ausente"
            )
        if unqualified_trace_count:
            limitations.append(
                f"{unqualified_trace_count} trace(s) da busca dirigida nao tinham "
                "ID utilizavel, assinatura de erro ou regressao sobre P95 baseline comparavel; "
                "nao foram aprofundados"
            )

        analyzed_trace_ids: set[str] = set()
        for candidate in targeted_trace_candidates:
            if len(analyzed_trace_ids) >= 2:
                break
            query(
                "tempo_analyze_trace",
                4,
                "traces",
                {"trace_id": candidate["trace_id"]},
                lambda raw, selected=candidate: _rca_normalize_trace_result(
                    raw, selected
                ),
                service=candidate["source_service"],
                phase="fault",
                kind="network_edge",
            )
            analyzed_trace_ids.add(candidate["trace_id"])
        out_of_scope_trace_count = 0
        for candidate in _rca_trace_candidates(calls):
            if len(analyzed_trace_ids) >= 2:
                break
            if candidate["trace_id"] in analyzed_trace_ids:
                continue
            if candidate.get("affected_service") not in trace_scope:
                out_of_scope_trace_count += 1
                continue
            query(
                "tempo_analyze_trace",
                4,
                "traces",
                {"trace_id": candidate["trace_id"]},
                lambda raw, selected=candidate: _rca_normalize_trace_result(
                    raw, selected
                ),
                service=candidate.get("affected_service"),
                phase=candidate["phase"],
            )
            analyzed_trace_ids.add(candidate["trace_id"])
        if out_of_scope_trace_count:
            limitations.append(
                f"{out_of_scope_trace_count} trace(s) do escopo de traces do grafo "
                "nao foram aprofundados por falta de servico autorizado"
            )
        if archive_traces is not None:
            query(
                "history_archive_traces",
                4,
                "traces",
                {
                    key: archive_traces[key]
                    for key in ("campaign_id", "run_id", "archive_key", "bundle_sha256")
                    if key in archive_traces
                },
                raw_override=archive_traces,
            )
        name = "check_network_packet_drops"
        if name in tools:
            arguments = _rca_tool_arguments(
                name,
                {},
                tools[name].get("inputSchema") or {},
                phases=windows,
                namespace=namespace,
            )
            query(name, 4, "metrics", arguments)
        additional_check_kinds = (
            "cpu",
            "memory",
            "lifecycle",
            "cpu_throttling",
            "gc",
            "http",
            "network",
            "logs",
            "traces",
            "trace_detail",
        )
        for check_round in range(max_additional_checks):
            executable_checks = []
            for kind in additional_check_kinds:
                for service in sorted(known):
                    for phase_name in ("baseline", "fault", "post"):
                        if phase_name not in windows:
                            continue
                        patterns = [kind] if kind == "logs" else []
                        candidate_check = SkillAdditionalCheck(
                            kind=kind,
                            service=service,
                            phase=phase_name,
                            hypothesis="Inventory candidate",
                            expected_support="Inventory candidate",
                            expected_refutation="Inventory candidate",
                            patterns=patterns,
                        )
                        try:
                            name, _, _, _, _ = _prepare_skill_additional_check(
                                candidate_check,
                                namespace,
                                windows,
                                known,
                                calls,
                                tools,
                            )
                        except SkillRCAError:
                            continue
                        executable_checks.append(
                            {
                                "kind": kind,
                                "service": service,
                                "phase": phase_name,
                                "tool": name,
                                "patterns_required": kind == "logs",
                            }
                        )
            if not executable_checks:
                additional_check_stop_reason = "no_executable_checks"
                break

            def validate_additional_check(value: dict[str, Any]) -> None:
                check_value = value.get("check")
                if value["action"] == "stop":
                    if check_value is not None:
                        raise ValueError("stop decision must not include a check")
                    return
                if check_value is None:
                    raise ValueError("query decision must include a check")
                check = SkillAdditionalCheck.model_validate(check_value)
                _prepare_skill_additional_check(
                    check, namespace, windows, known, calls, tools
                )

            try:
                decision = SkillAdditionalCheckDecision.model_validate(
                    model_json(
                        "additional_mcp_check",
                        SkillAdditionalCheckDecision,
                        {
                            "round": check_round + 1,
                            "max_additional_checks": max_additional_checks,
                            "plan": plan.model_dump(),
                            "restarted_services": restarted,
                            "edge_latency_candidates": edge_candidates,
                            "evidence": evidence_projector.model_view(evidence),
                            "previous_checks": additional_checks,
                            "executable_checks": executable_checks,
                            "rules": [
                                "Selecione no máximo um check executável desta rodada ou pare com motivo.",
                                "Priorize checks que discriminem hipóteses ainda abertas; não repita uma consulta.",
                                "Escolha o serviço e a fase a partir do inventário; logs exigem padrões literais observados.",
                                "Histórico consultado é evidência do run atual; não invente métricas nem resultados.",
                                "Se não houver pergunta diagnóstica útil, pare antes do limite.",
                            ],
                        },
                        validator=validate_additional_check,
                    )
                )
            except (
                *TOOL_ERRORS,
                LLMClientError,
                SkillRCAError,
                ValidationError,
            ) as exc:
                additional_check_error = _rca_redact_text(_mcp_exception_detail(exc))
                additional_check_stop_reason = "additional_check_model_error"
                limitations.append(
                    "Checks adicionais interrompidos por erro do modelo: "
                    + additional_check_error
                )
                break

            if decision.action == "stop":
                additional_check_stop_reason = decision.reason
                break
            check = decision.check
            if check is None:
                additional_check_stop_reason = "query_decision_missing_check"
                break
            name, arguments, family, normalize, candidate = (
                _prepare_skill_additional_check(
                    check, namespace, windows, known, calls, tools
                )
            )
            result = query(
                name,
                5,
                family,
                arguments,
                normalize,
                service=check.service,
                phase=check.phase,
                kind=check.kind,
            )
            additional_checks.append(
                {
                    "check": check.model_dump(),
                    "evidence_id": evidence[-1]["id"],
                    "status": evidence[-1]["status"],
                    "tool": name,
                    "service": check.service,
                    "phase": check.phase,
                    "result": _rca_tool_digest(name, result),
                }
            )
            if check_round + 1 == max_additional_checks:
                additional_check_stop_reason = "max_additional_mcp_checks"
        limitations.append(
            "Eventos Kubernetes historicos, exit code e motivos de probes nao foram "
            "consultados diretamente; nao inventar o motivo de um restart"
        )
        messages.append({"role": "user", "content": references["causal-checklist.md"]})
        diagnosis = Diagnosis.model_validate(
            model_json(
                "diagnosis",
                Diagnosis,
                {
                    "plan": plan.model_dump(),
                    "restarted_services": restarted,
                    "edge_latency_candidates": edge_candidates,
                    "additional_checks": additional_checks,
                    "additional_check_stop_reason": additional_check_stop_reason,
                    "additional_check_error": additional_check_error,
                    "collection_limitations": limitations,
                    "possible_fault_types": possible_fault_types,
                    "known_services": sorted(known),
                    "best_guess_requirement": (
                        "Always choose the most likely primary_service and fault_type "
                        "from the supplied options, even when evidence is insufficient "
                        "for confirmation. Do not return null or unknown for these fields."
                        if require_best_guess
                        else None
                    ),
                },
                allowed_fault_types=possible_fault_types or None,
                allowed_services=sorted(known) if require_best_guess else None,
                allowed_categories=allowed_categories,
            )
        )
        diagnosis = validate_diagnosis(
            diagnosis,
            evidence,
            known,
            set(allowed_categories),
            restarted,
            possible_fault_types,
            require_best_guess=require_best_guess,
        )
        if planning_was_invalid:
            diagnosis.status = "inconclusive"
            diagnosis.limitations.append(
                "O planejamento original incluiu servicos fora do cartao do sistema"
            )
        fault_source = ((window_metadata or {}).get("phase_sources") or {}).get("fault")
        if fault_source == "planned_phase_fallback":
            diagnosis.status = "inconclusive"
            limitations.append(
                "Horario real da falha nao confirmado; janela planejada usada"
            )
        diagnosis.limitations = list(dict.fromkeys(diagnosis.limitations + limitations))
    except (*TOOL_ERRORS, LLMClientError, ValidationError) as exc:
        error = _rca_redact_text(_mcp_exception_detail(exc))
        limitations.append(error)
        diagnosis = None

    if diagnosis:
        answer = (
            f"Localizacao provavel: {diagnosis.primary_service or 'indeterminada'}\n"
            f"Causa provavel: {diagnosis.mechanism}\n"
            f"Estado: {diagnosis.status} · categoria: {diagnosis.category}\n"
            + (
                f"Tipo de falha: {diagnosis.fault_type}\n"
                if possible_fault_types
                else ""
            )
            + f"Evidencias: {', '.join(diagnosis.supporting_evidence) or 'insuficientes'}\n"
            + "Limitacoes: "
            + "; ".join(diagnosis.limitations)
        )
    else:
        answer = "Skill RCA falhou sem produzir diagnostico: " + str(error)
    bundle = _rca_evidence_bundle(calls, "", windows)
    bundle["limitations"] = list(dict.fromkeys(bundle["limitations"] + limitations))
    bundle["localized_service"] = diagnosis.primary_service if diagnosis else None
    result = {
        "status": diagnosis.status if diagnosis else "error",
        "answer": answer,
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "possible_fault_types": possible_fault_types,
        "resource_signature": resource_signature_metadata(resource_signature_policy),
        "error": error,
        "skill": {"name": "rca", "sha256": skill_hash},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_source": reference_source,
        "calls": calls,
        "evidence": evidence,
        "evidence_context_projection": evidence_projector.audit,
        "additional_checks": additional_checks,
        "additional_check_stop_reason": additional_check_stop_reason,
        "additional_check_error": additional_check_error,
        "evidence_bundle": bundle,
        "model_attempts": model_attempts,
        "model_budget_rejections": model_budget_rejections,
        "tools_used": [call["tool"] for call in calls],
        "iterations": len(model_attempts),
        "stop_reason": "skill_completed" if diagnosis else "skill_error",
    }
    if budget_enabled:
        measured_calls = sum(
            bool((attempt.get("usage") or {}).get("total_tokens"))
            for attempt in model_attempts
        )
        result.update(
            model_calls=model_calls_used,
            total_tokens=model_tokens_used,
            budget={
                "max_model_calls": model_call_limit,
                "max_total_tokens": token_limit,
                "timeout_seconds": time_limit,
                "max_additional_checks": max_additional_checks,
                "model_calls_used": model_calls_used,
                "total_tokens_used": model_tokens_used,
                "additional_checks_used": len(additional_checks),
                "measured_token_usage_calls": measured_calls,
                "estimated_token_usage_calls": model_calls_used - measured_calls,
                "elapsed_seconds": round(time.monotonic() - budget_started, 3),
            },
        )
    return result
