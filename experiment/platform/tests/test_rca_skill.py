from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from experiment.platform.backend import rca_skill

PHASES = {
    "baseline": {"start": "2026-10-02T10:00:00Z", "end": "2026-10-02T10:01:00Z"},
    "fault": {"start": "2026-10-02T10:02:00Z", "end": "2026-10-02T10:03:00Z"},
    "post": {"start": "2026-10-02T10:03:00Z", "end": "2026-10-02T10:04:00Z"},
}


def diagnosis(**overrides):
    result = {
        "status": "confirmed",
        "primary_service": "front-end",
        "category": "network-latency",
        "mechanism": "Timeout para dependencia observado durante a falha",
        "supporting_evidence": ["E001", "E006"],
        "contradicting_evidence": [],
        "alternatives": [
            {
                "hypothesis": hypothesis,
                "supporting_evidence": [],
                "contradicting_evidence": [],
            }
            for hypothesis in ("Pressao de CPU", "Falha na dependencia")
        ],
        "causal_chain": ["Timeout na dependencia", "5xx no chamador"],
        "restart_analysis": [],
        "limitations": [],
        "next_checks": ["Correlacionar spans da dependencia"],
    }
    result.update(overrides)
    return result


def evidence(identifier, family, *, status="ok", count=2, fault_count=2):
    return {
        "id": identifier,
        "family": family,
        "status": status,
        "observation_count": count,
        "fault_observation_count": fault_count,
    }


def validate(
    value,
    items,
    restarted=None,
    possible_fault_types=None,
    require_best_guess=False,
):
    return rca_skill.validate_diagnosis(
        rca_skill.Diagnosis.model_validate(value),
        items,
        {"front-end", "orders"},
        {"network-latency", "pod-failure"},
        restarted or [],
        possible_fault_types,
        require_best_guess,
    )


@pytest.mark.parametrize(
    "items, expected",
    [
        ([evidence("E001", "metrics"), evidence("E006", "logs")], "confirmed"),
        ([evidence("E001", "metrics"), evidence("E006", "metrics")], "inconclusive"),
        (
            [
                evidence("E001", "metrics"),
                evidence("E006", "logs", status="tool_error"),
            ],
            "inconclusive",
        ),
        (
            [
                evidence("E001", "metrics"),
                evidence("E006", "logs", count=0, fault_count=0),
            ],
            "inconclusive",
        ),
        (
            [evidence("E001", "metrics"), evidence("E006", "traces", fault_count=0)],
            "inconclusive",
        ),
    ],
)
def test_confirmation_requires_independent_fault_telemetry(items, expected):
    assert validate(diagnosis(), items).status == expected


def test_unknown_restart_reason_prevents_confirmation():
    restart = {
        "service": "front-end",
        "reason_status": "supported",
        "reason": "Restart observado; motivo ainda desconhecido",
        "supporting_evidence": ["E001"],
        "next_check": "Consultar eventos e exit code",
    }
    result = validate(
        diagnosis(restart_analysis=[restart]),
        [evidence("E001", "metrics"), evidence("E006", "logs")],
        ["front-end"],
    )
    assert result.status == "inconclusive"
    assert result.restart_analysis[0].reason_status == "unknown"


@pytest.mark.parametrize(
    "value, service, category",
    [
        (diagnosis(primary_service="not-a-service"), None, "network-latency"),
        (diagnosis(category="invalid"), "front-end", "unknown"),
        (
            diagnosis(supporting_evidence=["E001", "E999"]),
            "front-end",
            "network-latency",
        ),
        (diagnosis(contradicting_evidence=["E001"]), "front-end", "network-latency"),
    ],
)
def test_invalid_model_claims_are_sanitized_and_downgraded(value, service, category):
    result = validate(value, [evidence("E001", "metrics"), evidence("E006", "logs")])
    assert result.status == "inconclusive"
    assert result.primary_service == service
    assert result.category == category
    assert set(result.supporting_evidence + result.contradicting_evidence) <= {
        "E001",
        "E006",
    }


def test_requires_investigation_of_every_restart():
    result = validate(diagnosis(), [], ["front-end"])
    assert result.status == "inconclusive"
    assert len(result.restart_analysis) == 1
    assert result.restart_analysis[0].service == "front-end"
    assert result.restart_analysis[0].reason_status == "unknown"


