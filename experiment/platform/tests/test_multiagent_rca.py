import asyncio
import json
import time
from datetime import timedelta

import pytest
from pydantic import ValidationError

from experiment.platform.backend import multiagent_rca
from experiment.platform.backend.agent_console import _rca_trace_candidates
from experiment.platform.backend.analysis.llm_client import LLMClientError
from experiment.platform.backend.multiagent_rca import (
    CausalAssessment,
    CausalHypothesis,
    CoordinatorDecision,
    CoordinatorWorkPlan,
    EvidenceObservation,
    FollowupRequest,
    InvestigationBudget,
    InvestigationContext,
    InvestigationPlan,
    SpecialistReport,
    TimeWindow,
    _EvidenceRegistry,
    _metric_candidate_fallback,
    answer_multiagent_rca,
    build_investigation_plan,
    validate_assessment,
)
from experiment.platform.backend.rca_skill import _json_content


def context(**overrides):
    return InvestigationContext.model_validate({
        "run_id": "run-test",
        "namespace": "sock-shop",
        "known_services": ["carts", "orders"],
        "allowed_categories": ["pod-failure"],
        "phases": {
            "baseline": {"start": "2026-10-16T10:00:00Z", "end": "2026-10-16T10:01:00Z"},
            "fault": {"start": "2026-10-16T10:01:00Z", "end": "2026-10-16T10:04:00Z"},
        },
        "phase_sources": {"fault": "manifest_observed"},
        "partial": False,
        **overrides,
    })


def hypothesis(hypothesis_id, category="pod-failure", service=None):
    return CausalHypothesis(
        hypothesis_id=hypothesis_id,
        service=service,
        category=category,
        mechanism=f"Mecanismo {hypothesis_id}",
        supporting_evidence=[],
        contradicting_evidence=[],
        unresolved_questions=[f"Qual sinal distingue {hypothesis_id}?"],
    )


def test_plan_is_deterministic_versioned_and_non_executing():
    plan = build_investigation_plan(context())
    assert plan == build_investigation_plan(context())
    assert plan.schema_version == "1.0"
    assert plan.mode == "planning_only" and plan.execution_available is False
    assert InvestigationPlan.model_validate_json(plan.model_dump_json()) == plan
    steps = {step.step_id: step for step in plan.steps}
    assert steps["triage"].owner == "metrics"
    assert steps["hypotheses"].depends_on == ["triage"]
    assert steps["logs"].activation == steps["traces"].activation == "on_demand"
    visited = set()
    for step in plan.steps:
        assert set(step.depends_on).issubset(visited)
        visited.add(step.step_id)
    assert plan.steps[-1].owner == "host"
    assert plan.budget.max_followup_rounds == 2
    assert plan.budget.max_model_calls == 10
    assert plan.budget.max_total_tokens == 120000
    assert plan.budget.max_tool_calls == 32


def test_coordinator_questions_allow_detailed_queries_with_a_bounded_limit():
    question = "x" * 1500
    plan = CoordinatorWorkPlan(
        candidates=["carts"],
        category="pod-failure",
        hypotheses=[hypothesis("H1"), hypothesis("H2")],
        logs_question=question,
        traces_question=question,
    )
    assert plan.logs_question == plan.traces_question == question


def test_metric_candidate_ranking_keeps_units_in_separate_groups():
    evidence = [{
        "status": "ok",
        "fault_observation_count": 10,
        "digest": {
            "metrics": {
                name: {
                    "status": "ok",
                    "phases": {
                        "baseline": {"sample_count": 10},
                        "fault": {"sample_count": 10},
                    },
                    "top_delta": rows,
                }
                for name, rows in {
                    "request_rate": [{"service": "carts", "delta": 1000.0}],
                    "cpu_saturation_pct": [{"service": "catalogue", "delta": 18.0}],
                    "latency_p95": [{"service": "orders", "delta": 2.0}],
                }.items()
            },
        },
    }]

    assert _metric_candidate_fallback(
        evidence, ["carts", "catalogue", "orders"],
    ) == ["catalogue", "orders", "carts"]


def test_metric_candidates_keep_cpu_memory_and_impact_leaders_distinct():
    evidence = [{
        "status": "ok",
        "fault_observation_count": 20,
        "digest": {
            "metrics": {
                "cpu_saturation_pct": {
                    "status": "ok",
                    "phases": {
                        "baseline": {"sample_count": 20},
                        "fault": {"sample_count": 20},
                    },
                    "top_delta": [{"service": "carts", "delta": 45.0}],
                },
                "memory_saturation_pct": {
                    "status": "ok",
                    "phases": {
                        "baseline": {"sample_count": 20},
                        "fault": {"sample_count": 20},
                    },
                    "top_delta": [{"service": "catalogue", "delta": 21.36}],
                },
                "error_rate": {
                    "status": "ok",
                    "phases": {
                        "baseline": {"sample_count": 20},
                        "fault": {"sample_count": 20},
                    },
                    "top_delta": [{"service": "orders", "delta": 34.22}],
                },
                "request_rate": {
                    "status": "ok",
                    "phases": {
                        "baseline": {"sample_count": 20},
                        "fault": {"sample_count": 20},
                    },
                    "top_delta": [{"service": "user", "delta": 36.11}],
                },
            },
        },
    }]

    assert _metric_candidate_fallback(
        evidence, ["carts", "catalogue", "orders", "user"],
    ) == ["carts", "catalogue", "orders"]


def test_metric_candidate_ranking_ignores_empty_or_unobserved_profiles():
    evidence = [{
        "status": "no_data",
        "fault_observation_count": 0,
        "digest": {
            "metrics": {
                "memory_saturation_pct": {
                    "status": "ok",
                    "phases": {"fault": {"sample_count": 20}},
                    "top_delta": [{"service": "catalogue", "delta": 90.0}],
                },
            },
        },
    }]

    assert _metric_candidate_fallback(evidence, ["catalogue"]) == []


