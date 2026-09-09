"""Tests for the constrained environment assessor agent."""
from __future__ import annotations

from experiment.platform.backend.environment_assessor import (
    AgentHooks,
    EnvironmentAssessorAgent,
    run_environment_assessor,
)


class _FakePrometheus:
    pass


def _baseline_snapshot(_: _FakePrometheus, **kwargs):
    return {
        "window": {"start": kwargs["start"], "end": kwargs["end"]},
        "namespace": kwargs["namespace"],
        "investigations": {
            "memory_saturation": {
                "found": True,
                "count": 1,
                "top": [{"service": "carts", "saturation_max_pct": 75.0}],
            },
            "http_error_rate": {"found": False, "count": 0, "top": []},
            "request_latency_p95": {"found": False, "count": 0, "top": []},
            "cpu_saturation": {"found": False, "count": 0, "top": []},
            "pod_restarts": {"found": False, "count": 0, "top": []},
            "oom_kills": {"found": False, "count": 0, "top": []},
        },
        "tool_calls": [],
    }


def test_agent_runs_with_hooks_and_loop_limit():
    events = []

    def _on_loop_start(payload):
        events.append(("loop_start", payload["loop"]))

    def _on_tool_start(payload):
        events.append(("tool_start", payload["tool"]))

    def _on_tool_end(payload):
        events.append(("tool_end", payload["tool"]))

    def _on_assessment_start(payload):
        events.append(("assessment_start", payload["snapshot_health"]["status"]))

    def _on_assessment_end(payload):
        events.append(("assessment_end", payload["assessment"]["status"]))

    hooks = AgentHooks(
        on_loop_start=_on_loop_start,
        on_tool_start=_on_tool_start,
        on_tool_end=_on_tool_end,
        on_assessment_start=_on_assessment_start,
        on_assessment_end=_on_assessment_end,
    )

    def _fake_llm(messages, max_new_tokens):
        assert messages[0]["role"] == "system"
        assert max_new_tokens == 180
        return (
            '{"status":"warning","services":["carts"],'
            '"summary":"Memory pressure is elevated.",'
            '"evidence":["memory_saturation"],'
            '"actions":["inspect memory limits"]}'
        )

    agent = EnvironmentAssessorAgent(
        minutes=10,
        max_loops=2,
        max_tool_calls=4,
        llm=_fake_llm,
        hooks=hooks,
        collector=_baseline_snapshot,
    )
    result = agent.run()

    assert result["assessment"]["parsed"]["status"] == "warning"
    assert result["assessment"]["llm_valid"] is True
    assert len(result["loops"]) >= 1
    assert events[0][0] == "loop_start"
    assert any(name == "assessment_start" for name, _ in events)
    assert any(name == "assessment_end" for name, _ in events)


def test_follow_up_chooses_more_specific_tools():
    snapshot = _baseline_snapshot(
        _FakePrometheus(),
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:10:00Z",
        namespace="sock-shop",
    )
    agent = EnvironmentAssessorAgent()
    follow_ups = agent.plan_follow_up_tools(snapshot)

    assert [spec.key for spec in follow_ups] == ["memory_saturation"]


def test_run_environment_assessor_works_with_compact_collector():
    def _fake_llm(messages, max_new_tokens):
        assert messages[0]["role"] == "system"
        assert max_new_tokens == 120
        return (
            '{"status":"healthy","services":[],'
            '"summary":"No issues detected.",'
            '"evidence":[],"actions":["keep monitoring"]}'
        )

    result = run_environment_assessor(
        minutes=10,
        max_loops=1,
        max_tool_calls=2,
        max_new_tokens=120,
        verbose=False,
        llm=_fake_llm,
        collector=_baseline_snapshot,
    )

    assert "snapshot" in result
    assert result["report_text"].startswith("Situação geral:")


def test_invalid_llm_is_repaired_or_baselined_without_breaking_json():
    responses = iter(["<think>no json</think>", "still no json"])

    def _fake_llm(messages, max_new_tokens):
        assert messages[0]["role"] == "system"
        assert max_new_tokens == 120
        return next(responses)

    result = run_environment_assessor(
        minutes=10,
        max_loops=1,
        max_tool_calls=2,
        max_new_tokens=120,
        verbose=False,
        llm=_fake_llm,
        collector=_baseline_snapshot,
    )

    assessment = result["assessment"]
    assert assessment["parsed"]["status"] in {"warning", "healthy", "critical", "unknown", "unavailable"}
    assert assessment["source"] == "baseline"
    assert assessment["llm_valid"] is False