def test_unobserved_model_restart_is_removed_and_downgraded():
    value = diagnosis(
        restart_analysis=[
            {
                "service": "orders",
                "reason_status": "supported",
                "reason": "Restart presumido",
                "supporting_evidence": [],
                "next_check": "Verificar",
            }
        ]
    )
    result = validate(value, [], [])
    assert result.status == "inconclusive"
    assert result.restart_analysis == []
    assert any("sem evento confirmado" in item for item in result.limitations)


def test_best_guess_requires_a_known_service_even_when_inconclusive():
    with pytest.raises(rca_skill.SkillRCAError, match="best guess"):
        validate(
            diagnosis(status="inconclusive", primary_service=None),
            [],
            possible_fault_types=["container-kill"],
            require_best_guess=True,
        )

    result = validate(
        diagnosis(
            status="inconclusive",
            primary_service="orders",
            fault_type="container-kill",
        ),
        [],
        possible_fault_types=["container-kill"],
        require_best_guess=True,
    )
    assert result.primary_service == "orders"
    assert result.fault_type == "container-kill"


class FakeMCP:
    def __init__(self, *, empty_logs=False, restart=False):
        self.calls = []
        self.empty_logs = empty_logs
        self.restart = restart

    def list_tools(self):
        names = (
            "get_run_golden_signals",
            "check_service_readiness",
            "check_oom_kills",
            "check_pod_restarts",
            "prometheus_range_query",
            "loki_range_query",
            "tempo_service_graph_phases",
            "tempo_analyze_trace",
        )
        properties = {
            key: {}
            for key in (
                "baseline_start",
                "baseline_end",
                "fault_start",
                "fault_end",
                "post_start",
                "post_end",
                "start",
                "end",
                "namespace",
                "step",
                "k",
            )
        }
        return [
            {"name": name, "inputSchema": {"properties": properties}} for name in names
        ]

    def call_tool_json(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_run_golden_signals":
            return {
                "status": "ok",
                "metrics": {
                    "error_rate": {
                        "status": "ok",
                        "unit": "%",
                        "phases": {phase: {"sample_count": 3} for phase in PHASES},
                        "top": [
                            {
                                "service": "front-end",
                                "baseline_mean": 0,
                                "fault_mean": 20,
                                "delta_abs": 20,
                            }
                        ],
                    }
                },
            }
        if name == "loki_range_query":
            timestamp = int(
                datetime.fromisoformat(
                    arguments["end"].replace("Z", "+00:00")
                ).timestamp()
                * 1_000_000_000
            )
            return {
                "data": {
                    "result": (
                        []
                        if self.empty_logs
                        else [
                            {
                                "stream": {
                                    "pod": "front-end-abc-123",
                                    "container": "front-end",
                                },
                                "values": [[str(timestamp), "error: upstream timeout"]],
                            }
                        ]
                    )
                }
            }
        if name == "prometheus_range_query":
            return {"status": "success", "data": {"result": []}}
        if name == "tempo_service_graph_phases":
            return {"phases": {phase: {"trace_count": 0} for phase in PHASES}}
        return {
            "status": "ok",
            "coverage": {"sample_count": 3},
            "top": [{"service": "front-end", "restarts_in_window": int(self.restart)}],
        }


def run_executor(
    client,
    final=None,
    metadata=None,
    candidate="front-end",
    possible_fault_types=None,
    require_best_guess=False,
    archive_traces=None,
    evidence_source="live",
):
    contexts = []

    def model(messages, tools, **kwargs):
        assert tools == [] and kwargs["tool_choice"] == "none"
        context = json.loads(messages[-1]["content"])
        contexts.append(context)
        if context["stage"] == "plan":
            value = {
                "candidates": [candidate],
                "hypotheses": ["Rede", "CPU"],
                "reasoning": "Comparar sinais",
            }
        else:
            evidence_by_family = {
                item["family"]: item["id"]
                for item in context["evidence"]
                if item["fault_observation_count"] > 0
                and item["status"] == "ok"
                and item["tool"]
                in {
                    "get_run_golden_signals",
                    "prometheus_range_query",
                    "loki_range_query",
                }
            }
            value = final or diagnosis(
                supporting_evidence=[
                    evidence_by_family[family]
                    for family in ("metrics", "logs")
                    if family in evidence_by_family
                ],
                **(
                    {"fault_type": possible_fault_types[0]}
                    if possible_fault_types
                    else {}
                ),
            )
        return {"content": json.dumps(value)}

    result = rca_skill.answer_skill_rca(
        mcp_client=client,
        phases=PHASES,
        namespace="sock-shop",
        llm=model,
        window_metadata=metadata,
        possible_fault_types=possible_fault_types,
        require_best_guess=require_best_guess,
        archive_traces=archive_traces,
        evidence_source=evidence_source,
    )
    return result, contexts


def test_progressive_execution_exact_log_identity_and_windows():
    client = FakeMCP()
    result, contexts = run_executor(client)
    assert result["status"] == "confirmed"
    assert [name for name, _ in client.calls[:4]] == [
        "get_run_golden_signals",
        "check_service_readiness",
        "check_oom_kills",
        "check_pod_restarts",
    ]
    logs = [arguments for name, arguments in client.calls if name == "loki_range_query"]
    assert len(logs) == 3
    assert logs[1]["start"] == PHASES["fault"]["start"]
    assert logs[1]["end"] == PHASES["fault"]["end"]
    assert 'container="front-end"' in logs[1]["query"]
    assert "\\-" not in logs[1]["query"]
    assert logs[1]["limit"] == 150
    term_query = next(
        arguments
        for name, arguments in client.calls
        if name == "prometheus_range_query"
    )
    assert "kube_pod_container_status_last_terminated_" in term_query["query"]
    assert PHASES["baseline"]["start"] <= term_query["start"] < PHASES["fault"]["start"]
    assert term_query["end"] > PHASES["fault"]["end"]
    fault_log = next(
        item
        for item in result["evidence"]
        if item["tool"] == "loki_range_query"
        and item["arguments"]["start"] == PHASES["fault"]["start"]
    )
    assert fault_log["fault_observation_count"] == 1
    assert "prometheus_range_query" in result["tools_used"]
    assert [context["stage"] for context in contexts] == ["plan", "diagnosis"]


def test_historical_evidence_source_and_trace_query_budget_are_explicit():
    class LiveLabeledMCP(FakeMCP):
        def call_tool_json(self, name, arguments):
            result = super().call_tool_json(name, arguments)
            if isinstance(result, dict):
                return {**result, "evidence_source": "live"}
            return result

    result, _ = run_executor(LiveLabeledMCP(), evidence_source="history")

    assert result["evidence"]
    assert {item["source"] for item in result["evidence"]} == {"history"}
    trace_call = next(
        call for call in result["calls"]
        if call["tool"] == "tempo_service_graph_phases"
    )
    assert trace_call["arguments"]["limit"] == 10


def test_archive_trace_evidence_is_read_only_and_reaches_diagnosis_context():
    archive = {
        "status": "ok",
        "evidence_source": "archive",
        "campaign_id": "campaign-2026",
        "run_id": "run-test",
        "archive_key": "campaign-2026/run-test/source-bundle.json.gz",
        "bundle_sha256": "a" * 64,
        "source_search_complete": True,
        "source_fetch_error_count": 0,
        "manifest_trace_status": "partial",
        "search_index_status": "partial",
        "trace_count": 1,
        "span_count": 1,
        "phases": {
            "baseline": {
                "trace_count": 0,
                "span_count": 0,
                "error_span_count": 0,
                "service_span_counts": {},
            },
            "fault": {
                "trace_count": 1,
                "span_count": 1,
                "error_span_count": 1,
                "service_span_counts": {"orders": 1},
            },
        },
        "trace_details": [
            {
                "trace_id": "1" * 32,
                "phase": "fault",
                "service": "orders",
                "operation": "GET /orders",
                "duration_ms": 200,
                "http_status_code": 500,
                "is_error": True,
                "spans": [
                    {
                        "span_id": "2" * 16,
                        "service": "orders",
                        "operation": "GET /orders",
                        "start_time": "2026-01-01T00:00:00Z",
                        "duration_ms": 200,
                        "is_error": True,
                    }
                ],
            }
        ],
    }
    client = FakeMCP()

    result, contexts = run_executor(client, archive_traces=archive)

    assert not any(name == "history_archive_traces" for name, _ in client.calls)
    evidence = next(
        item for item in result["evidence"] if item["tool"] == "history_archive_traces"
    )
    assert evidence["family"] == "traces"
    assert evidence["source"] == "archive"
    assert evidence["status"] == "ok"
    assert evidence["fault_observation_count"] == 1
    assert any(
        item["tool"] == "history_archive_traces"
        for item in contexts[-1]["evidence"]
    )
    assert result["evidence_bundle"]["coverage"]["history_archive_traces"]["source"] == "archive"
    assert any(
        item["kind"] == "archived_trace_span"
        for item in result["evidence_bundle"]["timeline"]
    )


def test_benchmark_fault_type_is_required_and_selected_even_when_inconclusive():
    result, contexts = run_executor(
        FakeMCP(),
        final=diagnosis(status="inconclusive", fault_type="container-kill"),
        possible_fault_types=["container-kill", "network-loss"],
        require_best_guess=True,
    )

    diagnosis_context = contexts[-1]
    output_schema = diagnosis_context["output_schema"]
    assert diagnosis_context["context"]["possible_fault_types"] == [
        "container-kill",
        "network-loss",
    ]
    assert output_schema["properties"]["fault_type"]["enum"] == [
        "container-kill",
        "network-loss",
    ]
    assert "fault_type" in output_schema["required"]
    services = output_schema["properties"]["primary_service"]["enum"]
    assert "front-end" in services
    assert "primary_service" in output_schema["required"]
    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["primary_service"] == "front-end"
    assert result["diagnosis"]["fault_type"] == "container-kill"
    assert len(result["skill"]["sha256"]) == 64
    assert "chaos_profile" not in json.dumps(contexts)


def test_edge_latency_profiles_are_collected_before_planning():
    class EdgeMCP(FakeMCP):
        def call_tool_json(self, name, arguments):
            if name != "prometheus_range_query":
                return super().call_tool_json(name, arguments)
            self.calls.append((name, arguments))
            query = arguments["query"]
            if "last_terminated" in query:
                return {"status": "success", "data": {"result": []}}
            baseline_start = "2026-10-02T10:01:15Z"
            baseline_timestamp = (
                int(
                    datetime.fromisoformat(
                        baseline_start.replace("Z", "+00:00")
                    ).timestamp()
                )
                + 10
            )
            fault_timestamp = (
                int(
                    datetime.fromisoformat(
                        PHASES["fault"]["start"].replace("Z", "+00:00")
                    ).timestamp()
                )
                + 10
            )
            labels = (
                {"source_workload": "front-end", "destination_workload": "orders"}
                if "istio_request_duration_milliseconds" in query
                else {"client": "front-end", "server": "orders"}
            )
            return {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "metric": labels,
                            "values": [
                                [baseline_timestamp, "0.01"],
                                [fault_timestamp, "0.15"],
                            ],
                        }
                    ]
                },
            }

    client = EdgeMCP()
    result, contexts = run_executor(client)
    edge_calls = [
        arguments
        for name, arguments in client.calls
        if name == "prometheus_range_query"
        and "last_terminated" not in arguments["query"]
    ]
    assert len(edge_calls) == 3
    assert all(arguments["step"] == "15s" for arguments in edge_calls)
    assert all(arguments["start"] == "2026-10-02T10:01:15Z" for arguments in edge_calls)
    assert all(arguments["end"] == PHASES["post"]["end"] for arguments in edge_calls)
    assert contexts[0]["context"]["edge_latency_candidates"] == ["orders"]
    assert contexts[1]["context"]["edge_latency_candidates"] == ["orders"]
    assert any(
        'container="orders"' in arguments["query"]
        for name, arguments in client.calls
        if name == "loki_range_query"
    )
    assert result["status"] == "confirmed"
    edge_evidence = [
        item
        for item in result["evidence"]
        if item["digest"].get("purpose") == "edge_latency_triage"
    ]
    assert len(edge_evidence) == 3
    assert all(item["fault_observation_count"] == 1 for item in edge_evidence)


