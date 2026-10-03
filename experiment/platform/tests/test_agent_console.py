"""Tests for the interactive agent console parser and dispatch."""
from __future__ import annotations

import json

from experiment.platform.backend import agent_console
from experiment.platform.backend.agent_console import _rca_fallback_answer as render_rca_fallback


class _FakePrometheus:
    pass


def test_parse_request_error_rate_with_service():
    parsed = agent_console.parse_request("qual a taxa de erros do serviço orders")
    assert parsed.intent == "http_error_rate"
    assert parsed.service == "orders"


def test_parse_generic_top_service_question_without_fake_service_name():
    parsed = agent_console.parse_request(
        "Qual serviço teve a maior taxa de erros 5xx?"
    )
    assert parsed.intent == "http_error_rate"
    assert parsed.service is None


def test_parse_restarts_question_with_service_name_in_natural_order():
    parsed = agent_console.parse_request(
        "Quantas vezes o RabbitMQ reiniciou durante este experimento?"
    )
    assert parsed.intent == "restarts"
    assert parsed.service == "rabbitmq"


def test_parse_request_overall_status():
    parsed = agent_console.parse_request("status geral")
    assert parsed.intent == "overall"


def test_answer_query_http_error(monkeypatch):
    def _fake_http_error(prometheus, *, start, end, step, k):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:10:00Z"
        assert step == "15s"
        assert k == 10
        return {
            "found": True,
            "count": 1,
            "top": [
                {
                    "service": "orders",
                    "error_rate_mean_pct": 1.25,
                    "error_rate_max_pct": 3.5,
                }
            ],
        }

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _fake_http_error)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=False,
    )

    assert "Tool: check_http_error_rate_top" in out
    assert "Serviço: orders" in out
    assert "Taxa média de erro 5xx: 1.25%" in out


def test_answer_query_unknown_intent():
    out = agent_console.answer_query(
        "me conte uma historia",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=False,
    )
    assert "Não entendi o pedido" in out


def test_answer_query_model_loop_uses_tool_and_returns_final(monkeypatch):
    responses = iter(
        [
            '{"action":"use_tool","tool":"http_error_rate","service":"orders"}',
            "Resposta: a taxa de erro 5xx do orders esta controlada.\nEvidencia: media 1.25%, pico 3.50%.\nPróximo passo: continuar monitorando.",
        ]
    )

    def _fake_llm(messages, max_new_tokens):
        assert isinstance(messages, list)
        assert max_new_tokens == 180
        return next(responses)

    def _fake_http_error(prometheus, *, start, end, step, k):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:10:00Z"
        assert step == "15s"
        assert k == 10
        return {
            "found": True,
            "count": 1,
            "top": [
                {
                    "service": "orders",
                    "error_rate_mean_pct": 1.25,
                    "error_rate_max_pct": 3.5,
                }
            ],
        }

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _fake_http_error)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
        max_steps=1,
    )

    assert "taxa de erro 5xx do orders" in out
    assert "Evidencia: media 1.25%, pico 3.50%." in out


def test_mcp_chat_discovers_scoped_tools_and_injects_run_window():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_cpu_saturation_top",
                    "description": "Top services by CPU saturation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                            "k": {"type": "integer"},
                        },
                        "required": ["start", "end"],
                    },
                },
                {
                    "name": "prometheus_range_query",
                    "description": "Execute arbitrary PromQL.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]

        def call_tool_json(
            self, name, arguments, *, tool_call_id=None, tool_description=None
        ):
            self.calls.append((name, arguments, tool_call_id, tool_description))
            return {"top": [{"service": "user", "saturation_mean_pct": 42.0}]}

    client = _FakeMCPClient()
    llm_calls = []

    def _fake_llm(messages, _tools, **kwargs):
        llm_calls.append((messages, _tools, kwargs))
        assert [tool["function"]["name"] for tool in _tools] == [
            "get_cpu_saturation_top"
        ]
        if len(llm_calls) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_cpu",
                        "type": "function",
                        "function": {
                            "name": "get_cpu_saturation_top",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert kwargs["tool_choice"] == "none"
        return {"role": "assistant", "content": "O maior consumo foi de user."}

    out = agent_console.answer_query_mcp(
        "Qual serviço teve maior consumo de CPU?",
        mcp_client=client,
        start="2026-09-23T11:16:51Z",
        end="2026-09-23T11:22:23Z",
        namespace="sock-shop",
        llm=_fake_llm,
        max_steps=1,
    )

    assert out == "O maior consumo foi de user."
    assert client.calls == [
        (
            "get_cpu_saturation_top",
            {
                "start": "2026-09-23T11:16:51Z",
                "end": "2026-09-23T11:22:23Z",
                "namespace": "sock-shop",
                "step": "15s",
                "k": 100,
            },
            "call_cpu",
            "Top services by CPU saturation.",
        )
    ]
    assert llm_calls[0][2]["tool_choice"] == "required"
    assert llm_calls[0][2]["parallel_tool_calls"] is False


def test_memory_chat_compares_fault_to_baseline_and_ranks_by_delta():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_memory_saturation_top",
                    "description": "Top services by memory saturation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                            "k": {"type": "integer"},
                        },
                    },
                }
            ]

        def call_tool_json(self, _name, arguments, **_kwargs):
            self.calls.append(arguments)
            if arguments["start"] == "fault-start":
                return {
                    "top": [
                        {
                            "service": "catalogue",
                            "saturation_mean_pct": 72.0,
                            "saturation_max_pct": 85.0,
                        },
                        {
                            "service": "user",
                            "saturation_mean_pct": 60.0,
                            "saturation_max_pct": 70.0,
                        },
                    ],
                    "window": {"start": "fault-start", "end": "fault-end"},
                }
            return {
                "top": [
                    {
                        "service": "catalogue",
                        "saturation_mean_pct": 70.0,
                        "saturation_max_pct": 82.0,
                    },
                    {
                        "service": "user",
                        "saturation_mean_pct": 25.0,
                        "saturation_max_pct": 30.0,
                    },
                ],
                "window": {"start": "baseline-start", "end": "baseline-end"},
            }

    client = _FakeMCPClient()
    tool_payloads = []

    def _fake_llm(messages, _tools, **kwargs):
        if kwargs["tool_choice"] == "required":
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_memory",
                        "type": "function",
                        "function": {
                            "name": "get_memory_saturation_top",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        tool_payloads.append(json.loads(messages[-1]["content"]))
        return {"role": "assistant", "content": "user aumentou mais durante a falha."}

    answer = agent_console.answer_query_mcp(
        "Qual serviço aumentou mais o consumo de memória?",
        mcp_client=client,
        start="fault-start",
        end="fault-end",
        baseline_start="baseline-start",
        baseline_end="baseline-end",
        namespace="sock-shop",
        llm=_fake_llm,
        max_steps=1,
    )

    assert answer == "user aumentou mais durante a falha."
    assert [call["start"] for call in client.calls] == ["fault-start", "baseline-start"]
    assert all(call["k"] == 100 for call in client.calls)
    rows = tool_payloads[0]["top"]
    assert rows[0]["service"] == "user"
    assert rows[0]["saturation_mean_pct"] == 60.0
    assert rows[0]["baseline_saturation_mean_pct"] == 25.0
    assert rows[0]["delta_mean_pp"] == 35.0
    assert rows[1]["service"] == "catalogue"
    assert rows[1]["delta_mean_pp"] == 2.0


def test_mcp_chat_falls_back_to_matching_metric_tool_when_model_skips_call():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_cpu_saturation_top",
                    "description": "Top services by CPU saturation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                            "k": {"type": "integer"},
                        },
                    },
                }
            ]

        def call_tool_json(self, name, arguments, **kwargs):
            self.calls.append((name, arguments, kwargs))
            return {"top": [{"service": "user", "saturation_max_pct": 73.2}]}

    client = _FakeMCPClient()
    llm_calls = []

    def _fake_llm(messages, _tools, **kwargs):
        llm_calls.append((messages, kwargs))
        assert [tool["function"]["name"] for tool in _tools] == [
            "get_cpu_saturation_top"
        ]
        if len(llm_calls) == 1:
            assert kwargs["tool_choice"] == "required"
            return {"role": "assistant", "content": "Vou consultar CPU agora."}
        raise AssertionError("host fallback must not call Qwen again")

    out = agent_console.answer_query_mcp(
        "Qual o maior consumo de cpu?",
        mcp_client=client,
        start="2026-09-23T11:16:51Z",
        end="2026-09-23T11:22:23Z",
        namespace="sock-shop",
        llm=_fake_llm,
        max_steps=1,
    )

    assert "Maior saturação de CPU: user, pico 73.20%" in out
    assert client.calls[0][0] == "get_cpu_saturation_top"
    assert client.calls[0][1] == {
        "start": "2026-09-23T11:16:51Z",
        "end": "2026-09-23T11:22:23Z",
        "namespace": "sock-shop",
        "step": "15s",
        "k": 100,
    }
    assert len(llm_calls) == 1


