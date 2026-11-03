"""Progressive Qwen RCA with evidence-family gates, alongside the legacy RCA."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    rca_reference_phases,
)
from .analysis.edge_latency import (
    build_edge_latency_queries,
    normalize_edge_latency_result,
)
from .analysis.llm_client import LLMClientError, call_llm_tool_messages
from .analysis.system_card import load_system_card, render_system_card
from .evidence_projection import compact_evidence_for_model, EVIDENCE_FORMAT_NOTICE
from .analysis.telemetry_coverage import termination_query, termination_summary
from .rca_options import validate_possible_fault_types

SKILL_DIR = Path(__file__).resolve().parents[3] / ".github" / "skills" / "rca"
MAX_LOG_SERVICES = 6
MAX_LOG_LINES = 150
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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InvestigationPlan(StrictModel):
    candidates: list[str] = Field(min_length=1, max_length=3)
    hypotheses: list[str] = Field(min_length=2, max_length=5)
    reasoning: str = Field(min_length=1, max_length=2000)


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


def _families(ids: list[str], evidence: list[dict[str, Any]]) -> set[str]:
    return {
        item["family"]
        for item in evidence
        if item["id"] in ids
        and item["status"] == "ok"
        and item["fault_observation_count"] > 0
    }


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
            and len(_families(restart.supporting_evidence, evidence)) < 2
        ):
            restart.reason_status = "unknown"
            diagnosis.limitations.append(
                f"{restart.service}: motivo do restart sem corroboração independente"
            )
    gates = []
    if len(_families(diagnosis.supporting_evidence, evidence)) < 2:
        gates.append("Causa sem suporte de duas familias de telemetria independentes")
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
    window_metadata: dict[str, Any] | None = None,
    possible_fault_types: list[str] | None = None,
    require_best_guess: bool = False,
    archive_traces: dict[str, Any] | None = None,
    evidence_source: Literal["live", "history"] = "live",
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
    if not re.fullmatch(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", namespace):
        raise SkillRCAError("Invalid namespace")
    card = load_system_card(namespace)
    known = set((card.get("services") or {}).keys())
    if not known or not {"baseline", "fault"}.issubset(phases):
        raise SkillRCAError(
            "Skill RCA requires a system card and baseline/fault windows"
        )
    windows, reference_source = rca_reference_phases(
        phases, baseline_source=((window_metadata or {}).get("phase_sources") or {}).get("baseline"),
    )
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
    restarted: list[str] = []
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": skill + "\n" + render_system_card(namespace) + "\n" + EVIDENCE_FORMAT_NOTICE,
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
    ) -> dict[str, Any]:
        started = time.monotonic()
        if raw_override is not _RAW_OVERRIDE:
            if not isinstance(raw_override, dict):
                result = {"status": "tool_error", "error": "Archive returned a non-object result"}
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
            count = _observation_count(name, result)
            fault_count = _observation_count(name, result, "fault")
            if count < 0 or fault_count < 0:
                raise SkillRCAError("MCP observation counts cannot be negative")
        except (ValueError, TypeError, AttributeError) as exc:
            result = {
                "status": "tool_error",
                "error": f"Invalid MCP coverage: {_mcp_exception_detail(exc)}",
            }
            count = fault_count = 0
        status = result.get("status", "ok" if count else "no_data")
        item = {
            "id": f"E{len(evidence) + 1:03d}",
            "level": level,
            "tool": name,
            "family": family,
            "status": status,
            "source": (
                evidence_source
                if evidence_source == "history"
                and result.get("evidence_source") in {None, "live"}
                else result.get("evidence_source", evidence_source)
            ),
            "observation_count": count,
            "fault_observation_count": fault_count,
            "arguments": arguments,
            "digest": _rca_tool_digest(name, result),
        }
        evidence.append(item)
        calls.append(
            {
                "tool": name,
                "arguments": arguments,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "result": {**result, "status": status},
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
            if not isinstance(field, dict):
                raise SkillRCAError(f"Schema for {stage} has no primary_service field")
            field.clear()
            field.update({"type": "string", "enum": allowed_services})
            required = output_schema.setdefault("required", [])
            if "primary_service" not in required:
                required.append("primary_service")
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "stage": stage,
                        "context": context,
                        "evidence": compact_evidence_for_model(evidence),
                        "output_schema": output_schema,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
        for attempt in range(3):
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
                and not message["content"].startswith(('{"stage":', "Resposta invalida:"))
            ] + messages[current_stage:]
            response = llm(
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
            )
            try:
                value = _json_content(response)
                schema.model_validate(value)
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
                    and value.get("primary_service") not in allowed_services
                ):
                    raise ValueError(
                        "primary_service must select one of "
                        + ", ".join(allowed_services)
                    )
            except (ValueError, ValidationError) as exc:
                model_attempts.append(
                    {
                        "stage": stage,
                        "status": "invalid_response",
                        "error": _rca_redact_text(exc),
                    }
                )
                if attempt == 2:
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
    try:
        tools = {
            tool["name"]: tool
            for tool in mcp_client.list_tools()
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }
        messages.append({"role": "user", "content": references["evidence-contract.md"]})
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
            )
        else:
            limitations.append(
                "Historico de terminacao indisponivel: prometheus_range_query ausente ou sem servicos conhecidos"
            )
        if "prometheus_range_query" in tools:
            query_end = windows.get("post", windows["fault"])["end"]
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
                    )
                )
        else:
            limitations.append(
                "Latencia por aresta indisponivel: prometheus_range_query ausente"
            )
        edge_candidates = _edge_latency_candidates(edge_latency_results, known)
        plan = InvestigationPlan.model_validate(
            model_json(
                "plan",
                InvestigationPlan,
                {
                    "restarted_services": restarted,
                    "edge_latency_candidates": edge_candidates,
                },
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
                # Exact container identity excludes matching app-db pods and sidecars.
                arguments = {
                    "query": (
                        f'{{namespace="{namespace}",container="{service}",'
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
                        raw, service=service, namespace=namespace, fault_window=window,
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
        for _ in range(2):
            candidate = _rca_next_trace_candidate(calls)
            if candidate is None:
                break
            query(
                "tempo_analyze_trace",
                4,
                "traces",
                {"trace_id": candidate["trace_id"]},
                lambda raw: _rca_normalize_trace_result(raw, candidate),
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
                    "collection_limitations": limitations,
                    "possible_fault_types": possible_fault_types,
                    "known_services": sorted(known),
                    "best_guess_requirement": (
                        "Always choose the most likely primary_service and fault_type "
                        "from the supplied options, even when evidence is insufficient "
                        "for confirmation. Do not return null or unknown for these fields."
                        if require_best_guess else None
                    ),
                },
                allowed_fault_types=possible_fault_types or None,
                allowed_services=sorted(known) if require_best_guess else None,
            )
        )
        diagnosis = validate_diagnosis(
            diagnosis,
            evidence,
            known,
            set(card.get("allowed_fault_categories") or []),
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
    return {
        "status": diagnosis.status if diagnosis else "error",
        "answer": answer,
        "diagnosis": diagnosis.model_dump() if diagnosis else None,
        "possible_fault_types": possible_fault_types,
        "error": error,
        "skill": {"name": "rca", "sha256": skill_hash},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_source": reference_source,
        "calls": calls,
        "evidence": evidence,
        "evidence_bundle": bundle,
        "model_attempts": model_attempts,
        "tools_used": [call["tool"] for call in calls],
        "iterations": len(model_attempts),
        "stop_reason": "skill_completed" if diagnosis else "skill_error",
    }