def test_no_logs_or_traces_cannot_confirm():
    result, _ = run_executor(FakeMCP(empty_logs=True))
    assert result["status"] == "inconclusive"


def test_asyncio_timeout_from_trace_tool_is_recorded_without_aborting():
    class TimedOutTraceMCP(FakeMCP):
        def call_tool_json(self, name, arguments):
            if name == "tempo_service_graph_phases":
                self.calls.append((name, arguments))
                raise asyncio.TimeoutError()
            return super().call_tool_json(name, arguments)

    result, _ = run_executor(TimedOutTraceMCP())

    trace_evidence = next(
        item
        for item in result["evidence"]
        if item["tool"] == "tempo_service_graph_phases"
    )
    assert trace_evidence["status"] == "tool_error"
    assert result["status"] == "confirmed"


def test_planned_fault_window_cannot_confirm():
    result, _ = run_executor(
        FakeMCP(), metadata={"phase_sources": {"fault": "planned_phase_fallback"}}
    )
    assert result["status"] == "inconclusive"
    assert any("Horario real" in item for item in result["diagnosis"]["limitations"])


def test_invalid_final_preserves_collected_evidence():
    result, _ = run_executor(FakeMCP(), diagnosis(supporting_evidence=["E999"]))
    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["supporting_evidence"] == []
    assert result["calls"] and result["evidence"]
    assert result["error"] is None


