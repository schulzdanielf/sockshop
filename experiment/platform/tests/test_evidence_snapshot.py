from __future__ import annotations

import asyncio

import pytest

from experiment.platform.backend.agent_console import rca_reference_phases
from experiment.platform.backend.engine import OrchestratorEngine
from experiment.platform.backend.plugins import mcp_evidence_snapshot_plugin as snapshot_module
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.plugins.mcp_evidence_snapshot_plugin import (
    MCPRcaEvidenceSnapshotPlugin,
    SnapshotPreferredMCPClient,
)
from experiment.platform.backend.analysis.system_card import SystemCardError
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage


class _FakeMCPClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []

    def call_tool_json(self, name, arguments=None, **_kwargs):
        self.calls.append((name, arguments))
        return {
            "status": "partial",
            "budget": {"max_total_lines": arguments["max_total_lines"]},
            "services": {service: {"status": "ok"} for service in arguments["services"]},
        }


def _observation_window():
    return {
        "phases": {
            "baseline": {
                "start": "2026-10-02T10:00:00Z",
                "end": "2026-10-02T10:01:00Z",
            },
            "warmup": {
                "start": "2026-10-02T10:01:00Z",
                "end": "2026-10-02T10:02:00Z",
            },
            "fault": {
                "start": "2026-10-02T10:02:00Z",
                "end": "2026-10-02T10:03:00Z",
            },
            "post": {
                "start": "2026-10-02T10:03:00Z",
                "end": "2026-10-02T10:04:00Z",
            },
        }
    }


def test_post_stabilization_baseline_is_preserved_in_rca_and_snapshot():
    window = _observation_window()
    window["phase_sources"] = {"baseline": "after_load_stabilization"}
    expected = window["phases"]["baseline"]
    phases, source = rca_reference_phases(window["phases"], baseline_source="after_load_stabilization")
    selected, snapshot_source = snapshot_module._reference_windows(window)
    assert phases["baseline"] == selected["baseline"] == expected
    assert source == snapshot_source == "after_load_stabilization"


def test_snapshot_plugin_uses_card_services_and_rca_reference_windows(monkeypatch):
    monkeypatch.setattr(
        snapshot_module,
        "load_system_card",
        lambda _namespace: {"services": {"user": {}, "orders": {}}},
    )
    clients = []

    def _client_factory(**kwargs):
        client = _FakeMCPClient(**kwargs)
        clients.append(client)
        return client

    plugin = MCPRcaEvidenceSnapshotPlugin(client_factory=_client_factory)
    observation_window = _observation_window()
    snapshot = plugin.capture(
        {"run_id": "run-test"},
        {"metrics": {"mcp_sse_url": "http://mcp/sse"}},
        "sock-shop",
        observation_window,
        {"max_total_lines": 600, "timeout_seconds": 4},
    )
    expected_phases, reference_source = rca_reference_phases(
        observation_window["phases"]
    )

    assert clients[0].init_kwargs == {
        "sse_url": "http://mcp/sse",
        "timeout_seconds": 4,
    }
    name, arguments = clients[0].calls[0]
    assert name == "capture_run_log_snapshot"
    assert arguments["services"] == ["orders", "user"]
    assert arguments["max_total_lines"] == 600
    assert arguments["baseline_start"] == expected_phases["baseline"]["start"]
    assert arguments["baseline_end"] == expected_phases["baseline"]["end"]
    assert snapshot["phase_windows"] == {
        key: expected_phases[key] for key in ("baseline", "fault", "post")
    }
    assert snapshot["reference_source"] == reference_source
    assert snapshot["status"] == "partial"


def test_snapshot_plugin_treats_missing_or_invalid_service_card_as_unavailable(monkeypatch):
    monkeypatch.setattr(
        snapshot_module,
        "load_system_card",
        lambda _namespace: (_ for _ in ()).throw(SystemCardError("bad card")),
    )

    snapshot = MCPRcaEvidenceSnapshotPlugin().capture(
        {"run_id": "run-test"},
        {"metrics": {"mcp_sse_url": "http://mcp/sse"}},
        "sock-shop",
        _observation_window(),
        {"max_total_lines": 100},
    )

    assert snapshot["status"] == "tool_unavailable"
    assert snapshot["phase_windows"]["fault"] == _observation_window()["phases"]["fault"]


def test_snapshot_preferred_client_uses_only_exact_compatible_windows():
    class _LiveClient:
        def __init__(self):
            self.calls = []

        def list_tools(self):
            return [{"name": "check_service_logs"}]

        def call_tool_json(self, name, arguments=None, **_kwargs):
            self.calls.append((name, arguments))
            return {"status": "ok", "evidence_source": "live"}

    phases = {
        "baseline": {"start": "b0", "end": "b1"},
        "fault": {"start": "f0", "end": "f1"},
        "post": {"start": "p0", "end": "p1"},
    }
    cached = {
        "status": "partial",
        "phases": {phase: {"status": "ok"} for phase in phases},
    }
    live = _LiveClient()
    client = SnapshotPreferredMCPClient(
        live,
        {
            "schema_version": 1,
            "namespace": "sock-shop",
            "phase_windows": phases,
            "services": {"user": cached},
        },
    )
    arguments = {
        "service": "user",
        "namespace": "sock-shop",
        "baseline_start": "b0",
        "baseline_end": "b1",
        "fault_start": "f0",
        "fault_end": "f1",
        "post_start": "p0",
        "post_end": "p1",
    }

    from_snapshot = client.call_tool_json("check_service_logs", arguments)
    from_live = client.call_tool_json(
        "check_service_logs", {**arguments, "fault_end": "f2"}
    )

    assert from_snapshot["evidence_source"] == "snapshot"
    assert from_snapshot["phases"] == cached["phases"]
    assert from_live["evidence_source"] == "live"
    assert len(live.calls) == 1


