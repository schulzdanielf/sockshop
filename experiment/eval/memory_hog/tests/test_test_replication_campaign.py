import os
import copy
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import test_replication_campaign as replication


def valid_entry(run_id="new-run"):
    return {
        "status": "completed", "run_id": run_id,
        "gates": {
            "submitted": True, "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True},
            "recovery": {"passed": True}, "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete", "mode": "dual_write", "source": "historical_mcp",
            "telemetry": {
                name: {"status": "complete"} for name in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 1, "logs": 1, "traces": 1},
        },
    }


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    source = yaml.safe_load((replication.HERE / "config.yaml").read_text())
    source = replication.campaign.build_config(source, evaluation, "medium", 42, {})
    (evaluation / "config.yaml").write_text(yaml.safe_dump(source))
    runs = [
        {"cell": {**cell, "phase": "test", "replica_idx": 1},
         "run_id": f"old-{index}", "status": "completed"}
        for index, cell in enumerate(replication.campaign.build_plan("medium", 42))
    ]
    replication.write_json(evaluation / "test_cohort.json", {
        "runs": runs, "training_run_ids": ["train-1"],
    })
    for name in ("rag_base.json", "evaluation_manifest.json", "precollection_manifest.json"):
        replication.write_json(evaluation / name, {})
    monkeypatch.setattr(replication, "process_identity", lambda pid: {
        "pid": pid, "start_ticks": "1",
        "command": ["python", "-m", "uvicorn", "experiment.platform.backend.main:app",
                    "--port", "8010"],
    } if pid == os.getpid() else None)
    root = tmp_path / "replication"
    replication.prepare(evaluation, root)
    return root, evaluation


def test_prepare_freezes_35_held_out_cells_without_side_effects(prepared):
    root, _ = prepared
    plan = replication.read_json(root / "plan.json")
    assert len(plan) == 35
    assert len({(c["service"], c["chaos_type"]) for c in plan}) == 35
    assert all(c["phase"] == "test" and c["replica_idx"] == 2 for c in plan)
    assert all(a["service"] != b["service"] for a, b in zip(plan, plan[1:]))
    config = yaml.safe_load((root / "config.yaml").read_text())
    assert config["train_experiments"] == []
    assert "knowledge_campaign" not in config
    assert config["controlled_campaign"]["level"] == replication.VERSION
    assert "pre_intervention_retry" not in config["controlled_campaign"]["resilience"]
    assert config["timeline"]["baseline_seconds"] == 180
    assert replication.verify_pins(root)["authorizations"]["automatic_diagnostics"] is False
    assert not (root / "attempts.jsonl").exists()


def test_prepare_records_authorized_supersession_of_empty_blocked_campaign(
    prepared, tmp_path, monkeypatch,
):
    root, evaluation = prepared
    api = Mock()
    api.get.return_value = []
    monkeypatch.setattr(replication, "api_for", lambda config: api)
    predecessor = tmp_path / "predecessor"
    predecessor.mkdir()
    replication.write_json(predecessor / "campaign_state.json", {
        "status": "blocked", "new_count": 0,
    })
    replication.write_json(predecessor / "collection_report.json", {
        "attempt_count": 0, "new_count": 0, "combined_count": 35,
    })
    successor = tmp_path / "successor"
    replication.prepare(evaluation, successor, supersedes_campaign=predecessor)
    auth = replication.read_json(successor / "manifest.json")["authorizations"][
        "user_authorized_supersession"
    ]
    assert auth["previous_campaign"] == str(predecessor)
    assert "600-second" in auth["retry_gate"]


def test_prepare_refuses_to_supersede_campaign_with_attempts(prepared, tmp_path):
    root, evaluation = prepared
    predecessor = tmp_path / "predecessor"
    predecessor.mkdir()
    replication.write_json(predecessor / "campaign_state.json", {
        "status": "blocked", "new_count": 0,
    })
    replication.write_json(predecessor / "collection_report.json", {
        "attempt_count": 1, "new_count": 0, "combined_count": 35,
    })
    with pytest.raises(RuntimeError, match="zero-attempt"):
        replication.prepare(
            evaluation, tmp_path / "successor", supersedes_campaign=predecessor,
        )


@pytest.mark.parametrize("filename", ["plan.json", "config.yaml", "original_cohort.json"])
def test_changed_local_input_blocks_before_execution(prepared, filename):
    root, _ = prepared
    with (root / filename).open("a") as stream:
        stream.write("\n")
    with pytest.raises(RuntimeError, match="local campaign input changed"):
        replication.verify_pins(root)


