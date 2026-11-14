from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Literal

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


def evidence(
    identifier,
    family,
    *,
    status="ok",
    count=2,
    fault_count=2,
    source_provenance=None,
    service=None,
):
    item = {
        "id": identifier,
        "family": family,
        "status": status,
        "observation_count": count,
        "fault_observation_count": fault_count,
        "source_provenance": source_provenance
        or {
            "metrics": "prometheus_golden_signals",
            "logs": "loki_logs",
            "traces": "tempo_traces",
        }.get(family, "unknown"),
    }
    if service:
        item["service"] = service
    return item


def http_istio_evidence(
    identifier,
    service,
    caller,
    *,
    response_code="500",
    response_flags="FI",
):
    item = evidence(
        identifier,
        "metrics",
        source_provenance="prometheus_istio",
        service=service,
    )
    item["result"] = {
        "purpose": "http_status_rates",
        "service": service,
        "phases": {
            "fault": {
                "series": [
                    {
                        "labels": {
                            "source_workload": caller,
                            "source_workload_namespace": "sock-shop",
                            "destination_workload": service,
                            "destination_workload_namespace": "sock-shop",
                            "response_code": response_code,
                            "response_flags": response_flags,
                        },
                        "mean": 1.0,
                        "max": 2.0,
                    }
                ],
            },
        },
    }
    return item


def http_caller_evidence(identifier, family, provenance):
    item = evidence(identifier, family, source_provenance=provenance, service="orders")
    if family == "logs":
        item["result"] = {
            "service": "orders",
            "phases": {
                "fault": {
                    "sample_lines": [
                        {
                            "message": "HTTP 500 from http://payment.sock-shop.svc.cluster.local/api",
                        }
                    ],
                },
            },
        }
    else:
        item["result"] = {
            "phases": {
                "fault": {
                    "error_spans": [
                        {
                            "service": "orders",
                            "remote_service": "payment",
                            "http_status_code": "500",
                        }
                    ],
                },
            },
        }
    return item


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
        {"front-end", "orders", "payment"},
        {"http-error", "network-latency", "pod-failure"},
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


@pytest.mark.parametrize(
    ("family", "provenance"),
    [("logs", "loki_logs"), ("traces", "tempo_traces")],
)
def test_http_istio_evidence_is_independent_from_caller_logs_or_traces(
    family, provenance
):
    result = validate(
        diagnosis(
            primary_service="payment",
            fault_type="http-status-code",
        ),
        [
            http_istio_evidence("E001", "payment", "orders"),
            http_caller_evidence("E006", family, provenance),
        ],
    )
    assert result.status == "confirmed"


@pytest.mark.parametrize(
    ("response_flags", "rate_max"),
    [("-", 2.0), ("FI", 0.0)],
)
def test_http_caller_error_requires_correlated_positive_fault_edge(
    response_flags, rate_max
):
    metric = http_istio_evidence(
        "E001",
        "payment",
        "orders",
        response_flags=response_flags,
    )
    metric["result"]["phases"]["fault"]["series"][0].update(mean=0.0, max=rate_max)
    result = validate(
        diagnosis(primary_service="payment", fault_type="http-status-code"),
        [
            metric,
            {
                **evidence("E006", "logs", service="orders"),
                "result": {"message": "HttpServerErrorException: 500"},
            },
        ],
    )
    assert result.status == "inconclusive"


@pytest.mark.parametrize(
    "caller_result",
    [
        {"message": "HTTP 500 from http://payment.sock-shop.svc/api"},
        {
            "service": "orders",
            "phases": {
                "fault": {
                    "sample_lines": [
                        {
                            "message": "HTTP 500 from http://shipping.sock-shop.svc/api",
                        }
                    ],
                },
            },
        },
        {
            "service": "orders",
            "phases": {
                "baseline": {
                    "sample_lines": [
                        {
                            "message": "HTTP 500 from http://payment.sock-shop.svc/api",
                        }
                    ],
                },
            },
        },
    ],
)
def test_http_caller_evidence_must_match_fault_phase_and_edge(caller_result):
    caller_item = {
        **evidence("E006", "logs", service="orders"),
        "result": caller_result,
    }
    if "baseline" in (caller_result.get("phases") or {}):
        caller_item["fault_observation_count"] = 0
    result = validate(
        diagnosis(primary_service="payment", fault_type="http-status-code"),
        [
            http_istio_evidence("E001", "payment", "orders"),
            caller_item,
        ],
    )
    assert result.status == "inconclusive"


