import json
import time

import pytest
from experiment.platform.backend.agent_console import (
    _rca_normalize_loki_range_result,
    _rca_tool_digest,
)
from experiment.platform.backend.analysis.system_card import application_context
from experiment.platform.backend.analysis.telemetry_coverage import coverage_report, termination_summary
from experiment.platform.backend.multiagent_rca import (
    FollowupRequest, InvestigationBudget, MultiagentRCAError,
    CausalAssessment, EvidenceObservation, SpecialistReport,
    _EvidenceRegistry, _log_filter, _model_digest, _model_json,
    answer_multiagent_rca, validate_assessment,
)
from experiment.platform.tests.test_multiagent_rca import (
    AdaptiveMCP, NoDataMCP, adaptive_llm, context, followup, response_for,
)


def test_log_patterns_count_repetitions_and_preserve_distinct_errors():
    start = "2026-10-16T10:00:00Z"
    end = "2026-10-16T10:01:00Z"
    messages = [
        "2026-10-16T10:00:01Z ERROR Connection refused request_id=abcdef123456",
        "2026-10-16T10:00:02Z ERROR Connection refused request_id=abcdef654321",
        "2026-10-16T10:00:03Z ERROR Authentication failed",
    ]
    raw = {"data": {"result": [{
        "stream": {"pod": "orders-abc", "container": "orders"},
        "values": [[str(1792144801000000000 + i * 1000000000), line]
                   for i, line in enumerate(messages)],
    }]}}
    result = _rca_normalize_loki_range_result(
        raw, service="orders", namespace="sock-shop",
        fault_window={"start": start, "end": end}, line_limit=3,
    )
    phase = result["phases"]["fault"]
    assert phase["coverage"]["truncated"] is True
    assert phase["coverage"]["count_scope"] == "sample"
    assert sorted(g["count_observed"] for g in phase["signatures"]) == [1, 2]
    repeated = next(g for g in phase["signatures"] if g["count_observed"] == 2)
    assert len(repeated["examples"]) == 1
    assert repeated["first_seen"] != repeated["last_seen"]
    assert "abcdef123456" not in repeated["pattern"]


def test_explicit_log_filters_escape_literals_and_use_regex_only_when_requested():
    request = FollowupRequest(
        tool="loki_range_query", service="orders", phase="fault",
        question="Buscar erros", patterns=["timeout", "Connection refused"],
        match_mode="literal",
    )
    assert _log_filter(request) == '|~ "(?i)(Connection\\\\ refused|timeout)"'
    regex = request.model_copy(update={"patterns": ["error.*timeout"], "match_mode": "regex"})
    assert _log_filter(regex) == '|~ "(?i)(error.*timeout)"'


def test_context_is_scoped_versioned_and_not_failure_evidence():
    context = application_context("sock-shop", ["catalogue"])
    assert context["kind"] == "application_context"
    assert context["sha256"]
    assert set(context["services"]) == {"catalogue", "catalogue-db", "front-end"}
    assert "role" in context["services"]["catalogue"]
    assert "allowed_fault_categories" not in context
    assert context["platform"]["service_mesh"] == "istio"


def test_trace_digest_keeps_collection_diagnostics_per_phase():
    result = {
        "status": "ok",
        "phases": {"fault": {
            "trace_count": 2,
            "collection": {"search_count": 3, "trace_fetch_error_count": 1},
        }},
    }
    digest = _rca_tool_digest("tempo_service_graph_phases", result)
    assert digest["phase_observations"]["fault"]["collection"] == {
        "search_count": 3, "trace_fetch_error_count": 1,
    }


def test_context_size_is_bounded_for_single_service():
    context = application_context("sock-shop", ["catalogue"])
    assert len(json.dumps(context)) < 3000


@pytest.mark.parametrize("query_request", [
    followup(contains="error|timeout"),
    followup(patterns=["error"], contains="timeout"),
    followup(patterns=["(?=error)"], match_mode="regex"),
    followup(patterns=["["], match_mode="regex"),
    followup(patterns=["a{100000}"], match_mode="regex"),
])
def test_invalid_or_ambiguous_filters_are_explicitly_rejected(query_request):
    with pytest.raises(MultiagentRCAError):
        _log_filter(FollowupRequest.model_validate(query_request))


def test_readiness_compaction_keeps_changes_and_reduces_context_by_half():
    result = {"status": "ok", "phases": {
        phase: {
            "status": "ok", "coverage": {"sample_count": 10},
            "services": [{
                "service": f"service-{i}", "pod_count": 1,
                "ready_mean_pct": 50 if i == 0 and phase == "fault" else 100,
                "pods": [{"pod": f"service-{i}-abc",
                                      "transitions": [{"from": 1, "to": 0}]
                                      if i == 0 and phase == "fault" else [],
                                      "omitted_transition_count": 0}],
            } for i in range(12)],
        }
        for phase in ("baseline", "fault", "post")
    }}
    before = _rca_tool_digest("check_service_readiness", result)
    after = _model_digest("check_service_readiness", result)
    assert len(json.dumps(after)) < len(json.dumps(before)) / 2
    assert after["phases"]["fault"]["affected"][0]["ready_mean_pct"] == 50
    assert after["phases"]["fault"]["affected"][0]["transitions"][0]["transitions"] == [{"from": 1, "to": 0}]


