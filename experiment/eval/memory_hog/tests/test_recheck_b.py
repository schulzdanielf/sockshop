import json

from experiment.eval.memory_hog.recheck_b import (
    HistoricalClient,
    _contract_retry_path,
    collect,
    focused_evidence,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.agent_console import _rca_normalize_trace_result


def test_focused_evidence_keeps_target_edge_after_long_critical_path():
    evidence = [{
        "id": "E019", "tool": "tempo_analyze_trace", "status": "ok",
        "family": "traces", "fault_observation_count": 12,
        "digest": {
            "critical_path": [{"service": "front-end", "padding": "x" * 3000}],
            "dependency_map": [
                {"from": "orders", "to": "payment", "is_error": True, "duration_ms": 0.82},
            ],
        },
    }]
    result = focused_evidence(evidence, "payment")
    assert result[0]["id"] == "E019"
    assert result[0]["details"]["dependency_map"] == evidence[0]["digest"]["dependency_map"]
    assert "padding" not in json.dumps(result)


def test_trace_normalization_preserves_http_error_destination():
    raw = {
        "span_count": 1,
        "error_spans": [{
            "service": "orders", "name": "POST", "duration_ms": 0.82,
            "http_status_code": "500", "remote_endpoint": "payment/paymentAuth",
            "remote_service": "payment",
        }],
    }
    result = _rca_normalize_trace_result(raw, {"trace_id": "abc", "phase": "fault"})
    assert result["error_spans"][0]["http_status_code"] == "500"
    assert result["error_spans"][0]["remote_service"] == "payment"


def test_focused_evidence_keeps_http_fields_from_raw_trace_recheck():
    span = {
        "service": "orders", "name": "POST", "http_status_code": "500",
        "remote_service": "payment", "remote_endpoint": "payment/paymentAuth",
    }
    evidence = [{
        "id": "T001", "tool": "tempo_get_trace", "status": "ok",
        "family": "traces", "fault_observation_count": 1,
        "digest": {"error_spans": [span]},
    }]
    result = focused_evidence(evidence, "payment")
    assert result[0]["id"] == "T001"
    assert result[0]["details"]["error_spans"] == [span]


def test_contract_retry_is_limited_to_failed_records_and_new_path(tmp_path):
    previous = tmp_path / "contract_repairs" / "failed-run.json"
    previous.parent.mkdir()
    previous.write_text(json.dumps({"status": "error"}), encoding="utf-8")
    retry_path = _contract_retry_path(tmp_path, "failed-run")
    assert retry_path == tmp_path / "contract_revalidations" / "failed-run.json"
    assert not retry_path.exists()

    previous.write_text(json.dumps({"status": "inconclusive"}), encoding="utf-8")
    try:
        _contract_retry_path(tmp_path, "failed-run")
    except ValueError as exc:
        assert "Only failed contract repairs" in str(exc)
    else:
        raise AssertionError("A valid prior annotation must not be retried")


def test_focused_evidence_preserves_baseline_resource_comparison():
    evidence = [{
        "id": "E025", "tool": "get_memory_saturation_top", "status": "ok",
        "family": "metrics", "fault_observation_count": 0, "observation_count": 3,
        "arguments": {"start": "2026-10-28T00:00:00Z", "end": "2026-10-28T00:01:00Z"},
        "digest": {"top": [{"service": "user", "saturation_mean_pct": 12.0}]},
    }]
    assert focused_evidence(evidence, "user")[0]["details"] == evidence[0]["digest"]


class EmptyLogs(MCPToolClient):
    def __init__(self):
        self.queries = []

    def list_tools(self):
        return [{"name": "loki_range_query", "inputSchema": {}}]

    def call_tool_json(self, name, arguments=None, **kwargs):
        self.queries.append(arguments["query"])
        return {"status": "success", "data": {"resultType": "streams", "result": []}}


def test_recheck_retries_literal_filters_and_records_unfiltered_no_data(tmp_path):
    original = tmp_path / "annotate.json"
    original.write_text("{}", encoding="utf-8")
    raw_client = EmptyLogs()
    client = HistoricalClient(raw_client)
    window = {"start": "2026-10-28T00:00:00Z", "end": "2026-10-28T00:01:00Z"}
    check = {
        "kind": "logs", "service": "user", "phase": "fault",
        "hypothesis": "Investigar", "expected_support": "Erro",
        "expected_refutation": "Dados contrarios",
        "historical_pair_ids": [], "patterns": ["ts=... ERROR ..."],
    }
    source = {
        "path": original,
        "labels": {"service": "user", "fault_type": "network-latency"},
        "response": {
            "evidence": [], "calls": [],
            "additional_checks": [{"check": check, "status": "no_data"}],
        },
        "pair": {"phases": {"baseline": window, "fault": window}},
    }
    result = collect(source, client, {"user"}, [])
    assert " |~ " in raw_client.queries[0]
    assert " |~ " not in raw_client.queries[1]
    assert all(row["status"] == "no_data" for row in result["rechecks"])
    assert len(result["transport_requests"]) == 3
    assert all(row["fault_observation_count"] == 0 for row in result["evidence"])
