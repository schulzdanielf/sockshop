"""Tests for the interactive agent console parser and dispatch."""
from __future__ import annotations

from experiment.platform.backend import agent_console


class _FakePrometheus:
    pass


def test_parse_request_error_rate_with_service():
    parsed = agent_console.parse_request("qual a taxa de erros do serviço orders")
    assert parsed.intent == "http_error_rate"
    assert parsed.service == "orders"


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
