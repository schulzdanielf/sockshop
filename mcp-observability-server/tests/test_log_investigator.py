from __future__ import annotations

import importlib
import json

from log_investigator import (
    _sanitize_line,
    capture_run_log_snapshot,
    check_service_logs,
)

mcp_main = importlib.import_module("main")


def _stream(pod, container, values):
    return {
        "stream": {"pod": pod, "container": container, "namespace": "sock-shop"},
        "values": values,
    }


def test_check_service_logs_bounds_queries_and_redacts_secret_values():
    class _Loki:
        def __init__(self):
            self.calls = []

        def query_range(self, query, **kwargs):
            self.calls.append((query, kwargs))
            if len(self.calls) == 1:
                return {
                    "data": {
                        "result": [
                            _stream(
                                "user-abc123-def456",
                                "user",
                                [
                                    [
                                        "1790935201000000000",
                                        '{"message":"upstream request timeout token=do-not-leak",'
                                        '"trace_id":"trace-1"}',
                                    ]
                                ],
                            )
                        ]
                    }
                }
            return {"data": {"result": [_stream("user-abc123-def456", "user", [])]}}

    loki = _Loki()
    result = check_service_logs(
        loki,
        service="user",
        namespace="sock-shop",
        baseline_start="2026-10-02T10:00:00Z",
        baseline_end="2026-10-02T10:01:00Z",
        fault_start="2026-10-02T10:02:00Z",
        fault_end="2026-10-02T10:03:00Z",
        post_start="2026-10-02T10:03:00Z",
        post_end="2026-10-02T10:04:00Z",
    )

    assert len(loki.calls) == 3
    assert all(call[1]["direction"] == "forward" for call in loki.calls)
    assert all(call[1]["limit"] == 300 for call in loki.calls)
    assert all('pod=~"^user(-[a-z0-9]+)*$"' in call[0] for call in loki.calls)
    fault = result["phases"]["baseline"]
    assert result["status"] == "ok"
    assert fault["signatures"][0]["signature"] == "NETWORK_TIMEOUT"
    example = fault["signatures"][0]["examples"][0]
    assert "do-not-leak" not in example["message"]
    assert "[REDACTED]" in example["message"]
    assert fault["signatures"][0]["trace_id"] == "trace-1"


def test_check_service_logs_handles_no_data_and_rejects_invalid_input():
    class _Loki:
        def query_range(self, *_args, **_kwargs):
            return {"data": {"result": []}}

    args = {
        "loki": _Loki(),
        "service": "user",
        "namespace": "sock-shop",
        "baseline_start": "2026-10-02T10:00:00Z",
        "baseline_end": "2026-10-02T10:01:00Z",
        "fault_start": "2026-10-02T10:02:00Z",
        "fault_end": "2026-10-02T10:03:00Z",
    }
    result = check_service_logs(**args)
    invalid = check_service_logs(**{**args, "service": 'user"} | on {app="x"}'})

    assert result["status"] == "no_data"
    assert all(phase["status"] == "no_data" for phase in result["phases"].values())
    assert invalid == {"status": "invalid_input", "error": "invalid service or namespace"}


def test_sanitizer_redacts_sensitive_compound_query_parameter_names():
    sanitized = _sanitize_line(
        "GET /?access_token=first-secret&client_secret=second-secret&region=west"
    )

    assert "first-secret" not in sanitized
    assert "second-secret" not in sanitized
    assert "region=west" in sanitized


def test_capture_run_log_snapshot_distributes_global_budget_across_services():
    class _Loki:
        def __init__(self):
            self.calls = []

        def query_range(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return {"data": {"result": []}}

    loki = _Loki()
    result = capture_run_log_snapshot(
        loki,
        services=["user", "orders", "user"],
        namespace="sock-shop",
        baseline_start="2026-10-02T10:00:00Z",
        baseline_end="2026-10-02T10:01:00Z",
        fault_start="2026-10-02T10:02:00Z",
        fault_end="2026-10-02T10:03:00Z",
        post_start="2026-10-02T10:03:00Z",
        post_end="2026-10-02T10:04:00Z",
        max_total_lines=120,
    )

    assert result["status"] == "no_data"
    assert result["budget"] == {
        "max_total_lines": 120,
        "lines_returned": 0,
        "per_service_phase_limit": 20,
        "service_count": 2,
        "phase_count": 3,
    }
    assert len(loki.calls) == 6
    assert all(call[1]["limit"] == 20 for call in loki.calls)
    assert any('pod=~"^orders(-[a-z0-9]+)*$"' in call[0] for call in loki.calls)
    assert any('pod=~"^user(-[a-z0-9]+)*$"' in call[0] for call in loki.calls)


def test_capture_run_log_snapshot_rejects_budget_that_cannot_cover_every_phase():
    class _Loki:
        def query_range(self, *_args, **_kwargs):
            raise AssertionError("invalid budgets must be rejected before querying")

    result = capture_run_log_snapshot(
        _Loki(),
        services=["orders", "user"],
        namespace="sock-shop",
        baseline_start="2026-10-02T10:00:00Z",
        baseline_end="2026-10-02T10:01:00Z",
        fault_start="2026-10-02T10:02:00Z",
        fault_end="2026-10-02T10:03:00Z",
        post_start="2026-10-02T10:03:00Z",
        post_end="2026-10-02T10:04:00Z",
        max_total_lines=5,
    )

    assert result == {
        "status": "invalid_input",
        "error": "budget too small for requested services",
    }


def test_capture_run_log_snapshot_rejects_invalid_namespace_and_window_without_queries():
    class _Loki:
        def query_range(self, *_args, **_kwargs):
            raise AssertionError("invalid capture inputs must be rejected before querying")

    args = {
        "loki": _Loki(),
        "services": ["orders"],
        "namespace": "sock-shop",
        "baseline_start": "2026-10-02T10:00:00Z",
        "baseline_end": "2026-10-02T10:01:00Z",
        "fault_start": "2026-10-02T10:02:00Z",
        "fault_end": "2026-10-02T10:03:00Z",
    }

    invalid_namespace = capture_run_log_snapshot(**{**args, "namespace": "bad namespace"})
    invalid_window = capture_run_log_snapshot(**{**args, "fault_end": "2026-10-02T10:01:00Z"})

    assert invalid_namespace["status"] == "invalid_input"
    assert invalid_window["status"] == "invalid_input"


def test_capture_run_log_snapshot_mcp_wrapper_uses_host_budget(monkeypatch):
    class _Loki:
        pass

    monkeypatch.setattr(mcp_main, "loki", _Loki())
    monkeypatch.setattr(
        mcp_main,
        "_inv_capture_run_log_snapshot",
        lambda _loki, **kwargs: {"status": "no_data", "budget": kwargs["max_total_lines"]},
    )

    result = json.loads(
        mcp_main.capture_run_log_snapshot.fn(
            services=["user"],
            baseline_start="b0",
            baseline_end="b1",
            fault_start="f0",
            fault_end="f1",
            max_total_lines=300,
        )
    )

    assert result == {"status": "no_data", "budget": 300}
