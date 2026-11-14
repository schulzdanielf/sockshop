import json
from types import SimpleNamespace

import yaml
import pytest

from experiment.eval.memory_hog import supplemental_campaign as supplemental


@pytest.mark.parametrize("jump", [0, 2.828, -2.828])
def test_clock_preflight_detects_real_steps_without_load(monkeypatch, jump):
    elapsed = [0.0]
    monkeypatch.setattr(supplemental.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        supplemental.time, "time",
        lambda: 1000 + elapsed[0] + (jump if elapsed[0] >= 2 else 0),
    )
    monkeypatch.setattr(
        supplemental.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )

    report = supplemental.clock_preflight(duration_seconds=3)

    assert report["passed"] is (jump == 0)
    assert report["injections_submitted"] == 0
    assert report["load_started"] is False
    if jump:
        assert "invalid cgroup timing" in report["error"]


def test_clock_preflight_detects_accumulated_drift(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(supplemental.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(supplemental.time, "time", lambda: 1000 + 1.4 * elapsed[0])
    monkeypatch.setattr(
        supplemental.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )

    report = supplemental.clock_preflight(duration_seconds=3)

    assert report["passed"] is False
    assert "wall clock changed" in report["error"]


def test_readiness_preflight_rejects_stale_api_before_clock_or_environment(monkeypatch):
    def stale_api(api):
        raise RuntimeError("controlled backend implementation mismatch; restart API")

    monkeypatch.setattr(supplemental.campaign, "require_controlled_backend", stale_api)
    monkeypatch.setattr(
        supplemental.campaign, "environment_preflight",
        lambda config: pytest.fail("stale API must fail before infrastructure checks"),
    )
    monkeypatch.setattr(
        supplemental, "clock_preflight",
        lambda: pytest.fail("stale API must fail before the long clock check"),
    )

    report = supplemental.readiness_preflight({
        "platform": {"api_base": "http://api", "user_role": "chaos_engineer"},
    })

    assert report["passed"] is False
    assert "implementation mismatch" in report["error"]


def test_readiness_preflight_checks_environment_before_clock(monkeypatch):
    monkeypatch.setattr(supplemental.campaign, "require_controlled_backend", lambda api: None)
    monkeypatch.setattr(supplemental.runner.ApiClient, "get", lambda *args: [])
    monkeypatch.setattr(supplemental.campaign, "environment_preflight", lambda config: {
        "passed": False, "checks": [{"name": "nodes", "passed": False}],
    })
    monkeypatch.setattr(
        supplemental, "clock_preflight",
        lambda: pytest.fail("failed environment must skip the long clock check"),
    )

    report = supplemental.readiness_preflight({
        "platform": {"api_base": "http://api", "user_role": "chaos_engineer"},
    })

    assert report["passed"] is False
    assert report["environment"]["passed"] is False
    assert "clock" not in report


@pytest.mark.parametrize("phase", ["training", "test"])
def test_failed_readiness_preserves_existing_campaign_state(tmp_path, monkeypatch, phase):
    state = {"status": "awaiting_review", "history_campaign_id": "original", "completed": ["run-1"]}
    (tmp_path / "campaign_state.json").write_text(json.dumps(state))
    monkeypatch.setattr(supplemental, "readiness_preflight", lambda config: {
        "passed": False, "error": "clock unstable",
    })

    assert supplemental._require_readiness(tmp_path, {}, phase) is False
    observed = json.loads((tmp_path / "campaign_state.json").read_text())
    assert observed["status"] == "blocked"
    assert observed["completed"] == state["completed"]
    assert observed["history_campaign_id"] == "original"
    assert observed["readiness_sha256"] == supplemental._sha256(tmp_path / f"{phase}_readiness.json")


def test_new_campaign_readiness_failure_does_not_enable_dual_write_or_load(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    monkeypatch.setattr(supplemental, "_source_config", lambda directory: {})
    monkeypatch.setattr(supplemental, "_new_manifest", lambda root, source: {})
    monkeypatch.setattr(supplemental, "build_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(supplemental, "readiness_preflight", lambda config: {
        "passed": False, "error": "clock unstable",
    })
    monkeypatch.setattr(
        supplemental, "_set_campaign_dual_write",
        lambda *args: pytest.fail("failed initial readiness must not change dual-write"),
    )
    monkeypatch.setattr(
        supplemental, "_run_load_controls",
        lambda *args: pytest.fail("failed readiness must not start controls"),
    )

    assert supplemental._run_new(root) == 1
    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "blocked"
    assert state["readiness_sha256"] == supplemental._sha256(root / "initial_readiness.json")


def test_preflight_cli_does_not_start_a_campaign(monkeypatch, capsys):
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    monkeypatch.setattr(supplemental, "readiness_preflight", lambda config: {
        "passed": True, "load_started": False, "injections_submitted": 0,
    })
    monkeypatch.setattr(
        supplemental, "_run_new", lambda *args: pytest.fail("preflight must not start a campaign"),
    )

    assert supplemental.main(["--preflight"]) == 0
    assert json.loads(capsys.readouterr().out)["injections_submitted"] == 0


def test_supplemental_plan_preserves_exact_three_train_and_three_test_cases(tmp_path):
    source = yaml.safe_load((supplemental.HERE / "config.yaml").read_text())

    config = supplemental.build_config(
        source, tmp_path / "supplemental", list(supplemental.CASES),
    )

    assert [
        cell["cell_id"] for cell in config["train_experiments"]
    ] == [
        "front-end-memory-hog-r0",
        "front-end-network-latency-r0",
        "orders-container-kill-r0",
    ]
    assert [
        cell["cell_id"] for cell in config["test_experiments"]
    ] == [
        "user-network-latency-r1",
        "front-end-network-latency-r1",
        "orders-container-kill-r1",
    ]
    assert config["controlled_campaign"]["preparation_policy"] == (
        supplemental.SUPPLEMENTAL_PREPARATION_POLICY
    )
    assert config["controlled_campaign"]["recovery_timeout_by_service"] == {
        "orders": 600,
    }
    assert config["historical_observability"]["campaign_id"] == "supplemental"


def test_supplemental_plan_rejects_mutated_or_unapproved_cell(tmp_path):
    source = yaml.safe_load((supplemental.HERE / "config.yaml").read_text())
    cells = [dict(case) for case in supplemental.CASES]
    cells[0]["service"] = "orders"

    with pytest.raises(ValueError, match="frozen six-case matrix"):
        supplemental.build_config(source, tmp_path / "invalid", cells)


def test_supplemental_plan_accepts_a_valid_single_case_subset(tmp_path):
    source = yaml.safe_load((supplemental.HERE / "config.yaml").read_text())
    cell = {
        key: value
        for key, value in supplemental.CASES[0].items()
        if key != "previous_run_ids"
    }

    config = supplemental.build_config(source, tmp_path / "single-case", [cell])

    assert [item["cell_id"] for item in config["train_experiments"]] == [cell["cell_id"]]
    assert config["test_experiments"] == []


def test_history_visibility_waits_for_a_fresh_campaign_labeled_sample(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    queries = []
    responses = iter([
        {"status": "success", "data": {"result": []}},
        {"status": "success", "data": {"result": [{"value": [1, "2.5"]}]}},
    ])

    class Client:
        def __init__(self, url, *, timeout_seconds):
            assert url == "http://history/sse"
            assert timeout_seconds == 30

        def call_tool_json(self, name, arguments):
            assert name == "prometheus_instant_query"
            queries.append(arguments["query"])
            return next(responses)

    monkeypatch.setattr(supplemental, "MCPToolClient", Client)
    monkeypatch.setattr(
        supplemental.runner,
        "_kubectl",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "items": [{
                    "metadata": {"name": "front-end-pod"},
                    "status": {"phase": "Running"},
                }],
            }),
            stderr="",
        ),
    )
    monkeypatch.setattr(supplemental.time, "sleep", lambda seconds: None)
    config = {
        "cluster": {"app_namespace": "sock-shop"},
        "historical_observability": {
            "campaign_id": root.name,
            "mcp_sse_url": "http://history/sse",
            "visibility_timeout_seconds": 5,
        },
    }

    result = supplemental._wait_for_history_visibility(
        config, root, services=("front-end",),
    )

    assert result == {
        "front-end": {"pod": "front-end-pod", "sample_age_seconds": 2.5},
    }
    assert len(queries) == 2
    assert all('history_campaign="campaign"' in query for query in queries)


