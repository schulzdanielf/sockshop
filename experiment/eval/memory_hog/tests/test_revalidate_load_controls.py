import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from experiment.eval.memory_hog import revalidate_load_controls as recheck
from experiment.platform.backend.controlled_experiment import ControlledRun, GateBlocked


def make_run():
    return ControlledRun(Mock(), "check", {
        "controlled_campaign": {"service": "user", "fault": "network-latency"},
        "scope": {"namespace": "sock-shop"},
        "experiment": {"id": "test"},
        "timeline": {},
    })


def make_raw(seconds=60):
    values = {
        "traffic": 55, "cpu_cores": .1, "memory_bytes": 100,
        "latency_ms": 100, "target_edge_requests": 55, "target_edge_latency_ms": 100,
        "checkout_orders": 5, "checkout_payment": 5, "checkout_shipping": 5,
        "errors_percent": 0,
    }
    timestamps = (0, seconds / 3, 2 * seconds / 3, seconds)
    return {
        "data": {
            key: {"status": "success", "data": {"result": [
                {"metric": {}, "values": [[t, str(value)] for t in timestamps]},
            ]}} for key, value in values.items()
        },
        "resource_telemetry": {"samples": [{"timestamp": t, "read_duration_seconds": .2}
                                           for t in timestamps]},
    }


def test_revalidation_keeps_thresholds_and_passes_stable_data():
    raw = make_raw()
    before = copy.deepcopy(raw)
    result = recheck.evaluate_window(make_run(), raw, 60, check_stability=True)
    assert result["passed"] is True
    assert result["metrics"]["cpu_cores"]["limit"] == pytest.approx(.02)
    assert result["metrics"]["latency_ms"]["limit"] == 20
    assert result["metrics"]["target_edge_requests"]["limit"] == 11
    assert raw == before


def test_revalidation_uses_the_manifested_user_latency_override():
    raw = make_raw()
    raw["data"]["latency_ms"]["data"]["result"][0]["values"] = [
        [0, "75"], [20, "125"], [40, "75"], [60, "125"],
    ]

    legacy = recheck.evaluate_window(make_run(), raw, 60, check_stability=True)
    prospective = recheck.evaluate_window(
        make_run(), raw, 60, check_stability=True,
        relative_limit_overrides={"latency_ms": .25},
    )
    all_stability = recheck.evaluate_window(
        make_run(), raw, 60, check_stability=True,
        default_relative_limit=.25,
    )

    assert legacy["metrics"]["latency_ms"]["passed"] is False
    assert prospective["metrics"]["latency_ms"]["relative_limit"] == .25
    assert prospective["metrics"]["latency_ms"]["limit"] == 25
    assert prospective["passed"] is True
    assert all_stability["metrics"]["latency_ms"]["relative_limit"] == .25
    assert all_stability["passed"] is True


def test_revalidation_marks_clock_corrupted_cpu_invalid_not_successful():
    raw = make_raw()
    raw["resource_telemetry"]["samples"][1]["read_duration_seconds"] = -8.55
    result = recheck.evaluate_window(make_run(), raw, 60, check_stability=True)
    assert result["passed"] is False
    assert result["metrics"]["cpu_cores"]["status"] == "invalid"
    assert result["summary"]["cpu_cores"]["status"] == "invalid"
    assert result["metrics"]["target_edge_requests"]["passed"] is True
    assert "negative" in result["errors"][0]


def test_revalidation_does_not_hide_real_instability():
    raw = make_raw()
    raw["data"]["latency_ms"]["data"]["result"][0]["values"][-1][1] = "600"
    result = recheck.evaluate_window(make_run(), raw, 60, check_stability=True)
    assert result["passed"] is False
    assert result["metrics"]["latency_ms"]["passed"] is False