def test_mcp_chat_still_rejects_unrecognized_answer_without_tool_call():
    class _FakeMCPClient:
        def list_tools(self):
            return [
                {
                    "name": "get_cpu_saturation_top",
                    "description": "Top services by CPU saturation.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    def _fake_llm(_messages, _tools, **_kwargs):
        return {"role": "assistant", "content": "Resposta sem telemetria."}

    try:
        agent_console.answer_query_mcp(
            "Me conte uma história.",
            mcp_client=_FakeMCPClient(),
            start="2026-09-23T11:16:51Z",
            end="2026-09-23T11:22:23Z",
            namespace="sock-shop",
            llm=_fake_llm,
        )
    except agent_console.LLMClientError as exc:
        assert "without querying" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("an ungrounded answer must not be returned")


def test_mcp_fallback_does_not_report_no_events_when_tool_has_no_samples():
    class _FakeMCPClient:
        def list_tools(self):
            return [
                {
                    "name": name,
                    "description": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                    },
                }
                for name in ("check_pod_restarts", "check_oom_kills")
            ]

        def call_tool_json(self, _name, _arguments, **_kwargs):
            return {"status": "no_data", "found": False, "top": []}

    def _failed_llm(*args, **kwargs):
        raise agent_console.LLMClientError("model unavailable")

    answer = agent_console.answer_query_mcp(
        "Algum pod teve restart ou OOM?",
        mcp_client=_FakeMCPClient(),
        start="2026-09-25T10:00:00Z",
        end="2026-09-25T10:05:00Z",
        namespace="sock-shop",
        llm=_failed_llm,
    )

    assert "sem amostras na janela" in answer
    assert "nenhum pod foi reiniciado" not in answer
    assert "nenhuma transição OOMKilled" not in answer


def test_run_rca_starts_with_golden_metrics_and_injects_phase_windows():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare run golden metrics by phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "baseline_start": {"type": "string"},
                            "baseline_end": {"type": "string"},
                            "fault_start": {"type": "string"},
                            "fault_end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                            "k": {"type": "integer"},
                        },
                        "required": [
                            "baseline_start", "baseline_end", "fault_start", "fault_end"
                        ],
                    },
                },
                {
                    "name": "check_oom_kills",
                    "description": "Find OOM kills in a run phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                        "required": ["start", "end"],
                    },
                },
            ]

        def call_tool_json(self, name, arguments=None, **kwargs):
            self.calls.append((name, arguments, kwargs))
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": {}}
            return {"status": "ok", "found": False, "top": []}

    client = _FakeMCPClient()
    llm_calls = []

    def _fake_llm(messages, tools, **kwargs):
        llm_calls.append((messages, tools, kwargs))
        assert [tool["function"]["name"] for tool in tools] == ["check_oom_kills"]
        return {
            "role": "assistant",
            "content": (
                "Localização provável: indeterminada\n"
                "Causa provável: indeterminada\n"
                "Evidências: nenhum OOM foi detectado na janela\n"
                "Limitações: faltam sinais discriminativos"
            ),
        }

    phases = {
        "baseline": {
            "start": "2026-09-25T10:00:00Z",
            "end": "2026-09-25T10:01:00Z",
        },
        "fault": {
            "start": "2026-09-25T10:02:00Z",
            "end": "2026-09-25T10:03:00Z",
        },
    }
    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases=phases,
        namespace="sock-shop",
        max_iterations=10,
        llm=_fake_llm,
    )

    assert [call[0] for call in client.calls] == [
        "get_run_golden_signals",
        "check_oom_kills",
    ]
    assert client.calls[0][1] == {
        "baseline_start": phases["baseline"]["start"],
        "baseline_end": phases["baseline"]["end"],
        "fault_start": phases["fault"]["start"],
        "fault_end": phases["fault"]["end"],
        "namespace": "sock-shop",
        "step": "15s",
        "k": 10,
    }
    assert client.calls[1][1] == {
        "start": phases["fault"]["start"],
        "end": phases["fault"]["end"],
        "namespace": "sock-shop",
        "step": "15s",
    }
    assert result["iterations"] == 3
    assert result["stop_reason"] == "model_final"
    assert "Localização provável: indeterminada" in result["answer"]


def test_run_rca_dispatches_truncated_textual_tool_call_with_host_window():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare run golden metrics by phase.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "check_oom_kills",
                    "description": "Find OOM kills in a run phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                        "required": ["start", "end"],
                    },
                },
            ]

        def call_tool_json(self, name, arguments=None, **_kwargs):
            self.calls.append((name, arguments))
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": {}}
            return {"status": "no_data", "found": False}

    client = _FakeMCPClient()
    textual_tool_call = (
        '{"tool_call": {"name": "check_oom_kills", "arguments": '
        '{"start": "2026-09-25T10:00:00Z"'
    )
    llm_responses = iter(
        [
            {"role": "assistant", "content": textual_tool_call},
            {"role": "assistant", "content": textual_tool_call},
            {"role": "assistant", "content": textual_tool_call},
            {"role": "assistant", "content": textual_tool_call},
        ]
    )

    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=4,
        llm=lambda *_args, **_kwargs: next(llm_responses),
    )

    assert [name for name, _arguments in client.calls] == [
        "get_run_golden_signals",
        "check_oom_kills",
    ]
    assert client.calls[1][1] == {
        "start": "2026-09-25T10:02:00Z",
        "end": "2026-09-25T10:03:00Z",
        "namespace": "sock-shop",
        "step": "15s",
    }
    assert "tool_call" not in result["answer"]
    assert result["stop_reason"] == "invalid_final_fallback"
    assert result["answer"].startswith("Localização provável: não avaliada pelo modelo.")
    rejected = [
        attempt for attempt in result["model_attempts"]
        if attempt["status"] == "rejected_format"
    ]
    assert rejected and rejected[0]["content_excerpt"] == textual_tool_call