def test_reference_validation_accepts_pinned_inputs(tmp_path, monkeypatch):
    training = tmp_path / "training.json"
    cohort = tmp_path / "cohort.json"
    training.write_text('{"training": true}\n', encoding="utf-8")
    cohort.write_text('{"cohort": true}\n', encoding="utf-8")
    monkeypatch.setattr(supplemental, "REFERENCE_TRAINING_BASE", training)
    monkeypatch.setattr(supplemental, "REFERENCE_TEST_COHORT", cohort)
    monkeypatch.setattr(
        supplemental, "REFERENCE_TRAINING_SHA256", supplemental._sha256(training),
    )
    monkeypatch.setattr(
        supplemental, "REFERENCE_TEST_SHA256", supplemental._sha256(cohort),
    )

    assert supplemental.validate_reference_inputs() == {
        "training_base_sha256": supplemental._sha256(training),
        "test_cohort_sha256": supplemental._sha256(cohort),
    }


def test_reference_validation_rejects_changed_input(tmp_path, monkeypatch):
    training = tmp_path / "training.json"
    cohort = tmp_path / "cohort.json"
    training.write_text('{"training": "changed"}\n', encoding="utf-8")
    cohort.write_text('{"cohort": true}\n', encoding="utf-8")
    monkeypatch.setattr(supplemental, "REFERENCE_TRAINING_BASE", training)
    monkeypatch.setattr(supplemental, "REFERENCE_TEST_COHORT", cohort)
    monkeypatch.setattr(supplemental, "REFERENCE_TRAINING_SHA256", "0" * 64)
    monkeypatch.setattr(
        supplemental, "REFERENCE_TEST_SHA256", supplemental._sha256(cohort),
    )

    with pytest.raises(ValueError, match="frozen v6 training base or test cohort changed"):
        supplemental.validate_reference_inputs()