@pytest.mark.parametrize("invalid", [None, "clock", "coverage"])
def test_diagnostic_revalidation_preserves_variation_without_forgiving_invalid_data(invalid):
    raw = make_raw()
    raw["data"]["latency_ms"]["data"]["result"][0]["values"][-1][1] = "600"
    if invalid == "clock":
        raw["resource_telemetry"]["samples"][1]["read_duration_seconds"] = -8
    elif invalid == "coverage":
        raw["data"]["target_edge_requests"]["data"]["result"] = []

    result = recheck.evaluate_window(
        make_run(), raw, 60, check_stability=True, stability_blocking=False,
    )

    assert result["passed"] is (invalid is None)
    assert result["stability_passed"] is False
    if invalid != "coverage":
        assert result["metrics"]["latency_ms"]["passed"] is False
    assert bool(result["errors"]) is (invalid is not None)


def test_revalidation_missing_coverage_cannot_pass():
    raw = make_raw()
    raw["data"]["target_edge_requests"]["data"]["result"] = []
    result = recheck.evaluate_window(make_run(), raw, 60, check_stability=True)
    assert result["passed"] is False
    assert any("coverage" in error for error in result["errors"])


def test_query_window_preserves_grid_and_absent_metrics():
    client = Mock()
    client.call_tool_json.return_value = {"status": "success", "data": {
        "resultType": "matrix", "result": [
            {"metric": {"recheck_metric": "traffic"}, "values": [[1, "55"]]},
        ],
    }}
    data, query = recheck.query_window(client, {"traffic": "sum(x)", "errors": "sum(y)"}, "a", "b")
    assert data["traffic"]["data"]["result"][0]["values"] == [[1, "55"]]
    assert data["errors"]["data"]["result"] == []
    assert "recheck_metric" in client.call_tool_json.return_value["data"]["result"][0]["metric"]
    assert query["arguments"]["start"] == "a"
    assert query["arguments"]["end"] == "b"
    assert query["arguments"]["step"] == "5s"


def test_query_failure_is_explicit():
    client = Mock()
    client.call_tool_json.return_value = {"status": "error", "error": "unavailable"}
    with pytest.raises(GateBlocked, match="historical metric requery failed"):
        recheck.query_window(client, {"traffic": "sum(x)"}, "a", "b")


@pytest.mark.parametrize("passed", [False, True])
def test_revalidation_preserves_sources_and_never_automatically_releases_campaign(
    monkeypatch, tmp_path, passed,
):
    state_path = tmp_path / "campaign_state.json"
    state_path.write_text('{"status":"blocked","reason":"original failure"}\n')
    (tmp_path / "campaign_manifest.json").write_text('{"policy":{}}\n')
    original = state_path.read_bytes()
    monkeypatch.setattr(recheck, "OrchestratorEngine", Mock())
    monkeypatch.setattr(recheck, "SqliteStorage", Mock())
    def control(root, output, index, manifest, engine, client, sources):
        return {"index": index, "passed": passed}
    monkeypatch.setattr(recheck, "revalidate_control", control)
    output = recheck.revalidate(tmp_path, "http://127.0.0.1:18082/sse")
    state = json.loads(state_path.read_text())
    summary = json.loads((output / "report.json").read_text())
    assert state["status"] == "blocked"
    assert state["verification_revalidation"]["passed"] is passed
    assert summary["sources_sha256"][str(state_path)] == hashlib.sha256(original).hexdigest()
    assert json.loads((output / "original_campaign_state.json").read_text()) == json.loads(original)
    assert summary["new_loads"] == summary["new_injections"] == 0