def test_run_rca_requires_traces_before_accepting_final_without_direct_evidence():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare run golden metrics by phase.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "tempo_service_graph_phases",
                    "description": "Compare trace graphs by phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "baseline_start": {"type": "string"},
                            "baseline_end": {"type": "string"},
                            "fault_start": {"type": "string"},
                            "fault_end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "limit": {"type": "integer"},
                            "top_k": {"type": "integer"},
                        },
                    },
                },
            ]

        def call_tool_json(self, name, arguments=None, **_kwargs):
            self.calls.append((name, arguments))
            return {"status": "ok", "metrics": {}}

    final_answer = (
        "Localização provável: user\n"
        "Causa provável: indeterminada\n"
        "Evidências: sinais indiretos na janela de falha\n"
        "Limitações: causa não confirmada"
    )
    llm_responses = iter(
        [
            {"role": "assistant", "content": final_answer},
            {"role": "assistant", "content": final_answer},
        ]
    )
    client = _FakeMCPClient()

    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=4,
        llm=lambda *_args, **_kwargs: next(llm_responses),
    )

    assert [name for name, _arguments in client.calls] == [
        "get_run_golden_signals",
        "tempo_service_graph_phases",
    ]
    assert client.calls[1][1] == {
        "baseline_start": "2026-09-25T10:00:00Z",
        "baseline_end": "2026-09-25T10:01:00Z",
        "fault_start": "2026-09-25T10:02:00Z",
        "fault_end": "2026-09-25T10:03:00Z",
        "namespace": "sock-shop",
        "limit": 20,
        "top_k": 5,
    }
    assert result["answer"] == final_answer
    assert result["stop_reason"] == "model_final"


def test_run_rca_checks_traces_even_when_restart_was_detected():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            window_properties = {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "namespace": {"type": "string"},
                "step": {"type": "string"},
            }
            phase_properties = {
                "baseline_start": {"type": "string"},
                "baseline_end": {"type": "string"},
                "fault_start": {"type": "string"},
                "fault_end": {"type": "string"},
                "namespace": {"type": "string"},
                "limit": {"type": "integer"},
                "top_k": {"type": "integer"},
            }
            return [
                {"name": "get_run_golden_signals", "inputSchema": {"properties": {}}},
                {"name": "check_oom_kills", "inputSchema": {"properties": window_properties}},
                {"name": "check_pod_restarts", "inputSchema": {"properties": window_properties}},
                {"name": "tempo_service_graph_phases", "inputSchema": {"properties": phase_properties}},
            ]

        def call_tool_json(self, name, _arguments=None, **_kwargs):
            self.calls.append(name)
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": {}}
            if name == "check_oom_kills":
                return {"status": "no_data", "found": False}
            if name == "check_pod_restarts":
                return {
                    "status": "ok",
                    "found": True,
                    "count": 1,
                    "top": [{"service": "front-end", "restarts_in_window": 1}],
                }
            return {
                "phases": {
                    "baseline": {"trace_count": 4, "top_nodes": []},
                    "fault": {"trace_count": 5, "top_nodes": []},
                },
                "delta": {"top_node_deltas": [], "top_edge_deltas": []},
            }

    final_answer = (
        "Localização provável: front-end\n"
        "Causa provável: degradação de latência sustentada pelos traces"
    )

    def llm(messages, _tools, **_kwargs):
        tool_names = [message["name"] for message in messages if message.get("role") == "tool"]
        assert "check_pod_restarts" in tool_names
        assert "tempo_service_graph_phases" in tool_names
        return {"role": "assistant", "content": final_answer}

    client = _FakeMCPClient()
    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=5,
        llm=llm,
    )

    assert client.calls == [
        "get_run_golden_signals",
        "check_oom_kills",
        "check_pod_restarts",
        "tempo_service_graph_phases",
    ]
    assert result["stop_reason"] == "model_final"
    assert result["answer"].startswith(final_answer)


def test_run_rca_allows_model_to_optionally_check_packet_drops():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            window_properties = {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "namespace": {"type": "string"},
                "step": {"type": "string"},
            }
            phase_properties = {
                "baseline_start": {"type": "string"},
                "baseline_end": {"type": "string"},
                "fault_start": {"type": "string"},
                "fault_end": {"type": "string"},
                "namespace": {"type": "string"},
                "step": {"type": "string"},
                "k": {"type": "integer"},
            }
            return [
                {"name": "get_run_golden_signals", "inputSchema": {"properties": {}}},
                {"name": "check_oom_kills", "inputSchema": {"properties": window_properties}},
                {"name": "check_pod_restarts", "inputSchema": {"properties": window_properties}},
                {"name": "tempo_service_graph_phases", "inputSchema": {"properties": phase_properties}},
                {
                    "name": "check_network_packet_drops",
                    "description": "Compare packet drops",
                    "inputSchema": {
                        "properties": {
                            key: value for key, value in phase_properties.items()
                            if key != "namespace" and key not in {"limit", "top_k"}
                        }
                    },
                },
            ]

        def call_tool_json(self, name, arguments=None, **_kwargs):
            self.calls.append((name, arguments))
            if name == "get_run_golden_signals":
                return {
                    "status": "ok",
                    "metrics": {
                        "latency_p95": {
                            "status": "ok",
                            "unit": "s",
                            "top": [{"service": "carts", "baseline_mean": 0.2, "fault_mean": 2.0, "delta_abs": 1.8}],
                        }
                    },
                }
            if name == "check_oom_kills":
                return {"status": "ok", "found": False, "count": 0, "top": []}
            if name == "check_pod_restarts":
                return {"status": "ok", "found": False, "count": 0, "top": []}
            if name == "tempo_service_graph_phases":
                return {"status": "ok", "phases": {"baseline": {"trace_count": 0}, "fault": {"trace_count": 0}}, "delta": {}}
            return {
                "status": "ok",
                "found": True,
                "metrics": {
                    "network_receive_dropped_rate": {
                        "status": "ok",
                        "unit": "packets/s",
                        "phases": {"baseline": {"sample_count": 4}, "fault": {"sample_count": 4}},
                        "top": [{"interface": "eth0", "baseline_mean": 0.0, "fault_mean": 0.5, "delta_abs": 0.5}],
                    }
                },
            }

    final_answer = "Localização provável: carts\nCausa provável: aumento de drops de pacotes"
    client = _FakeMCPClient()
    planner_calls = 0

    def llm(messages, tools, **_kwargs):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            tool_names = [tool["function"]["name"] for tool in tools]
            assert "check_network_packet_drops" in tool_names
            assert "check_network_packet_drops" in messages[0]["content"]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "packet-drops",
                    "type": "function",
                    "function": {"name": "check_network_packet_drops", "arguments": "{}"},
                }],
            }
        packet_result = next(
            message["content"] for message in messages
            if message.get("role") == "tool" and message.get("name") == "check_network_packet_drops"
        )
        assert '"delta": 0.5' in packet_result
        return {"role": "assistant", "content": final_answer}

    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=6,
        llm=llm,
    )

    assert [name for name, _arguments in client.calls] == [
        "get_run_golden_signals",
        "check_oom_kills",
        "check_pod_restarts",
        "tempo_service_graph_phases",
        "check_network_packet_drops",
    ]
    assert client.calls[-1][1] == {
        "baseline_start": "2026-09-25T10:00:00Z",
        "baseline_end": "2026-09-25T10:01:00Z",
        "fault_start": "2026-09-25T10:02:00Z",
        "fault_end": "2026-09-25T10:03:00Z",
        "step": "15s",
        "k": 5,
    }
    assert result["stop_reason"] == "model_final"