def test_supplemental_manifest_pins_the_prospective_stability_policy(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    monkeypatch.setattr(supplemental, "build_config", lambda *args, **kwargs: {
        "historical_observability": {"visibility_timeout_seconds": 30},
        "controlled_campaign": {},
    })

    manifest = supplemental._new_manifest(tmp_path / "campaign", {})

    assert manifest["stability_policy"] == supplemental.SUPPLEMENTAL_STABILITY_POLICY
    assert manifest["control_acceptance_policy"] == supplemental.CONTROL_ACCEPTANCE_POLICY
    assert manifest["control_acceptance_policy"]["long_window_stability"] == "diagnostic"


def test_no_fault_controls_use_the_pinned_stability_policy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(supplemental, "build_config", lambda *args, **kwargs: {})

    def run_load_check(*args, **kwargs):
        calls.append(kwargs)
        return {
            "passed": True,
            "control_acceptance_policy": supplemental.CONTROL_ACCEPTANCE_POLICY,
            "injections_submitted": 0,
            "load_stopped": {"stopped": True},
            "no_fault_window": {
                "observed_seconds": supplemental.CONTROL_SECONDS,
                "health_checks": [{"healthy": True}],
            },
        }

    monkeypatch.setattr(supplemental.load_check, "run_load_check", run_load_check)

    assert supplemental._run_load_controls(tmp_path / "campaign", {}, {})
    assert len(calls) == 2
    assert all(
        call["stability_relative_limit_overrides"]
        == supplemental.SUPPLEMENTAL_STABILITY_POLICY["relative_limit_overrides"]
        and call["stability_default_relative_limit"]
        == supplemental.SUPPLEMENTAL_STABILITY_POLICY["default_relative_limit"]
        and call["control_window_stability_mode"] == "diagnostic"
        for call in calls
    )


def test_new_campaign_pauses_for_calibration_review_before_training(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    monkeypatch.setattr(supplemental, "_require_readiness", lambda *args: True)
    monkeypatch.setattr(supplemental, "_source_config", lambda directory: {})
    monkeypatch.setattr(supplemental, "_new_manifest", lambda root, source: {})
    monkeypatch.setattr(
        supplemental, "build_config",
        lambda source, directory, cells, **kwargs: {},
    )
    monkeypatch.setattr(supplemental, "_run_load_controls", lambda root, source, config: True)
    monkeypatch.setattr(
        supplemental, "_wait_for_history_visibility",
        lambda config, root: {"front-end": {"sample_age_seconds": 1}},
    )
    monkeypatch.setattr(
        supplemental.campaign, "environment_preflight", lambda config: {"passed": True},
    )
    dual_write_calls = []
    monkeypatch.setattr(
        supplemental, "_set_campaign_dual_write",
        lambda root, enabled: dual_write_calls.append((root, enabled)),
    )
    monkeypatch.setattr(
        supplemental, "_run_training_phase",
        lambda root, source: pytest.fail("training must wait for calibration review"),
    )
    assert supplemental._run_new(root) == 0

    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "awaiting_calibration_review"
    assert state["history_campaign_id"] == root.name
    assert dual_write_calls == [(root, True)]


def test_training_continuation_requires_two_passing_controls(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    root.mkdir()
    supplemental._write_json(root / "campaign_manifest.json", {
        "protocol_version": supplemental.PROTOCOL_VERSION,
    })
    supplemental._write_json(root / "preflight.json", {"passed": True})
    supplemental._write_json(root / "campaign_state.json", {
        "status": "awaiting_calibration_review",
        "preflight_sha256": supplemental._sha256(root / "preflight.json"),
    })
    for index, passed in ((1, False), (2, True)):
        path = root / "calibrations" / f"no-fault-{index:02d}" / "report.json"
        supplemental._write_json(path, {
            "passed": passed,
            "injections_submitted": 0,
            "no_fault_window_seconds": supplemental.CONTROL_SECONDS,
        })
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    monkeypatch.setattr(supplemental, "_set_campaign_dual_write", lambda root, enabled: None)
    monkeypatch.setattr(
        supplemental, "_run_training_phase",
        lambda root, source: pytest.fail("failed controls must block training"),
    )
    with pytest.raises(RuntimeError, match="no-fault control 1 is not approved"):
        supplemental._continue_training(root)


@pytest.mark.parametrize("manifest_diagnostic", [False, True])
def test_training_rejects_mismatched_control_acceptance_policy(tmp_path, monkeypatch, manifest_diagnostic):
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    manifest = {"protocol_version": supplemental.PROTOCOL_VERSION}
    if manifest_diagnostic:
        manifest["control_acceptance_policy"] = supplemental.CONTROL_ACCEPTANCE_POLICY
    supplemental._write_json(tmp_path / "campaign_manifest.json", manifest)
    supplemental._write_json(tmp_path / "preflight.json", {"passed": True})
    supplemental._write_json(tmp_path / "campaign_state.json", {
        "status": "awaiting_calibration_review",
        "preflight_sha256": supplemental._sha256(tmp_path / "preflight.json"),
    })
    for index in (1, 2):
        supplemental._write_json(tmp_path / "calibrations" / f"no-fault-{index:02d}" / "report.json", {
            "passed": True,
            "injections_submitted": 0,
            "no_fault_window_seconds": supplemental.CONTROL_SECONDS,
            "control_acceptance_policy": supplemental.load_check.control_acceptance_policy(
                "blocking" if manifest_diagnostic else "diagnostic",
            ),
        })

    with pytest.raises(RuntimeError, match="acceptance policy does not match"):
        supplemental._continue_training(tmp_path)


@pytest.mark.parametrize("with_policy", [False, True, "diagnostic"])
def test_training_continuation_runs_only_after_approved_controls(
    tmp_path, monkeypatch, with_policy,
):
    monkeypatch.setattr(supplemental, "_require_readiness", lambda *args: True)
    root = tmp_path / "campaign"
    root.mkdir()
    manifest = {
        "protocol_version": supplemental.PROTOCOL_VERSION,
    }
    if with_policy:
        manifest["stability_policy"] = supplemental.SUPPLEMENTAL_STABILITY_POLICY
    if with_policy == "diagnostic":
        manifest["control_acceptance_policy"] = supplemental.CONTROL_ACCEPTANCE_POLICY
    supplemental._write_json(root / "campaign_manifest.json", manifest)
    supplemental._write_json(root / "preflight.json", {"passed": True})
    supplemental._write_json(root / "campaign_state.json", {
        "status": "awaiting_calibration_review",
        "preflight_sha256": supplemental._sha256(root / "preflight.json"),
    })
    for index in (1, 2):
        path = root / "calibrations" / f"no-fault-{index:02d}" / "report.json"
        report = {
            "passed": True,
            "injections_submitted": 0,
            "no_fault_window_seconds": supplemental.CONTROL_SECONDS,
        }
        if with_policy:
            report["stability_policy"] = {
                "default_relative_limit": supplemental.SUPPLEMENTAL_STABILITY_POLICY[
                    "default_relative_limit"
                ],
                "relative_limit_overrides": supplemental.SUPPLEMENTAL_STABILITY_POLICY[
                    "relative_limit_overrides"
                ],
            }
        if with_policy == "diagnostic":
            report["control_acceptance_policy"] = supplemental.CONTROL_ACCEPTANCE_POLICY
            report["warnings"] = [{"service": "orders", "metrics": ["latency_ms"]}]
        supplemental._write_json(path, report)
    monkeypatch.setattr(
        supplemental.campaign, "environment_preflight",
        lambda config: {"passed": True, "checks": []},
    )
    monkeypatch.setattr(supplemental, "_set_campaign_dual_write", lambda root, enabled: None)
    monkeypatch.setattr(
        supplemental, "_wait_for_history_visibility",
        lambda config, root: {"front-end": {"sample_age_seconds": 1}},
    )
    training_calls = []

    def run_training(root, source):
        state = json.loads((root / "campaign_state.json").read_text())
        state["status"] = "awaiting_training_base_review"
        supplemental._record_state(root, state)
        training_calls.append(root)
        return True

    monkeypatch.setattr(
        supplemental, "_run_training_phase",
        run_training,
    )

    assert supplemental._continue_training(root) == 0

    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "awaiting_training_base_review"
    assert training_calls == [root]


def test_training_resumes_only_the_known_postprocessing_failure_without_reinjection(
    tmp_path, monkeypatch,
):
    root = tmp_path / "campaign"
    root.mkdir()
    supplemental._write_json(root / "campaign_manifest.json", {
        "protocol_version": supplemental.PROTOCOL_VERSION,
    })
    preflight_path = root / "preflight.json"
    supplemental._write_json(preflight_path, {"passed": True})
    case = next(case for case in supplemental.CASES if case["phase"] == "train")
    run_id = "completed-training-run"
    supplemental._write_json(root / "campaign_state.json", {
        "status": "blocked",
        "blocked_at_cell": case["cell_id"],
        "blocked_run_id": run_id,
        "block_reason": "supplemental training case did not complete",
        "preflight_sha256": supplemental._sha256(preflight_path),
    })
    for index in (1, 2):
        supplemental._write_json(
            root / "calibrations" / f"no-fault-{index:02d}" / "report.json",
            {
                "passed": True,
                "injections_submitted": 0,
                "no_fault_window_seconds": supplemental.CONTROL_SECONDS,
            },
        )
    case_dir = supplemental._case_directory(root, case)
    case_dir.mkdir(parents=True)
    supplemental._append_jsonl(case_dir / "ledger.jsonl", {
        "cell_id": case["cell_id"],
        "run_id": run_id,
        "status": "completed",
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True},
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    })
    supplemental._append_jsonl(root / "campaign_events.jsonl", {
        "event": "supplemental_case_stopped",
        "cell_id": case["cell_id"],
        "run_id": run_id,
        "status": "completed",
        "execution_error": "ValueError: campaign contains no test fault types",
    })
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    monkeypatch.setattr(supplemental, "_source_config", lambda directory: {})
    monkeypatch.setattr(supplemental, "_require_readiness", lambda *args: True)
    monkeypatch.setattr(
        supplemental, "build_config",
        lambda source, directory, cells, **kwargs: {},
    )
    monkeypatch.setattr(
        supplemental, "_wait_for_history_visibility",
        lambda config, root: {"front-end": {"sample_age_seconds": 1}},
    )
    monkeypatch.setattr(
        supplemental.campaign, "environment_preflight", lambda config: {"passed": True},
    )
    monkeypatch.setattr(supplemental, "_set_campaign_dual_write", lambda *args: None)
    training_calls = []

    def run_training(campaign_root, source):
        training_calls.append(campaign_root)
        state = json.loads((campaign_root / "campaign_state.json").read_text())
        state["status"] = "awaiting_training_base_review"
        supplemental._record_state(campaign_root, state)
        return True

    monkeypatch.setattr(supplemental, "_run_training_phase", run_training)

    assert supplemental._continue_training(root) == 0

    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "awaiting_training_base_review"
    assert training_calls == [root]
    events = [
        json.loads(line)
        for line in (root / "campaign_events.jsonl").read_text().splitlines()
    ]
    resume_event = next(
        event for event in events
        if event["event"] == "resuming_completed_training_case_after_postprocessing_error"
    )
    assert resume_event["run_id"] == run_id
    assert resume_event["fault_injection_repeated"] is False


def test_training_does_not_resume_a_different_postprocessing_failure(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    case = next(case for case in supplemental.CASES if case["phase"] == "train")
    state = {
        "status": "blocked",
        "blocked_at_cell": case["cell_id"],
        "blocked_run_id": "completed-training-run",
        "block_reason": "supplemental training case did not complete",
    }
    case_dir = supplemental._case_directory(root, case)
    case_dir.mkdir(parents=True)
    supplemental._append_jsonl(case_dir / "ledger.jsonl", {
        "cell_id": case["cell_id"],
        "run_id": "completed-training-run",
        "status": "completed",
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True},
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    })
    supplemental._append_jsonl(root / "campaign_events.jsonl", {
        "event": "supplemental_case_stopped",
        "cell_id": case["cell_id"],
        "run_id": "completed-training-run",
        "status": "completed",
        "execution_error": "RuntimeError: unrelated failure",
    })

    assert supplemental._recoverable_completed_training_case(root, state) is None


def test_main_routes_continue_training_to_training_phase(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    root.mkdir()
    training_calls = []
    monkeypatch.setattr(supplemental, "validate_reference_inputs", lambda: {})
    monkeypatch.setattr(supplemental.fcntl, "flock", lambda *args: None)
    monkeypatch.setattr(
        supplemental,
        "_continue_training",
        lambda path: training_calls.append(path) or 0,
    )

    assert supplemental.main([
        "--continue-training", "--resume", str(root),
    ]) == 0

    assert training_calls == [root.resolve()]


def test_training_exception_records_a_blocked_campaign_state(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    root.mkdir()
    case = next(case for case in supplemental.CASES if case["phase"] == "train")
    directory = root / "case"
    directory.mkdir()
    (directory / "ledger.jsonl").write_text(json.dumps({
        "cell_id": case["cell_id"],
        "status": "aborted",
        "run_id": "run-aborted",
        "reason": "invalid cgroup timing: wall clock changed between reads",
    }) + "\n", encoding="utf-8")
    supplemental._write_json(root / "campaign_state.json", {
        "status": "collecting_training",
    })
    monkeypatch.setattr(
        supplemental, "_write_case_files",
        lambda campaign_root, source, planned_case: (directory, {}),
    )

    def fail_execution(*args, **kwargs):
        raise RuntimeError("run failed/aborted; stop before next target")

    monkeypatch.setattr(supplemental.campaign, "execute", fail_execution)

    assert supplemental._run_training_phase(root, {}) is False

    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "blocked"
    assert state["blocked_at_cell"] == case["cell_id"]
    assert state["blocked_run_id"] == "run-aborted"
    assert state["block_reason"] == "invalid cgroup timing: wall clock changed between reads"
    events = [
        json.loads(line)
        for line in (root / "campaign_events.jsonl").read_text().splitlines()
    ]
    assert events[0]["event"] == "supplemental_case_stopped"
    assert events[0]["execution_error"] == (
        "RuntimeError: run failed/aborted; stop before next target"
    )


def test_load_controls_run_both_safe_windows_after_a_stability_failure(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    root.mkdir()
    monkeypatch.setattr(
        supplemental, "build_config",
        lambda source, directory, cells, **kwargs: {},
    )
    monkeypatch.setattr(
        supplemental, "_wait_for_history_visibility",
        lambda config, root: {"front-end": {"sample_age_seconds": 1}},
    )
    calls = []

    def run_control(config, directory, **kwargs):
        calls.append(directory.name)
        return {
            "passed": len(calls) == 2,
            "control_acceptance_policy": supplemental.CONTROL_ACCEPTANCE_POLICY,
            "injections_submitted": 0,
            "load_stopped": {"stopped": True},
            "no_fault_window": {
                "observed_seconds": supplemental.CONTROL_SECONDS,
                "health_checks": [{"healthy": True}],
            },
        }

    monkeypatch.setattr(supplemental.load_check, "run_load_check", run_control)

    assert supplemental._run_load_controls(root, {}, {}) is False
    assert calls == ["no-fault-01", "no-fault-02"]


def test_load_controls_stop_after_unconfirmed_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    root.mkdir()
    monkeypatch.setattr(
        supplemental, "build_config",
        lambda source, directory, cells, **kwargs: {},
    )
    calls = []

    def run_control(config, directory, **kwargs):
        calls.append(directory.name)
        return {
            "passed": False,
            "control_acceptance_policy": supplemental.CONTROL_ACCEPTANCE_POLICY,
            "injections_submitted": 0,
            "load_stopped": {"stopped": False},
            "no_fault_window": {
                "observed_seconds": supplemental.CONTROL_SECONDS,
                "health_checks": [{"healthy": True}],
            },
        }

    monkeypatch.setattr(supplemental.load_check, "run_load_check", run_control)

    assert supplemental._run_load_controls(root, {}, {}) is False
    assert calls == ["no-fault-01"]


def test_new_campaign_records_a_control_exception_as_blocked(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    monkeypatch.setattr(supplemental, "_require_readiness", lambda *args: True)
    monkeypatch.setattr(supplemental, "_source_config", lambda directory: {})
    monkeypatch.setattr(supplemental, "_new_manifest", lambda root, source: {})
    monkeypatch.setattr(
        supplemental, "build_config",
        lambda source, directory, cells, **kwargs: {},
    )
    monkeypatch.setattr(
        supplemental, "_wait_for_history_visibility",
        lambda config, root: {"front-end": {"sample_age_seconds": 1}},
    )
    dual_write_calls = []
    monkeypatch.setattr(
        supplemental, "_set_campaign_dual_write",
        lambda root, enabled: dual_write_calls.append((root, enabled)),
    )

    def abort_controls(root, source, config):
        raise RuntimeError("load runner could not start")

    monkeypatch.setattr(supplemental, "_run_load_controls", abort_controls)

    with pytest.raises(RuntimeError, match="load runner could not start"):
        supplemental._run_new(root)

    state = json.loads((root / "campaign_state.json").read_text())
    assert state["status"] == "blocked"
    assert state["reason"] == "no-fault control aborted"
    assert dual_write_calls == [(root, True), (root, False)]
