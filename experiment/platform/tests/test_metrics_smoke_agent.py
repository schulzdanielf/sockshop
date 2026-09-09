"""Tests for the lightweight metrics smoke agent."""
from __future__ import annotations

from experiment.platform.backend import metrics_smoke_agent as agent


class _FakePrometheus:
    pass


def test_collect_snapshot_uses_expected_investigation_keys(monkeypatch):
    calls = []

    def _stub(prometheus, **kwargs):
        assert isinstance(prometheus, _FakePrometheus)
        calls.append(kwargs)
        return {"found": False, "count": 0, "top": []}

    monkeypatch.setattr(agent, "check_http_error_rate_top", _stub)
    monkeypatch.setattr(agent, "check_request_latency_top", _stub)
    monkeypatch.setattr(agent, "get_cpu_saturation_top", _stub)
    monkeypatch.setattr(agent, "get_memory_saturation_top", _stub)
    monkeypatch.setattr(agent, "check_pod_restarts", _stub)
    monkeypatch.setattr(agent, "check_oom_kills", _stub)

    snapshot = agent.collect_snapshot(
        _FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
    )

    assert snapshot["namespace"] == "sock-shop"
    assert set(snapshot["investigations"]) == {
        "http_error_rate",
        "request_latency_p95",
        "cpu_saturation",
        "memory_saturation",
        "pod_restarts",
        "oom_kills",
    }
    assert len(calls) == 6
    assert len(snapshot["tool_calls"]) == 6


def test_snapshot_health_reports_unavailable_on_errors():
    health = agent.snapshot_health(
        {
            "investigations": {
                "http_error_rate": {"found": False, "error": "down"},
                "request_latency_p95": {"found": False, "count": 0, "top": []},
            }
        }
    )

    assert health["status"] == "unavailable"
    assert health["error_keys"] == ["http_error_rate"]


def test_build_assessment_messages_compacts_snapshot():
    messages = agent.build_assessment_messages(
        {
            "investigations": {
                "http_error_rate": {
                    "found": True,
                    "count": 1,
                    "top": [
                        {"service": "orders", "error_rate_max_pct": 12.0},
                        {"service": "payment", "error_rate_max_pct": 2.0},
                        {"service": "shipping", "error_rate_max_pct": 1.0},
                    ],
                }
            }
        }
    )

    assert messages[0]["role"] == "system"
    assert '"http_error_rate"' in messages[1]["content"]
    assert '"shipping"' not in messages[1]["content"]


def test_assess_snapshot_returns_parsed_json():
    snapshot = {"investigations": {}}

    def _fake_llm(messages, max_new_tokens):
        assert messages[0]["role"] == "system"
        assert max_new_tokens == 123
        return (
            '{"status":"warning","services":["orders"],'
            '"summary":"Elevated errors on orders.",'
            '"evidence":["http_error_rate"],'
            '"actions":["inspect orders logs"]}'
        )

    out = agent.assess_snapshot(snapshot, llm=_fake_llm, max_new_tokens=123)

    assert out["parsed"]["status"] == "warning"
    assert out["parsed"]["services"] == ["orders"]
    assert out["source"] == "llm_json"


def test_assess_snapshot_falls_back_when_llm_returns_non_json():
    snapshot = {
        "investigations": {
            "memory_saturation": {
                "found": True,
                "top": [{"service": "carts", "saturation_max_pct": 75.0}],
            }
        }
    }

    def _fake_non_json_llm(messages, max_new_tokens):
        assert messages[0]["role"] == "system"
        assert max_new_tokens == 220
        return "<think>..."

    out = agent.assess_snapshot(snapshot, llm=_fake_non_json_llm)

    assert out["source"] == "fallback"
    assert out["parsed"]["status"] == "warning"
    assert out["parsed"]["services"] == ["carts"]


def test_build_assessment_prompt_contains_compact_snapshot():
    prompt = agent.build_assessment_prompt(
        {
            "investigations": {
                "memory_saturation": {
                    "found": True,
                    "count": 1,
                    "top": [{"service": "carts", "saturation_max_pct": 75.0}],
                }
            }
        }
    )

    assert "[system]" in prompt
    assert "memory_saturation" in prompt