def test_trace_candidate_selector_includes_error_free_latency_regressions():
    trace_id = "a" * 32
    candidates = _rca_trace_candidates([{
        "tool": "tempo_service_graph_phases",
        "result": {
            "phases": {
                "fault": {
                    "failure_signatures": [],
                    "latency_candidates": [{
                        "trace_id": trace_id,
                        "affected_service": "orders",
                        "endpoint": "GET /orders",
                        "operations": ["orders.GET /orders"],
                        "fault_duration_ms": 900.0,
                        "baseline_p95_ms": 100.0,
                        "latency_delta_ms": 800.0,
                        "baseline_sample_count": 4,
                    }],
                },
            },
        },
    }])

    assert candidates[0]["trace_id"] == trace_id
    assert candidates[0]["signature"] == "LATENCY_REGRESSION"
    assert candidates[0]["latency_delta_ms"] == 800.0


def test_assessment_requires_a_non_unknown_category():
    with pytest.raises(ValidationError):
        CausalAssessment(
            run_id="run-test", status="inconclusive", primary_service=None,
            observed_events=[], probable_mechanism=None, root_cause=None,
            category="unknown", impact=[], causal_chain=[],
            supporting_evidence=[], contradicting_evidence=[],
            restart_analysis=[], alternatives=[hypothesis("H1"), hypothesis("H2")],
            limitations=[], next_checks=[],
        )


def test_evidence_registry_rejects_queries_outside_metric_selected_scope():
    ctx = context()
    phases = {key: value.model_dump(mode="json") for key, value in ctx.phases.items()}
    registry = _EvidenceRegistry(
        client=NoDataMCP(),
        tools={"loki_range_query": {"name": "loki_range_query"}},
        phases=phases,
        namespace=ctx.namespace,
        budget=InvestigationBudget(),
        started=0.0,
    )
    registry.set_service_scope(["orders"])

    with pytest.raises(ValueError, match="outside investigation scope"):
        registry.query_logs("carts", "fault")
    with pytest.raises(ValueError, match="outside investigation scope"):
        registry.followup(
            FollowupRequest(
                tool="loki_range_query",
                service="carts",
                phase="fault",
                question="Inspecionar falhas",
                hypothesis_ids=["H1"],
            ),
            ctx,
        )


def test_investigation_scope_caps_candidates_lifecycle_and_dependencies():
    services, reasons = multiagent_rca._investigation_scope(
        ["svc-a", "svc-b", "svc-c"],
        ["svc-d", "svc-e"],
        ["svc-a", "svc-b", "svc-c", "svc-d", "svc-e", "svc-f"],
        {"services": {"svc-a": {"upstream": ["svc-f"]}}},
        limit=4,
    )
    dependency_scope, dependency_reasons = multiagent_rca._investigation_scope(
        ["svc-a"], [],
        ["svc-a", "svc-f"],
        {"services": {"svc-a": {"upstream": ["svc-f"]}}},
    )

    assert len(services) == 4
    assert services == ["svc-a", "svc-b", "svc-c", "svc-d"]
    assert "svc-e" not in reasons
    assert dependency_scope == ["svc-a", "svc-f"]
    assert "dependencia upstream declarada" in dependency_reasons["svc-f"][0]


def test_multiagent_json_parser_accepts_unescaped_control_characters_only_when_enabled():
    response = {"content": '{"reason":"line one\nline two"}'}
    with pytest.raises(json.JSONDecodeError):
        _json_content(response)
    assert _json_content(response, strict=False) == {"reason": "line one\nline two"}


@pytest.mark.parametrize("overrides", [
    {"known_services": []},
    {"namespace": "../secret"},
    {"ground_truth_service": "carts"},
    {"phases": {"fault": {"start": "2026-10-16T10:01:00Z", "end": "2026-10-16T10:04:00Z"}}},
])
def test_context_rejects_invalid_or_privileged_fields(overrides):
    with pytest.raises(ValidationError):
        context(**overrides)


def test_context_requires_selectable_failure_category():
    with pytest.raises(ValidationError):
        context(allowed_categories=["unknown"])


def test_windows_require_timezone_order_and_nonoverlapping_phases():
    for start, end in [
        ("2026-10-16T10:00:00", "2026-10-16T10:01:00"),
        ("2026-10-16T10:01:00Z", "2026-10-16T10:00:00Z"),
    ]:
        with pytest.raises(ValidationError):
            TimeWindow(start=start, end=end)
    data = context().model_dump()
    data["phases"]["baseline"]["end"] = data["phases"]["fault"]["end"]
    with pytest.raises(ValidationError):
        InvestigationContext.model_validate(data)


def test_specialist_errors_are_explicit_and_not_success_shaped():
    report = dict(
        task_id="logs-1", specialist="logs", status="error", findings=[],
        limitations=[], next_questions=[],
    )
    with pytest.raises(ValidationError):
        SpecialistReport.model_validate(report)
    assert SpecialistReport.model_validate({**report, "error": "Loki unavailable"}).status == "error"
    with pytest.raises(ValidationError):
        SpecialistReport.model_validate({**report, "status": "completed", "error": "Loki unavailable"})


def test_assessment_separates_observed_event_from_unknown_root_cause():
    assessment = CausalAssessment(
        run_id="run-test", status="inconclusive", primary_service="carts",
        observed_events=["Restart observado"], probable_mechanism=None,
        root_cause=None, category="pod-failure", impact=[], causal_chain=[],
        supporting_evidence=["E001"], contradicting_evidence=[],
        restart_analysis=[], alternatives=[hypothesis("H1"), hypothesis("H2")],
        limitations=["Motivo desconhecido"],
        next_checks=["Consultar eventos historicos"],
    )
    assert assessment.root_cause is None and assessment.observed_events


