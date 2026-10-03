from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

from experiment.platform.backend.analysis.istio_edges import collect_istio_edges
from experiment.platform.backend.api import _select_telemetry_windows
from experiment.platform.backend.models import TelemetryChatRequest
from experiment.platform.backend.plugins.litmus_plugin import LitmusChaosPlugin
from experiment.platform.backend.plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from experiment.platform.backend.plugins import mcp_client as mcp_client_module


def test_telemetry_chat_defaults_to_fault_and_selects_baseline_separately():
    observation_window = {
        "window": {"start": "run-start", "end": "run-end"},
        "phases": {
            "baseline": {"start": "baseline-start", "end": "baseline-end"},
            "fault": {"start": "fault-start", "end": "fault-end"},
        },
    }

    request = TelemetryChatRequest(query="memória")
    assert request.window_scope == "fault"
    assert _select_telemetry_windows(observation_window, request.window_scope) == (
        observation_window["phases"]["fault"],
        observation_window["phases"]["baseline"],
    )
    assert _select_telemetry_windows(observation_window, "run") == (
        observation_window["window"],
        None,
    )


def test_istio_collection_reports_unknown_source_series():
    client = Mock()
    client.call_tool_json.return_value = {
        "data": {
            "result": [
                {"metric": {"source_workload": "unknown"}, "values": []},
                {"metric": {"source_workload": "front-end"}, "values": []},
            ]
        }
    }
    result = collect_istio_edges(client, "start", "end", 15)
    assert result["collection"]["unknown_source_series"]["istio_requests_total"] == {
        "unknown": 1,
        "total": 2,
    }


def test_empty_tempo_search_is_explicit():
    plugin = MCPTempoTracesPlugin()
    client = Mock()
    client.call_tool_json.return_value = {"traces": []}
    with patch.object(plugin, "_client", return_value=client):
        result = plugin.collect_window({}, {"mcp_sse_url": "http://tempo"}, "s", "e")
    assert result["count"] == 0
    assert result["collection"]["empty_reason"] == "no_traces_in_window"


def test_manifest_lifecycle_is_persisted():
    plugin = LitmusChaosPlugin()
    completed = Mock(returncode=0, stdout="applied", stderr="")
    with patch("subprocess.run", return_value=completed):
        handle = plugin.inject(
            {}, {"manifest_path": "/tmp/fault.yaml", "namespace": "sock-shop"}
        )
        plugin.stop({}, handle)
        artifact = plugin.collect_artifacts({}, handle)
    assert artifact["apply"]["returncode"] == 0
    assert artifact["cleanup"]["returncode"] == 0
    assert artifact["injected_at"]
    assert artifact["stopped_at"]


def test_mcp_discovery_has_rpc_span(monkeypatch):
    client = mcp_client_module.MCPToolClient("http://mcp.example/sse")
    tracer = MagicMock()
    span = tracer.start_as_current_span.return_value.__enter__.return_value
    monkeypatch.setattr(mcp_client_module, "get_tracer", lambda: tracer)

    async def _tools():
        return [{"name": "cpu", "description": "CPU", "inputSchema": {}}]

    monkeypatch.setattr(client, "_list_tools_async", _tools)

    assert client.list_tools()[0]["name"] == "cpu"
    span.set_attribute.assert_any_call("rpc.system.name", "mcp")
    span.set_attribute.assert_any_call("rpc.method", "tools/list")
    span.set_attribute.assert_any_call("rca.mcp.tools.count", 1)


def test_mcp_tool_span_records_semantic_tool_call_id(monkeypatch):
    client = mcp_client_module.MCPToolClient("http://mcp.example/sse")
    tracer = MagicMock()
    span = tracer.start_as_current_span.return_value.__enter__.return_value
    monkeypatch.setattr(mcp_client_module, "get_tracer", lambda: tracer)

    async def _call_tool(_name, _arguments):
        return {"structuredContent": {"ok": True}}

    monkeypatch.setattr(client, "_call_tool_async", _call_tool)

    result = client.call_tool_json(
        "get_cpu_saturation_top",
        {"start": "start", "end": "end"},
        tool_call_id="call_cpu_1",
        tool_description="CPU saturation investigator",
    )

    assert result == {"ok": True}
    span.set_attribute.assert_any_call("gen_ai.operation.name", "execute_tool")
    span.set_attribute.assert_any_call("gen_ai.tool.name", "get_cpu_saturation_top")
    span.set_attribute.assert_any_call("gen_ai.tool.type", "function")
    span.set_attribute.assert_any_call("gen_ai.tool.call.id", "call_cpu_1")
    span.set_attribute.assert_any_call(
        "gen_ai.tool.description", "CPU saturation investigator"
    )
