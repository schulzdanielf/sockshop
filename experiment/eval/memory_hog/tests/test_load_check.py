import json
import threading
import time as time_module
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import load_check
from experiment.platform.backend.controlled_experiment import SUPPLEMENTAL_PREPARATION_POLICY


@pytest.mark.parametrize(
    "failure",
    [
        None, "collect", "traces", "cleanup", "sampler", "slow_queries",
        "unstable_window", "preparation_deadline", "control_variability", "control_coverage",
        "control_health",
    ],
)
@pytest.mark.parametrize("level", ["medium", "rag"])
@pytest.mark.parametrize(
    ("duration_seconds", "control_mode"),
    [(None, "blocking"), (600, "blocking"), (600, "diagnostic")],
)
def test_single_load_rehearsal_never_injects_and_always_cleans_up(
    monkeypatch, tmp_path, failure, level, duration_seconds, control_mode,
):
    if failure in {"control_variability", "control_coverage", "control_health"} and duration_seconds is None:
        pytest.skip("control-specific failures need a fixed-duration control")
    if (
        duration_seconds is not None and failure is not None
        and failure not in {"control_variability", "control_coverage", "control_health"}
        and not (control_mode == "diagnostic" and failure in {
            "traces", "cleanup", "sampler", "preparation_deadline",
        })
    ):
        pytest.skip("the fixed-duration control window is covered by the passing-path test")
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, level, 42, {})
    cfg["controlled_campaign"]["preparation_policy"] = dict(
        SUPPLEMENTAL_PREPARATION_POLICY,
    )
    original_config = json.dumps(cfg, sort_keys=True)
    engine = Mock()
    engine._stop_flags = {}
    engine.load.start_load.return_value = {"remote_pid": 42}
    engine.load.status.return_value = {"state": "running"}
    engine.load.stop_load.return_value = {"stopped": failure != "cleanup"}
    engine.load.collect_summary.return_value = {}
    clock = [0.]
    wall_clock_rate = 0.997 if duration_seconds is not None and failure is None else 1.0
    wall_clock_epoch = time_module.time()
    monkeypatch.setattr(
        load_check,
        "time",
        Mock(
            monotonic=lambda: clock[0],
            time=lambda: wall_clock_epoch + clock[0] * wall_clock_rate,
        ),
    )
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
        run.raw = {
            **{f"stabilization_{i}": {"data": {}} for i in range(9)},
            "no_fault_control": {"data": {}},
        }
        run.health.return_value = {"healthy": True}
        if failure == "control_health":
            run.health.side_effect = lambda: {"healthy": clock[0] < 500}
        run.collect.return_value = {
            "memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000},
        }
        if failure == "control_coverage":
            def require_coverage(summary, seconds):
                if seconds == duration_seconds:
                    raise load_check.GateBlocked("missing control coverage")
            run.require_coverage.side_effect = require_coverage
        if failure == "collect":
            run.collect.side_effect = RuntimeError("metrics unavailable")
        elif failure == "sampler":
            run.sample_cgroup.side_effect = RuntimeError("cgroup read unavailable")
        elif failure == "slow_queries":
            sampled_during_collect = threading.Event()
            run.sample_cgroup.side_effect = sampled_during_collect.set

            def slow_collect(*args):
                sampled_during_collect.clear()
                before = run.sample_cgroup.call_count
                assert sampled_during_collect.wait(1)
                assert run.sample_cgroup.call_count > before
                return {"memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000}}
            run.collect.side_effect = slow_collect
        elif failure == "preparation_deadline":
            def collect_with_overhead(*args):
                clock[0] += .01
                return {
                    "memory_usage_bytes": {"max": 100},
                    "memory_limit_bytes": {"mean": 1000},
                }
            run.collect.side_effect = collect_with_overhead
        created.append(run)
        return run

    monkeypatch.setattr(load_check, "ControlledRun", controlled_run)
    stability_calls = [0]

    def stability_result(*args, **kwargs):
        stability_calls[0] += 1
        if failure == "control_variability" and stability_calls[0] > 6:
            return {"cpu": {"passed": False}}
        passed = (
            failure not in {"unstable_window", "preparation_deadline"}
            or failure == "unstable_window" and stability_calls[0] > 3
        )
        return {"cpu": {"passed": passed}}

    stability_metrics = Mock(side_effect=stability_result)
    monkeypatch.setattr(load_check, "stability_metrics", stability_metrics)
    resource_environment = Mock(return_value={})
    monkeypatch.setattr(load_check, "resource_environment", resource_environment)
    client = Mock()
    client.call_tool_json.return_value = {"traces": [] if failure == "traces" else [{"traceID": "fresh"}]}
    monkeypatch.setattr(load_check, "MCPToolClient", Mock(return_value=client))
    directory = tmp_path / "load-check-test"
    run_kwargs = {
        "duration_seconds": duration_seconds,
        "services": ("front-end", "orders", "user"),
        "measurement_fault": (
            "network-latency" if duration_seconds is not None else "cpu-hog"
        ),
        "control_window_stability_mode": control_mode,
    }
    if duration_seconds is not None:
        run_kwargs["stability_default_relative_limit"] = .25
        run_kwargs["stability_relative_limit_overrides"] = {
            "user": {"latency_ms": .25},
        }
    if failure in {"collect", "cleanup", "sampler", "control_health"}:
        with pytest.raises(load_check.GateBlocked if failure == "control_health" else RuntimeError):
            load_check.run_load_check(cfg, directory, **run_kwargs)
    else:
        result = load_check.run_load_check(cfg, directory, **run_kwargs)
        assert result["passed"] is (
            failure in {None, "slow_queries", "unstable_window"}
            or failure == "control_variability" and control_mode == "diagnostic"
        )
    engine.load.start_load.assert_called_once()
    load_context = engine.load.start_load.call_args.args[0]
    assert load_context["run_id"].startswith(f"{directory.name}-")
    engine.load.stop_load.assert_called_once()
    engine.chaos.inject.assert_not_called()
    assert all(call.args[0] != "cpu-hog" for call in resource_environment.call_args_list)
    assert all(not run.run.called and not run.intervene.called for run in created)
    assert json.dumps(cfg, sort_keys=True) == original_config
    report = json.loads((directory / "report.json").read_text())
    assert report["injections_submitted"] == 0
    assert report["users"] == 30
    assert report["no_fault_window_seconds"] == duration_seconds
    assert report["control_acceptance_policy"] == load_check.control_acceptance_policy(control_mode)
    if failure == "control_variability":
        assert report["no_fault_window"]["stability_passed"] is False
        if control_mode == "diagnostic":
            assert len(report["warnings"]) == 3
            assert all(warning["metrics"] == ["cpu"] for warning in report["warnings"])
        else:
            assert report["warnings"] == []
    assert report["stability_policy"] == {
        "default_relative_limit": .25 if duration_seconds is not None else .20,
        "relative_limit_overrides": (
            {"user": {"latency_ms": .25}} if duration_seconds is not None else {}
        ),
    }
    if failure not in {"collect", "sampler"}:
        assert report["preparation_policy"] == SUPPLEMENTAL_PREPARATION_POLICY
        assert report["baseline"]["duration_seconds"] == 180
        expected_windows = {
            "unstable_window": 3,
            "preparation_deadline": 6,
        }.get(failure, 2)
        windows = report["preparation"]["stabilization_windows"]
        assert len(windows) == expected_windows
        assert all(
            window["observed_seconds"] == 60
            for window in windows
        )
        expected_passes = (
            [False, True, True] if failure == "unstable_window"
            else [False] * 6 if failure == "preparation_deadline"
            else [True, True]
        )
        assert [window["passed"] for window in windows] == expected_passes
        assert [window["consecutive_stable_windows"] for window in windows] == (
            [0, 1, 2] if failure == "unstable_window"
            else [0] * 6 if failure == "preparation_deadline"
            else [1, 2]
        )
        if failure == "preparation_deadline":
            assert report["preparation"]["elapsed_seconds"] == pytest.approx(540.18)
            assert report["preparation"]["timed_out"] is True
        else:
            assert report["preparation"]["elapsed_seconds"] == 300 + (
                60 if failure == "unstable_window" else 0
            )
            assert report["preparation"]["timed_out"] is False
        assert all(window["end"] <= following["start"] for window, following in zip(
            windows,
            report["preparation"]["stabilization_windows"][1:],
        ))
        expected_stability_calls = expected_windows * 3 + (
            3 if duration_seconds is not None
            and failure not in {"control_coverage", "control_health"} else 0
        )
        assert len(stability_metrics.call_args_list) == expected_stability_calls
        assert all(
            call.kwargs["include_target_edge"] is (duration_seconds is not None)
            for call in stability_metrics.call_args_list
        )
        assert all(
            call.kwargs["default_relative_limit"]
            == (.25 if duration_seconds is not None else .20)
            for call in stability_metrics.call_args_list
        )
        assert [
            call.kwargs["relative_limit_overrides"]
            for call in stability_metrics.call_args_list
        ] == [
            {"latency_ms": .25}
            if duration_seconds is not None and index % 3 == 2
            else None
            for index in range(expected_stability_calls)
        ]
    if "no_fault_window" in report:
        assert duration_seconds is not None
        window = report["no_fault_window"]
        if failure != "control_health":
            assert window["observed_seconds"] >= duration_seconds
            assert window["monotonic_observed_seconds"] >= duration_seconds
        assert window["max_wall_clock_extension_seconds"] == 30
        if failure is None:
            assert window["wall_clock_extension_seconds"] > 1
    elif duration_seconds is not None:
        assert failure in {"collect", "sampler"}
    assert report["load_stopped"]["stopped"] is (failure != "cleanup")
    if failure in {"collect", "traces", "cleanup", "sampler", "control_coverage", "control_health"}:
        assert report["passed"] is False


@pytest.mark.parametrize("mode", ["", "ignore", None, [], True])
def test_invalid_control_acceptance_mode_is_rejected_before_load(tmp_path, mode):
    with pytest.raises(ValueError, match="stability mode"):
        load_check.run_load_check({}, tmp_path / "control", control_window_stability_mode=mode)
    assert not (tmp_path / "control").exists()


def test_diagnostic_mode_requires_a_fixed_duration(tmp_path):
    with pytest.raises(ValueError, match="fixed-duration"):
        load_check.run_load_check(
            {}, tmp_path / "control", control_window_stability_mode="diagnostic",
        )
    assert not (tmp_path / "control").exists()


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