class NoDataMCP:
    def list_tools(self):
        return []

    def call_tool_json(self, *args, **kwargs):
        pytest.fail("Unavailable MCP tools must not be called")


def response_for(messages, *_args, **_kwargs):
    assert _kwargs["chat_template_kwargs"] == {"enable_thinking": False}
    request = json.loads(messages[-1]["content"])
    _kwargs["usage"].update(total_tokens=10)
    if request.get("stage") == "coordinator_plan":
        schema = _kwargs["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["category"]["enum"] == request["allowed_categories"]
        candidate_items = schema["properties"]["candidates"]["items"]
        if request["candidate_services"]:
            assert candidate_items["enum"] == request["candidate_services"]
        else:
            assert "enum" not in candidate_items
        content = {
            "candidates": request["candidate_services"][:3],
            "category": request["allowed_categories"][0],
            "hypotheses": [
                {
                    "hypothesis_id": "H1",
                    "service": (request["candidate_services"] or [None])[0],
                    "category": request["allowed_categories"][0],
                    "mechanism": "Pressao de recurso antecede a degradacao.",
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                    "unresolved_questions": ["A pressao precede os erros?"],
                },
                {
                    "hypothesis_id": "H2",
                    "service": (request["candidate_services"] or [None])[-1],
                    "category": request["allowed_categories"][-1],
                    "mechanism": "Falha em dependencia propaga o impacto.",
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                    "unresolved_questions": ["Qual dependencia falhou primeiro?"],
                },
            ],
            "logs_question": "Ha erros relevantes nos logs?",
            "traces_question": "Ha falha em dependencia nos traces?",
        }
    elif request.get("stage") == "coordinator_followup":
        content = {"action": "stop", "reason": "Sem consultas discriminativas disponiveis"}
    elif request.get("task_id"):
        specialist = request["task_id"].split("-", 1)[0]
        content = {
            "task_id": request["task_id"],
            "specialist": specialist,
            "status": "no_data",
            "findings": [],
            "limitations": ["Sem observacoes positivas"],
            "next_questions": [],
        }
    else:
        schema = _kwargs["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["category"]["enum"] == request["allowed_categories"]
        assert schema["properties"]["primary_service"]["anyOf"][0]["enum"] == request["allowed_services"]
        possible_fault_types = request.get("possible_fault_types") or []
        if possible_fault_types:
            assert schema["properties"]["fault_type"]["enum"] == possible_fault_types
            assert "fault_type" in schema["required"]
        content = {
            "run_id": request["run_id"],
            "status": "confirmed",
            "primary_service": request["allowed_services"][0] if request["allowed_services"] else None,
            "observed_events": ["Falha nao estabelecida"],
            "probable_mechanism": "Hipotese sem dados",
            "root_cause": "Hipotese sem dados",
            "category": request["allowed_categories"][0],
            "impact": [],
            "causal_chain": ["Hipotese sem dados"],
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "restart_analysis": [],
            "alternatives": [
                {
                    "hypothesis_id": "H1",
                    "service": request["allowed_services"][0] if request["allowed_services"] else None,
                    "category": request["allowed_categories"][0],
                    "mechanism": "Pressao de recurso antecede a degradacao.",
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                    "unresolved_questions": ["A pressao precede os erros?"],
                },
                {
                    "hypothesis_id": "H2",
                    "service": None,
                    "category": request["allowed_categories"][-1],
                    "mechanism": "Falha em dependencia propaga o impacto.",
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                    "unresolved_questions": ["Qual dependencia falhou primeiro?"],
                },
            ],
            "limitations": [],
            "next_checks": [],
        }
        if possible_fault_types:
            content["fault_type"] = possible_fault_types[0]
    return {"content": json.dumps(content)}


def test_executor_uses_shared_evidence_ids_and_downgrades_without_fault_data():
    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(),
        context=context(),
        llm=response_for,
    )
    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["status"] == "inconclusive"
    assert result["diagnosis"]["supporting_evidence"] == []
    assert [item["evidence_id"] for item in result["evidence"]] == [
        f"E{number:03d}" for number in range(1, len(result["evidence"]) + 1)
    ]
    assert [item["query_ref"] for item in result["evidence"]] == [
        f"Q{number:03d}" for number in range(1, len(result["evidence"]) + 1)
    ]
    assert result["budget"]["model_calls_used"] == 2
    assert result["stop_reason"] == "no_candidate_services"
    assert all(item["status"] == "tool_error" for item in result["evidence"])
    assert "must-not-reach-model" not in json.dumps(result)
    assert not any(item["family"] in {"logs", "traces"} for item in result["evidence"])
    assert result["diagnosis"]["category"] == "pod-failure"


def test_final_diagnosis_always_selects_a_supplied_fault_type_when_inconclusive():
    options = ["container-kill", "network-loss"]
    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(),
        context=context(possible_fault_types=options),
        llm=response_for,
    )

    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["fault_type"] == "container-kill"
    assert "Tipo de falha: container-kill" in result["answer"]
    assert result["possible_fault_types"] == options


def test_network_category_records_missing_packet_drop_check():
    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(),
        context=context(allowed_categories=["network-loss"]),
        llm=response_for,
    )

    assert result["investigation_scope"]["preliminary_category"] == "network-loss"
    assert any(
        "Drops de interface nao foram coletados" in limitation
        for limitation in result["limitations"]
    )


