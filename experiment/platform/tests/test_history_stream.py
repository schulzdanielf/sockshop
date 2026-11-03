from experiment.platform.backend.history_stream import validate_history_stream


PHASES = {
    "baseline": {
        "start": "2026-11-01T20:00:00Z",
        "end": "2026-11-01T20:01:00Z",
    },
    "fault": {
        "start": "2026-11-01T20:02:00Z",
        "end": "2026-11-01T20:03:00Z",
    },
    "post": {
        "start": "2026-11-01T20:03:00Z",
        "end": "2026-11-01T20:04:00Z",
    },
}


class FakeClient:
    def __init__(self, *, empty=False, target_logs_empty=False, namespace_logs_empty=False):
        self.empty = empty
        self.target_logs_empty = target_logs_empty
        self.namespace_logs_empty = namespace_logs_empty
        self.calls = []

    def list_tools(self):
        return [{"name": name} for name in (
            "get_run_golden_signals",
            "loki_range_query",
            "tempo_search_traces",
        )]

    def call_tool_json(self, name, arguments, *, timeout_seconds):
        self.calls.append((name, arguments, timeout_seconds))
        if name == "get_run_golden_signals":
            count = 0 if self.empty else 2
            return {
                "metrics": {
                    "request_rate": {
                        "phases": {
                            "baseline": {"sample_count": count},
                            "fault": {"sample_count": count},
                        }
                    }
                }
            }
        if name == "loki_range_query":
            query = arguments["query"]
            logs_empty = self.empty or (
                self.target_logs_empty and "container=" in query
            ) or (
                self.namespace_logs_empty and "container=" not in query
            )
            return {
                "data": {
                    "result": [] if logs_empty else [{"values": [["1", "line"]]}]
                }
            }
        return {"traces": [] if self.empty else [{"traceID": "1" * 32}]}


def test_dual_write_validation_requires_readback_from_all_three_families():
    client = FakeClient()

    result = validate_history_stream(
        client,
        phases=PHASES,
        namespace="sock-shop",
        service="payment",
    )

    assert result["status"] == "complete"
    assert result["source"] == "historical_mcp"
    assert result["source_counts"] == {
        "metrics": 4,
        "logs": 1,
        "traces": 1,
    }
    assert result["telemetry"]["logs"] == {
        "status": "complete",
        "entries_returned": 1,
        "target_entries_returned": 1,
        "validation_scope": "target_container",
        "error": None,
    }
    assert [call[0] for call in client.calls] == [
        "get_run_golden_signals",
        "loki_range_query",
        "tempo_search_traces",
    ]
    assert all(call[2] == 30 for call in client.calls)


def test_dual_write_validation_falls_back_to_namespace_logs_when_target_is_silent():
    client = FakeClient(target_logs_empty=True)

    result = validate_history_stream(
        client,
        phases=PHASES,
        namespace="sock-shop",
        service="carts",
    )

    assert result["status"] == "complete"
    assert result["source_counts"]["logs"] == 1
    assert result["telemetry"]["logs"] == {
        "status": "complete",
        "entries_returned": 1,
        "target_entries_returned": 0,
        "validation_scope": "namespace",
        "error": None,
    }
    log_calls = [call for call in client.calls if call[0] == "loki_range_query"]
    assert [call[1]["query"] for call in log_calls] == [
        '{namespace="sock-shop",container="carts"}',
        '{namespace="sock-shop"}',
    ]


def test_dual_write_validation_remains_pending_when_a_family_is_empty():
    result = validate_history_stream(
        FakeClient(empty=True),
        phases=PHASES,
        namespace="sock-shop",
        service="payment",
    )

    assert result["status"] == "pending"
    assert {
        name: value["status"] for name, value in result["telemetry"].items()
    } == {
        "metrics": "pending",
        "logs": "pending",
        "traces": "pending",
    }


def test_dual_write_validation_fails_when_required_tool_is_missing():
    class MissingLogsClient(FakeClient):
        def list_tools(self):
            return [{"name": "tempo_search_traces"}]

    result = validate_history_stream(
        MissingLogsClient(),
        phases=PHASES,
        namespace="sock-shop",
        service="payment",
    )

    assert result["status"] == "failed"
    assert "get_run_golden_signals" in result["error"]
    assert "loki_range_query" in result["error"]