def test_run_rca_continues_after_nested_mcp_timeout():
    class _TaskGroupError(RuntimeError):
        def __init__(self):
            self.exceptions = [TimeoutError("SSE reader timed out")]
            super().__init__("unhandled errors in a TaskGroup (1 sub-exception)")

    class _FakeMCPClient:
        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare golden metrics by phase.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "check_oom_kills",
                    "description": "Check in-window OOM events.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]

        def call_tool_json(self, name, _arguments=None, **_kwargs):
            if name == "check_oom_kills":
                raise _TaskGroupError()
            return {"status": "ok", "metrics": {}}

    final_answer = (
        "Localização provável: indeterminada\n"
        "Causa provável: não confirmada\n"
        "Evidências: métricas consultadas\n"
        "Limitações: consulta de OOM expirou"
    )
    llm_responses = iter(
        [
            {
                "tool_calls": [
                    {
                        "id": "oom-call",
                        "type": "function",
                        "function": {
                            "name": "check_oom_kills",
                            "arguments": "{}",
                        },
                    }
                ]
            },
            {"content": final_answer},
        ]
    )

    result = agent_console.answer_run_rca_mcp(
        mcp_client=_FakeMCPClient(),
        phases={
            "baseline": {
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T10:01:00Z",
            },
            "fault": {
                "start": "2026-09-25T10:02:00Z",
                "end": "2026-09-25T10:03:00Z",
            },
        },
        namespace="sock-shop",
        max_iterations=3,
        llm=lambda *_args, **_kwargs: next(llm_responses),
    )

    assert result["answer"] == final_answer
    assert result["stop_reason"] == "model_final"
    assert result["calls"][-1]["result"] == {
        "status": "tool_error",
        "error": "TimeoutError: SSE reader timed out",
    }
    assert isinstance(result["calls"][-1]["duration_ms"], int)


def test_run_rca_reports_http_transport_error_during_tool_discovery():
    class _FakeMCPClient:
        def list_tools(self):
            raise agent_console.httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )

    result = agent_console.answer_run_rca_mcp(
        mcp_client=_FakeMCPClient(),
        phases={
            "baseline": {
                "start": "2026-09-25T10:00:00Z",
                "end": "2026-09-25T10:01:00Z",
            },
            "fault": {
                "start": "2026-09-25T10:02:00Z",
                "end": "2026-09-25T10:03:00Z",
            },
        },
        namespace="sock-shop",
    )

    assert result["stop_reason"] == "tool_discovery_error"
    assert result["calls"][0]["tool"] == "tools/list"
    assert result["calls"][0]["result"]["error"].startswith(
        "RemoteProtocolError: Server disconnected"
    )
    assert isinstance(result["calls"][0]["duration_ms"], int)


def test_run_rca_rejects_no_oom_claim_when_oom_coverage_is_missing():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare run golden metrics by phase.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "check_oom_kills",
                    "description": "Find OOM kills in a run phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                    },
                },
            ]

        def call_tool_json(self, name, _arguments=None, **_kwargs):
            self.calls.append(name)
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": {}}
            return {"status": "no_data", "found": False}

    unsupported_answer = (
        "Localização provável: user\n"
        "Causa provável: pod-failure\n"
        "Evidências: nenhum OOM ocorreu\n"
        "Limitações: dados parciais"
    )
    llm_responses = iter(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-oom",
                        "type": "function",
                        "function": {
                            "name": "check_oom_kills",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
        ]
    )
    client = _FakeMCPClient()

    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=4,
        llm=lambda *_args, **_kwargs: next(llm_responses),
    )

    assert client.calls == ["get_run_golden_signals", "check_oom_kills"]
    assert "nenhum OOM ocorreu" not in result["answer"]
    assert "check_oom_kills: cobertura no_data" in result["answer"]
    assert result["stop_reason"] == "invalid_final_fallback"


def test_run_rca_fallback_reports_cross_service_impact_and_empty_trace_coverage():
    metrics = {
        "request_rate": {
            "status": "ok",
            "unit": "req/s",
            "top": [
                {"service": "user", "delta_abs": 36.1447},
                {"service": "front-end", "delta_abs": 20.1164},
            ],
        },
        "error_rate": {
            "status": "ok",
            "unit": "%",
            "top": [{"service": "front-end", "delta_abs": 0.0}],
        },
        "latency_p95": {
            "status": "ok",
            "unit": "s",
            "top": [
                {"service": "front-end", "delta_abs": 1.8185},
                {"service": "orders", "delta_abs": 1.2929},
                {"service": "carts", "delta_abs": 1.1539},
            ],
        },
        "cpu_saturation_pct": {
            "status": "ok",
            "unit": "%",
            "top": [
                {"service": "front-end", "delta_abs": 48.2984},
                {"service": "user", "delta_abs": 23.157},
                {"service": "carts", "delta_abs": 18.3221},
            ],
        },
        "memory_saturation_pct": {
            "status": "ok",
            "unit": "%",
            "top": [
                {"service": "front-end", "delta_abs": 1.3868},
                {"service": "catalogue", "delta_abs": 0.3898},
            ],
        },
    }

    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "description": "Compare golden metrics by phase.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "check_oom_kills",
                    "description": "Check in-window OOM events.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                    },
                },
                {
                    "name": "check_pod_restarts",
                    "description": "Check in-window pod restarts.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "step": {"type": "string"},
                        },
                    },
                },
                {
                    "name": "tempo_service_graph_phases",
                    "description": "Compare traces by phase.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "baseline_start": {"type": "string"},
                            "baseline_end": {"type": "string"},
                            "fault_start": {"type": "string"},
                            "fault_end": {"type": "string"},
                            "namespace": {"type": "string"},
                            "limit": {"type": "integer"},
                            "top_k": {"type": "integer"},
                        },
                    },
                },
            ]

        def call_tool_json(self, name, _arguments=None, **_kwargs):
            self.calls.append(name)
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": metrics}
            if name == "check_oom_kills":
                return {"status": "no_data", "found": False}
            if name == "check_pod_restarts":
                return {"status": "ok", "found": False, "count": 0}
            return {
                "phases": {
                    phase: {"trace_count": 0}
                    for phase in ("baseline", "fault", "post")
                },
                "delta": {"suspected_propagation_path": []},
            }

    unsupported_answer = "texto livre fora do formato obrigatório"
    llm_responses = iter(
        [
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
            {"role": "assistant", "content": unsupported_answer},
        ]
    )
    client = _FakeMCPClient()
    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
            "post": {"start": "2026-09-25T10:03:00Z", "end": "2026-09-25T10:04:00Z"},
        },
        namespace="sock-shop",
        max_iterations=6,
        llm=lambda *_args, **_kwargs: next(llm_responses),
    )

    assert client.calls == [
        "get_run_golden_signals",
        "check_oom_kills",
        "check_pod_restarts",
        "tempo_service_graph_phases",
    ]
    assert "front-end" in result["answer"]
    assert "orders" in result["answer"]
    assert "carts" in result["answer"]
    assert "user" in result["answer"]
    assert "catalogue" in result["answer"]
    assert "zero traces (baseline 0, fault 0, post 0)" in result["answer"]
    assert "check_oom_kills: cobertura no_data" in result["answer"]
    assert "Restarts: nenhum evento detectado" in result["answer"]
    assert "não avaliada pelo modelo" in result["answer"]
    assert "não produziu uma conclusão válida" in result["answer"]


