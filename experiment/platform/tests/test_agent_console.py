"""Tests for the interactive agent console parser and dispatch."""
from __future__ import annotations

from experiment.platform.backend import agent_console


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
                "k": 10,
            },
            "call_cpu",
            "Top services by CPU saturation.",
        )
    ]
    assert llm_calls[0][2]["tool_choice"] == "required"
    assert llm_calls[0][2]["parallel_tool_calls"] is False


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
        "k": 10,
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
