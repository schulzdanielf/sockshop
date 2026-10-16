import json

import pytest
from pydantic import ValidationError

from experiment.platform.backend.multiagent_rca import (
    CausalAssessment,
    CoordinatorDecision,
    CoordinatorWorkPlan,
    EvidenceObservation,
    InvestigationContext,
    InvestigationBudget,
    InvestigationPlan,
    FollowupRequest,
    SpecialistReport,
    TimeWindow,
    answer_multiagent_rca,
    build_investigation_plan,
    validate_assessment,
)
from experiment.platform.backend.analysis.llm_client import LLMClientError
from experiment.platform.backend import multiagent_rca
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
        hypotheses=["Falha no servico", "Falha em dependencia"],
        logs_question=question,
        traces_question=question,
    )
    assert plan.logs_question == plan.traces_question == question


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
        root_cause=None, category="unknown", impact=[], causal_chain=[],
        supporting_evidence=["E001"], contradicting_evidence=[],
        restart_analysis=[], alternatives=["OOM", "Termino externo"], limitations=["Motivo desconhecido"],
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
        content = {
            "candidates": ["carts"],
            "hypotheses": ["Degradacao no servico", "Falha em dependencia"],
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
        content = {
            "run_id": request["run_id"],
            "status": "confirmed",
            "primary_service": "carts",
            "observed_events": ["Falha nao estabelecida"],
            "probable_mechanism": "Hipotese sem dados",
            "root_cause": "Hipotese sem dados",
            "category": "pod-failure",
            "impact": [],
            "causal_chain": ["Hipotese sem dados"],
            "supporting_evidence": [],
            "contradicting_evidence": [],
            "restart_analysis": [],
            "alternatives": ["Falha na dependencia", "Falha local"],
            "limitations": [],
            "next_checks": [],
        }
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
    assert result["budget"]["model_calls_used"] == 3
    assert result["stop_reason"] == "coordinator_stop"
    assert all(item["status"] == "tool_error" for item in result["evidence"])
    assert "must-not-reach-model" not in json.dumps(result)
    log_windows = [
        next(iter(item["windows"]))
        for item in result["evidence"]
        if item["family"] == "logs"
    ]
    assert log_windows == ["baseline", "fault"]


def test_executor_returns_explicit_failure_when_model_is_unavailable():
    def unavailable(*_args, **_kwargs):
        raise LLMClientError("model unavailable")

    result = answer_multiagent_rca(
        mcp_client=NoDataMCP(),
        context=context(),
        llm=unavailable,
        budget=InvestigationBudget(max_model_calls=1),
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
        alternatives=["alternative one", "alternative two"],
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
                "loki_range_query", "get_memory_saturation_top",
                "get_cpu_saturation_top", "tempo_service_graph_phases",
            )
        ]

    def call_tool_json(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
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
        "question": "A dependencia explica os erros?", **extras,
    }


def test_adaptive_round_queries_new_service_reanalyses_only_logs_and_stops():
    mcp, captured = AdaptiveMCP(), []
    result = answer_multiagent_rca(
        mcp_client=mcp, context=context(),
        llm=adaptive_llm([followup(contains='timeout "quoted"'), None], captured),
    )
    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "coordinator_stop"
    assert result["budget"]["model_calls_used"] == 5
    assert result["budget"]["followup_rounds_used"] == 1
    assert len(result["followup_rounds"]) == 2
    record = result["followup_rounds"][0]
    assert record["status"] == "completed" and record["specialist_task_id"] == "logs-002"
    log_query = [args for tool, args, _ in mcp.calls if tool == "loki_range_query"][-1]
    assert 'container="orders"' in log_query["query"]
    assert log_query["query"].endswith('|= "timeout \\"quoted\\""')
    assert log_query["start"] == "2026-10-16T10:01:00Z"
    assert log_query["end"] == "2026-10-16T10:04:00Z"
    assert [r["task_id"] for r in captured if "task_id" in r] == ["traces-001"]
    assert result["specialists"][-1]["task_id"] == "logs-002"
    assert result["specialists"][-1]["status"] == "no_data"
    final = captured[-1]
    assert final["followup_rounds"] == result["followup_rounds"]
    assert all(call[2]["timeout_seconds"] <= 30 for call in mcp.calls)


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
        (InvestigationBudget(), 2, 7, "round_limit"),
        (InvestigationBudget(max_model_calls=5), 1, 5, "model_budget"),
        (InvestigationBudget(max_model_calls=3), 0, 3, "model_budget"),
        (InvestigationBudget(max_followup_rounds=0), 0, 3, "followups_disabled"),
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
    assert result["stop_reason"] == "invalid_followup"
    assert result["followup_rounds"][0]["status"] == "rejected"
    assert "contains is only supported for logs" in result["followup_rounds"][0]["error"]


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