def test_unknown_plan_candidate_is_filtered_and_downgraded():
    result, contexts = run_executor(FakeMCP(), candidate="invented-service")
    assert result["status"] == "inconclusive"
    assert any(
        "Candidatos desconhecidos" in item
        for item in result["diagnosis"]["limitations"]
    )
    assert contexts[0]["context"]["restarted_services"] == []


def test_malformed_model_response_is_retried_and_reported():
    model_options = []

    def malformed_model(*args, **kwargs):
        model_options.append(kwargs)
        return {"content": "not JSON"}

    result = rca_skill.answer_skill_rca(
        mcp_client=FakeMCP(),
        phases=PHASES,
        namespace="sock-shop",
        llm=malformed_model,
    )
    assert result["status"] == "error"
    assert len(result["model_attempts"]) == 3
    assert result["calls"]
    assert len(model_options) == 3
    assert all(
        options["response_format"]["type"] == "json_schema"
        and options["response_format"]["json_schema"]["name"] == "InvestigationPlan"
        and options["chat_template_kwargs"] == {"enable_thinking": False}
        for options in model_options
    )


def test_missing_tools_are_explicit_not_empty_success():
    client = FakeMCP()
    client.list_tools = lambda: []
    result, _ = run_executor(client)
    assert result["status"] == "inconclusive"
    assert all(item["status"] == "tool_error" for item in result["evidence"])
    assert not client.calls