def test_empty_domains_skip_llm_and_context_has_no_experiment_labels():
    captured = []

    def llm(messages, *args, **kwargs):
        captured.append((messages, kwargs))
        return response_for(messages, *args, **kwargs)

    result = answer_multiagent_rca(mcp_client=NoDataMCP(), context=context(), llm=llm)
    assert result["budget"]["model_calls_used"] == 3
    assert all(report["status"] == "no_data" for report in result["specialists"])
    assert result["telemetry_coverage"]["missing_fault_families"] == ["metrics", "logs", "traces"]
    for messages, options in captured:
        assert len(messages[0]["content"]) < 2000
        request = json.loads(messages[-1]["content"])
        assert request["application_context"]["sha256"]
        assert "ground_truth" not in request["application_context"]
        assert "schema" not in request
        assert options["response_format"]["type"] == "json_schema"
    assert result["prompt_version"] == "compact-context-v2"


def test_followup_gets_incremental_evidence_and_coordinator_only_latest_reports():
    class WithLogs(AdaptiveMCP):
        def call_tool_json(self, name, arguments, **kwargs):
            if name == "loki_range_query":
                return {"status": "success", "data": {"result": [{
                    "stream": {"pod": "orders-abc", "container": "orders"},
                    "values": [["1792144861000000000", "ERROR connection refused"]],
                }]}}
            return super().call_tool_json(name, arguments, **kwargs)

    captured = []
    result = answer_multiagent_rca(
        mcp_client=WithLogs(), context=context(),
        llm=adaptive_llm([followup(patterns=["connection refused"]), None], captured),
    )
    updated = next(row for row in captured if row.get("task_id") == "logs-002")
    assert len(updated["evidence"]) == 1
    assert updated["previous_report"]["task_id"] == "logs-001"
    assert updated["application_context"]["services"]["orders"]["stack"]
    decisions = [row for row in captured if row.get("stage") == "coordinator_followup"]
    assert len({row["specialist"] for row in decisions[-1]["specialists"]}) == len(decisions[-1]["specialists"])
    assert all("digest" not in row for row in decisions[-1]["queries"])
    assert len(result["specialists"]) == 4


def test_trace_probes_are_budgeted_and_never_causal_evidence():
    class ProbeMCP:
        def __init__(self):
            self.calls = []

        def call_tool_json(self, name, arguments, **kwargs):
            self.calls.append(arguments)
            return {"traces": [{"traceID": "untrusted-id", "rootServiceName": "unrelated"}]}

    client = ProbeMCP()
    registry = _EvidenceRegistry(
        client=client, tools={"tempo_search_traces": {}},
        phases={"fault": {"start": "2026-10-16T10:01:00Z", "end": "2026-10-16T10:04:00Z"}},
        namespace="sock-shop", budget=InvestigationBudget(max_tool_calls=1),
        started=time.monotonic(),
    )
    registry.probe_trace_coverage()
    registry.probe_trace_coverage()
    assert len(client.calls) == registry.tool_calls_used == 1
    assert not registry.observations
    assert registry.diagnostics[0]["status"] == "unscoped_traces_present"
    assert "unrelated" not in json.dumps(registry.diagnostics)
    assert registry.diagnostics[0]["is_causal_evidence"] is False


def test_termination_history_preserves_phase_and_does_not_invent_events():
    raw = {"status": "success", "data": {"result": [{
        "metric": {"__name__": "kube_pod_container_status_last_terminated_timestamp",
                   "pod": "orders-abc", "container": "orders"},
        "values": [[1792144801, "1792140000"], [1792144861, "1792140000"]],
    }]}}
    phases = {key: value.model_dump(mode="json") for key, value in context().phases.items()}
    summary = termination_summary(raw, phases)
    assert summary["phases"]["baseline"]["coverage"]["sample_count"] == 1
    assert summary["phases"]["fault"]["coverage"]["sample_count"] == 1
    assert summary["phases"]["fault"]["termination_states"][0]["last_value"] == 1792140000
    assert any("preceder" in item for item in summary["limitations"])
    assert termination_summary({"status": "error", "error": "down"}, phases)["status"] == "tool_error"