def test_executor_returns_explicit_failure_when_model_is_unavailable():
    def unavailable(*_args, **_kwargs):
        raise LLMClientError("model unavailable")

    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(),
        context=context(),
        llm=unavailable,
        budget=InvestigationBudget(max_model_calls=2),
    )
    assert result["status"] == "error"
    assert result["diagnosis"] is None
    assert result["error"].endswith("model unavailable")
    assert result["budget"]["model_calls_used"] == 1


def test_validation_corrects_run_identity_and_prevents_confirmation():
    assessment = CausalAssessment(
        run_id="other-run",
        status="confirmed",
        primary_service="carts",
        observed_events=["event"],
        probable_mechanism="mechanism",
        root_cause="root",
        category="pod-failure",
        impact=[],
        causal_chain=["event", "mechanism", "root"],
        supporting_evidence=["E001", "E999"],
        contradicting_evidence=[],
        restart_analysis=[
            {
                "service": "carts",
                "reason_status": "supported",
                "reason": "Possible memory pressure",
                "supporting_evidence": ["E001"],
                "next_check": "Check historical pod events",
            }
        ],
        alternatives=[hypothesis("H1"), hypothesis("H2")],
        limitations=[],
        next_checks=[],
    )
    observation = {
        "evidence_id": "E001",
        "family": "metrics",
        "level": 1,
        "source": "live",
        "query_ref": "Q001",
        "status": "ok",
        "service": "carts",
        "windows": {
            "fault": {
                "start": "2026-10-16T10:01:00Z",
                "end": "2026-10-16T10:04:00Z",
            }
        },
        "observation_count": 1,
        "fact": "Restart metric observed",
        "limitations": [],
    }
    validated = validate_assessment(
        assessment,
        context=context(),
        observations=[EvidenceObservation.model_validate(observation)],
        calls=[{"evidence_id": "E001", "fault_observation_count": 1}],
        restarted_services=["carts"],
    )
    assert validated.run_id == "run-test"
    assert validated.status == "inconclusive"
    assert validated.supporting_evidence == ["E001"]
    assert validated.restart_analysis[0].reason_status == "unknown"


def test_confirmation_requires_fault_support_tied_to_primary_or_declared_dependency():
    investigation = context()
    fault_window = investigation.phases["fault"]
    observations = [
        EvidenceObservation(
            evidence_id="E001",
            family="metrics",
            level=1,
            source="live",
            query_ref="Q001",
            status="ok",
            service="carts",
            windows={"fault": fault_window},
            observation_count=2,
            fact="Metrics observed for carts",
            limitations=[],
        ),
        EvidenceObservation(
            evidence_id="E002",
            family="logs",
            level=3,
            source="live",
            query_ref="Q002",
            status="ok",
            service="orders",
            windows={"fault": fault_window},
            observation_count=2,
            fact="Logs observed for orders",
            limitations=[],
        ),
    ]
    calls = [
        {"evidence_id": "E001", "fault_observation_count": 2},
        {"evidence_id": "E002", "fault_observation_count": 2},
    ]

    def assessment():
        return CausalAssessment(
            run_id="run-test",
            status="confirmed",
            primary_service="carts",
            observed_events=["latency increase"],
            probable_mechanism="A dependency is slow",
            root_cause="A causal failure",
            category="pod-failure",
            category_confidence="high",
            impact=[],
            causal_chain=["carts is affected through orders"],
            supporting_evidence=["E001", "E002"],
            contradicting_evidence=[],
            restart_analysis=[],
            alternatives=[hypothesis("H1"), hypothesis("H2")],
            limitations=[],
            next_checks=[],
        )

    unrelated = validate_assessment(
        assessment(),
        context=investigation,
        observations=observations,
        calls=calls,
        restarted_services=[],
        allowed_services=["carts", "orders"],
        service_relations={"carts": []},
    )
    related = validate_assessment(
        assessment(),
        context=investigation,
        observations=observations,
        calls=calls,
        restarted_services=[],
        allowed_services=["carts", "orders"],
        service_relations={"carts": ["orders"]},
    )

    assert unrelated.status == "inconclusive"
    assert unrelated.category_confidence == "low"
    assert any("nao estao alinhadas" in item for item in unrelated.limitations)
    assert related.status == "confirmed"

    non_overlapping_observations = [
        observations[0].model_copy(update={
            "windows": {
                "fault": TimeWindow(
                    start=fault_window.start,
                    end=fault_window.start + timedelta(seconds=30),
                ),
            },
        }),
        observations[1].model_copy(update={
            "windows": {
                "fault": TimeWindow(
                    start=fault_window.start + timedelta(seconds=31),
                    end=fault_window.end,
                ),
            },
        }),
    ]
    non_overlapping = validate_assessment(
        assessment(),
        context=investigation,
        observations=non_overlapping_observations,
        calls=calls,
        restarted_services=[],
        allowed_services=["carts", "orders"],
        service_relations={"carts": ["orders"]},
    )

    assert non_overlapping.status == "inconclusive"
    assert any("nao estao alinhadas" in item for item in non_overlapping.limitations)