def test_redaction_preserves_json_structure():
    value = {"nested": ['password=secret "quoted"', {"value": "x" * 20000}]}
    sanitized = rca_skill._sanitize(value)
    assert "secret" not in json.dumps(sanitized)
    assert isinstance(sanitized["nested"][1], dict)


def test_skill_frontmatter_and_references():
    body, references, digest = rca_skill.load_skill()
    assert "restart" in body
    assert set(references) == {"evidence-contract.md", "causal-checklist.md"}
    assert len(digest) == 64


def test_executor_investigates_restart_and_preserves_unknown_reason():
    restart = {
        "service": "front-end",
        "reason_status": "unknown",
        "reason": "Restart sem eventos anteriores suficientes",
        "supporting_evidence": ["E004"],
        "next_check": "Consultar motivo de terminacao e probes",
    }
    result, contexts = run_executor(
        FakeMCP(restart=True), diagnosis(restart_analysis=[restart])
    )
    assert contexts[0]["context"]["restarted_services"] == ["front-end"]
    assert result["status"] == "inconclusive"
    assert result["diagnosis"]["restart_analysis"][0]["reason_status"] == "unknown"
    assert (
        len([call for call in result["calls"] if call["tool"] == "loki_range_query"])
        == 3
    )


def test_executor_bounds_fault_trace_details():
    class TraceMCP(FakeMCP):
        def call_tool_json(self, name, arguments):
            if name == "tempo_service_graph_phases":
                self.calls.append((name, arguments))
                return {
                    "phases": {
                        "fault": {
                            "trace_count": 20,
                            "failure_signatures": [
                                {
                                    "trace_id": character * 32,
                                    "signature": "NETWORK_TIMEOUT",
                                    "affected_service": "front-end",
                                }
                                for character in "abc"
                            ],
                        }
                    }
                }
            if name == "tempo_analyze_trace":
                self.calls.append((name, arguments))
                return {
                    "span_count": 3,
                    "error_spans": [
                        {
                            "service": "front-end",
                            "name": "GET /",
                            "message": "Bearer secret-token",
                        }
                    ],
                }
            return super().call_tool_json(name, arguments)

    client = TraceMCP()
    result, _ = run_executor(client)
    details = [args for name, args in client.calls if name == "tempo_analyze_trace"]
    assert details == [{"trace_id": "a" * 32}, {"trace_id": "b" * 32}]
    assert "secret-token" not in json.dumps(result)
    assert any(
        "limite de traces" in item for item in result["diagnosis"]["limitations"]
    )


def test_malformed_coverage_becomes_explicit_tool_error():
    class InvalidMCP(FakeMCP):
        def call_tool_json(self, name, arguments):
            value = super().call_tool_json(name, arguments)
            if name == "get_run_golden_signals":
                value["metrics"] = {
                    "invalid": {"phases": {"fault": {"sample_count": "bad"}}}
                }
            return value

    result, _ = run_executor(InvalidMCP())
    assert result["evidence"][0]["status"] == "tool_error"
    assert "Invalid MCP coverage" in result["calls"][0]["result"]["error"]
    assert result["status"] == "inconclusive"