def test_snapshot_preferred_client_uses_live_when_snapshot_capture_failed():
    class _LiveClient:
        def __init__(self):
            self.calls = []

        def call_tool_json(self, name, arguments=None, **_kwargs):
            self.calls.append((name, arguments))
            return {"status": "ok", "evidence_source": "live"}

    live = _LiveClient()
    client = SnapshotPreferredMCPClient(
        live,
        {
            "schema_version": 1,
            "namespace": "sock-shop",
            "phase_windows": {
                "baseline": {"start": "b0", "end": "b1"},
                "fault": {"start": "f0", "end": "f1"},
            },
            "services": {
                "user": {"status": "tool_error", "phases": {"baseline": {}, "fault": {}}}
            },
        },
    )

    result = client.call_tool_json(
        "check_service_logs",
        {
            "service": "user",
            "namespace": "sock-shop",
            "baseline_start": "b0",
            "baseline_end": "b1",
            "fault_start": "f0",
            "fault_end": "f1",
        },
    )

    assert result["evidence_source"] == "live"
    assert len(live.calls) == 1


def test_engine_snapshot_capture_is_opt_in_and_persists_references():
    class _SnapshotPort:
        def __init__(self):
            self.calls = []

        def capture(self, *args):
            self.calls.append(args)
            return {"schema_version": 1, "status": "ok", "namespace": "sock-shop"}

    class _Storage:
        def __init__(self):
            self.saved = []

        def save_artifact(self, run_id, name, content):
            self.saved.append((run_id, name, content))
            return f"object_store/{run_id}/{name}.json"

    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.evidence_snapshot = _SnapshotPort()
    engine.storage = _Storage()
    references = {
        "metrics_raw": "metrics.json",
        "traces_raw": "traces.json",
        "observation_window": "window.json",
    }
    args = {
        "run_id": "run-test",
        "context": {"run_id": "run-test"},
        "observability_config": {},
        "namespace": "sock-shop",
        "observation_window": _observation_window(),
        "artifact_paths": references,
    }
    persist_snapshot = getattr(engine, "_persist_rca_evidence_snapshot")

    assert persist_snapshot(
        **args, capture_config={"enabled": False}
    ) is None
    assert not engine.evidence_snapshot.calls
    assert not engine.storage.saved

    snapshot = persist_snapshot(
        **args, capture_config={"enabled": True}
    )

    assert snapshot["references"] == {
        "metrics_raw": "metrics.json",
        "traces_raw": "traces.json",
        "observation_window": "window.json",
    }
    assert references["rca_evidence_snapshot"] == "object_store/run-test/rca_evidence_snapshot.json"
    assert engine.storage.saved[0][1] == "rca_evidence_snapshot"


def test_engine_snapshot_capture_failure_does_not_escape_or_save_partial_data():
    class _FailingSnapshotPort:
        def capture(self, *_args):
            raise TimeoutError("endpoint details must not be persisted")

    class _Storage:
        def save_artifact(self, *_args):
            raise AssertionError("failed capture must not persist a partial snapshot")

    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.evidence_snapshot = _FailingSnapshotPort()
    engine.storage = _Storage()

    result = getattr(engine, "_persist_rca_evidence_snapshot")(
        run_id="run-test",
        context={},
        observability_config={},
        namespace="sock-shop",
        observation_window=_observation_window(),
        capture_config={"enabled": True},
        artifact_paths={},
    )

    assert result is None


def test_storage_reads_latest_artifact_and_returns_none_when_missing(tmp_path):
    storage = SqliteStorage(tmp_path / "platform.db", tmp_path / "object_store")

    assert storage.get_artifact("run-test", "rca_evidence_snapshot") is None
    storage.save_artifact("run-test", "rca_evidence_snapshot", {"schema_version": 0})
    storage.save_artifact("run-test", "rca_evidence_snapshot", {"schema_version": 1})

    assert storage.get_artifact("run-test", "rca_evidence_snapshot") == {
        "schema_version": 1
    }


def test_mcp_client_supports_a_hard_per_call_deadline(monkeypatch):
    client = MCPToolClient("http://unused/sse")

    async def _never_finishes(_name, _arguments):
        await asyncio.Future()

    monkeypatch.setattr(client, "_call_tool_async", _never_finishes)

    with pytest.raises(asyncio.TimeoutError):
        client.call_tool_json("capture_run_log_snapshot", timeout_seconds=0.01)