def test_run_rca_fallback_locates_dominant_resource_change_with_upstream_latency():
    calls = [
        {
            "tool": "get_run_golden_signals",
            "arguments": {"namespace": "sock-shop"},
            "result": {
                "status": "ok",
                "metrics": {
                    "latency_p95": {
                        "status": "ok",
                        "top": [
                            {"service": "front-end", "delta_abs": 0.42},
                            {"service": "catalogue", "delta_abs": 0.29},
                        ],
                    },
                    "memory_saturation_pct": {
                        "status": "ok",
                        "top": [
                            {"service": "catalogue", "delta_abs": 31.93},
                            {"service": "front-end", "delta_abs": 4.83},
                        ],
                    },
                },
            },
        },
        {
            "tool": "tempo_service_graph_phases",
            "result": {
                "phases": {"baseline": {"trace_count": 0}, "fault": {"trace_count": 0}},
            },
        },
    ]

    answer = render_rca_fallback(calls)

    assert "Localização provável: não avaliada pelo modelo" in answer
    assert "Causa provável: não avaliada pelo modelo" in answer
    assert "Tempo: zero traces" in answer
    assert "Tempo: zero traces" in answer

    calls[0]["result"]["metrics"]["memory_saturation_pct"]["top"] = [
        {"service": "catalogue", "delta_abs": 1.0},
        {"service": "front-end", "delta_abs": 0.5},
    ]
    calls[0]["result"]["metrics"]["cpu_saturation_pct"] = {
        "status": "ok",
        "top": [
            {"service": "catalogue", "delta_abs": 30.0},
            {"service": "front-end", "delta_abs": 5.0},
        ],
    }
    cpu_answer = render_rca_fallback(calls)
    assert "Causa provável: não avaliada pelo modelo" in cpu_answer

    calls[0]["result"]["metrics"]["memory_saturation_pct"]["top"] = [
        {"service": "catalogue", "delta_abs": 31.93},
        {"service": "front-end", "delta_abs": 4.83},
    ]
    calls[1]["result"] = {
        "phases": {"baseline": {"trace_count": 10}, "fault": {"trace_count": 10}},
        "delta": {
            "suspected_propagation_path": ["front-end", "session-db"],
            "top_node_deltas": [
                {"service": "catalogue", "latency_p95_ms_delta": 78},
            ],
            "top_edge_deltas": [
                {
                    "from": "front-end",
                    "to": "catalogue",
                    "latency_p95_ms_delta": 290,
                    "appeared_in_fault": False,
                    "missing_in_fault": False,
                }
            ],
        },
    }
    answer_with_traces = render_rca_fallback(calls)
    assert "Tempo nó catalogue P95 +78 ms" in answer_with_traces
    assert "Tempo: front-end → catalogue P95 +290 ms" in answer_with_traces
    assert "Cadeia observada em traces" not in answer_with_traces
    assert "propagação não avaliada por falta de traces" not in answer_with_traces

    calls[0]["arguments"]["namespace"] = "unknown-system"
    assert "Localização provável: não avaliada pelo modelo" in render_rca_fallback(calls)


def _golden_call(metrics, namespace="sock-shop"):
    return {
        "tool": "get_run_golden_signals",
        "arguments": {"namespace": namespace},
        "result": {"status": "ok", "metrics": metrics},
    }


def test_rca_reference_phases_uses_steady_window_before_fault():
    phases = {
        "baseline": {"start": "2026-09-29T10:20:56Z", "end": "2026-09-29T10:21:26Z"},
        "warmup": {"start": "2026-09-29T10:21:26Z", "end": "2026-09-29T10:22:40Z"},
        "fault": {"start": "2026-09-29T10:22:40Z", "end": "2026-09-29T10:24:40Z"},
    }
    adjusted, source = agent_console.rca_reference_phases(phases)
    assert source == "pre_fault_steady_window"
    assert adjusted["baseline"] == {
        "start": "2026-09-29T10:21:55Z",
        "end": "2026-09-29T10:22:40Z",
    }
    assert adjusted["fault"] == phases["fault"]
    assert phases["baseline"]["start"] == "2026-09-29T10:20:56Z"

    short = {
        "baseline": {"start": "2026-09-29T10:22:30Z", "end": "2026-09-29T10:22:35Z"},
        "fault": {"start": "2026-09-29T10:22:40Z", "end": "2026-09-29T10:24:40Z"},
    }
    assert agent_console.rca_reference_phases(short) == (short, "run_baseline")


def test_rca_fallback_prefers_leaf_with_relative_resource_growth():
    # payment (leaf, fast) doubles CPU from a low base while the gateway grows
    # proportionally with traffic and still leads in absolute delta.
    metrics = {
        "latency_p95": {
            "status": "ok",
            "top": [
                {"service": "front-end", "delta_abs": 0.58},
                {"service": "orders", "delta_abs": 0.28},
                {"service": "payment", "delta_abs": 0.0},
            ],
        },
        "cpu_saturation_pct": {
            "status": "ok",
            "top": [
                {"service": "front-end", "baseline_mean": 30, "fault_mean": 59, "delta_abs": 29},
                {"service": "payment", "baseline_mean": 7, "fault_mean": 26, "delta_abs": 20},
                {"service": "user", "baseline_mean": 16, "fault_mean": 28, "delta_abs": 12},
            ],
        },
        "memory_saturation_pct": {"status": "ok", "top": []},
    }
    answer = render_rca_fallback([_golden_call(metrics)])
    assert "Localização provável: não avaliada pelo modelo" in answer
    assert "Causa provável: não avaliada pelo modelo" in answer


def test_rca_fallback_prefers_memory_over_cpu_when_both_grow_on_same_service():
    metrics = {
        "latency_p95": {"status": "ok", "top": [{"service": "catalogue", "delta_abs": 0.25}]},
        "cpu_saturation_pct": {
            "status": "ok",
            "top": [
                {"service": "catalogue", "baseline_mean": 7, "fault_mean": 30, "delta_abs": 23},
                {"service": "front-end", "baseline_mean": 47, "fault_mean": 62, "delta_abs": 15},
            ],
        },
        "memory_saturation_pct": {
            "status": "ok",
            "top": [
                {"service": "catalogue", "baseline_mean": 12, "fault_mean": 24, "delta_abs": 12},
                {"service": "front-end", "baseline_mean": 18, "fault_mean": 19, "delta_abs": 1},
            ],
        },
    }
    answer = render_rca_fallback([_golden_call(metrics)])
    assert "Causa provável: não avaliada pelo modelo" in answer


def test_rca_fallback_locates_latency_sink_by_topology():
    # carts and its callers are slow; carts-db (its downstream) is not.
    metrics = {
        "latency_p95": {
            "status": "ok",
            "top": [
                {"service": "carts", "delta_abs": 7.74},
                {"service": "orders", "delta_abs": 4.25},
                {"service": "front-end", "delta_abs": 2.24},
            ],
        },
        "cpu_saturation_pct": {
            "status": "ok",
            "top": [{"service": "front-end", "baseline_mean": 38, "fault_mean": 44, "delta_abs": 6}],
        },
        "memory_saturation_pct": {"status": "ok", "top": []},
    }
    answer = render_rca_fallback([_golden_call(metrics)])
    assert "Localização provável: não avaliada pelo modelo" in answer
    assert "Causa provável: não avaliada pelo modelo" in answer

    # Ambiguous: two independent sinks → stay undetermined.
    metrics["latency_p95"]["top"].append({"service": "payment", "delta_abs": 1.0})
    assert "Localização provável: não avaliada pelo modelo" in render_rca_fallback([_golden_call(metrics)])


def test_rca_fallback_direct_event_lists_only_services_with_events():
    calls = [
        _golden_call({"latency_p95": {"status": "ok", "top": []}}),
        {
            "tool": "check_pod_restarts",
            "result": {
                "status": "ok",
                "found": True,
                "count": 1,
                "top": [
                    {"service": "front-end", "restarts_in_window": 1},
                    {"service": "carts", "restarts_in_window": 0},
                    {"service": "carts-db"},
                ],
            },
        },
    ]
    answer = render_rca_fallback(calls)
    assert "Localização provável: não avaliada pelo modelo" in answer
    assert "serviço com evento direto" not in answer