def test_changed_evaluation_input_is_rejected(prepared):
    root, evaluation = prepared
    (evaluation / "rag_base.json").write_text("{}")
    with pytest.raises(RuntimeError, match="pinned input/code changed"):
        replication.verify_pins(root)


def test_reference_must_have_exact_matrix_and_no_train_overlap(prepared):
    root, _ = prepared
    cohort = replication.read_json(root / "original_cohort.json")
    cohort["training_run_ids"].append(cohort["runs"][0]["run_id"])
    with pytest.raises(ValueError, match="held-out"):
        replication.validate_matrix(cohort)


def test_evaluation_active_never_reads_or_accepts_partial_report(monkeypatch, tmp_path):
    monkeypatch.setattr(replication, "evaluation_processes", lambda: [123])
    assert replication.evaluation_succeeded(tmp_path) is False
    monkeypatch.setattr(replication, "evaluation_processes", lambda: [])
    with pytest.raises(RuntimeError, match="without a complete"):
        replication.evaluation_succeeded(tmp_path)


@pytest.mark.parametrize("errors", [0, 1])
def test_evaluation_gate_validates_saved_report_and_errors(monkeypatch, tmp_path, errors):
    from experiment.eval.memory_hog import test_base_rag_evaluation as evaluation

    report = {
        "evaluation_id": tmp_path.name, "provenance": {"test_run_count": 35},
        "diagnosis_summary": {"rag": {"errors": errors}},
    }
    replication.write_json(tmp_path / "test_base_rag_evaluation.json", report)
    replication.write_json(tmp_path / "diagnostics_complete.json", {})
    monkeypatch.setattr(replication, "evaluation_processes", lambda: [])
    validator = Mock(return_value=report)
    monkeypatch.setattr(evaluation, "build_report", validator)
    if errors:
        with pytest.raises(RuntimeError, match="contains errors"):
            replication.evaluation_succeeded(tmp_path)
    else:
        assert replication.evaluation_succeeded(tmp_path) is True
    validator.assert_called_once_with(tmp_path)


def test_negative_effect_is_not_a_technical_retry():
    entry = valid_entry()
    entry["gates"]["intervention_confirmed_on_target"] = False
    entry["gates"]["mechanism_observed"] = {"status": "ok", "passed": False}
    assert not replication.technical_retry(entry)
    entry["gates"]["cgroup_telemetry"] = {"passed": False}
    assert replication.technical_retry(entry)
    entry["gates"]["cleanup"]["passed"] = False
    assert not replication.technical_retry(entry)


def test_preinjection_retry_requires_cleanup_and_load_stop():
    entry = valid_entry()
    entry["status"] = "blocked"
    entry["gates"]["submitted"] = False
    assert replication.technical_retry(entry)
    entry["gates"].pop("load_stopped")
    assert not replication.technical_retry(entry)


def test_unique_attempt_experiment_ids_across_campaigns(prepared):
    root, _ = prepared
    cell = replication.read_json(root / "plan.json")[0]
    first = replication.attempt_config(root, cell, 1)
    second = replication.attempt_config(root, cell, 2)
    ids = {
        yaml.safe_load((directory / "config.yaml").read_text())["experiment_campaign_id"]
        for directory in (first, second)
    }
    assert len(ids) == 2
    assert all(value.startswith(root.name + "-") for value in ids)


def test_two_technical_attempts_and_no_third_on_resume(prepared, monkeypatch):
    root, _ = prepared
    cell = replication.read_json(root / "plan.json")[0]
    monkeypatch.setattr(replication, "budget", lambda *args: None)
    monkeypatch.setattr(replication.time, "sleep", lambda *args: None)
    monkeypatch.setattr(replication, "recover_service", lambda *args: None)
    monkeypatch.setattr(replication.supplemental, "_wait_for_history_visibility", lambda *args: {})
    execute = Mock(return_value=1)
    monkeypatch.setattr(replication.campaign, "execute", execute)
    entry = valid_entry()
    entry["status"] = "blocked"
    entry["gates"]["submitted"] = False
    monkeypatch.setattr(replication.supplemental, "_latest_entry", lambda *args: entry)
    replication.collect_cell(root, cell)
    replication.collect_cell(root, cell)
    assert execute.call_count == 2
    attempts = replication.campaign.read_ledger(root / "attempts.jsonl")
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert all(not row["accepted"] for row in attempts)


