from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, Mock, patch

import yaml

from experiment.platform.backend.analysis.istio_edges import collect_istio_edges
from experiment.platform.backend.api import _select_telemetry_windows
from experiment.platform.backend.models import TelemetryChatRequest
from experiment.platform.backend.observability import otel as otel_module
from experiment.platform.backend.plugins.litmus_plugin import LitmusChaosPlugin
from experiment.platform.backend.plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from experiment.platform.backend.plugins import mcp_client as mcp_client_module
from experiment.platform.backend.rca_knowledge import prepare_check
from experiment.platform.backend.rca_skill import SkillAdditionalCheck
from experiment.platform.backend.analysis.istio_edges import resolve_istio_destination
from experiment.platform.tools.configure_http_access_logs import with_http_log_provider


def test_setup_telemetry_waits_for_concurrent_initialization(monkeypatch):
    monkeypatch.setattr(otel_module, "_configured", False)
    monkeypatch.setattr(otel_module, "_tracer", None)
    monkeypatch.setattr(otel_module, "_hist_duration", None)
    monkeypatch.setattr(otel_module, "_hist_tokens", None)
    monkeypatch.setattr(otel_module, "_counter_override", None)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    install_started = Event()
    allow_install_to_finish = Event()
    install = Mock(
        side_effect=lambda: (
            install_started.set(),
            allow_install_to_finish.wait(timeout=5),
        )
    )
    tracer = object()
    meter = Mock()
    monkeypatch.setattr(otel_module, "_install_sdk", install)
    monkeypatch.setattr(otel_module.trace, "get_tracer", Mock(return_value=tracer))
    monkeypatch.setattr(otel_module.metrics, "get_meter", Mock(return_value=meter))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(otel_module.setup_telemetry)
        assert install_started.wait(timeout=5)
        second = pool.submit(otel_module.setup_telemetry)
        assert not second.done()
        allow_install_to_finish.set()
        first.result(timeout=5)
        second.result(timeout=5)

    install.assert_called_once_with()
    assert otel_module._tracer is tracer
    assert otel_module._configured is True


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


def test_http_fault_identity_query_keeps_unknown_workloads_and_proxy_flags():
    windows = {
        phase: {"start": "2026-11-04T10:00:00Z", "end": "2026-11-04T10:01:00Z"}
        for phase in ("baseline", "fault", "post")
    }
    check = SkillAdditionalCheck(
        kind="http", service="shipping", phase="fault", hypothesis="HTTP",
        expected_support="Proxy abort", expected_refutation="Sem erro",
    )
    _, arguments, _, _ = prepare_check(
        check, "sock-shop", windows, {"shipping"}, [],
        {"prometheus_range_query": {"inputSchema": {}}},
    )
    query = arguments["query"]
    assert 'destination_workload="unknown"' in query
    assert 'destination_service_name="shipping"' in query
    assert "response_flags" in query
    assert "destination_service" in query
    assert 'reporter="destination"' not in query


def test_http_proxy_identity_requires_namespace_verified_fqdn():
    labels = {
        "destination_workload": "unknown",
        "destination_service_name": "shipping",
        "destination_service": "shipping.sock-shop.svc.cluster.local",
    }
    assert resolve_istio_destination(labels, "sock-shop") == "shipping"
    assert resolve_istio_destination(
        {**labels, "destination_service": "shipping.other.svc.cluster.local"}, "sock-shop"
    ) is None
    assert labels["destination_workload"] == "unknown"


def test_http_log_provider_preserves_other_mesh_settings():
    mesh = {"defaultProviders": {"metrics": ["prometheus"]},
            "extensionProviders": [{"name": "other", "envoyFileAccessLog": {"path": "/tmp/other"}}]}
    updated = with_http_log_provider(mesh)
    assert updated["defaultProviders"] == mesh["defaultProviders"]
    assert updated["extensionProviders"][0] == mesh["extensionProviders"][0]
    assert with_http_log_provider(updated) == updated
    assert len(mesh["extensionProviders"]) == 1
    fields = updated["extensionProviders"][1]["envoyFileAccessLog"]["logFormat"]["labels"]
    assert "response_flags" in fields and "response_code_details" in fields
    assert "path" not in fields and "authorization" not in fields


def test_http_access_parser_is_scoped_to_envoy_provider():
    root = Path(__file__).resolve().parents[3]
    manifest = yaml.safe_load(
        (root / "deploy/kubernetes/manifests-monitoring/27-otel-collector-configmap.yaml").read_text()
    )
    config = yaml.safe_load(manifest["data"]["config.yaml"])
    parser = next(operator for operator in config["receivers"]["filelog"]["operators"]
                  if operator.get("id") == "http_access_json")
    assert parser["parse_to"] == "attributes.http_access"
    assert "log_source" in parser["if"] and "istio_http_access" in parser["if"]
    assert parser["on_error"] == "send"


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