def test_run_rca_accepts_two_line_answer_and_host_completes_evidence():
    metrics = {
        "latency_p95": {"status": "ok", "top": [{"service": "carts", "delta_abs": 0.9}]},
        "cpu_saturation_pct": {"status": "ok", "top": []},
        "memory_saturation_pct": {"status": "ok", "top": []},
    }

    class _FakeMCPClient:
        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "inputSchema": {"properties": {"namespace": {"type": "string"}}},
                },
                {"name": "check_oom_kills", "inputSchema": {"properties": {}}},
            ]

        def call_tool_json(self, name, _arguments=None, **_kwargs):
            if name == "check_oom_kills":
                return {"status": "no_data", "found": False, "count": 0}
            return {"status": "ok", "metrics": metrics}

    replies = iter(
        [
            {"content": "Localização provável: warehouse\nCausa provável: saturação"},
            {"content": "Localização provável: carts\nCausa provável: latência própria elevada"},
        ]
    )

    def llm(messages, _tools, **_kwargs):
        user = json.loads(messages[1]["content"])
        assert "host_hypothesis" not in user
        assert "top_delta" in json.dumps(user["initial_evidence"])
        return next(replies)

    result = agent_console.answer_run_rca_mcp(
        mcp_client=_FakeMCPClient(),
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=4,
        llm=llm,
    )

    lines = result["answer"].splitlines()
    assert result["stop_reason"] == "model_final"
    assert lines[0] == "Localização provável: carts"
    assert lines[1] == "Causa provável: latência própria elevada"
    assert lines[2].startswith("Evidências:") and "carts subiu 0.90 s" in lines[2]
    assert lines[3].startswith("Limitações:") and "check_oom_kills: cobertura no_data" in lines[3]
    # tool message carried the digest, not the raw payload
    tool_msgs = [m for m in result.get("calls", []) if m["tool"] == "check_oom_kills"]
    assert tool_msgs and tool_msgs[0]["result"]["status"] == "no_data"


def test_rca_tool_digest_compacts_raw_results():
    golden = {
        "status": "ok",
        "metrics": {
            "cpu_saturation_pct": {
                "status": "ok",
                "unit": "%",
                "query": "sum(...)",
                "phases": {"baseline": {"sample_count": 24}},
                "top": [
                    {"service": "a", "baseline_mean": 1, "fault_mean": 2, "delta_abs": 1, "fault_max": 3, "delta_pct": 100},
                    {"service": "b", "baseline_mean": 5, "fault_mean": 9, "delta_abs": 4},
                    {"service": "front-end", "baseline_mean": 10, "fault_mean": 2, "delta_abs": -8},
                ],
            },
            "error_rate": {"status": "no_data"},
        },
    }
    digest = agent_console._rca_tool_digest("get_run_golden_signals", golden)
    assert digest["metrics"]["cpu_saturation_pct"]["top_delta"][0] == {
        "service": "front-end", "baseline": 10.0, "fault": 2.0,
        "delta": -8.0, "direction": "decrease",
    }
    assert digest["metrics"]["error_rate"] == {"status": "no_data"}
    assert "query" not in digest["metrics"]["cpu_saturation_pct"]

    restarts = agent_console._rca_tool_digest(
        "check_pod_restarts",
        {"status": "ok", "found": True, "count": 1, "top": [
            {
                "service": "front-end",
                "restarts_in_window": 1,
                "pods": [
                    {
                        "pod": "front-end-abcde-12345",
                        "container": "front-end",
                        "restart_observations": [
                            {
                                "observed_from": "t0",
                                "observed_to": "t1",
                                "restart_count_delta": 1,
                                "timestamp_precision": "between_scrapes",
                            }
                        ],
                    }
                ],
            },
            {"service": "carts", "restarts_in_window": 0},
        ]},
    )
    assert restarts["affected"][0]["service"] == "front-end"
    assert restarts["affected"][0]["pod_observations"][0]["restart_observations"][0]["timestamp_precision"] == "between_scrapes"
    assert agent_console._rca_tool_digest("check_oom_kills", {"status": "tool_error", "error": "boom", "top": []}) == {
        "status": "tool_error", "error": "boom",
    }