class AdaptiveMCP:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [
            {"name": name, "inputSchema": {"properties": {
                key: {} for key in (
                    "namespace", "baseline_start", "baseline_end",
                    "fault_start", "fault_end", "query", "limit", "top_k",
                )
            }}}
            for name in (
                "get_run_golden_signals", "check_service_readiness",
                "check_oom_kills", "check_pod_restarts",
                "loki_range_query", "get_memory_saturation_top",
                "get_cpu_saturation_top", "tempo_service_graph_phases",
            )
        ]

    def call_tool_json(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if name == "get_run_golden_signals":
            return {
                "status": "ok",
                "metrics": {
                    "cpu_saturation_pct": {
                        "status": "ok", "unit": "%",
                        "top": [{
                            "service": "orders", "baseline_mean": 10.0,
                            "fault_mean": 40.0, "delta_abs": 30.0,
                        }],
                        "phases": {
                            "baseline": {"sample_count": 1},
                            "fault": {"sample_count": 1},
                        },
                    },
                    "memory_saturation_pct": {
                        "status": "ok", "unit": "%",
                        "top": [{
                            "service": "orders", "baseline_mean": 20.0,
                            "fault_mean": 25.0, "delta_abs": 5.0,
                        }],
                        "phases": {
                            "baseline": {"sample_count": 1},
                            "fault": {"sample_count": 1},
                        },
                    },
                },
            }
        if name == "check_service_readiness":
            return {
                "status": "ok",
                "phases": {
                    phase: {"status": "ok", "coverage": {"sample_count": 1}, "services": []}
                    for phase in ("baseline", "fault")
                },
            }
        if name in {"check_oom_kills", "check_pod_restarts"}:
            return {
                "status": "ok", "found": False, "count": 0, "top": [],
                "coverage": {"sample_count": 1},
            }
        if name == "loki_range_query":
            return {"status": "success", "data": {"result": []}}
        if name == "tempo_service_graph_phases":
            return {"status": "ok", "phases": {
                phase: {"trace_count": 1, "failure_signatures": []}
                for phase in ("baseline", "fault")
            }}
        return {
            "status": "ok", "coverage": {"sample_count": 5},
            "top": [{"service": "orders", "saturation_max_pct": 99}],
        }


def adaptive_llm(requests, captured):
    decisions = iter(requests)

    def respond(messages, *args, **kwargs):
        request = json.loads(messages[-1]["content"])
        captured.append(request)
        if "validation_errors" in request:
            kwargs["usage"].update(total_tokens=10)
            return {"content": json.dumps({
                "action": "stop",
                "reason": "A consulta nao passou no contrato; interrompendo com seguranca",
            })}
        if request.get("stage") == "coordinator_followup":
            kwargs["usage"].update(total_tokens=10)
            followup = next(decisions)
            content = (
                {"action": "investigate", "reason": "Resolver hipotese alternativa", "request": followup}
                if followup is not None else
                {"action": "stop", "reason": "Nao ha novo check discriminativo"}
            )
            return {"content": json.dumps(content)}
        return response_for(messages, *args, **kwargs)

    return respond


def followup(tool="loki_range_query", service="orders", phase="fault", **extras):
    return {
        "tool": tool, "service": service, "phase": phase,
        "question": "A dependencia explica os erros?", "hypothesis_ids": ["H1"], **extras,
    }


def test_followup_evidence_reference_does_not_imply_scope_revision():
    request = FollowupRequest.model_validate(
        followup(contains="timeout", evidence_ids=["E001"])
    )

    assert request.evidence_ids == ["E001"]
    assert request.scope_revision is False


def test_adaptive_round_queries_new_service_reanalyses_only_logs_and_stops():
    mcp, captured = AdaptiveMCP(), []
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(),
        llm=adaptive_llm([
            followup(contains='timeout "quoted"', evidence_ids=["E001"]),
            None,
        ], captured),
    )
    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "coordinator_stop"
    assert result["budget"]["model_calls_used"] == 6
    assert result["budget"]["followup_rounds_used"] == 1
    assert len(result["followup_rounds"]) == 2
    record = result["followup_rounds"][0]
    assert record["status"] == "completed" and record["specialist_task_id"] == "logs-002"
    log_query = [args for tool, args, _ in mcp.calls if tool == "loki_range_query"][-1]
    assert 'container="orders"' in log_query["query"]
    assert log_query["query"].endswith('|= "timeout \\"quoted\\""')
    assert log_query["start"] == "2026-10-16T10:01:00Z"
    assert log_query["end"] == "2026-10-16T10:04:00Z"
    assert [r["task_id"] for r in captured if "task_id" in r] == [
        "metrics-001", "traces-001",
    ]
    assert result["specialists"][-1]["task_id"] == "logs-002"
    assert result["specialists"][-1]["status"] == "no_data"
    final = captured[-1]
    assert final["followup_rounds"] == result["followup_rounds"]
    assert all(call[2]["timeout_seconds"] <= 30 for call in mcp.calls)


def test_termination_history_is_queried_even_when_restart_counter_is_zero():
    class TerminationMCP(AdaptiveMCP):
        def list_tools(self):
            tools = super().list_tools()
            tools.append({
                "name": "prometheus_range_query",
                "inputSchema": {
                    "properties": {
                        key: {} for key in ("query", "start", "end", "step")
                    }
                },
            })
            return tools

        def call_tool_json(self, name, arguments, **kwargs):
            if name == "prometheus_range_query":
                self.calls.append((name, arguments, kwargs))
                return {"status": "success", "data": {"result": []}}
            return super().call_tool_json(name, arguments, **kwargs)

    investigation = context()
    result = answer_multiagent_rca(
        mcp_client=TerminationMCP(),
        context=investigation,
        llm=response_for,
    )

    termination_calls = [
        call for call in result["calls"]
        if call["tool"] == "prometheus_range_query"
        and "kube_pod_container_status_last_terminated_" in call["arguments"]["query"]
    ]
    assert len(termination_calls) == 1
    assert termination_calls[0]["arguments"]["start"] == (
        investigation.phases["baseline"].start.isoformat().replace("+00:00", "Z")
    )
    termination_end = investigation.phases.get(
        "post", investigation.phases["fault"]
    ).end
    assert termination_calls[0]["arguments"]["end"] == (
        termination_end.isoformat().replace("+00:00", "Z")
    )