def test_span_derived_metric_does_not_independently_corroborate_http_trace():
    result = validate(
        diagnosis(primary_service="orders", fault_type="http-status-code"),
        [
            evidence(
                "E001",
                "metrics",
                source_provenance="span_derived",
                service="orders",
            ),
            evidence(
                "E006",
                "traces",
                source_provenance="tempo_traces",
                service="orders",
            ),
        ],
    )
    assert result.status == "inconclusive"


def test_unknown_metric_provenance_is_not_counted_and_is_reported():
    result = validate(
        diagnosis(),
        [
            evidence(
                "E001",
                "metrics",
                source_provenance="unknown",
                service="front-end",
            ),
            evidence("E006", "logs", service="front-end"),
        ],
    )

    assert result.status == "inconclusive"
    assert any(
        "Proveniencia de uma metrica desconhecida" in item
        for item in result.limitations
    )


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
            "tempo_search_traces",
            "tempo_analyze_trace",
            "get_cpu_saturation_top",
            "get_memory_saturation_top",
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
    max_additional_checks=0,
    additional_checks=0,
    max_model_calls=None,
    max_total_tokens=None,
    timeout_seconds=None,
    malformed_additional_attempts=0,
    model_usage_tokens=None,
    archive_traces=None,
    evidence_source: Literal["live", "history"] = "live",
    model_id: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    captured_model_kwargs: list[dict[str, Any]] | None = None,
):
    contexts = []
    additional_model_attempts = 0

    def model(messages, tools, **kwargs):
        nonlocal additional_model_attempts
        assert tools == [] and kwargs["tool_choice"] == "none"
        if captured_model_kwargs is not None:
            captured_model_kwargs.append(kwargs)
        context = next(
            json.loads(message["content"])
            for message in reversed(messages)
            if message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith('{"stage":')
        )
        contexts.append(context)
        if context["stage"] == "additional_mcp_check":
            additional_model_attempts += 1
            if additional_model_attempts <= malformed_additional_attempts:
                response = {"content": "not JSON"}
                if model_usage_tokens is not None:
                    response["usage"] = {"total_tokens": model_usage_tokens}
                return response
        if context["stage"] == "plan":
            value = {
                "candidates": [candidate],
                "hypotheses": [
                    {
                        "hypothesis_id": "H1",
                        "service": "front-end",
                        "category": "network-latency",
                        "mechanism": "A chamada do cliente excede a latencia basal da aresta",
                        "supporting_evidence": [],
                        "contradicting_evidence": [],
                        "unresolved_questions": [
                            "A operacao tem amostras comparaveis?"
                        ],
                    },
                    {
                        "hypothesis_id": "H2",
                        "service": "orders",
                        "category": "http-error",
                        "mechanism": "A dependencia retorna erro HTTP na janela fault",
                        "supporting_evidence": [],
                        "contradicting_evidence": [],
                        "unresolved_questions": ["Ha log ou span do mesmo erro?"],
                    },
                ],
                "reasoning": "Comparar sinais",
            }
        elif context["stage"] == "additional_mcp_check":
            check_context = context["context"]
            if check_context["round"] <= additional_checks:
                selected = check_context["executable_checks"][0]
                value = {
                    "action": "query",
                    "reason": "Verificar a hipótese com sinal atual",
                    "check": {
                        "kind": selected["kind"],
                        "service": selected["service"],
                        "phase": selected["phase"],
                        "hypothesis": "Distinguir a hipótese principal",
                        "expected_support": "Sinal compatível na janela consultada",
                        "expected_refutation": "Sinal ausente ou comportamento basal",
                        "patterns": ["timeout"] if selected["kind"] == "logs" else [],
                    },
                }
            else:
                value = {
                    "action": "stop",
                    "reason": "Nenhum check adicional necessário",
                    "check": None,
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
        response = {"content": json.dumps(value)}
        if model_usage_tokens is not None:
            response["usage"] = {"total_tokens": model_usage_tokens}
        return response

    result = rca_skill.answer_skill_rca(
        mcp_client=client,
        phases=PHASES,
        namespace="sock-shop",
        max_additional_checks=max_additional_checks,
        max_model_calls=max_model_calls,
        max_total_tokens=max_total_tokens,
        timeout_seconds=timeout_seconds,
        llm=model,
        window_metadata=metadata,
        possible_fault_types=possible_fault_types,
        require_best_guess=require_best_guess,
        archive_traces=archive_traces,
        evidence_source=evidence_source,
        model=model_id,
        temperature=temperature,
        seed=seed,
    )
    return result, contexts


def test_skill_model_budget_records_measured_usage_and_limits():
    result, _ = run_executor(
        FakeMCP(),
        max_model_calls=30,
        max_total_tokens=300_000,
        timeout_seconds=600,
        model_usage_tokens=15,
    )

    assert result["status"] == "confirmed"
    assert result["model_calls"] == result["budget"]["model_calls_used"] == 2
    assert result["total_tokens"] == result["budget"]["total_tokens_used"] == 30
    assert result["budget"]["max_model_calls"] == 30
    assert result["budget"]["max_total_tokens"] == 300_000
    assert result["budget"]["timeout_seconds"] == 600
    assert result["budget"]["max_additional_checks"] == 0
    assert result["budget"]["additional_checks_used"] == 0
    assert result["budget"]["measured_token_usage_calls"] == 2
    assert result["budget"]["estimated_token_usage_calls"] == 0
    assert result["model_budget_rejections"] == []
    assert all(
        attempt["estimated_input_tokens"] > 0
        and attempt["reserved_output_tokens"] > 0
        for attempt in result["model_attempts"]
    )


def test_skill_model_token_budget_is_enforced():
    result, _ = run_executor(
        FakeMCP(),
        max_model_calls=30,
        max_total_tokens=8_000,
        timeout_seconds=600,
    )

    assert result["status"] == "error"
    assert "token budget exhausted" in result["error"]
    assert result["total_tokens"] <= 8_000
    assert result["budget"]["total_tokens_used"] <= 8_000
    rejection = result["model_budget_rejections"][0]
    assert rejection["reason"] == "token_budget"
    assert rejection["estimated_total_tokens"] > rejection["token_limit"]


def test_skill_model_call_budget_includes_retries():
    result, contexts = run_executor(
        FakeMCP(),
        max_additional_checks=4,
        additional_checks=4,
        max_model_calls=6,
        max_total_tokens=300_000,
        timeout_seconds=600,
        malformed_additional_attempts=1,
    )

    assert result["status"] == "error"
    assert "model call budget exhausted" in result["error"]
    assert result["model_calls"] == result["budget"]["model_calls_used"] == 6
    assert len(contexts) == 6
    assert result["budget"]["estimated_token_usage_calls"] == 6


def test_model_generation_settings_are_forwarded_to_skill_calls():
    captured_model_kwargs = []

    run_executor(
        FakeMCP(),
        model_id="qwen3.5-9b-grafana",
        temperature=0.1,
        seed=20261107,
        captured_model_kwargs=captured_model_kwargs,
    )

    assert captured_model_kwargs
    assert all(
        kwargs["model"] == "qwen3.5-9b-grafana"
        and kwargs["temperature"] == 0.1
        and kwargs["seed"] == 20261107
        for kwargs in captured_model_kwargs
    )


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
    assert 'container=~"front-end|istio-proxy"' in logs[1]["query"]
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


def test_skill_additional_mcp_check_budget_is_capped_and_recorded():
    client = FakeMCP()
    result, contexts = run_executor(
        client,
        max_additional_checks=rca_skill.MAX_ADDITIONAL_MCP_CHECKS,
        additional_checks=rca_skill.MAX_ADDITIONAL_MCP_CHECKS + 2,
    )

    followup_calls = [call for call in result["calls"] if call["level"] == 5]
    assert len(followup_calls) == rca_skill.MAX_ADDITIONAL_MCP_CHECKS
    assert len(result["additional_checks"]) == rca_skill.MAX_ADDITIONAL_MCP_CHECKS
    assert all(
        check["evidence_id"] in {item["id"] for item in result["evidence"]}
        for check in result["additional_checks"]
    )
    assert result["additional_check_stop_reason"] == "max_additional_mcp_checks"
    assert [context["stage"] for context in contexts].count(
        "additional_mcp_check"
    ) == rca_skill.MAX_ADDITIONAL_MCP_CHECKS
    assert (
        len(
            [
                call
                for call in client.calls
                if call[0] in {"get_cpu_saturation_top", "get_memory_saturation_top"}
            ]
        )
        == rca_skill.MAX_ADDITIONAL_MCP_CHECKS
    )


def test_skill_additional_mcp_check_budget_can_stop_early():
    client = FakeMCP()
    result, contexts = run_executor(
        client,
        max_additional_checks=rca_skill.MAX_ADDITIONAL_MCP_CHECKS,
        additional_checks=0,
    )

    assert result["additional_checks"] == []
    assert not any(call["level"] == 5 for call in result["calls"])
    assert result["additional_check_stop_reason"] == "Nenhum check adicional necessário"
    assert [context["stage"] for context in contexts].count("additional_mcp_check") == 1


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
        call for call in result["calls"] if call["tool"] == "tempo_service_graph_phases"
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
        item["tool"] == "history_archive_traces" for item in contexts[-1]["evidence"]
    )
    assert (
        result["evidence_bundle"]["coverage"]["history_archive_traces"]["source"]
        == "archive"
    )
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
    assert len(edge_calls) == 4
    assert any("response_flags" in arguments["query"] for arguments in edge_calls)
    assert all(arguments["step"] == "15s" for arguments in edge_calls)
    assert all(arguments["start"] == "2026-10-02T10:01:15Z" for arguments in edge_calls)
    assert all(arguments["end"] == PHASES["post"]["end"] for arguments in edge_calls)
    assert contexts[0]["context"]["edge_latency_candidates"] == ["orders"]
    assert contexts[1]["context"]["edge_latency_candidates"] == ["orders"]
    assert any(
        'container=~"orders|istio-proxy"' in arguments["query"]
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
    http_evidence = next(
        item for item in result["evidence"] if item.get("kind") == "http"
    )
    assert http_evidence["source_provenance"] == "prometheus_istio"


def test_edge_latency_directs_fault_trace_search_to_client_and_destination():
    class DirectedTraceMCP(FakeMCP):
        def __init__(self, *, validated_latency_candidate=True):
            super().__init__()
            self.validated_latency_candidate = validated_latency_candidate

        def call_tool_json(self, name, arguments):
            if name == "tempo_service_graph_phases":
                self.calls.append((name, arguments))
                return {
                    "phases": {
                        "fault": {
                            "trace_count": 1,
                            "failure_signatures": [],
                            "latency_candidates": (
                                [
                                    {
                                        "trace_id": "a" * 32,
                                        "signature": "LATENCY_REGRESSION",
                                        "affected_service": "orders",
                                        "endpoint": "GET /orders",
                                        "operations": ["front-end.GET /orders"],
                                        "fault_duration_ms": 180,
                                        "baseline_p95_ms": 100,
                                        "latency_delta_ms": 80,
                                        "baseline_sample_count": 4,
                                    }
                                ]
                                if self.validated_latency_candidate
                                else []
                            ),
                        },
                    },
                }
            if name == "tempo_search_traces":
                self.calls.append((name, arguments))
                return {
                    "traces": [
                        {
                            "traceID": "a" * 32,
                            "rootTraceName": "GET /orders",
                            "startTimeUnixNano": "1790935330000000000",
                        }
                    ]
                }
            if name == "tempo_analyze_trace":
                self.calls.append((name, arguments))
                return {
                    "status": "ok",
                    "trace_duration_ms": 180,
                    "span_count": 2,
                    "critical_path": [],
                    "hot_spans": [],
                    "error_spans": [],
                    "failure_signatures": [],
                    "dependency_map": [],
                    "service_latency": [],
                }
            if name != "prometheus_range_query":
                return super().call_tool_json(name, arguments)
            self.calls.append((name, arguments))
            query = arguments["query"]
            if "last_terminated" in query:
                return {"status": "success", "data": {"result": []}}
            baseline_timestamp = int(
                datetime.fromisoformat(
                    "2026-10-02T10:01:25Z".replace("Z", "+00:00")
                ).timestamp()
            )
            fault_timestamp = int(
                datetime.fromisoformat(
                    "2026-10-02T10:02:10Z".replace("Z", "+00:00")
                ).timestamp()
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

    client = DirectedTraceMCP()
    result, _ = run_executor(client)

    search_call = next(
        (
            arguments
            for name, arguments in client.calls
            if name == "tempo_search_traces"
        ),
        None,
    )
    assert search_call is not None
    assert search_call["start"] == PHASES["fault"]["start"]
    assert search_call["end"] == PHASES["fault"]["end"]
    assert search_call["limit"] == 5
    assert 'resource.service.name = "front-end"' in search_call["query"]
    assert "kind = client" in search_call["query"]
    assert "http://orders" in search_call["query"]
    search_evidence = next(
        item for item in result["evidence"] if item["tool"] == "tempo_search_traces"
    )
    assert search_evidence["is_causal_evidence"] is False
    trace_detail = next(
        call for call in result["calls"] if call["tool"] == "tempo_analyze_trace"
    )
    assert trace_detail["phase"] == "fault"
    assert trace_detail["service"] == "front-end"
    assert trace_detail["result"]["source_service"] == "front-end"
    assert trace_detail["result"]["destination_service"] == "orders"
    assert {
        profile["metric"]: profile["unit"]
        for profile in trace_detail["result"]["metric_profiles"]
    } == {
        "edge_latency_istio_mean_ms": "ms",
        "edge_latency_service_graph_client_p90_s": "s",
        "edge_latency_service_graph_server_p90_s": "s",
    }
    assert trace_detail["result"]["baseline_p95_ms"] == 100

    unvalidated_client = DirectedTraceMCP(validated_latency_candidate=False)
    unvalidated_result, _ = run_executor(unvalidated_client)
    assert not any(
        name == "tempo_analyze_trace" for name, _ in unvalidated_client.calls
    )
    assert any(
        "nao foram aprofundados" in item
        for item in unvalidated_result["diagnosis"]["limitations"]
    )


def test_plan_hypotheses_are_structured_and_constrained_to_the_system_card():
    _, contexts = run_executor(FakeMCP())
    plan_context = contexts[0]
    hypothesis_schema = plan_context["output_schema"]["$defs"]["CausalHypothesis"]
    card = rca_skill.load_system_card("sock-shop")
    assert hypothesis_schema["properties"]["category"]["enum"] == sorted(
        card["allowed_fault_categories"]
    )
    service_schema = hypothesis_schema["properties"]["service"]["anyOf"]
    assert next(item for item in service_schema if item.get("type") == "string")["enum"]
    assert "IDs exatos" in plan_context["context"]["hypothesis_contract"]


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