def test_window_count_is_distinct_from_sample_and_reaches_digest():
    class CountMCP:
        def call_tool_json(self, name, arguments, **kwargs):
            assert 'count_over_time(' in arguments["query"]
            assert arguments["start"] == arguments["end"] == "2026-10-16T10:04:00Z"
            return {"status": "success", "data": {"resultType": "matrix", "result": [{
                "metric": {"container": "orders", "error_pattern": "timeout"},
                "values": [[1792145040, "84"]],
            }, {
                "metric": {"container": "orders", "error_pattern": "TIMEOUT"},
                "values": [[1792145040, "16"]],
            }]}}

    registry = _EvidenceRegistry(
        client=CountMCP(), tools={"loki_range_query": {}},
        phases={key: value.model_dump(mode="json") for key, value in context().phases.items()},
        namespace="sock-shop", budget=InvestigationBudget(), started=time.monotonic(),
    )
    registry.query_log_counts(["orders"])
    call = registry.calls[0]
    phase = call["digest"]["phases"]["fault"]
    assert phase["pattern_counts"] == [{"service": "orders", "pattern": "timeout", "count": 100}]
    assert phase["coverage"]["count_scope"] == "window"
    assert call["fault_observation_count"] == 100
    assert coverage_report(registry.calls)["families"]["logs"]["fault_observations"] == 100


def test_hyphenated_service_regexes_are_quoted_for_promql_and_logql():
    class QuotedMCP:
        def call_tool_json(self, name, arguments, **kwargs):
            query = arguments["query"]
            assert r"queue\\-master" in query
            if name == "prometheus_range_query":
                return {"status": "success", "data": {"result": []}}
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    registry = _EvidenceRegistry(
        client=QuotedMCP(), tools={"loki_range_query": {}, "prometheus_range_query": {}},
        phases={key: value.model_dump(mode="json") for key, value in context().phases.items()},
        namespace="sock-shop", budget=InvestigationBudget(), started=time.monotonic(),
    )
    registry.query_log_counts(["queue-master"])
    registry.query_termination(["queue-master"])
    assert all(call["status"] == "no_data" for call in registry.calls)


@pytest.mark.parametrize("bad_value", ["NaN", "+Inf", "-Inf"])
def test_termination_rejects_nonfinite_values_as_tool_error(bad_value):
    phases = {key: value.model_dump(mode="json") for key, value in context().phases.items()}
    result = termination_summary(
        {"status": "success", "data": {"result": [{"metric": {},
                                                 "values": [[1792144861, bad_value]]}]}},
        phases,
    )
    assert result["status"] == "tool_error"


@pytest.mark.parametrize("unknown_restart,overlap", [(True, False), (False, True)])
def test_unknown_restart_or_conflicting_references_prevents_confirmation(unknown_restart, overlap):
    assessment = CausalAssessment(
        run_id=context().run_id, status="confirmed", primary_service="orders",
        observed_events=["restart"], probable_mechanism="crash", root_cause="process failure",
        category="pod-failure", impact=[], causal_chain=["crash", "restart"],
        supporting_evidence=["E001", "E002"],
        contradicting_evidence=["E002"] if overlap else [],
        restart_analysis=[], alternatives=["oom", "probe"], limitations=[], next_checks=[],
    )
    observations = [
        EvidenceObservation(
            evidence_id=ref, family=family, level=2, source="live", query_ref=ref,
            status="ok", service="orders", windows={}, observation_count=1,
            fact="observed", limitations=[],
        )
        for ref, family in [("E001", "metrics"), ("E002", "logs")]
    ]
    result = validate_assessment(
        assessment, context=context(), observations=observations,
        calls=[{"evidence_id": ref, "fault_observation_count": 1} for ref in ["E001", "E002"]],
        restarted_services=["orders"] if unknown_restart else [],
    )
    assert result.status == "inconclusive"
    assert any(("restart desconhecido" if unknown_restart else "ambiguo") in row for row in result.limitations)


def test_specialist_schema_restricts_references_and_has_room_for_complete_output():
    def llm(messages, tools, **kwargs):
        assert kwargs["max_new_tokens"] == 2048
        schema = kwargs["response_format"]["json_schema"]["schema"]
        refs = schema["$defs"]["SpecialistFinding"]["properties"]["evidence_ids"]
        assert refs["items"]["enum"] == ["E001", "E002"]
        return {"content": json.dumps({
            "task_id": "metrics-001", "specialist": "metrics", "status": "completed",
            "findings": [{"statement": "Observado", "relation": "context", "evidence_ids": ["E001"]}],
            "limitations": [], "next_questions": [],
        })}

    result, attempt = _model_json(
        llm=llm, messages=[{"role": "user", "content": "{}"}], schema=SpecialistReport,
        model_budget={"calls": 0, "tokens": 0}, budget=InvestigationBudget(),
        started=time.monotonic(), stage="metrics_specialist", model="test",
        allowed_evidence=["E001", "E002"],
    )
    assert result.findings[0].evidence_ids == ["E001"]
    assert attempt["estimated_input_tokens"] > attempt["prompt_characters"] / 4