@pytest.mark.parametrize("tool", ["get_memory_saturation_top", "get_cpu_saturation_top"])
def test_adaptive_metric_queries_preserve_phase_and_do_not_count_baseline_as_fault(tool):
    mcp, captured = AdaptiveMCP(), []
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(),
        llm=adaptive_llm([followup(tool, phase="baseline"), None], captured),
    )
    call = next(call for call in result["calls"] if call["tool"] == tool)
    assert call["observation_count"] == 5
    assert call["fault_observation_count"] == 0
    assert call["arguments"]["start"] == "2026-10-16T10:00:00Z"
    assert result["specialists"][-1]["task_id"] == "metrics-002"
    assert result["budget"]["followup_rounds_used"] == 1


def test_adaptive_trace_query_is_scoped_to_known_service_and_run_windows():
    mcp, captured = AdaptiveMCP(), []
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(),
        llm=adaptive_llm([followup("tempo_service_graph_phases"), None], captured),
    )
    trace_calls = [call for call in result["calls"] if call["tool"] == "tempo_service_graph_phases"]
    query = trace_calls[-1]["arguments"]
    assert query["query"] == (
        '{ resource.k8s.namespace.name = "sock-shop" && resource.service.name = "orders" }'
    )
    assert query["fault_start"] == "2026-10-16T10:01:00Z"
    assert query["baseline_start"] == "2026-10-16T10:00:00Z"
    assert result["specialists"][-1]["task_id"] == "traces-002"


def test_trace_gap_is_probed_and_analyzed_for_fault_unobserved_service(monkeypatch):
    monkeypatch.setattr(
        multiagent_rca,
        "_investigation_scope",
        lambda _candidates, _restarted, _known_services, _card, *, limit=4: (
            ["orders", "carts"],
            {"orders": ["metrics"], "carts": ["metrics"]},
        ),
    )
    trace_id = "b" * 32

    class TraceProbeMCP(AdaptiveMCP):
        def __init__(self):
            super().__init__()
            self.probe_arguments = []

        def list_tools(self):
            tools = super().list_tools()
            tools.extend([
                {
                    "name": "tempo_search_traces",
                    "inputSchema": {"properties": {
                        key: {} for key in ("query", "service_name", "start", "end", "limit")
                    }},
                },
                {
                    "name": "tempo_analyze_trace",
                    "inputSchema": {"properties": {"trace_id": {}}},
                },
            ])
            return tools

        def call_tool_json(self, name, arguments, **kwargs):
            if name == "tempo_service_graph_phases":
                self.calls.append((name, arguments, kwargs))
                return {
                    "status": "ok",
                    "phases": {
                        "baseline": {
                            "trace_count": 1,
                            "top_nodes": [{"service": "orders"}],
                        },
                        "fault": {
                            "trace_count": 1,
                            "top_nodes": [{"service": "carts"}],
                            "failure_signatures": [],
                            "latency_candidates": [],
                        },
                        "post": {
                            "trace_count": 1,
                            "top_nodes": [{"service": "orders"}],
                        },
                    },
                }
            if name == "tempo_search_traces":
                self.calls.append((name, arguments, kwargs))
                self.probe_arguments.append(arguments)
                return {"traces": [{"traceID": trace_id}]}
            if name == "tempo_analyze_trace":
                self.calls.append((name, arguments, kwargs))
                return {
                    "status": "ok",
                    "span_count": 2,
                    "trace_duration_ms": 42,
                    "critical_path": [{"service": "orders", "operation": "GET /orders"}],
                }
            return super().call_tool_json(name, arguments, **kwargs)

    mcp = TraceProbeMCP()
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(), llm=response_for,
    )

    assert len(mcp.probe_arguments) == 1
    assert mcp.probe_arguments[0]["service_name"] == "orders"
    assert mcp.probe_arguments[0]["start"] == "2026-10-16T10:01:00Z"
    assert mcp.probe_arguments[0]["end"] == "2026-10-16T10:04:00Z"
    graph_call = next(
        call for call in result["calls"]
        if call["tool"] == "tempo_service_graph_phases"
    )
    assert graph_call["observation_count"] == 3
    assert graph_call["fault_observation_count"] == 1
    trace_call = next(
        call for call in result["calls"] if call["tool"] == "tempo_analyze_trace"
    )
    assert trace_call["arguments"]["trace_id"] == trace_id
    assert trace_call["service"] == "orders"
    assert trace_call["phase"] == "fault"
    assert trace_call["fault_observation_count"] == 2
    assert trace_call["evidence_id"] in {
        item["evidence_id"] for item in result["evidence"]
    }


def test_fault_trace_service_detection_ignores_malformed_service_values():
    registry = _EvidenceRegistry(
        client=None,
        tools={},
        phases={},
        namespace="sock-shop",
        budget=InvestigationBudget(max_tool_calls=1),
        started=time.monotonic(),
    )
    registry.set_service_scope(["orders"])
    registry.calls.extend([
        {
            "family": "traces",
            "service": ["orders"],
            "phase": "fault",
            "fault_observation_count": 1,
        },
        {
            "family": "traces",
            "tool": "tempo_service_graph_phases",
            "result": {
                "phases": {
                    "fault": {
                        "top_nodes": [{"service": ["orders"]}],
                        "services": "orders",
                        "top_edges": [{"from": ["orders"], "to": {"service": "orders"}}],
                        "failure_signatures": [{"affected_service": []}],
                        "latency_candidates": 4,
                    },
                },
            },
        },
    ])

    assert registry.fault_trace_services_observed(["orders"]) == set()