def test_failed_requery_leaves_campaign_state_unchanged(monkeypatch, tmp_path):
    state = b'{"status":"blocked"}\n'
    (tmp_path / "campaign_state.json").write_bytes(state)
    (tmp_path / "campaign_manifest.json").write_text('{"policy":{}}\n')
    monkeypatch.setattr(recheck, "OrchestratorEngine", Mock())
    monkeypatch.setattr(recheck, "SqliteStorage", Mock())
    monkeypatch.setattr(recheck, "revalidate_control", Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        recheck.revalidate(tmp_path, "http://127.0.0.1:18082/sse")
    assert (tmp_path / "campaign_state.json").read_bytes() == state


@pytest.mark.parametrize("clock_anomaly", [False, True])
@pytest.mark.parametrize("diagnostic", [False, True])
def test_full_control_reassessment_preserves_windows_and_evidence(
    monkeypatch, tmp_path, clock_anomaly, diagnostic,
):
    directory = tmp_path / "calibrations/no-fault-01"
    artifacts = directory / "artifacts/no-fault-01-user"
    artifacts.mkdir(parents=True)
    phases = ("stabilization_0", "stabilization_1", "baseline", "no_fault_control")
    for i, phase in enumerate(phases, 1):
        raw = make_raw(600 if phase == "no_fault_control" else 60)
        raw["window_start"], raw["window_end"] = f"start-{phase}", f"end-{phase}"
        raw["data"]["memory_usage_bytes"] = copy.deepcopy(raw["data"]["memory_bytes"])
        raw["data"]["memory_limit_bytes"] = copy.deepcopy(raw["data"]["memory_bytes"])
        for value in raw["data"]["memory_limit_bytes"]["data"]["result"][0]["values"]:
            value[1] = str(256 * 2**20)
        if clock_anomaly and phase == "no_fault_control":
            raw["resource_telemetry"]["samples"][1]["read_duration_seconds"] = -8
        recheck._write(artifacts / f"controlled_metrics_{i:04d}_{phase}.json", raw)
    recheck._write(artifacts / "controlled_report.json", {
        "planned": {}, "target_before": {
            "pod": "user-pod", "limits": {"cpu": "300m", "memory": "256Mi"},
        },
    })
    original = {
        "kind": "load_only_rehearsal", "injections_submitted": 0, "ended_at": "ended",
        "load_stopped": {"stopped": True}, "no_fault_window_seconds": 600,
        "measurement_fault": "network-latency", "limitations": [],
        "preparation_policy": {"stable_windows_required": 2, "stabilization_window_seconds": 60},
        "preparation": {
            "health_checks": [{"healthy": True}],
            "stabilization_windows": [{"attempt": 0}, {"attempt": 1}],
        },
        "baseline": {"duration_seconds": 60, "eligible": False},
        "no_fault_window": {"observed_seconds": 600, "health_checks": [{"healthy": True}],
                            "services": {}},
        "services": {"user": {"passed": True}},
    }
    manifest = {"policy": {}}
    if diagnostic:
        manifest["control_acceptance_policy"] = recheck.control_acceptance_policy("diagnostic")
        original["control_acceptance_policy"] = manifest["control_acceptance_policy"]
    recheck._write(directory / "report.json", original)
    output = tmp_path / "revision"
    def query_window(client, queries, start, end):
        data = make_raw(600 if start.endswith("no_fault_control") else 60)["data"]
        if diagnostic and start.endswith("no_fault_control"):
            data["latency_ms"]["data"]["result"][0]["values"][-1][1] = "600"
        return {key: data.get(key, {"status": "success", "data": {"result": []}})
                for key in queries}, {"arguments": {"start": start, "end": end}}
    monkeypatch.setattr(recheck, "query_window", query_window)
    engine, client = Mock(), Mock(sse_url="http://127.0.0.1:18082/sse")
    sources = {}
    result = recheck.revalidate_control(
        tmp_path, output, 1, manifest, engine, client, sources,
    )
    report = json.loads((output / "no-fault-01/report.json").read_text())
    assert report["stabilization_passed"] is True
    assert report["passed"] is (not clock_anomaly)
    assert report["verification_status"] == ("invalid_telemetry" if clock_anomaly else "passed")
    assert result["verification_status"] == report["verification_status"]
    if diagnostic and not clock_anomaly:
        assert report["no_fault_window"]["stability_passed"] is False
        assert report["warnings"][0]["metrics"] == ["latency_ms"]
    assert json.loads((directory / "report.json").read_text()) == original
    assert all(hashlib.sha256(Path(filename).read_bytes()).hexdigest() == digest
               for filename, digest in sources.items())
    engine.load.start_load.assert_not_called()
    engine.chaos.inject.assert_not_called()
