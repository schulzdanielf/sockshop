from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from experiment.eval.memory_hog import autonomous_campaign as autonomous


class FakeProcess:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def create_pilot_campaign(tmp_path: Path):
    return autonomous._create_campaign(
        "pilot",
        tmp_path / "pilot",
        max_infra_retries=2,
        run_timeout_seconds=1800,
        recovery_timeout_seconds=300,
        pilot_report=None,
    )


def test_simulation_is_read_only_and_never_runs_preflight(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(autonomous, "CAMPAIGN_ROOT", tmp_path)
    monkeypatch.setattr(
        autonomous.forty_faults,
        "preflight",
        lambda _config: pytest.fail("simulation must not touch Kubernetes"),
    )

    assert autonomous.main(["--simulate"]) == 0

    output = capsys.readouterr().out
    assert "40 unique sequential scenarios" in output
    assert "360 min minimum" in output
    assert "No cluster/API calls" in output
    assert list(tmp_path.iterdir()) == []


def test_campaign_persists_unique_scenarios_config_and_checkpoint(tmp_path):
    campaign, scenarios, repository = create_pilot_campaign(tmp_path)

    assert campaign["mode"] == "pilot"
    assert len(scenarios) == 1
    assert scenarios[0]["scenario_id"]
    assert scenarios[0]["cell"] == autonomous.forty_faults.CELLS[0]
    assert (tmp_path / "pilot" / "config.yaml").is_file()
    assert (tmp_path / "pilot" / "events.jsonl").is_file()
    checkpoint = json.loads(
        (tmp_path / "pilot" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["next_order"] == 0

    loaded_campaign, config, loaded_scenarios, _ = autonomous._load_campaign(
        tmp_path / "pilot"
    )
    assert loaded_campaign["campaign_id"] == campaign["campaign_id"]
    assert config["test_experiments"] == [autonomous.forty_faults.CELLS[0]]
    assert loaded_scenarios[0]["scenario_id"] == scenarios[0]["scenario_id"]
    assert repository.save_report()["summary"]["incomplete_count"] == 1


def test_completed_scenario_resume_skips_all_execution(tmp_path, monkeypatch):
    _campaign, scenarios, repository = create_pilot_campaign(tmp_path)
    scenario = scenarios[0]
    scenario["state"] = "completed"
    scenario["spec"] = {"experiment": {"id": "saved-run"}}
    scenario["run_id"] = "run-saved"
    directory = autonomous._scenario_dir(tmp_path / "pilot", scenario)
    for filename in (
        "run.json",
        "events.json",
        "run_features.json",
        "observation_window.json",
        "effect_validation.json",
        "rca_evaluation.json",
    ):
        (directory / filename).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        autonomous,
        "_find_or_submit_run",
        lambda *_args: pytest.fail("completed scenarios must not be submitted again"),
    )

    autonomous._process_scenario(
        scenario,
        {},
        api=None,
        supervisor=None,
        repository=repository,
        run_timeout_seconds=1,
        recovery_timeout_seconds=1,
        max_infra_retries=0,
    )


def test_port_forward_starts_once_and_reuses_healthy_tunnel(tmp_path, monkeypatch):
    manager = autonomous.PortForwardSupervisor(
        tmp_path, max_restarts=1, startup_timeout_seconds=0, probe_interval_seconds=0
    )
    spec = autonomous.DEFAULT_PORT_FORWARDS[0]
    probes = iter([False, True, True])
    starts = []
    process = FakeProcess()
    monkeypatch.setattr(manager, "_probe", lambda _spec: next(probes))
    monkeypatch.setattr(manager, "_matching_pids", lambda _spec: [])
    monkeypatch.setattr(manager, "_port_open", lambda _port: False)
    monkeypatch.setattr(
        manager, "_start", lambda _spec: starts.append(_spec) or process
    )
    monkeypatch.setattr(manager, "_wait_healthy", lambda _spec: True)

    manager.ensure(spec)
    manager.ensure(spec)

    assert starts == [spec]
    assert manager.restart_counts[spec.name] == 1


def test_port_forward_refuses_to_duplicate_unhealthy_listener(tmp_path, monkeypatch):
    manager = autonomous.PortForwardSupervisor(tmp_path)
    spec = autonomous.DEFAULT_PORT_FORWARDS[0]
    monkeypatch.setattr(manager, "_probe", lambda _spec: False)
    monkeypatch.setattr(manager, "_matching_pids", lambda _spec: [])
    monkeypatch.setattr(manager, "_port_open", lambda _port: True)
    monkeypatch.setattr(
        manager,
        "_start",
        lambda _spec: pytest.fail("must not bind a second tunnel on an occupied port"),
    )

    with pytest.raises(
        autonomous.PortForwardConflict, match="refusing to create a duplicate"
    ):
        manager.ensure(spec)


def test_mcp_tunnel_probe_checks_sse_transport_not_tool_capability(monkeypatch):
    calls = []

    class FakeMCPClient:
        def __init__(self, url, timeout_seconds):
            calls.append((url, timeout_seconds))

        def assert_sse_connectivity(self):
            calls.append("connected")

    monkeypatch.setattr(autonomous, "MCPToolClient", FakeMCPClient)
    manager = autonomous.PortForwardSupervisor(Path("/tmp"))

    assert manager._probe(autonomous.DEFAULT_PORT_FORWARDS[0]) is True
    assert calls == [(autonomous.MCP_SSE_URL, 5), "connected"]


def test_port_forward_restart_budget_is_bounded(tmp_path, monkeypatch):
    manager = autonomous.PortForwardSupervisor(
        tmp_path, max_restarts=0, startup_timeout_seconds=0, probe_interval_seconds=0
    )
    spec = autonomous.DEFAULT_PORT_FORWARDS[0]
    starts = []
    process = FakeProcess()
    monkeypatch.setattr(manager, "_probe", lambda _spec: False)
    monkeypatch.setattr(manager, "_matching_pids", lambda _spec: [])
    monkeypatch.setattr(manager, "_port_open", lambda _port: False)

    def start(_spec):
        starts.append(1)
        manager.children[spec.name] = process
        return process

    monkeypatch.setattr(manager, "_start", start)
    monkeypatch.setattr(manager, "_wait_healthy", lambda _spec: False)

    with pytest.raises(autonomous.InfrastructureError, match="failed health checks"):
        manager.ensure(spec)
    with pytest.raises(
        autonomous.InfrastructureError, match="exhausted its restart budget"
    ):
        manager.ensure(spec)

    assert starts == [1]
    assert process.terminated


def test_recovery_gate_requires_ready_deployments(monkeypatch):
    services = autonomous.forty_faults.diverse.SERVICES
    deployments = [
        {
            "metadata": {"name": service, "generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        }
        for service in services
    ]
    monkeypatch.setattr(
        autonomous.runner,
        "_kubectl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["kubectl"], 0, json.dumps({"items": deployments}), ""
        ),
    )
    supervisor = SimpleNamespace(ensure_all=lambda: None)

    result = autonomous._wait_for_recovery(
        "sock-shop",
        supervisor,
        timeout_seconds=1,
        stable_seconds=0,
        poll_seconds=0,
    )

    assert result["passed"] is True


def test_recovery_timeout_is_explicit_when_deployments_are_not_ready(monkeypatch):
    monkeypatch.setattr(
        autonomous.runner,
        "_kubectl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["kubectl"], 0, json.dumps({"items": []}), ""
        ),
    )
    supervisor = SimpleNamespace(ensure_all=lambda: None)

    with pytest.raises(autonomous.InfrastructureError, match="did not remain healthy"):
        autonomous._wait_for_recovery(
            "sock-shop",
            supervisor,
            timeout_seconds=0,
            stable_seconds=1,
            poll_seconds=0,
        )


def test_completed_run_without_full_observation_window_is_rejected_and_preserved(
    tmp_path,
):
    _campaign, scenarios, repository = create_pilot_campaign(tmp_path)
    scenario = scenarios[0]
    scenario["run_id"] = "run-window"

    class API:
        def get(self, path):
            if path.endswith("/events"):
                return [{"event_type": "run_started"}]
            if path.endswith("/features"):
                return {"metric_hotspots": {}}
            if path.endswith("/observation-window"):
                return {
                    "partial": False,
                    "phases": {
                        "baseline": {"start": "b0", "end": "b1"},
                        "fault": {"start": "f0", "end": "f1"},
                    },
                }
            raise AssertionError(f"unexpected API path {path}")

    with pytest.raises(autonomous.InfrastructureError, match="incomplete or partial"):
        autonomous._collect_run_evidence(
            API(),
            scenario,
            repository,
            {"run_id": "run-window", "status": "completed"},
        )

    scenario_directory = autonomous._scenario_dir(tmp_path / "pilot", scenario)
    assert (scenario_directory / "run.json").is_file()
    assert (scenario_directory / "events.json").is_file()
    assert (scenario_directory / "run_features.json").is_file()
    assert (scenario_directory / "observation_window.json").is_file()
    assert (scenario_directory / "observation_window_error.json").is_file()


def test_invalid_injection_evidence_fails_scenario_before_rca(tmp_path, monkeypatch):
    _campaign, scenarios, repository = create_pilot_campaign(tmp_path)
    scenario = scenarios[0]
    scenario["spec"] = {"experiment": {"id": "pilot-test"}}
    monkeypatch.setattr(
        autonomous,
        "_find_or_submit_run",
        lambda item, *_args: item.update(run_id="run-1") or "run-1",
    )
    monkeypatch.setattr(
        autonomous,
        "_wait_for_run",
        lambda *_args, **_kwargs: {
            "run_id": "run-1",
            "status": "completed",
            "started_at": "start",
            "ended_at": "end",
        },
    )
    monkeypatch.setattr(
        autonomous, "_collect_run_evidence", lambda *_args: ({}, [], {})
    )
    monkeypatch.setattr(
        autonomous,
        "_wait_for_recovery",
        lambda *_args, **_kwargs: {"passed": True},
    )
    monkeypatch.setattr(
        autonomous,
        "_validate_single_run",
        lambda *_args: {
            "passed": False,
            "result_status": "injection_failed",
            "checks": {
                "injection": {"passed": False},
                "metrics": {"signal_observed": False},
            },
        },
    )
    monkeypatch.setattr(
        autonomous,
        "_run_rca",
        lambda *_args: pytest.fail("RCA must not run on invalid fault evidence"),
    )
    supervisor = SimpleNamespace(ensure_all=lambda: None)

    class API:
        def get(self, path, **_query):
            if path == "/api/runs":
                return []
            raise AssertionError(f"unexpected API path {path}")

    with pytest.raises(autonomous.ScenarioError, match="fault_injection_failed"):
        autonomous._process_scenario(
            scenario,
            {
                "platform": {
                    "poll_interval_seconds": 1,
                    "inter_run_cooldown_seconds": 0,
                },
                "cluster": {"app_namespace": "sock-shop"},
            },
            api=API(),
            supervisor=supervisor,
            repository=repository,
            run_timeout_seconds=1,
            recovery_timeout_seconds=1,
            max_infra_retries=0,
        )

    assert scenario["state"] == "failed"
    assert scenario["failure_domain"] == "fault_injection_failed_or_unverified"
    assert scenario["result"]["run_status"] == "completed"


def test_lost_start_response_is_reconciled_without_duplicate_submission(
    tmp_path, monkeypatch
):
    _campaign, scenarios, repository = create_pilot_campaign(tmp_path)
    scenario = scenarios[0]
    scenario["spec"] = {
        "experiment": {
            "id": f"pilot-{scenario['scenario_id'][:8]}",
        }
    }

    class API:
        def __init__(self):
            self.run = None
            self.start_requests = []

        def get(self, path, **query):
            if path == "/api/runs":
                if query.get("experiment_id"):
                    return [self.run] if self.run else []
                return [self.run] if self.run else []
            if path.startswith("/api/experiments/"):
                raise RuntimeError(f"HTTP 404 on GET {path}")
            raise AssertionError(f"unexpected API path {path}")

        def post(self, path, *, body, **_kwargs):
            if path == "/api/experiments":
                return {"version": 1}
            if path == "/api/runs/start":
                self.start_requests.append(body)
                self.run = {
                    "run_id": "existing-run",
                    "experiment_id": body["experiment_id"],
                    "status": "running",
                }
                raise TimeoutError("response lost after platform accepted the run")
            raise AssertionError(f"unexpected API path {path}")

    api = API()

    result = autonomous._find_or_submit_run(
        scenario,
        {"platform": {"initiated_by": "test"}},
        api,
        repository,
        max_infra_retries=2,
    )

    assert result == "existing-run"
    assert len(api.start_requests) == 1
    assert api.start_requests[0]["idempotency_key"] == (
        f"autonomous:{scenario['spec']['experiment']['id']}"
    )
    assert scenario["run_id"] == "existing-run"


def test_failed_effect_report_classification_distinguishes_missing_artifacts():
    injection_failure = {
        "checks": {"injection": {"passed": False}},
        "reason": "Litmus verdict was Fail",
    }
    missing_artifact = {
        "checks": {},
        "reason": "cannot read run artifacts: file missing",
    }
    unobserved_signal = {
        "checks": {
            "injection": {"passed": True},
            "metrics": {
                "signal_observed": False,
                "metrics": {"target_metric": {"delta_abs": 0}},
            },
        },
    }

    assert (
        autonomous._classify_effect_failure(injection_failure)
        == "fault_injection_failed_or_unverified"
    )
    assert (
        autonomous._classify_effect_failure(missing_artifact)
        == "experimental_infrastructure"
    )
    assert (
        autonomous._classify_effect_failure(unobserved_signal)
        == "injected_fault_effect_not_observed"
    )
    assert (
        autonomous._classify_effect_failure(
            {
                "checks": {
                    "injection": {"passed": True},
                    "metrics": {
                        "signal_observed": False,
                        "metrics": {"target_metric": {}},
                    },
                }
            }
        )
        == "telemetry_or_target_coverage_gap"
    )


def test_full_campaign_rejects_incomplete_pilot_report(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "protocol_version": autonomous.PROTOCOL_VERSION,
                "mode": "pilot",
                "status": "interrupted",
                "passed": False,
                "plan": [autonomous.forty_faults.CELLS[0]],
                "summary": {"completed_count": 0, "failed_count": 0},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(autonomous.CampaignError, match="successful pilot report"):
        autonomous._load_pilot_report(report_path, autonomous.forty_faults.CELLS[0])