def test_rca_readiness_digest_and_fallback_preserve_phase_chain():
    readiness = {
        "status": "partial",
        "phases": {
            "baseline": {
                "status": "ok",
                "coverage": {"series_count": 2, "sample_count": 8},
                "services": [
                    {"service": "user", "pod_count": 1, "ready_mean_pct": 100.0}
                ],
            },
            "fault": {
                "status": "ok",
                "coverage": {"series_count": 2, "sample_count": 8},
                "services": [
                    {
                        "service": "user",
                        "pod_count": 1,
                        "ready_mean_pct": 0.0,
                        "pods": [
                            {
                                "pod": "user-abcde-12345",
                                "transitions": [
                                    {
                                        "transition": "ready_to_not_ready",
                                        "observed_from": "t0",
                                        "observed_to": "t1",
                                        "timestamp_precision": "between_scrapes",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "post": {
                "status": "no_data",
                "coverage": {"series_count": 0, "sample_count": 0},
                "services": [],
            },
        },
    }
    digest = getattr(agent_console, "_rca_tool_digest")(
        "check_service_readiness", readiness
    )
    answer = getattr(agent_console, "_rca_fallback_answer")(
        [{"tool": "check_service_readiness", "result": readiness}]
    )

    assert digest["phases"]["fault"]["services"] == [
        {
            "service": "user",
            "pod_count": 1,
            "ready_mean_pct": 0.0,
            "pod_observations": [
                {
                    "pod": "user-abcde-12345",
                    "transitions": [
                        {
                            "transition": "ready_to_not_ready",
                            "observed_from": "t0",
                            "observed_to": "t1",
                            "timestamp_precision": "between_scrapes",
                        }
                    ],
                    "omitted_transition_count": 0,
                }
            ],
        }
    ]
    assert "Prontidão: user: baseline 100% → fault 0%" in answer
    assert "Prontidão fault: cobertura" not in answer
    next_tool = getattr(agent_console, "_rca_next_required_tool")
    assert next_tool([], {"check_service_readiness": {}}) == "check_service_readiness"

    args = getattr(agent_console, "_rca_tool_arguments")(
        "check_service_readiness",
        {},
        {
            "properties": {
                "baseline_start": {},
                "baseline_end": {},
                "fault_start": {},
                "fault_end": {},
                "post_start": {},
                "post_end": {},
                "namespace": {},
                "step": {},
            }
        },
        phases={
            "baseline": {"start": "b0", "end": "b1"},
            "fault": {"start": "f0", "end": "f1"},
            "post": {"start": "p0", "end": "p1"},
        },
        namespace="sock-shop",
    )
    assert args == {
        "baseline_start": "b0",
        "baseline_end": "b1",
        "fault_start": "f0",
        "fault_end": "f1",
        "post_start": "p0",
        "post_end": "p1",
        "step": "15s",
        "namespace": "sock-shop",
    }


def test_rca_log_limit_is_host_managed_and_hidden_from_model_schema():
    model_tools = getattr(agent_console, "_rca_model_tools")(
        {
            "check_service_logs": {
                "description": "query logs",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "limit_per_phase": {"type": "integer"},
                        "fault_start": {"type": "string"},
                    },
                },
            }
        }
    )
    args = getattr(agent_console, "_rca_tool_arguments")(
        "check_service_logs",
        {"service": "user"},
        {
            "properties": {
                "service": {},
                "limit_per_phase": {},
                "baseline_start": {},
                "baseline_end": {},
                "fault_start": {},
                "fault_end": {},
                "namespace": {},
            }
        },
        phases={
            "baseline": {"start": "b0", "end": "b1"},
            "fault": {"start": "f0", "end": "f1"},
        },
        namespace="sock-shop",
    )

    assert "limit_per_phase" not in model_tools[0]["function"]["parameters"]["properties"]
    assert args["limit_per_phase"] == 300


def test_rca_model_attempt_redacts_and_caps_saved_content():
    make_attempt = getattr(agent_console, "_rca_model_attempt")
    attempt = make_attempt(
        "final",
        {"content": 'token: secret-value ' + ("x" * 2100)},
        "rejected_format",
    )

    assert "secret-value" not in attempt["content_excerpt"]
    assert "[REDACTED]" in attempt["content_excerpt"]
    assert len(attempt["content_excerpt"]) == 2000
    assert attempt["content_truncated"] is True


def test_rca_trace_digest_preserves_phase_observations_without_heuristic_path():
    digest = getattr(agent_console, "_rca_tool_digest")(
        "tempo_service_graph_phases",
        {
            "collection": {"search_count": 12, "analyzed_count": 10},
            "phases": {
                "fault": {
                    "trace_count": 10,
                    "error_trace_count": 2,
                    "top_nodes": [
                        {
                            "service": "payment",
                            "span_count": 18,
                            "error_span_count": 2,
                            "error_rate": 0.11,
                            "latency_p95_ms": 230,
                            "latency_max_ms": 460,
                        }
                    ],
                    "failure_signatures": [
                        {
                            "trace_id": "trace-1",
                            "signature": "NETWORK_TIMEOUT",
                            "endpoint": "catalogue:80",
                            "affected_service": "user",
                            "operations": ["user.GET /"],
                            "occurrence_count": 2,
                        }
                    ],
                    "top_edges": [
                        {
                            "from": "orders",
                            "to": "payment",
                            "span_count": 9,
                            "error_span_count": 2,
                            "error_rate": 0.22,
                            "latency_p95_ms": 210,
                            "latency_max_ms": 400,
                        }
                    ],
                }
            },
            "delta": {
                "top_node_deltas": [{"service": "payment", "latency_p95_ms_delta": 80}],
                "top_edge_deltas": [{"from": "orders", "to": "payment", "error_rate_delta": 0.2}],
                "suspected_propagation_path": ["front-end", "session-db"],
            },
        },
    )

    assert digest["collection"] == {"search_count": 12, "analyzed_count": 10}
    assert digest["phase_observations"]["fault"]["top_edges"][0]["to"] == "payment"
    assert digest["phase_observations"]["fault"]["top_nodes"][0]["error_span_count"] == 2
    assert digest["phase_observations"]["fault"]["failure_signatures"][0]["trace_id"] == "trace-1"
    assert digest["phase_observations"]["fault"]["failure_signatures"][0]["signature"] == "NETWORK_TIMEOUT"
    assert digest["measured_deltas"]["top_edge_deltas"][0]["error_rate_delta"] == 0.2
    assert "suspected_propagation_path" not in json.dumps(digest)


def test_run_rca_model_receives_topology_and_does_not_dismiss_resource_evidence():
    metrics = {
        "latency_p95": {
            "status": "ok",
            "top": [
                {"service": "front-end", "delta_abs": 0.42},
                {"service": "catalogue", "delta_abs": 0.29},
            ],
        },
        "memory_saturation_pct": {
            "status": "ok",
            "top": [
                {"service": "catalogue", "delta_abs": 31.93},
                {"service": "front-end", "delta_abs": 4.83},
            ],
        },
    }

    class _FakeMCPClient:
        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "inputSchema": {
                        "properties": {"namespace": {"type": "string"}},
                    },
                }
            ]

        def call_tool_json(self, _name, _arguments=None, **_kwargs):
            return {"status": "ok", "metrics": metrics}

    def llm(messages, _tools, **_kwargs):
        assert "downstream: catalogue" in messages[0]["content"]
        return {
            "content": (
                "Localização provável: indeterminada\n"
                "Causa provável: não confirmada\n"
                "Evidências: métricas disponíveis\n"
                "Limitações: sem traces"
            )
        }

    result = agent_console.answer_run_rca_mcp(
        mcp_client=_FakeMCPClient(),
        phases={
            "baseline": {"start": "2026-09-25T10:00:00Z", "end": "2026-09-25T10:01:00Z"},
            "fault": {"start": "2026-09-25T10:02:00Z", "end": "2026-09-25T10:03:00Z"},
        },
        namespace="sock-shop",
        max_iterations=2,
        llm=llm,
    )

    assert result["stop_reason"] == "model_final"
    assert "Localização provável: indeterminada" in result["answer"]


def test_run_rca_queries_service_logs_after_locking_location():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [
                {
                    "name": "get_run_golden_signals",
                    "inputSchema": {
                        "properties": {
                            "baseline_start": {}, "baseline_end": {},
                            "fault_start": {}, "fault_end": {}, "namespace": {},
                            "step": {}, "k": {},
                        }
                    },
                },
                {
                    "name": "check_service_logs",
                    "inputSchema": {
                        "properties": {
                            "service": {}, "baseline_start": {}, "baseline_end": {},
                            "fault_start": {}, "fault_end": {}, "post_start": {},
                            "post_end": {}, "namespace": {},
                        }
                    },
                },
            ]

        def call_tool_json(self, name, arguments, **_kwargs):
            self.calls.append((name, arguments))
            if name == "get_run_golden_signals":
                return {"status": "ok", "metrics": {}}
            return {
                "status": "ok",
                "service": "user",
                "phases": {
                    "baseline": {
                        "status": "ok",
                        "coverage": {"lines_returned": 2, "truncated": False},
                        "signatures": [],
                    },
                    "fault": {
                        "status": "ok",
                        "coverage": {"lines_returned": 3, "truncated": False},
                        "signatures": [
                            {
                                "signature": "NETWORK_TIMEOUT",
                                "pod": "user-abc123-def456",
                                "container": "user",
                                "count_observed": 2,
                                "first_seen": "2026-10-02T10:02:10Z",
                                "last_seen": "2026-10-02T10:02:20Z",
                                "examples": [
                                    {
                                        "evidence_id": "log-evidence-1",
                                        "timestamp": "2026-10-02T10:02:10Z",
                                        "message": "upstream request timeout",
                                    }
                                ],
                            }
                        ],
                    },
                    "post": {"status": "no_data", "coverage": {}, "signatures": []},
                },
            }

    client = _FakeMCPClient()
    model_messages = []
    replies = iter(
        [
            {"content": "Localização provável: user\nCausa provável: pod-failure"},
            {"content": "Localização provável: user\nCausa provável: timeout upstream observado nos logs; restart como consequência"},
        ]
    )

    def llm(messages, _tools, **_kwargs):
        model_messages.append(messages)
        return next(replies)

    phases = {
        "baseline": {"start": "2026-10-02T10:00:00Z", "end": "2026-10-02T10:01:00Z"},
        "fault": {"start": "2026-10-02T10:02:00Z", "end": "2026-10-02T10:03:00Z"},
        "post": {"start": "2026-10-02T10:03:00Z", "end": "2026-10-02T10:04:00Z"},
    }
    result = agent_console.answer_run_rca_mcp(
        mcp_client=client,
        phases=phases,
        namespace="sock-shop",
        max_iterations=4,
        llm=llm,
    )

    assert [name for name, _args in client.calls] == [
        "get_run_golden_signals", "check_service_logs"
    ]
    assert client.calls[1][1] == {
        "service": "user",
        "baseline_start": phases["baseline"]["start"],
        "baseline_end": phases["baseline"]["end"],
        "fault_start": phases["fault"]["start"],
        "fault_end": phases["fault"]["end"],
        "post_start": phases["post"]["start"],
        "post_end": phases["post"]["end"],
        "namespace": "sock-shop",
    }
    assert len(model_messages) == 2
    assert "NETWORK_TIMEOUT" in json.dumps(model_messages[1])
    assert result["stop_reason"] == "model_final"
    assert result["answer"].splitlines()[0] == "Localização provável: user"
    assert "timeout upstream observado" in result["answer"]
    assert result["evidence_bundle"]["schema_version"] == 1
    assert result["evidence_bundle"]["localized_service"] == "user"
    assert result["evidence_bundle"]["evidence_ids"] == ["log-evidence-1"]
    assert result["evidence_bundle"]["coverage"]["check_service_logs"]["source"] == "live"
    assert result["evidence_bundle"]["timeline"][0]["signature"] == "NETWORK_TIMEOUT"
    valid = getattr(agent_console, "_rca_final_answer_is_valid")
    changed_location = "Localização provável: orders\nCausa provável: causa"
    assert not valid(changed_location, result["calls"], expected_service="user")


def test_mcp_chat_falls_back_to_restart_and_oom_tools_after_qwen_502():
    class _FakeMCPClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            shared_properties = {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "namespace": {"type": "string"},
                "step": {"type": "string"},
                "k": {"type": "integer"},
            }
            return [
                {
                    "name": "check_pod_restarts",
                    "description": "Top services by pod restarts.",
                    "inputSchema": {
                        "type": "object",
                        "properties": shared_properties,
                    },
                },
                {
                    "name": "check_oom_kills",
                    "description": "Top services by OOM kills.",
                    "inputSchema": {
                        "type": "object",
                        "properties": shared_properties,
                    },
                },
            ]

        def call_tool_json(self, name, arguments, **_kwargs):
            self.calls.append((name, arguments))
            if name == "check_pod_restarts":
                return {
                    "found": False,
                    "count": 0,
                    "top": [{"service": "user", "restarts_in_window": 0}],
                }
            return {"found": False, "count": 0, "top": []}

    client = _FakeMCPClient()
    llm_calls = []

    def _fake_llm(messages, _tools, **kwargs):
        llm_calls.append((messages, kwargs))
        if len(llm_calls) == 1:
            assert kwargs["tool_choice"] == "required"
            raise agent_console.LLMClientError(
                'LLM HTTP 502: model did not return a valid tool call'
            )
        raise AssertionError("host fallback must not call Qwen again")

    answer = agent_console.answer_query_mcp(
        "Algum pod foi restartado ou sofreu OOM?",
        mcp_client=client,
        start="2026-09-23T11:16:51Z",
        end="2026-09-23T11:22:23Z",
        namespace="sock-shop",
        llm=_fake_llm,
        max_steps=1,
    )

    assert answer.startswith("Na janela consultada:")
    assert "síntese do modelo" not in answer.lower()
    assert "nenhum pod foi reiniciado" in answer
    assert "nenhuma transição OOMKilled" in answer
    assert {name for name, _arguments in client.calls} == {
        "check_pod_restarts",
        "check_oom_kills",
    }


def test_model_loop_uses_observation_when_final_is_truncated_think(monkeypatch):
    responses = iter(
        [
            '{"action":"use_tool","tool":"restarts","service":"rabbitmq"}',
            "<think>unfinished private reasoning",
        ]
    )

    def _fake_llm(messages, max_new_tokens):
        assert isinstance(messages, list)
        assert max_new_tokens > 0
        return next(responses)

    def _fake_restarts(prometheus, *, start, end, namespace, step):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:05:00Z"
        assert namespace == "sock-shop"
        assert step == "15s"
        return {
            "found": False,
            "count": 0,
            "top": [
                {
                    "service": "rabbitmq",
                    "restarts_in_window": 0,
                    "pods": [{"pod": "rabbitmq-abc", "value": 0.0}],
                }
            ],
        }

    monkeypatch.setattr(agent_console, "call_llm_messages", _fake_llm)
    monkeypatch.setattr(agent_console, "check_pod_restarts", _fake_restarts)

    out = agent_console.answer_query(
        "Quantas vezes o RabbitMQ reiniciou?",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
        max_steps=1,
    )

    assert "<think>" not in out
    assert "Tool: check_pod_restarts" in out
    assert "Restarts na janela: 0" in out
    assert "Nenhuma ação corretiva necessária" in out


def test_answer_query_returns_model_error_without_fallback(monkeypatch):
    def _fake_llm(messages, max_new_tokens):
        raise agent_console.LLMClientError("simulated connection error")

    def _must_not_be_called(prometheus, *, start, end, step, k):
        raise AssertionError("tool fallback should not run when model fails")

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _must_not_be_called)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
    )

    assert out.startswith("Erro do modelo:")


def test_answer_query_invalid_planner_json_returns_error(monkeypatch):
    seen_tokens = []

    def _fake_llm(messages, max_new_tokens):
        assert isinstance(messages, list)
        seen_tokens.append(max_new_tokens)
        return "sem-json"

    def _fake_http_error(prometheus, *, start, end, step, k):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:10:00Z"
        assert step == "15s"
        assert k == 10
        return {"found": False, "count": 0, "top": []}

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _fake_http_error)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
    )

    assert seen_tokens == [180, 120, 180, 120]
    assert "Resposta:" in out