@pytest.mark.parametrize("metric_evidence_identifies_service", [True, False])
def test_scope_revision_requires_positive_initial_fault_metric_evidence(
    monkeypatch, metric_evidence_identifies_service,
):
    scope_calls = []
    monkeypatch.setattr(
        multiagent_rca, "_metric_candidate_fallback",
        lambda _evidence, _services, limit=3: ["orders", "carts"][:limit],
    )
    monkeypatch.setattr(
        multiagent_rca,
        "_metric_evidence_mentions_service",
        lambda _evidence, service: (
            metric_evidence_identifies_service and service == "carts"
        ),
    )

    def bounded_scope(candidates, restarted, known_services, card, *, limit=4):
        scope_calls.append(list(candidates))
        selected = ["orders"] if len(scope_calls) == 1 else ["orders", "carts"]
        selected = [service for service in selected if service in known_services][:limit]
        return selected, {service: ["test scope"] for service in selected}

    monkeypatch.setattr(multiagent_rca, "_investigation_scope", bounded_scope)

    coordinator_round = [0]
    requests = []

    def scoped_llm(messages, *args, **kwargs):
        request = json.loads(messages[-1]["content"])
        if request.get("stage") != "coordinator_followup":
            return response_for(messages, *args, **kwargs)
        kwargs["usage"].update(total_tokens=10)
        coordinator_round[0] += 1
        if coordinator_round[0] > 1:
            return {"content": json.dumps({
                "action": "stop", "reason": "Sem novo check discriminativo",
            })}
        metric_evidence = next(
            (
                item for item in request["metric_scope_revision_evidence"]
                if item["service"] == "carts"
            ),
            None,
        )
        evidence_ids = (
            metric_evidence["evidence_ids"] if metric_evidence else ["E001"]
        )
        if not evidence_ids:
            evidence_ids = ["E001"]
        followup_request = {
            "tool": "loki_range_query",
            "service": "carts",
            "phase": "fault",
            "question": "Os logs do servico corroboram a hipotese?",
            "hypothesis_ids": ["H1"],
            "scope_revision": True,
            "evidence_ids": evidence_ids,
        }
        requests.append(followup_request)
        return {"content": json.dumps({
            "action": "investigate",
            "reason": "Testar servico fora do escopo inicial com evidencia de metricas",
            "request": followup_request,
        })}

    mcp = AdaptiveMCP()
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(), llm=scoped_llm,
    )

    assert result["status"] == "inconclusive"
    assert result["investigation_scope"]["history"][0]["services"] == ["orders"]
    revision = result["investigation_scope"]["revisions"]
    cart_queries = [
        call for call in result["calls"]
        if call["family"] in {"logs", "traces"} and call.get("service") == "carts"
    ]
    if metric_evidence_identifies_service:
        assert len(revision) == 1
        assert revision[0]["added_service"] == "carts"
        assert revision[0]["services"] == ["carts", "orders"]
        assert result["investigation_scope"]["history"][-1]["services"] == [
            "carts", "orders",
        ]
        assert result["followup_rounds"][0]["status"] == "completed"
        assert len(cart_queries) == 1
        assert cart_queries[0]["scope_services"] == ["carts"]
    else:
        assert revision == []
        assert len(result["investigation_scope"]["history"]) == 1
        assert result["followup_rounds"][0]["status"] == "rejected"
        assert "positively identify" in result["followup_rounds"][0]["error"]
        assert cart_queries == []


@pytest.mark.parametrize("followup_request,expected_error", [
    (followup(service="not-known"), "unknown service"),
    (followup(phase="post"), "unavailable phase"),
    (followup(service="carts"), "repeats an existing query"),
])
def test_invalid_followup_is_recorded_without_executing_query(followup_request, expected_error):
    mcp, captured = AdaptiveMCP(), []
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(),
        llm=adaptive_llm([followup_request], captured),
    )
    assert result["stop_reason"] == "invalid_followup"
    assert result["status"] == "inconclusive"
    assert result["followup_rounds"][0]["status"] == "rejected"
    assert expected_error in result["followup_rounds"][0]["error"]
    assert len(result["specialists"]) == 3
    assert result["budget"]["followup_rounds_used"] == 0


def test_adaptive_round_limit_and_model_reservation():
    for budget, expected_rounds, expected_calls, reason in [
        (InvestigationBudget(), 2, 8, "round_limit"),
        (InvestigationBudget(max_model_calls=5), 0, 4, "model_budget"),
        (InvestigationBudget(max_model_calls=3), 0, 3, "model_budget"),
        (InvestigationBudget(max_followup_rounds=0), 0, 4, "followups_disabled"),
    ]:
        mcp, captured = AdaptiveMCP(), []
        result = answer_multiagent_rca(
            mcp_client=mcp, context=context(), budget=budget,
            llm=adaptive_llm([
                followup("get_memory_saturation_top"),
                followup("get_cpu_saturation_top"),
            ], captured),
        )
        assert result["status"] == "inconclusive", result
        assert result["stop_reason"] == reason
        assert result["budget"]["followup_rounds_used"] == expected_rounds
        assert result["budget"]["model_calls_used"] == expected_calls
        assert captured[-1].get("run_id") == "run-test"


@pytest.mark.parametrize("payload", [
    {"action": "investigate", "reason": "missing request"},
    {"action": "stop", "reason": "unexpected request", "request": followup()},
    {"action": "investigate", "reason": "unapproved tool", "request": followup("shell")},
    {"action": "investigate", "reason": "override", "request": followup(start="2020-01-01")},
])
def test_decision_contract_rejects_unsafe_or_inconsistent_actions(payload):
    with pytest.raises(ValidationError):
        CoordinatorDecision.model_validate(payload)


def test_invalid_followup_is_rejected_without_aborting_investigation():
    captured = []
    result = answer_multiagent_rca(
        mcp_client=AdaptiveMCP(),
        context=context(),
        llm=adaptive_llm(
            [followup("get_memory_saturation_top", contains="OOM")], captured
        ),
    )
    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "coordinator_stop"
    assert result["followup_rounds"][0]["status"] == "stopped"
    assert result["followup_rounds"][0]["action"] == "stop"
    assert result["budget"]["repair_calls_used"] == 1
    assert not any(
        call.get("service") == "orders" and call.get("tool") == "get_memory_saturation_top"
        for call in result["calls"]
    )


