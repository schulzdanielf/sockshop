import json
import time
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import load_check


@pytest.mark.parametrize("failure", [None, "collect", "traces", "cleanup", "sampler", "slow_queries"])
@pytest.mark.parametrize("level", ["medium", "rag"])
def test_single_load_rehearsal_never_injects_and_always_cleans_up(monkeypatch, tmp_path, failure, level):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, level, 42, {})
    original_config = json.dumps(cfg, sort_keys=True)
    engine = Mock()
    engine._stop_flags = {}
    engine.load.start_load.return_value = {"remote_pid": 42}
    engine.load.status.return_value = {"state": "running"}
    engine.load.stop_load.return_value = {"stopped": failure != "cleanup"}
    engine.load.collect_summary.return_value = {}
    clock = [0.]
    monkeypatch.setattr(load_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(load_check, "SAMPLE_INTERVAL_SECONDS", .01)
    engine._sleep_interruptible.side_effect = lambda flag, seconds: clock.__setitem__(0, clock[0] + seconds)
    monkeypatch.setattr(load_check, "OrchestratorEngine", Mock(return_value=engine))
    monkeypatch.setattr(load_check, "SqliteStorage", Mock())
    monkeypatch.setattr(campaign, "require_environment", Mock())
    created = []

    def controlled_run(engine, run_id, spec):
        assert "knowledge_campaign" not in spec
        run = Mock()
        run.run_id, run.service = run_id, spec["controlled_campaign"]["service"]
        run.target = {"container_id": "original", "limits": {}}
        run.application_container_status.return_value = {"container_id": "original"}
        run.spec = {"observability": {"metrics": {"mcp_sse_url": "http://mcp/sse"}}}
        run.raw = {f"stabilization_{i}": {"data": {}} for i in range(9)}
        run.health.return_value = {"healthy": True}
        run.collect.return_value = {
            "memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000},
        }
        if failure == "collect":
            run.collect.side_effect = RuntimeError("metrics unavailable")
        elif failure == "sampler":
            run.sample_cgroup.side_effect = RuntimeError("cgroup read unavailable")
        elif failure == "slow_queries":
            def slow_collect(*args):
                before = run.sample_cgroup.call_count
                time.sleep(.04)
                assert run.sample_cgroup.call_count > before
                return {"memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000}}
            run.collect.side_effect = slow_collect
        created.append(run)
        return run

    monkeypatch.setattr(load_check, "ControlledRun", controlled_run)
    monkeypatch.setattr(load_check, "stability_metrics", lambda data: {"cpu": {"passed": True}})
    monkeypatch.setattr(load_check, "resource_environment", Mock(return_value={}))
    client = Mock()
    client.call_tool_json.return_value = {"traces": [] if failure == "traces" else [{"traceID": "fresh"}]}
    monkeypatch.setattr(load_check, "MCPToolClient", Mock(return_value=client))
    directory = tmp_path / "load-check-test"
    if failure in {"collect", "cleanup", "sampler"}:
        with pytest.raises(RuntimeError):
            load_check.run_load_check(cfg, directory)
    else:
        result = load_check.run_load_check(cfg, directory)
        assert result["passed"] is (failure in {None, "slow_queries"})
    engine.load.start_load.assert_called_once()
    engine.load.stop_load.assert_called_once()
    engine.chaos.inject.assert_not_called()
    assert all(not run.run.called and not run.intervene.called for run in created)
    assert json.dumps(cfg, sort_keys=True) == original_config
    report = json.loads((directory / "report.json").read_text())
    assert report["injections_submitted"] == 0
    assert report["users"] == 30
    assert report["load_stopped"]["stopped"] is (failure != "cleanup")
    if failure and failure != "slow_queries":
        assert report["passed"] is False


def test_late_recovery_check_rejects_ineligible_source_before_load(tmp_path):
    source_report = tmp_path / "source-report.json"
    effective_spec = tmp_path / "effective-spec.json"
    source_report.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    effective_spec.write_text(json.dumps({
        "controlled_campaign": {"fault": "container-kill", "service": "orders"},
    }), encoding="utf-8")
    output = tmp_path / "late-recovery"

    with pytest.raises(ValueError, match="not eligible"):
        load_check.run_late_operational_recovery_check(
            "run-source",
            source_report,
            effective_spec,
            output,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("attempt_number", "latency_override"),
    [(3, None), (4, 370.0)],
)
def test_late_recovery_check_starts_exact_container_cgroup_sampler_before_load(
    monkeypatch, tmp_path, attempt_number, latency_override,
):
    source_target = {
        "pod": "orders-123",
        "uid": "pod-uid",
        "container": "orders",
        "container_id": "container-old",
        "restart_count": 0,
    }
    source_event = {
        "restart_observed": True,
        "container_id_after_restart": "container-new",
        "restart_count_after_signal": 1,
        "target": {
            "pod_uid": "pod-uid",
            "container_id": "container-old",
            "restart_count": 0,
        },
    }
    source_report = tmp_path / "source-report.json"
    source_report.write_text(json.dumps({
        "status": "aborted",
        "fault": "container-kill",
        "service": "orders",
        "target_before": source_target,
        "container_kill_event": source_event,
        "gates": {
            "recovery": {"passed": False},
            "operational_recovery": {
                "passed": False,
                "target": {"container_id": "container-new", "restart_count": 1},
            },
            "mechanism_observed": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "observations": [{
            "phase": "baseline",
            "summary": {
                key: {"status": "ok"}
                for key in ("traffic", "cpu_cores", "memory_bytes", "errors_percent", "latency_ms")
            },
        }],
    }), encoding="utf-8")
    effective_spec = tmp_path / "effective-spec.json"
    effective_spec.write_text(json.dumps({
        "controlled_campaign": {
            "fault": "container-kill",
            "service": "orders",
            "resilience": {"operational_timeout_seconds": 600, "stable_seconds": 120},
        },
        "load_profile": {
            "command": "kubectl exec deploy/locust-web -- locust",
            "locust_file": "/mnt/locust/locustfile.py",
            "host": "http://front-end",
            "users": 30,
            "spawn_rate": 5,
        },
        "observability": {"metrics": {}},
    }), encoding="utf-8")

    events = []
    engine = Mock()
    engine._runtime_handles = {}
    engine._stop_flags = {}
    engine.load.start_load.side_effect = lambda *args: (
        events.append("load") or {"controlled_remote": True}
    )
    engine.load.stop_load.return_value = {"stopped": True}
    engine.load.collect_summary.return_value = {}
    run = Mock()
    run.target = {}
    run.baseline = {}
    run.report = {"gates": {"late_operational_recovery": {"passed": True}}}
    run.application_container_status.return_value = {
        "container_id": "container-new",
        "restart_count": 1,
        "ready": True,
        "running": True,
    }
    run.health.return_value = {"healthy": True}
    run.start_cgroup_sampling.side_effect = lambda: events.append("cgroup")
    run.operational_recovery.return_value = True
    monkeypatch.setattr(load_check, "OrchestratorEngine", Mock(return_value=engine))
    monkeypatch.setattr(load_check, "SqliteStorage", Mock())
    monkeypatch.setattr(load_check, "ControlledRun", Mock(return_value=run))

    report = load_check.run_late_operational_recovery_check(
        "run-source",
        source_report,
        effective_spec,
        tmp_path / "late-recovery",
        attempt_number=attempt_number,
        latency_p95_limit_override_ms=latency_override,
    )

    assert events == ["cgroup", "load"]
    run.start_cgroup_sampling.assert_called_once()
    assert report["late_recovery_policy"]["attempt"] == attempt_number
    assert report["late_recovery_policy"]["resource_measurement_source"] == "cgroup-v2"
    assert report["late_recovery_policy"]["resource_sampling_started_before_load"] is True
    assert report["late_recovery_policy"]["latency_p95_limit_override_ms"] == latency_override
    if latency_override is None:
        run.operational_recovery.assert_called_once_with(
            gate_name="late_operational_recovery", timeout_seconds=600,
        )
    else:
        assert report["late_recovery_policy"]["amendment_version"] == (
            "controlled-late-operational-recovery-check-v3"
        )
        run.operational_recovery.assert_called_once_with(
            gate_name="late_operational_recovery",
            timeout_seconds=600,
            latency_p95_limit_override_ms=370.0,
        )
    assert report["passed"] is True
    engine.chaos.inject.assert_not_called()


@pytest.mark.parametrize("args", [
    ["--load-check", "--run"], ["--load-check", "--preflight"],
    ["--load-check", "--resume", "campaign"],
])
def test_load_check_cannot_be_combined_with_campaign_execution(args):
    with pytest.raises(SystemExit) as exc:
        campaign.main(args)
    assert exc.value.code == 2