def test_nonretryable_prior_outcome_is_not_reinjected(prepared, monkeypatch):
    root, _ = prepared
    cell = replication.read_json(root / "plan.json")[0]
    replication.campaign.append_ledger(root / "attempts.jsonl", {
        "cell": cell, "accepted": False, "retry_eligible": False,
    })
    execute = Mock(side_effect=AssertionError("must not reinject a nontechnical result"))
    monkeypatch.setattr(replication.campaign, "execute", execute)
    replication.collect_cell(root, cell)
    execute.assert_not_called()


def test_publication_requires_evidence_and_disjoint_ids(prepared):
    root, _ = prepared
    cell = replication.read_json(root / "plan.json")[0]
    entry = valid_entry("old-0")
    row = {"cell": cell, "attempt": 1, "directory": "attempt", "entry": entry, "accepted": True}
    replication.campaign.append_ledger(root / "attempts.jsonl", row)
    with pytest.raises(RuntimeError, match="overlap"):
        replication.publish(root)


def test_partial_publication_never_claims_70_runs(prepared):
    root, _ = prepared
    report = replication.publish(root)
    assert report["new_count"] == 0
    assert report["combined_count"] == 35
    assert report["complete"] is False
    combined = replication.read_json(root / "cohort_combined_70.json")
    assert combined["run_count"] == len(combined["runs"]) == 35
    assert combined["complete"] is False


@pytest.fixture
def isolated_run(prepared, monkeypatch):
    root, _ = prepared
    monkeypatch.setattr(replication, "evaluation_succeeded", lambda *args: True)
    monkeypatch.setattr(replication, "ensure_connections", lambda *args: None)
    monkeypatch.setattr(replication, "ensure_backend", lambda *args: None)
    monkeypatch.setattr(replication, "repair_memory_templates", lambda *args: None)
    monkeypatch.setattr(replication, "require_idle", lambda *args: None)
    monkeypatch.setattr(replication, "stop_owned_active", lambda *args: None)
    monkeypatch.setattr(replication.supplemental, "readiness_preflight", lambda *args: {"passed": True})
    monkeypatch.setattr(replication.supplemental, "_wait_for_history_visibility", lambda *args: {})
    dual_write = Mock()
    monkeypatch.setattr(replication.supplemental, "_set_campaign_dual_write", dual_write)
    return root, dual_write


def test_failed_controls_never_start_cases_and_disable_history(isolated_run, monkeypatch):
    root, dual_write = isolated_run
    monkeypatch.setattr(replication.supplemental, "_run_load_controls", lambda *args: False)
    collect = Mock(side_effect=AssertionError("controls must gate all cases"))
    monkeypatch.setattr(replication, "collect_cell", collect)
    assert replication.run(root) == 2
    collect.assert_not_called()
    assert [call.args[1] for call in dual_write.call_args_list] == [True, False]
    assert replication.read_json(root / "campaign_state.json")["status"] == "blocked"


def test_failed_evaluation_never_repairs_or_loads(isolated_run, monkeypatch):
    root, dual_write = isolated_run
    gate = Mock(side_effect=RuntimeError("evaluation exited without success"))
    monkeypatch.setattr(replication, "evaluation_succeeded", gate)
    repair = Mock()
    monkeypatch.setattr(replication, "ensure_backend", repair)
    assert replication.run(root) == 2
    repair.assert_not_called()
    dual_write.assert_not_called()
    assert "deadline_monotonic" not in replication.read_json(root / "campaign_state.json")


def test_complete_orchestration_publishes_exactly_35_35_70(isolated_run, monkeypatch):
    root, dual_write = isolated_run
    monkeypatch.setattr(replication.supplemental, "_run_load_controls", lambda *args: True)

    def collect(directory, cell):
        replication.campaign.append_ledger(directory / "attempts.jsonl", {
            "cell": cell, "attempt": 1, "directory": str(directory),
            "entry": valid_entry(f"new-{cell['cell_id']}"), "accepted": True,
        })

    monkeypatch.setattr(replication, "collect_cell", collect)
    assert replication.run(root) == 0
    for name, count in (("original_35", 35), ("new_35", 35), ("combined_70", 70)):
        cohort = replication.read_json(root / f"cohort_{name}.json")
        assert cohort["run_count"] == count
        assert len({row["run_id"] for row in cohort["runs"]}) == count
        assert cohort["diagnostics_executed"] is False
        assert cohort["complete"] is True
    assert replication.read_json(root / "campaign_state.json")["status"] == "completed"
    assert [call.args[1] for call in dual_write.call_args_list] == [True, False]