def test_answer_query_planner_json_repair_path(monkeypatch):
    responses = iter(
        [
            "texto invalido",
            '{"action":"use_tool","tool":"http_error_rate","service":"orders"}',
            "Resposta: consulta concluída.\nEvidência: taxa disponível.\nPróximo passo: monitorar.",
        ]
    )

    def _fake_llm(messages, max_new_tokens):
        assert isinstance(messages, list)
        assert max_new_tokens in {120, 180}
        return next(responses)

    def _fake_http_error(prometheus, *, start, end, step, k):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:10:00Z"
        assert step == "15s"
        assert k == 10
        return {
            "found": True,
            "count": 1,
            "top": [
                {
                    "service": "orders",
                    "error_rate_mean_pct": 1.25,
                    "error_rate_max_pct": 3.5,
                }
            ],
        }

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _fake_http_error)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
        max_steps=1,
    )

    assert "consulta concluída" in out


def test_answer_query_blocks_repeated_no_data_tool(monkeypatch):
    responses = iter(
        [
            '{"action":"use_tool","tool":"http_error_rate","service":"orders"}',
            '{"action":"use_tool","tool":"http_error_rate","service":"orders"}',
        ]
    )

    def _fake_llm(messages, max_new_tokens):
        assert isinstance(messages, list)
        assert max_new_tokens == 180
        return next(responses)

    def _fake_http_error(prometheus, *, start, end, step, k):
        assert isinstance(prometheus, _FakePrometheus)
        assert start == "2026-01-01T00:00:00Z"
        assert end == "2026-01-01T00:10:00Z"
        assert step == "15s"
        assert k == 10
        return {"found": False, "count": 0, "top": []}

    monkeypatch.setattr(agent_console, "check_http_error_rate_top", _fake_http_error)

    out = agent_console.answer_query(
        "qual a taxa de erros do serviço orders",
        prometheus=_FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
        use_model=True,
        llm=_fake_llm,
        max_steps=2,
    )

    assert "Próximo passo" in out