def test_overlong_log_filter_is_rejected_without_aborting_investigation():
    captured = []
    result = answer_multiagent_rca(
        mcp_client=AdaptiveMCP(),
        context=context(),
        llm=adaptive_llm([followup(contains="x" * 121)], captured),
    )
    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "invalid_followup"
    assert result["followup_rounds"][0]["status"] == "rejected"
    assert "exceeds 120 characters" in result["followup_rounds"][0]["error"]


def test_adaptive_query_errors_are_registered_and_not_causal_evidence():
    class FailingMCP(AdaptiveMCP):
        def call_tool_json(self, name, arguments, **kwargs):
            if name == "get_memory_saturation_top":
                raise TimeoutError("Prometheus timed out")
            return super().call_tool_json(name, arguments, **kwargs)

    captured = []
    result = answer_multiagent_rca(
        mcp_client=FailingMCP(), context=context(),
        llm=adaptive_llm([followup("get_memory_saturation_top"), None], captured),
    )
    call = next(c for c in result["calls"] if c["tool"] == "get_memory_saturation_top")
    assert call["status"] == "tool_error"
    assert call["fault_observation_count"] == 0
    assert result["followup_rounds"][0]["query_status"] == "tool_error"
    assert result["diagnosis"]["supporting_evidence"] == []
    assert result["status"] == "inconclusive"


def test_transient_mcp_timeout_is_retried_once_and_counted():
    class RecoveringMCP(AdaptiveMCP):
        failures = 0

        def call_tool_json(self, name, arguments, **kwargs):
            if name == "get_memory_saturation_top" and self.failures == 0:
                self.failures += 1
                raise asyncio.TimeoutError("temporary Prometheus timeout")
            return super().call_tool_json(name, arguments, **kwargs)

    result = answer_multiagent_rca(
        mcp_client=RecoveringMCP(),
        context=context(),
        llm=adaptive_llm([followup("get_memory_saturation_top"), None], []),
    )

    call = next(
        item for item in result["calls"]
        if item["tool"] == "get_memory_saturation_top"
    )
    assert call["status"] == "ok"
    assert call["attempts"] == 2
    assert call["retry_count"] == 1
    assert result["budget"]["tool_retry_attempts_used"] == 1


def test_asyncio_timeout_after_retry_is_recorded_as_tool_error():
    class TimedOutMCP(AdaptiveMCP):
        def call_tool_json(self, name, arguments, **kwargs):
            if name == "get_memory_saturation_top":
                raise asyncio.TimeoutError()
            return super().call_tool_json(name, arguments, **kwargs)

    result = answer_multiagent_rca(
        mcp_client=TimedOutMCP(),
        context=context(),
        llm=adaptive_llm([followup("get_memory_saturation_top"), None], []),
    )

    call = next(
        item for item in result["calls"]
        if item["tool"] == "get_memory_saturation_top"
    )
    assert call["status"] == "tool_error"
    assert call["attempts"] == 2
    assert "TimeoutError" in call["result"]["error"]


@pytest.mark.parametrize(
    "response,normalize",
    [
        (
            {"status": "ok", "coverage": {"sample_count": 1}},
            lambda _raw: (_ for _ in ()).throw(TypeError("malformed result")),
        ),
        (
            {"status": "unexpected", "coverage": {"sample_count": 1}},
            None,
        ),
    ],
)
def test_malformed_tool_results_are_isolated_as_tool_errors(response, normalize):
    class MalformedMCP:
        def call_tool_json(self, *_args, **_kwargs):
            return response

    phases = {
        name: window.model_dump(mode="json")
        for name, window in context().phases.items()
    }
    registry = _EvidenceRegistry(
        client=MalformedMCP(),
        tools={"test_metric": {"name": "test_metric"}},
        phases=phases,
        namespace="sock-shop",
        budget=InvestigationBudget(),
        started=time.monotonic(),
    )

    result = registry.query(
        "test_metric",
        "metrics",
        {},
        level=1,
        normalize=normalize,
    )

    assert result["status"] == "tool_error"
    assert registry.calls[0]["status"] == "tool_error"
    assert registry.observations[0].status == "tool_error"


def test_tool_budget_stops_adaptation_but_preserves_final_synthesis():
    captured = []
    result = answer_multiagent_rca(
        mcp_client=AdaptiveMCP(), context=context(),
        budget=InvestigationBudget(max_tool_calls=7),
        llm=adaptive_llm([], captured),
    )
    assert result["budget"]["tool_calls_used"] == 7
    assert result["budget"]["model_calls_used"] == 3
    assert result["stop_reason"] == "tool_budget"
    assert result["diagnosis"] is not None


def test_token_preflight_does_not_call_model_when_context_exceeds_budget():
    def forbidden(*args, **kwargs):
        pytest.fail("Model must not be called when context exceeds token budget")

    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(), context=context(),
        budget=InvestigationBudget(max_total_tokens=1),
        llm=forbidden,
    )
    assert result["status"] == "error"
    assert result["budget"]["model_calls_used"] == 0
    assert "tokens" in result["error"]


def test_model_returning_after_deadline_is_explicit_execution_error(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(multiagent_rca.time, "monotonic", lambda: now[0])

    def slow(messages, *args, **kwargs):
        now[0] = 2.0
        return response_for(messages, *args, **kwargs)

    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(), context=context(),
        budget=InvestigationBudget(timeout_seconds=1), llm=slow,
    )
    assert result["status"] == "error"
    assert result["stop_reason"] == "execution_error"
    assert result["diagnosis"] is None
    assert result["budget"]["model_calls_used"] == 1
    assert "Tempo total" in result["error"]