def test_budget_uses_persisted_monotonic_deadline(prepared):
    root, _ = prepared
    replication.state(
        root, boot_id=replication.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        deadline_monotonic=replication.time.monotonic() - 1,
    )
    with pytest.raises(TimeoutError, match="24-hour"):
        replication.budget(root, 1)


def test_restart_refused_without_cleanup(prepared, monkeypatch):
    root, _ = prepared
    entry = valid_entry()
    entry["gates"]["cleanup"]["passed"] = False
    restart = Mock(side_effect=AssertionError("unsafe restart"))
    monkeypatch.setattr(replication.runner, "_kubectl", restart)
    with pytest.raises(RuntimeError, match="cleanup"):
        replication.recover_service(root, {}, {"service": "orders"}, entry)
    restart.assert_not_called()


def test_api_restart_refuses_changed_pid_identity(prepared, monkeypatch):
    root, _ = prepared
    manifest = replication.verify_pins(root)
    monkeypatch.setattr(replication, "require_idle", lambda *args: None)
    monkeypatch.setattr(
        replication.campaign, "require_controlled_backend",
        Mock(side_effect=RuntimeError("stale backend")),
    )
    monkeypatch.setattr(replication, "process_identity", lambda pid: None)
    kill = Mock()
    monkeypatch.setattr(replication.os, "kill", kill)
    with pytest.raises(RuntimeError, match="identity changed"):
        replication.ensure_backend(root, {"platform": {"api_base": "http://api", "user_role": "test"}}, manifest)
    kill.assert_not_called()


def test_stop_only_owns_matching_campaign_runs(prepared, monkeypatch):
    root, _ = prepared
    api = Mock()
    owned = {"experiment_id": root.name + "-case", "run_id": "owned", "status": "running"}
    foreign = {"experiment_id": "other-case", "run_id": "foreign", "status": "running"}
    api.get.side_effect = [
        [owned, foreign],
        {"status": "stopped", "summary": {"controlled_gates": {
            "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
        }}},
    ]
    monkeypatch.setattr(replication, "api_for", lambda config: api)
    replication.stop_owned_active(root, {})
    assert api.post.call_count == 1
    assert api.post.call_args.args[0] == "/api/runs/owned/stop"


def test_terminal_unclean_run_keeps_history_enabled(prepared, monkeypatch):
    root, _ = prepared
    api = Mock()
    api.get.side_effect = [
        [{"experiment_id": root.name + "-case", "run_id": "owned", "status": "failed"}],
        {"status": "failed", "summary": {"controlled_gates": {"cleanup": {"passed": False}}}},
    ]
    monkeypatch.setattr(replication, "api_for", lambda config: api)
    ticks = iter([0, 1000])
    monkeypatch.setattr(replication.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="retain historical"):
        replication.stop_owned_active(root, {})
    api.post.assert_not_called()


def test_memory_repair_changes_only_cleanup_in_terminal_workflow():
    workflow = yaml.safe_load(replication.campaign.manifests._render_memory_hog("carts", {}))
    workflow["metadata"]["resourceVersion"] = "123"
    workflow["status"] = {"phase": "Succeeded"}
    original = copy.deepcopy(workflow)
    assert replication.memory_cleanup_patch(workflow, "carts") == []
    for template in workflow["spec"]["templates"]:
        for artifact in template.get("inputs", {}).get("artifacts", []):
            raw = artifact.get("raw", {}).get("data", "")
            if "kind: ChaosEngine" in raw:
                engine = yaml.safe_load(raw)
                for env in engine["spec"]["experiments"][0]["spec"]["components"]["env"]:
                    if env["name"] == "CHAOS_KILL_COMMAND":
                        env["value"] = "old incompatible cleanup"
                artifact["raw"]["data"] = yaml.safe_dump(engine)
    patch = replication.memory_cleanup_patch(workflow, "carts")
    assert [operation["op"] for operation in patch] == ["test", "test", "replace"]
    assert patch[0]["value"] == "123"
    original_engine = next(
        yaml.safe_load(a["raw"]["data"])
        for t in original["spec"]["templates"]
        for a in t.get("inputs", {}).get("artifacts", [])
        if "kind: ChaosEngine" in a.get("raw", {}).get("data", "")
    )
    assert yaml.safe_load(patch[-1]["value"]) == original_engine


def test_memory_repair_never_patches_active_workflow():
    workflow = {"status": {"phase": "Running"}}
    with pytest.raises(RuntimeError, match="not terminal"):
        replication.memory_cleanup_patch(workflow, "carts")
