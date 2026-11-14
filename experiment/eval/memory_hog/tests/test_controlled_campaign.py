import csv
import fcntl
import hashlib
import json
import hashlib
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import revalidate_cpu_mean80

require_environment = campaign.require_environment


@pytest.fixture(autouse=True)
def isolate_infrastructure(monkeypatch):
    monkeypatch.setattr(campaign, "require_environment", Mock())


@pytest.mark.parametrize("level,count", [("small", 5), ("medium", 35), ("large", 175)])
def test_matrix_is_seeded_application_only_and_avoids_consecutive_service(level, count):
    plan = campaign.build_plan(level, 42)
    assert plan == campaign.build_plan(level, 42)
    assert len(plan) == count
    assert len({cell["cell_id"] for cell in plan}) == count
    assert {cell["chaos_type"] for cell in plan} == set(campaign.FAULTS)
    assert all(cell["service"] in campaign.SERVICES for cell in plan)
    assert all(a["service"] != b["service"] for a, b in zip(plan, plan[1:]))
    if level == "large":
        assert {cell["replica_idx"] for cell in plan} == set(range(5))


def test_rag_strategy_check_accepts_additive_capabilities():
    info = {
        "version": "skill-rag-ab-v1",
        "modes": list(campaign.RAG_STRATEGY_MODES),
        "recommended_procedural_retriever": "opensearch-bm25-metric-gated-rrf-v6",
    }

    assert campaign.loaded_rca_version_matches(
        info, {"version": "skill-rag-ab-v1"}
    )
    assert not campaign.loaded_rca_version_matches(
        {**info, "version": "unexpected"}, {"version": "skill-rag-ab-v1"}
    )
    assert not campaign.loaded_rca_version_matches(
        {**info, "modes": ["annotate"]}, {"version": "skill-rag-ab-v1"}
    )


def test_train_only_plan_is_exactly_35_train_cells_with_cpu_first():
    plan = campaign.build_plan("train-only", 42)

    assert len(plan) == 35
    assert all(cell["phase"] == "train" for cell in plan)
    assert {cell["service"] for cell in plan} == set(campaign.SERVICES)
    assert {cell["chaos_type"] for cell in plan} == set(campaign.FAULTS)
    assert {
        (cell["service"], cell["chaos_type"]) for cell in plan
    } == {
        (service, fault)
        for service in campaign.SERVICES
        for fault in campaign.FAULTS
    }
    assert {cell["chaos_type"] for cell in plan[:5]} == set(campaign.FAULTS)
    assert plan[0]["chaos_type"] == "cpu-hog"
    assert all(a["service"] != b["service"] for a, b in zip(plan, plan[1:]))
    assert plan == campaign.build_plan("train-only", 42)


def test_large_repeats_each_combination_five_times():
    plan = campaign.build_plan("large")
    for service in campaign.SERVICES:
        for fault in campaign.FAULTS:
            assert sum(c["service"] == service and c["chaos_type"] == fault for c in plan) == 5


def test_dry_run_never_touches_cluster_or_writes(monkeypatch, capsys):
    monkeypatch.setattr(campaign, "execute", lambda *a, **kw: pytest.fail("execution in dry run"))
    monkeypatch.setattr(campaign.Path, "mkdir", lambda *a, **kw: pytest.fail("mkdir in dry run"))
    assert campaign.main(["--level", "medium"]) == 0
    output = capsys.readouterr().out
    assert "35 planned runs" in output
    assert "single_event; post_observation>=120s" in output


def test_train_only_dry_run_never_touches_cluster_or_writes(monkeypatch, capsys):
    monkeypatch.setattr(campaign, "execute", lambda *a, **kw: pytest.fail("execution in dry run"))
    monkeypatch.setattr(campaign.Path, "mkdir", lambda *a, **kw: pytest.fail("mkdir in dry run"))

    assert campaign.main(["--level", "train-only"]) == 0

    output = capsys.readouterr().out
    assert "35 planned runs" in output
    assert "35 train / 0 test" in output
    assert "Plan only: no files written" in output


def test_train_only_execution_requires_an_explicit_tranche(capsys):
    with pytest.raises(SystemExit) as exc:
        campaign.main(["--level", "train-only", "--run"])

    assert exc.value.code == 2
    assert "--through-train-count" in capsys.readouterr().err


def test_train_only_config_has_no_test_cells_and_pins_protocol(tmp_path):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())

    result = campaign.build_config(source, tmp_path, "train-only", 42, {})

    assert result["test_experiments"] == []
    assert result["train_experiments"] == campaign.build_plan("train-only", 42)
    assert result["train_only_campaign"] == {
        "version": campaign.TRAIN_ONLY_PLAN_VERSION,
        "tranches": [1, 5, 10, 20, 35],
        "automatic_advance": False,
        "test_split_included": False,
    }
    campaign.validate_train_only_plan(result, campaign.campaign_cells(result))


def test_rag_campaign_uses_four_shared_followup_checks_and_model_budget(tmp_path):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())

    result = campaign.build_config(source, tmp_path, "rag", 42, {})

    knowledge = result["knowledge_campaign"]
    assert knowledge["max_rounds"] == 4
    assert knowledge["max_model_calls"] == 30
    assert knowledge["max_total_tokens"] == 200000


def test_train_only_cli_persists_exact_plan_and_selected_tranche(monkeypatch, tmp_path):
    source_dir = Path(__file__).parents[1]
    source = yaml.safe_load((source_dir / "config.yaml").read_text())
    calibration = yaml.safe_load((source_dir / "cpu-calibration.yaml").read_text())
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
    (tmp_path / "cpu-calibration.yaml").write_text(
        yaml.safe_dump(calibration), encoding="utf-8",
    )
    monkeypatch.setattr(campaign, "HERE", tmp_path)
    captured = {}

    def execute(directory, **kwargs):
        captured["config"] = yaml.safe_load(
            (directory / "config.yaml").read_text(encoding="utf-8")
        )
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(campaign, "execute", execute)
    assert campaign.main([
        "--level", "train-only", "--run", "--through-train-count", "5",
    ]) == 0

    assert captured["config"]["test_experiments"] == []
    assert len(captured["config"]["train_experiments"]) == 35
    assert captured["kwargs"]["through_train_count"] == 5


def test_train_only_resume_requires_explicit_tranche(monkeypatch, tmp_path, capsys):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    config = campaign.build_config(source, tmp_path, "train-only", 42, {})
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(
        campaign, "execute",
        lambda *args, **kwargs: pytest.fail("resume started without a tranche"),
    )

    with pytest.raises(SystemExit) as exc:
        campaign.main(["--run", "--resume", str(tmp_path)])

    assert exc.value.code == 2
    assert "--through-train-count" in capsys.readouterr().err


def test_safe_block_skip_cli_forwards_full_train_only_resume(monkeypatch, tmp_path):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, "train-only", 42, {})
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    captured = {}
    monkeypatch.setattr(
        campaign, "execute",
        lambda directory, **kwargs: captured.update(kwargs) or 0,
    )

    assert campaign.main([
        "--run",
        "--resume", str(tmp_path),
        "--through-train-count", "35",
        "--continue-after-technical-failure",
        "--continue-after-safe-blocked-cell",
    ]) == 0

    assert captured["continue_after_safe_blocked_cell"] is True
    assert captured["continue_after_technical_failure"] is True


def test_train_only_resume_records_pause_and_never_resubmits_terminal_cells(
    monkeypatch, tmp_path,
):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, "train-only", 42, {})
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "plan.json").write_text(
        json.dumps(campaign.campaign_cells(cfg)), encoding="utf-8",
    )
    (tmp_path / ".campaign.lock").touch()
    history = {
        "status": "complete",
        "mode": "dual_write",
        "source": "historical_mcp",
        "telemetry": {
            family: {"status": "complete"}
            for family in ("metrics", "logs", "traces")
        },
        "source_counts": {"metrics": 2, "logs": 1, "traces": 1},
    }
    gates = {
        "submitted": True,
        "intervention_confirmed_on_target": True,
        "mechanism_observed": {"passed": True},
        "recovery": {"passed": True},
        "cleanup": {"passed": True},
        "load_stopped": {"stopped": True},
    }
    for cell in cfg["train_experiments"][:1]:
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            **cell,
            "status": "completed",
            "run_id": "run-" + cell["cell_id"],
            "gates": gates,
            "historical_validation": history,
        })

    class API:
        base_url = "http://127.0.0.1:8010"

        def get(self, path):
            assert path == "/api/runs"
            return []

        def post(self, *args, **kwargs):
            pytest.fail("terminal training cells must never be resubmitted")

    monkeypatch.setattr(campaign.runner, "ApiClient", lambda *a, **kw: API())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION,
        "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *_: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *_: None)

    assert campaign.execute(
        tmp_path, resume=True, through_train_count=1,
    ) == 0
    progress = json.loads((tmp_path / "train_only_progress.json").read_text())
    assert progress["approved_through_cell_count"] == 1
    assert progress["prefix_gates_passed"] is True
    assert progress["next_tranche"] == 5

    for cell in cfg["train_experiments"][1:5]:
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            **cell,
            "status": "completed",
            "run_id": "run-" + cell["cell_id"],
            "gates": gates,
            "historical_validation": history,
        })
    assert campaign.execute(
        tmp_path, resume=True, through_train_count=5,
    ) == 0
    progress = json.loads((tmp_path / "train_only_progress.json").read_text())
    assert progress["approved_through_cell_count"] == 5
    assert progress["prefix_gates_passed"] is True
    assert progress["next_tranche"] == 10


def test_train_only_resume_blocks_next_cell_after_failed_prior_gate(
    monkeypatch, tmp_path,
):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, "train-only", 42, {})
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "plan.json").write_text(
        json.dumps(campaign.campaign_cells(cfg)), encoding="utf-8",
    )
    (tmp_path / ".campaign.lock").touch()
    first = cfg["train_experiments"][0]
    campaign.append_ledger(tmp_path / "ledger.jsonl", {
        **first,
        "status": "blocked",
        "run_id": "blocked-" + first["cell_id"],
        "gates": {"submitted": False},
    })

    class API:
        base_url = "http://127.0.0.1:8010"

        def get(self, path):
            assert path == "/api/runs"
            return []

        def post(self, *args, **kwargs):
            pytest.fail("a later train cell must not start after a blocked gate")

    monkeypatch.setattr(campaign.runner, "ApiClient", lambda *a, **kw: API())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION,
        "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *_: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *_: None)

    with pytest.raises(RuntimeError, match="prior cell .* is not fully validated"):
        campaign.execute(tmp_path, resume=True, through_train_count=5)


def test_train_only_gate_requires_confirmed_effect_recovery_cleanup_and_history():
    entry = {
        "status": "completed",
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True},
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete",
            "mode": "dual_write",
            "source": "historical_mcp",
            "telemetry": {
                family: {"status": "complete"}
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 2, "logs": 1, "traces": 1},
        },
    }

    assert campaign.train_cell_gate_passed(entry)
    entry["gates"]["mechanism_observed"]["passed"] = False
    assert not campaign.train_cell_gate_passed(entry)


@pytest.mark.parametrize(
    "mean_fraction,expected", [(.85, True), (.8, True), (.799, False)],
)
def test_cpu_mean80_amendment_revalidates_legacy_target_gate(mean_fraction, expected):
    cell = {
        "cell_id": "payment-cpu-hog-r0",
        "service": "payment",
        "chaos_type": "cpu-hog",
        "phase": "train",
    }
    entry = {
        "status": "completed",
        "run_id": "run-cpu-mean80",
        "attempt": 1,
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {
                "passed": False,
                "metric": "cpu_cores",
                "baseline": {"status": "ok"},
                "intervention": {"status": "ok"},
                "intervention_mean_quota_fraction": mean_fraction,
                "target_attainment": {
                    "passed": False,
                    "observed_mean_fraction": mean_fraction,
                    "observed_seconds": 25,
                },
            },
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete",
            "mode": "dual_write",
            "source": "historical_mcp",
            "telemetry": {
                family: {"status": "complete"}
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 840, "logs": 20, "traces": 1},
        },
    }
    amendment = {
        "version": campaign.CPU_MEAN80_AMENDMENT_VERSION,
        "minimum_quota_fraction": .8,
    }
    assessment = campaign.cpu_mean80_revalidation_record(
        cell, entry, amendment,
    )
    assert assessment is not None
    entry["cpu_mean80_revalidation"] = assessment
    assert assessment["diagnostic_seconds_above_legacy_threshold"] == 25
    assert assessment["eligible_for_training"] is expected
    assert campaign.train_cell_gate_passed(entry) is expected


def test_cpu_mean80_amendment_does_not_override_recovery_gate():
    cell = {
        "cell_id": "payment-cpu-hog-r0",
        "service": "payment",
        "chaos_type": "cpu-hog",
        "phase": "train",
    }
    entry = {
        "status": "completed",
        "run_id": "run-cpu-unsafe",
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {
                "passed": False,
                "metric": "cpu_cores",
                "baseline": {"status": "ok"},
                "intervention": {"status": "ok"},
                "intervention_mean_quota_fraction": .85,
            },
            "recovery": {"passed": False},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete",
            "mode": "dual_write",
            "source": "historical_mcp",
            "telemetry": {
                family: {"status": "complete"}
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 840, "logs": 20, "traces": 1},
        },
    }
    assessment = campaign.cpu_mean80_revalidation_record(
        cell, entry, {
            "version": campaign.CPU_MEAN80_AMENDMENT_VERSION,
            "minimum_quota_fraction": .8,
        },
    )
    assert assessment is not None
    entry["cpu_mean80_revalidation"] = assessment
    assert assessment["mean_passed"] is True
    assert assessment["eligible_for_training"] is False
    assert not campaign.train_cell_gate_passed(entry)


def test_cpu_mean80_revalidation_appends_idempotent_sidecar_without_editing_ledger(
    tmp_path, monkeypatch,
):
    campaign_id = "controlled-20261103T234452388707Z"
    cell = {
        "cell_id": "payment-cpu-hog-r0",
        "service": "payment",
        "chaos_type": "cpu-hog",
        "phase": "train",
    }
    entry = {
        "cell_id": cell["cell_id"],
        "status": "completed",
        "run_id": "run-cpu-mean80",
        "attempt": 1,
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {
                "passed": False,
                "metric": "cpu_cores",
                "baseline": {"status": "ok"},
                "intervention": {"status": "ok"},
                "intervention_mean_quota_fraction": .85,
            },
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete",
            "mode": "dual_write",
            "source": "historical_mcp",
            "telemetry": {
                family: {"status": "complete"}
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 840, "logs": 20, "traces": 1},
        },
    }
    ledger_path = tmp_path / "ledger.jsonl"
    original_ledger = json.dumps(entry) + "\n"
    ledger_path.write_text(original_ledger, encoding="utf-8")
    (tmp_path / ".campaign.lock").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"experiment_campaign_id": campaign_id}),
        encoding="utf-8",
    )
    amendment = {
        "version": campaign.CPU_MEAN80_AMENDMENT_VERSION,
        "campaign_id": campaign_id,
        "minimum_quota_fraction": .8,
    }
    monkeypatch.setattr(campaign, "campaign_cells", lambda config: [cell])
    monkeypatch.setattr(
        campaign, "load_cpu_mean80_amendment", lambda: amendment,
    )

    first = revalidate_cpu_mean80.revalidate_campaign(tmp_path)
    second = revalidate_cpu_mean80.revalidate_campaign(tmp_path)

    sidecar = tmp_path / campaign.CPU_MEAN80_REVALIDATION_FILE
    records = campaign.read_ledger(sidecar)
    assert first["assessed"] == second["assessed"] == 1
    assert first["new_records"] == 1
    assert second["new_records"] == 0
    assert len(records) == 1
    assert records[0]["assessment"]["eligible_for_training"] is True
    assert records[0]["source_ledger_sha256"] == hashlib.sha256(
        original_ledger.encode()
    ).hexdigest()
    assert ledger_path.read_text(encoding="utf-8") == original_ledger


def test_cpu_mean80_revalidation_refuses_active_campaign(tmp_path):
    lock_path = tmp_path / ".campaign.lock"
    lock_path.write_text("", encoding="utf-8")
    with lock_path.open(encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="campaign is active"):
            revalidate_cpu_mean80.revalidate_campaign(tmp_path)


def test_technical_gate_failure_is_continuable_only_with_measured_evidence_and_safety_gates():
    entry = {
        "status": "completed",
        "gates": {
            "submitted": True,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {
                "passed": False,
                "baseline": {"status": "ok"},
                "intervention": {"status": "ok"},
            },
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
        "historical_validation": {
            "status": "complete",
            "mode": "dual_write",
            "source": "historical_mcp",
            "telemetry": {
                family: {"status": "complete"}
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": {"metrics": 2, "logs": 1, "traces": 1},
        },
    }

    assert not campaign.train_cell_gate_passed(entry)
    assert campaign.train_cell_technical_failure_continuable(entry)

    for status in ("blocked", "aborted", "failed", "stopped"):
        assert not campaign.train_cell_technical_failure_continuable({
            **entry, "status": status,
        })
    for gate, key, value in (
        ("recovery", "passed", False),
        ("cleanup", "passed", False),
        ("load_stopped", "stopped", False),
    ):
        unsafe = json.loads(json.dumps(entry))
        unsafe["gates"][gate][key] = value
        assert not campaign.train_cell_technical_failure_continuable(unsafe)
    invalid_history = json.loads(json.dumps(entry))
    invalid_history["historical_validation"]["status"] = "incomplete"
    assert not campaign.train_cell_technical_failure_continuable(invalid_history)
    missing_measurement = json.loads(json.dumps(entry))
    missing_measurement["gates"]["mechanism_observed"]["baseline"]["status"] = "no_data"
    assert not campaign.train_cell_technical_failure_continuable(missing_measurement)


def test_train_only_continuation_excludes_failed_run_and_preserves_saved_config(
    monkeypatch, tmp_path, capsys,
):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    saved_calibrations = {
        service: {
            "cpu": {
                "cores": 2,
                "load": 100,
                "target_quota_fraction": 1.0,
                "target_tolerance_fraction": .05,
                "calibration_status": "candidate",
                "rationale": "original saved profile",
            },
        }
        for service in campaign.SERVICES
    }
    cfg = campaign.build_config(
        source, tmp_path, "train-only", 42, saved_calibrations,
    )
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "plan.json").write_text(
        json.dumps(campaign.campaign_cells(cfg)), encoding="utf-8",
    )
    (tmp_path / ".campaign.lock").touch()
    history = {
        "status": "complete",
        "mode": "dual_write",
        "source": "historical_mcp",
        "telemetry": {
            family: {"status": "complete"}
            for family in ("metrics", "logs", "traces")
        },
        "source_counts": {"metrics": 2, "logs": 1, "traces": 1},
    }
    gates = {
        "submitted": True,
        "intervention_confirmed_on_target": True,
        "mechanism_observed": {
            "passed": True,
            "metric": "cpu_cores",
            "baseline": {"status": "ok"},
            "intervention": {"status": "ok"},
            "intervention_mean_quota_fraction": .85,
        },
        "recovery": {"passed": True},
        "cleanup": {"passed": True},
        "load_stopped": {"stopped": True},
    }
    failed_gates = json.loads(json.dumps(gates))
    failed_gates["intervention_confirmed_on_target"] = False
    failed_gates["mechanism_observed"] = {
        "passed": False,
        "baseline": {"status": "ok"},
        "intervention": {"status": "ok"},
    }
    cpu_failed_gates = json.loads(json.dumps(failed_gates))
    cpu_failed_gates["mechanism_observed"].update({
        "metric": "cpu_cores",
        "intervention_mean_quota_fraction": .85,
    })
    technical_failure_index = next(
        index for index, cell in enumerate(cfg["train_experiments"])
        if index not in (0, 1) and cell["chaos_type"] != "cpu-hog"
    )
    for index, cell in enumerate(cfg["train_experiments"]):
        if index == 1:
            for attempt in (1, 2):
                campaign.append_ledger(tmp_path / "ledger.jsonl", {
                    **cell,
                    "status": "blocked",
                    "attempt": attempt,
                    "run_id": f"blocked-{attempt}-{cell['cell_id']}",
                    "reason": "load did not stabilize after minimum 60s warmup",
                    "gates": {
                        "submitted": False,
                        "intervention_confirmed_on_target": None,
                        "cleanup": {"passed": True},
                        "load_stopped": {"stopped": True},
                        "application_stability": {
                            "healthy": True,
                            "unhealthy_services": [],
                            "unhealthy_nodes": [],
                        },
                    },
                })
            continue
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            **cell,
            "status": "completed",
            "run_id": "run-" + cell["cell_id"],
            "gates": (
                cpu_failed_gates if index == 0
                else failed_gates if index == technical_failure_index
                else gates
            ),
            "historical_validation": history,
        })

    cpu_amendment = {
        "version": campaign.CPU_MEAN80_AMENDMENT_VERSION,
        "campaign_id": cfg["experiment_campaign_id"],
        "minimum_quota_fraction": .8,
    }
    monkeypatch.setattr(
        campaign, "load_cpu_mean80_amendment", lambda: cpu_amendment,
    )
    cpu_cell = cfg["train_experiments"][0]
    cpu_entry = next(
        entry for entry in campaign.read_ledger(tmp_path / "ledger.jsonl")
        if entry["cell_id"] == cpu_cell["cell_id"]
    )
    assessment = campaign.cpu_mean80_revalidation_record(
        cpu_cell, cpu_entry, cpu_amendment,
    )
    assert assessment is not None and assessment["eligible_for_training"] is True
    campaign.record_cpu_mean80_revalidation(
        tmp_path, cpu_entry, assessment, cfg["experiment_campaign_id"],
    )

    class API:
        base_url = "http://127.0.0.1:8010"

        def get(self, path):
            assert path == "/api/runs"
            return []

        def post(self, *args, **kwargs):
            pytest.fail("terminal training cells must never be resubmitted")

    monkeypatch.setattr(campaign.runner, "ApiClient", lambda *a, **kw: API())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION,
        "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *_: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *_: None)

    assert campaign.execute(
        tmp_path,
        resume=True,
        through_train_count=35,
        continue_after_technical_failure=True,
        continue_after_safe_blocked_cell=True,
        cpu_calibration_path=campaign.HERE / "cpu-calibration.yaml",
    ) == 0

    progress = json.loads((tmp_path / "train_only_progress.json").read_text())
    assert progress["collection_complete"] is True
    assert progress["all_gates_passed"] is False
    assert progress["completed_and_gated_count"] == 33
    assert progress["excluded_technical_failure_count"] == 1
    assert progress["excluded_clean_pre_intervention_block_count"] == 1
    assert progress["not_started_count"] == 0
    assert sum(cell["eligible_for_training"] for cell in progress["cells"]) == 33
    assert progress["cells"][0]["cpu_mean80_revalidation"]["eligible_for_training"] is True
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert (
        saved["controlled_campaign"]["calibrations"]["payment"]["cpu"]
        ["target_quota_fraction"]
        == 1.0
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "campaign_events.jsonl").read_text().splitlines()
    ]
    assert any(event["event"] == "technical_failure_continuation_authorized" for event in events)
    assert any(event["event"] == "clean_pre_intervention_block_skip_authorized" for event in events)
    skipped = next(
        event for event in events
        if event["event"] == "clean_pre_intervention_block_skipped"
    )
    assert skipped["attempt"] == 2
    assert skipped["eligible_for_training"] is False
    excluded = [
        event for event in events
        if event["event"] == "technical_failure_excluded_from_training"
    ]
    assert len(excluded) == 1
    assert excluded[0]["eligible_for_training"] is False
    assert "Train-only collection complete with 1 technical failure" in capsys.readouterr().out


def test_incomplete_train_run_records_one_readback_without_waiting(tmp_path):
    api = Mock()
    api.get.return_value = {"status": "pending", "run_id": "run-aborted"}
    entry = {
        "status": "aborted",
        "run_id": "run-aborted",
        "gates": {"submitted": True},
    }

    with pytest.raises(RuntimeError, match="did not pass historical"):
        campaign.ensure_train_history_validation(
            tmp_path, {}, {"cell_id": "payment-cpu-hog-r0"}, entry, api,
            wait_for_completion=False,
        )

    api.get.assert_called_once_with(
        "/api/runs/run-aborted/history/validation"
    )
    saved = json.loads(
        (tmp_path / "history_validation/payment-cpu-hog-r0.json").read_text()
    )
    assert saved["status"] == "pending"


def test_missing_calibration_path_fails_with_argument_error_before_creating_campaign(capsys):
    with pytest.raises(SystemExit) as exc:
        campaign.main(["--run", "--calibration", "/caminho/calibracao.yaml"])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "calibration file does not exist or is not a file" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("content", ["[]", "null", "broken: ["])
def test_invalid_calibration_yaml_fails_before_creating_campaign(tmp_path, capsys, content):
    calibration = tmp_path / "calibration.yaml"
    calibration.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        campaign.main(["--run", "--calibration", str(calibration)])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def config(tmp_path):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    return campaign.build_config(source, tmp_path, "small", 42, {})


@pytest.mark.parametrize("level", ["supplemental-6", "test-replication-35-v1"])
def test_supplemental_completed_train_case_defers_cohort_rca_evaluation(
    monkeypatch, tmp_path, level,
):
    cfg = config(tmp_path)
    cell = {
        "cell_id": "front-end-memory-hog-r0",
        "service": "front-end",
        "chaos_type": "memory-hog",
        "replica_idx": 0,
        "phase": "train",
    }
    cfg["controlled_campaign"]["level"] = level
    cfg["train_experiments"] = [cell]
    cfg["test_experiments"] = []
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "plan.json").write_text(json.dumps([cell]))
    campaign.append_ledger(tmp_path / "ledger.jsonl", {
        "cell_id": cell["cell_id"],
        "run_id": "completed-run",
        "status": "completed",
        "gates": {
            "submitted": True,
            "recovery": {"passed": True},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    })
    api = Mock(base_url=cfg["platform"]["api_base"])
    api.get.return_value = []
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kwargs: {
        "strategy_version": campaign.STRATEGY_VERSION,
        "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(
        campaign,
        "evaluate_campaign",
        Mock(side_effect=AssertionError("single train case has no RCA test cohort")),
    )

    assert campaign.execute(tmp_path, resume=True) == 0
    campaign.evaluate_campaign.assert_not_called()
    api.post.assert_not_called()


@pytest.fixture
def continuation_parent(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / ".campaign.lock").touch()
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, parent, "medium", 42, {})
    (parent / "config.yaml").write_text(yaml.safe_dump(cfg))
    (parent / "plan.json").write_text(json.dumps(cfg["test_experiments"]))
    reserved = {"catalogue-network-latency-r0", "orders-container-kill-r0",
                "front-end-network-latency-r0"}
    complete = [c for c in cfg["test_experiments"] if c["cell_id"] not in reserved][:23]
    for c in complete:
        campaign.append_ledger(parent / "ledger.jsonl", {
            "cell_id": c["cell_id"], "status": "completed", "run_id": "old-" + c["cell_id"],
        })
    for cell in reserved:
        campaign.append_ledger(parent / "ledger.jsonl", {
            "cell_id": cell,
            "status": "blocked" if cell == "front-end-network-latency-r0" else "aborted",
            "run_id": "old-" + cell,
        })
    return parent, cfg


def test_continuation_preserves_parent_and_only_plans_unfinished_cells(continuation_parent):
    parent, cfg = continuation_parent
    original = {p.name: p.read_bytes() for p in parent.iterdir()}
    target = parent.parent / "continuation"
    result = campaign.build_continuation(parent, target, "catalogue-network-latency-r0")
    assert len(result["test_experiments"]) == 12
    assert result["test_experiments"][0]["cell_id"] == "catalogue-network-latency-r0"
    assert result["continuation"]["inherited_completed_count"] == 23
    assert result["continuation"]["parent_campaign_id"] == parent.name
    assert set(result["continuation"]["source_sha256"]) == {"config.yaml", "plan.json", "ledger.jsonl"}
    assert result["experiment_campaign_id"] == target.name
    assert result["output"]["results_dir"] == str(target)
    for key in ("controlled_campaign", "timeline", "load_profile", "ordering"):
        assert result[key] == cfg[key]
    assert original == {p.name: p.read_bytes() for p in parent.iterdir()}
    assert not target.exists()
    latest = result["continuation"]["parent_latest_results"]
    assert all(latest.get(c["cell_id"], {}).get("status") != "completed"
               for c in result["test_experiments"])


def test_continuation_rejects_active_parent(continuation_parent):
    parent, _ = continuation_parent
    with (parent / ".campaign.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="parent campaign is active"):
            campaign.build_continuation(parent, parent.parent / "next", "catalogue-network-latency-r0")


def test_continuation_rejects_nonterminal_parent(continuation_parent):
    parent, _ = continuation_parent
    campaign.append_ledger(parent / "ledger.jsonl", {
        "cell_id": "catalogue-network-latency-r0", "status": "submitted", "run_id": "unresolved",
    })
    with pytest.raises(RuntimeError, match="nonterminal"):
        campaign.build_continuation(parent, parent.parent / "next", "catalogue-network-latency-r0")


def test_continuation_cannot_revalidate_a_completed_cell(continuation_parent):
    parent, _ = continuation_parent
    completed = campaign.read_ledger(parent / "ledger.jsonl")[0]["cell_id"]
    with pytest.raises(RuntimeError, match="pending parent combination"):
        campaign.build_continuation(parent, parent.parent / "next", completed)


@pytest.mark.parametrize("missing", [
    "status", "submitted", "intervention_confirmed_on_target", "mechanism_observed",
    "recovery", "cleanup", "load_stopped",
])
def test_continuation_validation_requires_each_gate(missing):
    cfg = {"continuation": {"validation_cell": "validation"}}
    entry = {"status": "completed", "gates": {
        "submitted": True, "intervention_confirmed_on_target": True,
        "mechanism_observed": {"passed": True}, "recovery": {"passed": True},
        "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
    }}
    campaign.require_continuation_validation(cfg, {"validation": entry})
    if missing == "status":
        entry["status"] = "blocked"
    else:
        entry["gates"].pop(missing)
    with pytest.raises(RuntimeError, match="no next cell authorized"):
        campaign.require_continuation_validation(cfg, {"validation": entry})


@pytest.mark.parametrize("args", [
    ["--continue-from", "parent"],
    ["--validation-cell", "catalogue-network-latency-r0"],
    ["--continue-from", "parent", "--validation-cell", "x", "--resume", "parent"],
    ["--continue-from", "parent", "--validation-cell", "x", "--preflight"],
])
def test_continuation_cli_rejects_conflicting_or_incomplete_arguments(args):
    with pytest.raises(SystemExit) as exc:
        campaign.main(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("validation_result", ["blocked", "missing_mechanism", "completed"])
def test_continuation_executes_no_next_cell_before_validation(
    monkeypatch, tmp_path, validation_result,
):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:2]
    first = cfg["test_experiments"][0]["cell_id"]
    cfg["continuation"] = {"validation_cell": first, "parent_campaign_id": "parent"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    known = []
    for index, cell in enumerate(cfg["test_experiments"]):
        attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 1)
        attempt.mkdir(parents=True)
        spec = campaign.build_spec(cfg, cell, attempt)
        (attempt / "spec.json").write_text(json.dumps(spec))
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            "cell_id": cell["cell_id"], "status": "submitted", "run_id": f"run-{index}",
        })
        known.append({"run_id": f"run-{index}", "experiment_id": spec["experiment"]["id"],
                      "status": "running"})
    calls = []
    api = Mock(base_url=cfg["platform"]["api_base"])

    def get(path):
        if path == "/api/runs":
            return known
        calls.append(path)
        gates = {
            "submitted": True, "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True}, "recovery": {"passed": True},
            "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
        }
        status = "completed"
        if path.endswith("run-0"):
            if validation_result == "blocked":
                status = "blocked"
                gates["submitted"] = False
            elif validation_result == "missing_mechanism":
                gates.pop("mechanism_observed")
        return {"status": "completed" if status == "completed" else "failed",
                "summary": {"controlled_status": status, "controlled_gates": gates}}

    api.get.side_effect = get
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(campaign, "require_controlled_backend", Mock())
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", Mock())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data")))
    if validation_result == "completed":
        assert campaign.execute(tmp_path, resume=True) == 0
        assert calls == ["/api/runs/run-0", "/api/runs/run-1"]
        events = campaign.read_ledger(tmp_path / "campaign_events.jsonl")
        assert any(e["event"] == "continuation_validation_passed" for e in events)
    else:
        with pytest.raises(RuntimeError, match="no next cell authorized"):
            campaign.execute(tmp_path, resume=True)
        assert calls == ["/api/runs/run-0"]
    api.post.assert_not_called()


def recovered_entry(cell_id="orders-container-kill-r0"):
    return {
        "cell_id": cell_id, "status": "aborted", "run_id": "recovered-run",
        "reason": "recovery not confirmed; campaign must stop before next service",
        "gates": {
            "submitted": True, "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True}, "recovery": {"passed": False},
            "operational_recovery": {
                "passed": True, "policy_version": 1, "stable_seconds": 120,
                "source": "same_load_same_baseline_unchanged_recovery_thresholds",
            },
            "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
        },
    }


def late_operational_entry():
    entry = recovered_entry()
    entry["gates"]["operational_recovery"].update(
        passed=False,
        stable_seconds=21.4,
        target={"container_id": "container-1", "restart_count": 1},
    )
    entry["gates"]["late_operational_recovery"] = {
        "passed": True,
        "policy_version": 1,
        "stable_seconds": 120,
        "required_stable_seconds": 120,
        "source": "same_load_same_baseline_unchanged_recovery_thresholds",
        "target": {"container_id": "container-1", "restart_count": 1},
    }
    return entry


def late_recovery_entry():
    entry = recovered_entry("orders-container-kill-r1")
    entry["run_id"] = "run-065ac29ef2ca"
    entry["gates"]["recovery"]["passed"] = False
    entry["gates"]["operational_recovery"].update({
        "passed": False, "stable_seconds": 45.62205842899857,
    })
    entry["gates"]["late_recovery"] = {
        "version": campaign.LATE_RECOVERY_AMENDMENT_VERSION,
        "passed": True,
        "user_approved": True,
        "skip_only": True,
        "original_status_preserved": True,
        "thresholds_unchanged": True,
        "fresh_baseline_for_next_run": True,
        "amendment_sha256": "a" * 64,
        "window_seconds": 120,
    }
    return entry


@pytest.mark.parametrize("gate", ["cleanup", "load_stopped", "mechanism_observed", "operational_recovery"])
def test_continuation_requires_all_safety_gates(tmp_path, gate):
    cfg = config(tmp_path)
    entry = recovered_entry()
    assert campaign.operationally_recovered(cfg, entry)
    assert campaign.train_cell_failure_continuation_authorized(cfg, entry)
    entry["gates"][gate] = {}
    assert not campaign.operationally_recovered(cfg, entry)
    assert not campaign.train_cell_failure_continuation_authorized(cfg, entry)


def test_train_continuation_requires_operational_stability_and_keeps_run_excluded(tmp_path):
    cfg = config(tmp_path)
    entry = recovered_entry()

    assert campaign.train_cell_failure_continuation_authorized(cfg, entry)
    assert not campaign.train_cell_gate_passed(entry)

    entry["gates"]["operational_recovery"].update(
        passed=False, stable_seconds=21.4,
    )
    assert not campaign.train_cell_failure_continuation_authorized(cfg, entry)
    assert not campaign.train_cell_gate_passed(entry)


def test_late_operational_recovery_allows_only_skipping_the_aborted_cell(tmp_path):
    cfg = config(tmp_path)
    entry = late_operational_entry()

    assert cfg["controlled_campaign"]["resilience"]["late_operational_timeout_seconds"] == 600
    assert cfg["controlled_campaign"]["time_budget"]["stages"]["late_operational_recovery"] == 600
    assert campaign.operationally_recovered(cfg, entry)
    assert campaign.train_cell_failure_continuation_authorized(cfg, entry)
    assert not campaign.train_cell_gate_passed(entry)

    entry["gates"]["late_operational_recovery"]["target"]["restart_count"] = 2
    assert not campaign.operationally_recovered(cfg, entry)

    entry = late_operational_entry()
    cfg["controlled_campaign"]["resilience"]["late_operational_timeout_seconds"] = 0
    assert not campaign.operationally_recovered(cfg, entry)


def test_separate_late_check_requires_confirmed_load_cleanup(tmp_path):
    cfg = config(tmp_path)
    entry = late_operational_entry()
    gate = entry["gates"]["late_operational_recovery"]
    gate.update(
        late_check_id="late-check-1",
        late_check_cleanup_passed=False,
        late_check_load_stopped=True,
    )
    assert not campaign.operationally_recovered(cfg, entry)

    gate["late_check_cleanup_passed"] = True
    assert campaign.operationally_recovered(cfg, entry)


def test_passive_recovery_amendment_is_scoped_and_idempotent(tmp_path):
    cfg = config(tmp_path)
    cfg["experiment_campaign_id"] = "controlled-20261103T234452388707Z"

    assert campaign.apply_passive_recovery_extension_amendment(tmp_path, cfg)
    assert cfg["controlled_campaign"]["resilience"]["late_operational_timeout_seconds"] == 600
    assert cfg["platform"]["run_timeout_seconds"] == (
        cfg["controlled_campaign"]["time_budget"]["poll_seconds"]
    )
    assert campaign.apply_passive_recovery_extension_amendment(tmp_path, cfg)
    events = campaign.read_ledger(tmp_path / "campaign_events.jsonl")
    assert len(events) == 1
    assert events[0]["event"] == "passive_recovery_extension_amendment_applied"
    assert events[0]["scope"] == "future_runs_only"
    assert events[0]["automatic_restart"] is False

    other = config(tmp_path)
    assert not campaign.apply_passive_recovery_extension_amendment(tmp_path, other)


@pytest.mark.parametrize(
    "scenario",
    ["fresh", "interrupted", "resource_telemetry_failure", "latency_stability_failure"],
)
def test_late_recovery_check_is_scoped_and_append_only(monkeypatch, tmp_path, scenario):
    cfg = config(tmp_path)
    cfg["experiment_campaign_id"] = "controlled-20261103T234452388707Z"
    cfg["load_profile"] = {
        "command": "kubectl -n loadtest exec deploy/locust-web -- locust",
        "locust_file": "/mnt/locust/locustfile.py",
        "host": "http://front-end.sock-shop.svc.cluster.local",
        "users": 30,
        "spawn_rate": 5,
        "grace_seconds": 30,
    }
    original_here = campaign.HERE
    repo = tmp_path / "repo"
    memory_hog = repo / "experiment" / "eval" / "memory_hog"
    memory_hog.mkdir(parents=True)
    monkeypatch.setattr(campaign, "HERE", memory_hog)
    (memory_hog / campaign.LATE_RECOVERY_CHECK_FILE).write_text(
        (original_here / campaign.LATE_RECOVERY_CHECK_FILE).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    target = {
        "cell_id": "orders-container-kill-r0",
        "run_id": "run-503646080ca5",
    }
    run_dir = repo / "experiment" / "platform" / "data" / "object_store" / target["run_id"]
    run_dir.mkdir(parents=True)
    baseline_values = {
        "traffic": 5.0,
        "cpu_cores": .1,
        "memory_bytes": 400 * 2**20,
        "errors_percent": 0.0,
        "latency_ms": 305.0,
    }
    source_report = {
        "status": "aborted",
        "observations": [{
            "phase": "baseline",
            "summary": {
                name: {
                    "status": "ok",
                    "samples": 12,
                    "mean": value,
                    "p95": value,
                }
                for name, value in baseline_values.items()
            },
        }],
    }
    (run_dir / "controlled_report.json").write_text(
        json.dumps(source_report) + "\n", encoding="utf-8",
    )
    (run_dir / "effective_spec.json").write_text(
        json.dumps({"load_profile": {**cfg["load_profile"], "extra_args": []}}),
        encoding="utf-8",
    )
    source_hash = hashlib.sha256(
        (run_dir / "controlled_report.json").read_bytes()
    ).hexdigest()
    directory = tmp_path / "campaign"
    directory.mkdir()
    (directory / "plan.json").write_text(json.dumps([{
        "cell_id": target["cell_id"], "service": "orders", "chaos_type": "container-kill",
    }]), encoding="utf-8")
    entry = {"cell_id": target["cell_id"], "run_id": target["run_id"], "status": "aborted"}
    ledger = [entry]
    monkeypatch.setattr(campaign, "require_environment", Mock())
    late_directory = directory / "late_recoveries" / target["run_id"]
    if scenario != "fresh":
        late_directory.mkdir(parents=True)
        (late_directory / "report.json").write_text(json.dumps({
            "run_id": "late-recovery-interrupted",
            "source_run_id": target["run_id"],
            "source_report_sha256": source_hash,
            "status": "late_recovery_check",
            "started_at": "started",
            "pre_load_health": {"healthy": True},
            "gates": {},
            "load_handle": {},
        }), encoding="utf-8")
        monkeypatch.setattr(
            campaign, "cleanup_interrupted_late_recovery_load",
            Mock(return_value={"found": False, "stopped": True}),
        )
    if scenario == "resource_telemetry_failure":
        attempt_two = late_directory / "attempt-2"
        attempt_two.mkdir()
        previous_report_path = attempt_two / "report.json"
        previous_report = {
            "run_id": "late-recovery-attempt-2",
            "source_run_id": target["run_id"],
            "source_report_sha256": source_hash,
            "ended_at": "complete",
            "passed": False,
            "late_operational_recovery_passed": False,
            "pre_load_health": {"healthy": True},
            "load_handle": {"controlled_remote": True},
            "load_stopped": {"stopped": True},
            "container_kill_event": {
                "container_id_after_restart": "container-1",
                "restart_count_after_signal": 1,
            },
            "gates": {"late_operational_recovery": {
                "passed": False,
                "stable_seconds": 0,
                "required_stable_seconds": 120,
                "health": {"healthy": True},
                "target": {
                    "container_id": "container-1",
                    "restart_count": 1,
                    "ready": True,
                    "running": True,
                },
                "metric_checks": {
                    "traffic": {"passed": True},
                    "cpu_cores": {
                        "passed": False,
                        "reason": "missing_or_insufficient_samples",
                        "observed_status": "no_data",
                        "sample_count": 0,
                        "minimum_samples": 4,
                    },
                    "memory_bytes": {
                        "passed": False,
                        "reason": "missing_or_insufficient_samples",
                        "observed_status": "no_data",
                        "sample_count": 0,
                        "minimum_samples": 4,
                    },
                    "errors_percent": {"passed": True},
                    "latency_ms": {"passed": True},
                },
            }},
        }
        previous_report_path.write_text(json.dumps(previous_report), encoding="utf-8")
        entry["gates"] = {"late_operational_recovery": {
            "passed": False,
            "late_check_cleanup_passed": True,
            "late_check_load_stopped": True,
            "late_check_report_path": str(previous_report_path),
            "late_check_report_sha256": hashlib.sha256(
                previous_report_path.read_bytes()
            ).hexdigest(),
        }}
    if scenario == "latency_stability_failure":
        attempt_two = late_directory / "attempt-2"
        attempt_two.mkdir()
        (attempt_two / "report.json").write_text(json.dumps({
            "run_id": "late-recovery-attempt-2",
            "source_run_id": target["run_id"],
            "source_report_sha256": source_hash,
            "ended_at": "complete",
            "passed": False,
            "load_stopped": {"stopped": True},
        }), encoding="utf-8")
        attempt_three = late_directory / "attempt-3"
        attempt_three.mkdir()
        previous_report_path = attempt_three / "report.json"
        def recovery_summary(latency_p95):
            return {
                "traffic": {"status": "ok", "samples": 10, "mean": 5.0, "p95": 5.0},
                "cpu_cores": {"status": "ok", "samples": 10, "mean": .1, "p95": .1},
                "memory_bytes": {
                    "status": "ok",
                    "samples": 10,
                    "mean": 400 * 2**20,
                    "p95": 400 * 2**20,
                },
                "errors_percent": {"status": "ok", "samples": 10, "mean": 0.0, "p95": 0.0},
                "latency_ms": {
                    "status": "ok",
                    "samples": 10,
                    "mean": latency_p95,
                    "p95": latency_p95,
                },
            }

        previous_report = {
            "run_id": "late-recovery-attempt-3",
            "source_run_id": target["run_id"],
            "source_report_sha256": source_hash,
            "started_at": "started",
            "ended_at": "complete",
            "passed": False,
            "late_operational_recovery_passed": False,
            "late_recovery_policy": {
                "attempt": 3,
                "resource_measurement_source": "cgroup-v2",
            },
            "pre_load_health": {"healthy": True},
            "load_handle": {"controlled_remote": True},
            "load_stopped": {"stopped": True},
            "container_kill_event": {
                "container_id_after_restart": "container-1",
                "restart_count_after_signal": 1,
            },
            "telemetry": {"cgroup_v2": {
                "source": "kubectl_exec_inside_application_container",
                "scope": "exact application container; sidecars excluded",
                "container_id_after_restart": "container-1",
                "sample_count": 138,
            }},
            "observations": [
                {"phase": "recovery_check", "end": "2026-11-05T01:03:00+00:00",
                 "summary": recovery_summary(400.0)},
                {"phase": "recovery_check", "end": "2026-11-05T01:03:30+00:00",
                 "summary": recovery_summary(366.5)},
                {"phase": "recovery_check", "end": "2026-11-05T01:04:00+00:00",
                 "summary": recovery_summary(350.0)},
                {"phase": "recovery_check", "end": "2026-11-05T01:04:30+00:00",
                 "summary": recovery_summary(350.0)},
                {"phase": "recovery_check", "end": "2026-11-05T01:05:30+00:00",
                 "summary": recovery_summary(350.0)},
            ],
            "gates": {"late_operational_recovery": {
                "passed": False,
                "stable_seconds": 100.59434776099806,
                "required_stable_seconds": 120,
                "health": {"healthy": True},
                "target": {
                    "container_id": "container-1",
                    "restart_count": 1,
                    "ready": True,
                    "running": True,
                },
                "metric_checks": {
                    "traffic": {"passed": True, "threshold": 4.0, "observed": 5.0},
                    "cpu_cores": {"passed": True, "threshold": .15, "observed": .1},
                    "memory_bytes": {
                        "passed": True,
                        "threshold": 400 * 2**20 * 1.2,
                        "observed": 400 * 2**20,
                    },
                    "errors_percent": {"passed": True, "threshold": 1.0, "observed": 0.0},
                    "latency_ms": {
                        "passed": True,
                        "threshold": 366.0,
                        "observed": 312.5,
                    },
                },
            }},
        }
        previous_report_path.write_text(json.dumps(previous_report), encoding="utf-8")
        cfg["controlled_campaign"]["resilience"]["late_operational_timeout_seconds"] = 600
        entry["reason"] = "recovery not confirmed; campaign must stop before next service"
        entry["gates"] = {
            "submitted": True,
            "intervention_confirmed_on_target": True,
            "mechanism_observed": {"passed": True},
            "recovery": {"passed": False},
            "operational_recovery": {
                "passed": False,
                "policy_version": 1,
                "source": "same_load_same_baseline_unchanged_recovery_thresholds",
                "target": {"container_id": "container-1", "restart_count": 1},
            },
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
            "late_operational_recovery": {
                "passed": False,
                "late_check_attempt": 3,
                "late_check_cleanup_passed": True,
                "late_check_load_stopped": True,
                "late_check_report_path": str(previous_report_path),
                "late_check_report_sha256": hashlib.sha256(
                    previous_report_path.read_bytes()
                ).hexdigest(),
            },
        }
    from experiment.eval.memory_hog import load_check

    def fake_late_check(
        source_run_id,
        source_report_path,
        spec_path,
        check_directory,
        *,
        attempt_number,
        latency_p95_limit_override_ms=None,
    ):
        policy = {
            "attempt": attempt_number,
            "latency_p95_limit_override_ms": latency_p95_limit_override_ms,
            "thresholds_unchanged": latency_p95_limit_override_ms is None,
        }
        if latency_p95_limit_override_ms is not None:
            policy["amendment_version"] = campaign.LATE_RECOVERY_CHECK_VERSION
        report = {
            "run_id": f"late-recovery-test-{attempt_number}",
            "source_run_id": source_run_id,
            "source_report_sha256": source_hash,
            "ended_at": "complete",
            "passed": True,
            "late_recovery_policy": policy,
            "load_handle": {"provider": "locust"},
            "load_stopped": {"stopped": True},
            "gates": {"late_operational_recovery": {
                "passed": True,
                "policy_version": 1,
                "stable_seconds": 120,
                "source": (
                    "same_load_same_baseline_user_authorized_latency_p95_limit_override"
                    if latency_p95_limit_override_ms is not None
                    else "same_load_same_baseline_unchanged_recovery_thresholds"
                ),
                "latency_p95_limit_override_ms": latency_p95_limit_override_ms,
                "target": {"container_id": "container-1", "restart_count": 1},
            }},
        }
        check_directory.mkdir(parents=True)
        (check_directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return report

    late_check = Mock(side_effect=fake_late_check)
    monkeypatch.setattr(load_check, "run_late_operational_recovery_check", late_check)
    updated = campaign.apply_late_operational_recovery_check(directory, cfg, ledger)

    assert len(ledger) == 1
    if scenario in {"resource_telemetry_failure", "latency_stability_failure"}:
        assert ledger[0]["gates"]["late_operational_recovery"]["passed"] is False
    else:
        assert "late_operational_recovery" not in ledger[0].get("gates", {})
    assert len(updated) == 2
    latest = updated[-1]
    assert latest["status"] == "aborted"
    assert latest["gates"]["late_operational_recovery"]["passed"] is True
    assert latest["gates"]["late_operational_recovery"]["late_check_cleanup_passed"] is True
    assert late_check.call_count == 1
    expected_attempt = {
        "fresh": 1,
        "interrupted": 2,
        "resource_telemetry_failure": 3,
        "latency_stability_failure": 4,
    }[scenario]
    expected_directory = (
        late_directory if scenario == "fresh" else late_directory / f"attempt-{expected_attempt}"
    )
    assert late_check.call_args.args[3] == expected_directory
    assert late_check.call_args.kwargs["attempt_number"] == expected_attempt
    expected_limit = 370.0 if scenario == "latency_stability_failure" else None
    assert late_check.call_args.kwargs.get("latency_p95_limit_override_ms") == expected_limit
    assert campaign.apply_late_operational_recovery_check(directory, cfg, updated) == updated
    assert late_check.call_count == 1
    events = campaign.read_ledger(directory / "campaign_events.jsonl")
    if scenario == "interrupted":
        assert events[0]["event"] == "late_recovery_interrupted_attempt_cleanup_verified"
        assert events[1]["event"] == "late_operational_recovery_check_completed"
    elif scenario == "resource_telemetry_failure":
        assert events[0]["event"] == "late_recovery_cgroup_sampler_retry_authorized"
        assert events[0]["attempt_number"] == 3
        assert events[0]["other_thresholds_unchanged"] is True
        assert events[1]["event"] == "late_operational_recovery_check_completed"
    elif scenario == "latency_stability_failure":
        assert events[0]["event"] == "late_recovery_latency_amendment_applied"
        assert events[0]["latency_p95_limit_override_ms"] == 370.0
        assert events[1]["event"] == "late_recovery_latency_threshold_retry_authorized"
        assert events[1]["attempt_number"] == 4
        assert events[1]["other_thresholds_unchanged"] is True
        assert events[2]["event"] == "late_operational_recovery_check_completed"
        assert campaign.operationally_recovered(cfg, latest)
    else:
        assert events[0]["event"] == "late_operational_recovery_check_completed"
    assert events[-1]["original_status_preserved"] == "aborted"


def test_train_continuation_accepts_only_explicit_late_recovery_amendment(tmp_path):
    entry = late_recovery_entry()

    assert campaign.late_recovery_authorized(config(tmp_path), entry)
    assert campaign.train_cell_failure_continuation_authorized(
        config(tmp_path), entry,
    )
    assert not campaign.train_cell_gate_passed(entry)


def test_train_only_prior_gate_continuation_requires_explicit_flag_and_recovery(
    tmp_path,
):
    cfg = config(tmp_path)
    entry = late_operational_entry()

    assert not campaign.train_only_prior_cell_continuation_authorized(
        cfg, entry, continue_after_technical_failure=False,
    )
    assert campaign.train_only_prior_cell_continuation_authorized(
        cfg, entry, continue_after_technical_failure=True,
    )

    entry["gates"]["load_stopped"]["stopped"] = False
    assert not campaign.train_only_prior_cell_continuation_authorized(
        cfg, entry, continue_after_technical_failure=True,
    )


def test_legacy_campaign_cannot_gain_automatic_abort_clearance(tmp_path):
    cfg = config(tmp_path)
    del cfg["controlled_campaign"]["resilience"]
    assert not campaign.operationally_recovered(cfg, recovered_entry())


def test_recovery_circuit_breaker_survives_prepared_rows_and_resume(tmp_path):
    cfg = config(tmp_path)
    entries = [recovered_entry("first"), recovered_entry("second")]
    entries.append({"cell_id": "third", "status": "prepared"})
    with pytest.raises(RuntimeError, match="circuit breaker"):
        campaign.check_recovery_budget(cfg, entries)
    campaign.check_recovery_budget(cfg, [
        recovered_entry("first"), {"cell_id": "good", "status": "completed"},
        recovered_entry("second"),
    ])


def test_total_recovery_budget_is_not_reset_by_success(tmp_path):
    cfg = config(tmp_path)
    entries = []
    for i in range(5):
        entries.extend([recovered_entry(str(i)), {"cell_id": f"good-{i}", "status": "completed"}])
    with pytest.raises(RuntimeError, match="circuit breaker"):
        campaign.check_recovery_budget(cfg, entries)


def test_late_recovery_amendment_only_skips_the_approved_aborted_cell(tmp_path):
    cfg = config(tmp_path)
    entry = late_recovery_entry()
    assert campaign.late_recovery_authorized(cfg, entry)

    entry["cell_id"] = "payment-container-kill-r1"
    assert not campaign.late_recovery_authorized(cfg, entry)

    entry = late_recovery_entry()
    entry["gates"]["late_recovery"]["user_approved"] = False
    assert not campaign.late_recovery_authorized(cfg, entry)

    entry = late_recovery_entry()
    entry["gates"]["late_recovery"]["window_seconds"] = 119
    assert not campaign.late_recovery_authorized(cfg, entry)

    entry = late_recovery_entry()
    entry["status"] = "completed"
    assert not campaign.late_recovery_authorized(cfg, entry)


def test_unsubmitted_abort_amendment_only_skips_the_approved_no_submit_case(tmp_path):
    cfg = config(tmp_path)
    cfg["experiment_campaign_id"] = "exploratory-rag-20261028T191703038725Z"
    cfg["knowledge_campaign"] = {"exploratory": True}
    entry = {
        "cell_id": "front-end-network-latency-r1",
        "run_id": "run-4dbd8ae472af",
        "attempt": 1,
        "status": "aborted",
        "reason": "application unhealthy after blocked attempt; stop campaign",
        "gates": {
            "submitted": False,
            "intervention_confirmed_on_target": None,
            "recovery": {"passed": False, "status": "not_checked"},
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
            "unsubmitted_abort_skip": {
                "version": campaign.UNSUBMITTED_ABORT_AMENDMENT_VERSION,
                "passed": True,
                "user_approved": True,
                "skip_only": True,
                "original_status_preserved": True,
                "fresh_baseline_for_next_run": True,
                "amendment_sha256": "b" * 64,
            },
        },
    }
    assert campaign.unsubmitted_abort_skip_authorized(cfg, entry)

    entry["cell_id"] = "catalogue-network-latency-r1"
    assert not campaign.unsubmitted_abort_skip_authorized(cfg, entry)

    entry["cell_id"] = "front-end-network-latency-r1"
    entry["gates"]["submitted"] = True
    assert not campaign.unsubmitted_abort_skip_authorized(cfg, entry)

    entry["gates"]["submitted"] = False
    entry["gates"]["unsubmitted_abort_skip"]["user_approved"] = False
    assert not campaign.unsubmitted_abort_skip_authorized(cfg, entry)


def test_late_recovery_counts_toward_campaign_recovery_budget(tmp_path):
    cfg = config(tmp_path)
    campaign.check_recovery_budget(cfg, [
        recovered_entry("carts-container-kill-r0"),
        {"cell_id": "healthy-1", "status": "completed"},
        late_recovery_entry(),
    ])
    entries = []
    for index in range(4):
        entries.extend([
            recovered_entry(f"recovery-{index}"),
            {"cell_id": f"healthy-{index}", "status": "completed"},
        ])
    entries.append(late_recovery_entry())
    with pytest.raises(RuntimeError, match="circuit breaker"):
        campaign.check_recovery_budget(cfg, entries)


def test_automatic_pre_intervention_retry_requires_clean_unsubmitted_block(tmp_path):
    cfg = config(tmp_path)
    blocked = {
        "cell_id": "front-end-network-latency-r0",
        "attempt": 1,
        "run_id": "blocked-run",
        "status": "blocked",
        "gates": {
            "submitted": False,
            "intervention_confirmed_on_target": None,
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    }

    assert campaign.automatic_pre_intervention_retry_authorized(cfg, blocked)
    assert not campaign.automatic_pre_intervention_retry_authorized(
        cfg,
        {**blocked, "attempt": 2},
    )
    assert not campaign.automatic_pre_intervention_retry_authorized(
        cfg,
        {**blocked, "gates": {**blocked["gates"], "submitted": True}},
    )
    assert not campaign.automatic_pre_intervention_retry_authorized(
        cfg,
        {
            **blocked,
            "gates": {**blocked["gates"], "cleanup": {"passed": False}},
        },
    )

    legacy = {**cfg, "controlled_campaign": dict(cfg["controlled_campaign"])}
    legacy["controlled_campaign"]["resilience"] = dict(
        cfg["controlled_campaign"]["resilience"]
    )
    legacy["controlled_campaign"]["resilience"].pop("pre_intervention_retry")
    assert not campaign.automatic_pre_intervention_retry_authorized(legacy, blocked)


def test_train_failure_continuation_allows_only_the_preregistered_clean_block_retry(
    tmp_path,
):
    cfg = config(tmp_path)
    blocked = {
        "cell_id": "front-end-network-latency-r0",
        "run_id": "run-blocked",
        "status": "blocked",
        "attempt": 1,
        "gates": {
            "submitted": False,
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    }

    assert campaign.train_cell_failure_continuation_authorized(cfg, blocked)
    assert not campaign.train_cell_failure_continuation_authorized(
        cfg, {**blocked, "attempt": 2},
    )
    assert not campaign.train_cell_failure_continuation_authorized(
        cfg, {
            **blocked,
            "gates": {
                **blocked["gates"],
                "submitted": True,
            },
        },
    )
    assert not campaign.train_cell_failure_continuation_authorized(
        cfg, {
            **blocked,
            "gates": {
                **blocked["gates"],
                "load_stopped": {"stopped": False},
            },
        },
    )


def test_safe_block_skip_requires_exhausted_retry_and_healthy_safe_state(tmp_path):
    cfg = config(tmp_path)
    blocked = {
        "cell_id": "front-end-network-latency-r0",
        "run_id": "run-blocked",
        "status": "blocked",
        "attempt": 2,
        "gates": {
            "submitted": False,
            "intervention_confirmed_on_target": None,
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
            "application_stability": {
                "healthy": True,
                "unhealthy_services": [],
                "unhealthy_nodes": [],
            },
        },
    }

    assert campaign.clean_pre_intervention_block_skip_authorized(cfg, blocked)
    assert campaign.train_cell_failure_continuation_authorized(
        cfg, blocked, allow_clean_pre_intervention_skip=True,
    )
    assert not campaign.train_cell_failure_continuation_authorized(cfg, blocked)
    assert not campaign.clean_pre_intervention_block_skip_authorized(
        cfg, {**blocked, "attempt": 1},
    )

    unsafe_variants = [
        {"submitted": True},
        {"cleanup": {"passed": False}},
        {"load_stopped": {"stopped": False}},
        {"application_stability": {"healthy": False}},
        {
            "application_stability": {
                "healthy": True,
                "unhealthy_services": ["front-end"],
                "unhealthy_nodes": [],
            },
        },
    ]
    for changed_gate in unsafe_variants:
        gates = {**blocked["gates"], **changed_gate}
        assert not campaign.clean_pre_intervention_block_skip_authorized(
            cfg, {**blocked, "gates": gates},
        )


def test_rag_accepts_only_policy_bound_automatic_pre_intervention_retry(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"] = {"version": "skill-rag-ab-v1"}
    cell_id = cfg["test_experiments"][0]["cell_id"]
    blocked = {
        "cell_id": cell_id,
        "attempt": 1,
        "run_id": "blocked-run",
        "status": "blocked",
        "gates": {
            "submitted": False,
            "cleanup": {"passed": True},
            "load_stopped": {"stopped": True},
        },
    }
    initial = {**blocked, "status": "prepared", "gates": {}}
    retry = {
        "cell_id": cell_id,
        "attempt": 2,
        "status": "prepared",
        "retry_of_run_id": "blocked-run",
        "retry_mode": campaign.PRE_INTERVENTION_RETRY_MODE,
        "retry_policy_version": campaign.PRE_INTERVENTION_RETRY_VERSION,
    }
    submitted_retry = {**retry, "status": "submitted", "run_id": "retry-run"}

    assert (
        campaign.validate_rag_reviewed_retry_protocol(
            tmp_path,
            cfg,
            cfg["test_experiments"],
            [initial, blocked, retry, submitted_retry],
        )
        is None
    )

    retry["retry_policy_version"] = "unreviewed"
    with pytest.raises(RuntimeError, match="automatic retry is not authorized"):
        campaign.validate_rag_reviewed_retry_protocol(
            tmp_path,
            cfg,
            cfg["test_experiments"],
            [blocked, retry],
        )


def test_poll_retries_same_run_and_records_transient_errors(monkeypatch, tmp_path):
    api = Mock()
    api.get.side_effect = [urllib.error.URLError("connection refused"), {"status": "running"}]
    monkeypatch.setattr(campaign.time, "sleep", Mock())
    assert campaign.poll_run(api, "same-run", tmp_path, 3) == {"status": "running"}
    assert [call.args for call in api.get.call_args_list] == [("/api/runs/same-run",)] * 2
    api.post.assert_not_called()
    events = campaign.read_ledger(tmp_path / "campaign_events.jsonl")
    assert events[0]["event"] == "run_poll_retry"


def test_poll_retry_budget_is_bounded(monkeypatch, tmp_path):
    api = Mock()
    api.get.side_effect = urllib.error.URLError("connection refused")
    monkeypatch.setattr(campaign.time, "sleep", Mock())
    with pytest.raises(urllib.error.URLError):
        campaign.poll_run(api, "same-run", tmp_path, 3)
    assert api.get.call_count == 4


@pytest.mark.parametrize("error", [ValueError("malformed JSON"), RuntimeError("HTTP 403")])
def test_poll_does_not_retry_protocol_or_authorization_errors(monkeypatch, tmp_path, error):
    api = Mock()
    api.get.side_effect = error
    monkeypatch.setattr(campaign.time, "sleep", Mock())
    with pytest.raises(type(error)):
        campaign.poll_run(api, "same-run", tmp_path, 3)
    assert api.get.call_count == 1


@pytest.mark.parametrize("gate", ["partial_telemetry", "cgroup_telemetry"])
def test_operational_recovery_cannot_hide_artifact_failure(tmp_path, gate):
    entry = recovered_entry()
    entry["gates"][gate] = {"passed": False}
    assert not campaign.operationally_recovered(config(tmp_path), entry)


def test_recovered_aborted_run_advances_without_reinjection_and_keeps_failed_status(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:2]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    known = []
    for index, cell in enumerate(cfg["test_experiments"]):
        attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 1)
        attempt.mkdir(parents=True)
        spec = campaign.build_spec(cfg, cell, attempt)
        (attempt / "spec.json").write_text(json.dumps(spec))
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            "cell_id": cell["cell_id"], "status": "submitted", "run_id": f"run-{index}",
        })
        known.append({"run_id": f"run-{index}", "experiment_id": spec["experiment"]["id"], "status": "running"})
    calls = []

    class API:
        def __init__(self, base_url, **kwargs):
            self.base_url = base_url

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return known
            calls.append(path)
            if path == "/api/runs/run-0":
                record = recovered_entry()
                return {
                    "status": "failed", "summary": {
                        "controlled_status": "aborted", "reason": record["reason"],
                        "controlled_gates": record["gates"],
                    },
                }
            assert path == "/api/runs/run-1"
            return {"status": "completed", "summary": {
                "controlled_status": "completed",
                "controlled_gates": {
                    "recovery": {"passed": True}, "cleanup": {"passed": True},
                    "load_stopped": {"stopped": True},
                },
            }}

        def post(self, *args, **kwargs):
            pytest.fail("must attach to existing runs, never reinject")

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data")))

    assert campaign.execute(tmp_path, resume=True) == 1
    assert calls == ["/api/runs/run-0", "/api/runs/run-1"]
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert [row["status"] for row in rows] == ["aborted", "completed"]
    assert campaign.execute(tmp_path, resume=True) == 1
    assert calls == ["/api/runs/run-0", "/api/runs/run-1"]

class MCPWithScrapeTargets:
    def __init__(self, **_kwargs):
        pass

    def list_tools(self):
        return [{"name": "prometheus_scrape_targets"}]


def test_spec_uses_controlled_lifecycle_without_legacy_memory_allocation(tmp_path):
    cfg = config(tmp_path)
    cell = {"service": "catalogue", "chaos_type": "memory-hog", "replica_idx": 0,
            "cell_id": "catalogue-memory-hog-r0"}
    spec = campaign.build_spec(cfg, cell, tmp_path)
    assert spec["controlled_campaign"]["enabled"] is True
    assert spec["chaos_profile"]["resource_chaos"] is None
    assert spec["chaos_profile"]["fault_duration_seconds"] == 180
    assert spec["timeline"]["baseline_seconds"] == 90
    assert spec["timeline"]["warmup_seconds"] == 60
    assert spec["load_profile"]["users"] == 30
    assert spec["load_profile"]["spawn_rate"] == 5
    assert spec["observability"]["metrics"]["timeout_seconds"] == 30
    assert "target:catalogue" in spec["experiment"]["tags"]
    assert not any("container-kill" in t for t in spec["experiment"]["tags"])


def test_container_kill_plan_is_a_single_event_with_no_active_duration(tmp_path):
    cfg = config(tmp_path)
    cell = {"service": "user", "chaos_type": "container-kill", "replica_idx": 0,
            "cell_id": "user-container-kill-r0"}

    spec = campaign.build_spec(cfg, cell, tmp_path)

    assert spec["chaos_profile"]["fault_duration_seconds"] == 0
    assert spec["timeline"]["fault_duration_seconds"] == 0
    assert spec["timeline"]["post_recovery_observation_seconds"] == 120


def write_container_kill_retry_artifacts(tmp_path, cell, attempt_number, target, report, submitted):
    attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], attempt_number)
    attempt.mkdir(parents=True)
    report_path = tmp_path / f"controlled-report-{attempt_number}.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    pods_after_path = tmp_path / f"pods-after-{attempt_number}.json"
    pods_after_path.write_text(json.dumps({
        "items": [{
            "metadata": {"name": target["pod"], "uid": target["uid"]},
            "status": {"containerStatuses": [{
                "name": target["container"], "containerID": target["container_id"],
                "restartCount": target["restart_count"], "ready": True,
                "state": {"running": {}},
            }]},
        }],
    }), encoding="utf-8")
    gates = {
        "submitted": submitted,
        "cleanup": {"passed": True},
        "load_stopped": {"stopped": True},
        "application_stability": {"healthy": True},
    }
    (attempt / "run.json").write_text(json.dumps({
        "summary": {
            "controlled_status": "aborted", "controlled_gates": gates,
            "artifact_paths": {
                "controlled_report": str(report_path), "pods_after": str(pods_after_path),
            },
        },
    }), encoding="utf-8")
    return gates


def test_unconfirmed_container_kill_retry_requires_report_and_preserves_target_identity(tmp_path):
    cell = {
        "cell_id": "user-container-kill-r0", "service": "user",
        "chaos_type": "container-kill", "replica_idx": 0,
    }
    target = {
        "pod": "user-pod", "uid": "pod-uid", "container": "user",
        "container_id": f"docker://{'a' * 64}", "restart_count": 3,
    }
    report = {
        "status": "aborted", "gates": {"intervention_confirmed_on_target": False},
        "target_before": target,
        "container_kill_event": {
            "signal": "SIGKILL", "event_count_submitted": 1, "restart_observed": False,
            "target": {
                "pod": target["pod"], "pod_uid": target["uid"],
                "container": target["container"], "container_id": target["container_id"],
                "restart_count": target["restart_count"],
            },
        },
    }
    gates = write_container_kill_retry_artifacts(tmp_path, cell, 2, target, report, True)
    previous = {
        "cell_id": cell["cell_id"], "status": "aborted", "attempt": 2,
        "run_id": "prior-run", "gates": {**gates,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {"status": "restart_not_confirmed"},
        },
    }

    guard = campaign.unconfirmed_container_kill_retry_guard(tmp_path, cell, previous)

    assert guard == {
        "previous_run_id": "prior-run",
        "pod": target["pod"], "pod_uid": target["uid"],
        "container": target["container"], "container_id": target["container_id"],
        "restart_count": target["restart_count"],
    }


def test_unconfirmed_container_kill_retry_rejects_a_report_that_observed_restart(tmp_path):
    cell = {
        "cell_id": "user-container-kill-r0", "service": "user",
        "chaos_type": "container-kill", "replica_idx": 0,
    }
    target = {
        "pod": "user-pod", "uid": "pod-uid", "container": "user",
        "container_id": f"docker://{'a' * 64}", "restart_count": 3,
    }
    report = {
        "status": "aborted", "gates": {"intervention_confirmed_on_target": False},
        "target_before": target,
        "container_kill_event": {
            "event_count_submitted": 1, "restart_observed": True, "target": {},
        },
    }
    gates = write_container_kill_retry_artifacts(tmp_path, cell, 1, target, report, True)
    previous = {
        "cell_id": cell["cell_id"], "status": "aborted", "attempt": 1,
        "run_id": "prior-run", "gates": {**gates,
            "intervention_confirmed_on_target": False,
            "mechanism_observed": {"status": "restart_not_confirmed"},
        },
    }

    with pytest.raises(RuntimeError, match="do not prove the target stayed stable"):
        campaign.unconfirmed_container_kill_retry_guard(tmp_path, cell, previous)


def test_unsubmitted_container_kill_abort_can_retry_only_with_unchanged_target(tmp_path):
    cell = {
        "cell_id": "user-container-kill-r0", "service": "user",
        "chaos_type": "container-kill", "replica_idx": 0,
    }
    target = {
        "pod": "user-pod", "uid": "pod-uid", "container": "user",
        "container_id": f"docker://{'a' * 64}", "restart_count": 3,
    }
    report = {"status": "aborted", "gates": {}, "target_before": target}
    gates = write_container_kill_retry_artifacts(tmp_path, cell, 3, target, report, False)
    previous = {
        "cell_id": cell["cell_id"], "status": "aborted", "attempt": 3,
        "run_id": "prior-run", "gates": gates,
    }

    guard = campaign.unconfirmed_container_kill_retry_guard(tmp_path, cell, previous)

    assert guard["previous_run_id"] == "prior-run"
    assert guard["container_id"] == target["container_id"]


def test_http_manifest_excludes_front_end_probe_and_uses_service_port(tmp_path):
    cfg = config(tmp_path)
    cell = {"service": "front-end", "chaos_type": "http-status-code", "replica_idx": 0,
            "cell_id": "front-end-http-status-code-r0"}
    spec = campaign.build_spec(cfg, cell, tmp_path)
    manifest = yaml.safe_load(Path(spec["chaos_profile"]["manifest_path"]).read_text())
    rules = manifest["spec"]["http"]

    assert manifest["metadata"]["name"].startswith("controlled-front-end-")
    assert rules[0]["match"] == [{"uri": {"exact": "/"}}]
    assert "fault" not in rules[0]
    assert rules[1]["fault"]["abort"]["httpStatus"] == 500
    assert "match" not in rules[1]
    assert [
        rule["route"][0]["destination"]["port"]["number"] for rule in rules
    ] == [80, 80]


def test_ledger_preserves_all_states_export_keeps_repetitions(tmp_path):
    cells = campaign.build_plan("large")
    for index, cell in enumerate(cells):
        campaign.append_ledger(tmp_path / "ledger.jsonl", {**cell, "status": "prepared"})
        campaign.append_ledger(tmp_path / "ledger.jsonl", {**cell, "status": "completed", "run_id": f"r{index}"})
    entries = campaign.read_ledger(tmp_path / "ledger.jsonl")
    assert len(entries) == 350
    campaign.export_runs(tmp_path, cells, entries)
    rows = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert len(rows) == 175
    assert len({row["run_id"] for row in rows}) == 175


@pytest.mark.parametrize("terminal_status", ["completed", "blocked"])
def test_resume_pauses_after_terminal_test_before_submitting_next(
    monkeypatch, tmp_path, terminal_status
):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:2]
    for test_cell in cfg["test_experiments"]:
        test_cell["phase"] = "test"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    cell = cfg["test_experiments"][0]
    campaign.append_ledger(tmp_path / "ledger.jsonl", {
        **cell, "status": terminal_status, "run_id": "original",
    })

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url

        def get(self, path):
            assert path == "/api/runs"
            return []

        def post(self, *a, **kw):
            pytest.fail("next test submitted before the terminal test was validated")

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data")))
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *_: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *_: None)
    campaign.execute(tmp_path, resume=True, stop_after_first_test=True)


def test_retry_blocked_cell_preserves_history_and_uses_unique_attempt(
    monkeypatch, tmp_path
):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    cell = cfg["test_experiments"][0]
    original_attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 1)
    original_attempt.mkdir(parents=True)
    original_run_path = original_attempt / "run.json"
    original_run_path.write_text('{"run_id":"original"}\n')
    campaign.append_ledger(
        tmp_path / "ledger.jsonl",
        {
            **cell,
            "status": "blocked",
            "attempt": 1,
            "run_id": "original",
            "reason": "missing telemetry",
            "gates": {
                "submitted": False,
                "cleanup": {"passed": True},
                "load_stopped": {"stopped": True},
            },
        },
    )

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url
            self.spec = None

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return []
            assert path == "/api/runs/retry-run"
            return {
                "run_id": "retry-run",
                "status": "completed",
                "summary": {
                    "controlled_status": "blocked",
                    "controlled_gates": {"submitted": False},
                    "reason": "still missing telemetry",
                },
            }

        def post(self, path, body):
            if path == "/api/experiments":
                self.spec = body["spec"]
                assert self.spec["experiment"]["id"].endswith("-attempt-2")
                return {"version": 1}
            assert path == "/api/runs/start"
            assert body["idempotency_key"].endswith("-attempt-2")
            return {"run_id": "retry-run"}

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(
        campaign,
        "_get_strategy_info",
        lambda **kw: {
            "strategy_version": campaign.STRATEGY_VERSION,
            "prompt_version": campaign.PROMPT_VERSION,
        },
    )
    monkeypatch.setattr(
        campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data"))
    )

    assert (
        campaign.execute(
            tmp_path,
            resume=True,
            retry_blocked={cell["cell_id"]},
        )
        == 1
    )

    entries = campaign.read_ledger(tmp_path / "ledger.jsonl")
    assert entries[0]["run_id"] == "original"
    assert entries[-1]["attempt"] == 2
    assert entries[-1]["retry_of_run_id"] == "original"
    assert entries[-1]["run_id"] == "retry-run"
    assert json.loads(original_run_path.read_text())["run_id"] == "original"
    retry_attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 2)
    assert json.loads((retry_attempt / "spec.json").read_text())["experiment"][
        "id"
    ].endswith("-attempt-2")
    effects = json.loads((tmp_path / "effect_validation.json").read_text())
    assert [item["run_id"] for item in effects] == ["original", "retry-run"]


def test_automatic_retry_uses_fresh_attempt_after_clean_pre_injection_block(
    monkeypatch,
    tmp_path,
):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    cell = cfg["test_experiments"][0]

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url
            self.starts = 0

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return []
            if path == "/api/runs/blocked-run":
                return {
                    "run_id": "blocked-run",
                    "status": "completed",
                    "summary": {
                        "controlled_status": "blocked",
                        "controlled_gates": {
                            "submitted": False,
                            "cleanup": {"passed": True},
                            "load_stopped": {"stopped": True},
                        },
                        "reason": "load did not stabilize after minimum 60s warmup",
                    },
                }
            assert path == "/api/runs/retry-run"
            return {
                "run_id": "retry-run",
                "status": "completed",
                "summary": {
                    "controlled_status": "completed",
                    "controlled_gates": {
                        "submitted": True,
                        "intervention_confirmed_on_target": True,
                        "mechanism_observed": {"passed": True},
                        "recovery": {"passed": True},
                        "cleanup": {"passed": True},
                        "load_stopped": {"stopped": True},
                    },
                },
            }

        def post(self, path, body):
            if path == "/api/experiments":
                spec = body["spec"]
                if self.starts == 0:
                    assert spec["experiment"]["id"].endswith(cell["cell_id"])
                else:
                    assert spec["experiment"]["id"].endswith("-attempt-2")
                return {"version": 1}
            assert path == "/api/runs/start"
            self.starts += 1
            return {"run_id": "blocked-run" if self.starts == 1 else "retry-run"}

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(
        campaign,
        "_get_strategy_info",
        lambda **kw: {
            "strategy_version": campaign.STRATEGY_VERSION,
            "prompt_version": campaign.PROMPT_VERSION,
        },
    )
    monkeypatch.setattr(campaign, "require_environment", lambda *a, **kw: None)
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *_: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *_: None)
    monkeypatch.setattr(
        campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data"))
    )
    sleeps = []
    monkeypatch.setattr(campaign.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert campaign.execute(tmp_path, resume=False) == 0

    entries = campaign.read_ledger(tmp_path / "ledger.jsonl")
    blocked = next(
        row
        for row in entries
        if row.get("run_id") == "blocked-run" and row.get("status") == "blocked"
    )
    completed = next(
        row
        for row in entries
        if row.get("run_id") == "retry-run" and row.get("status") == "completed"
    )
    assert blocked["gates"]["submitted"] is False
    assert completed["attempt"] == 2
    assert completed["retry_of_run_id"] == "blocked-run"
    assert completed["retry_mode"] == campaign.PRE_INTERVENTION_RETRY_MODE
    assert sleeps == [60]
    events = campaign.read_ledger(tmp_path / "campaign_events.jsonl")
    assert {row["event"] for row in events} >= {
        "automatic_pre_intervention_retry_scheduled",
        "automatic_pre_intervention_retry_started",
    }
    effects = json.loads((tmp_path / "effect_validation.json").read_text())
    assert [(row["run_id"], row["result_status"]) for row in effects] == [
        ("blocked-run", "blocked"),
        ("retry-run", "completed"),
    ]


def test_retry_invalid_window_preserves_history_and_exports_latest_attempt(
    monkeypatch, tmp_path
):
    cfg = config(tmp_path)
    cell = {
        "service": "orders",
        "chaos_type": "http-status-code",
        "replica_idx": 0,
        "cell_id": "orders-http-status-code-r0",
    }
    cfg["test_experiments"] = [cell]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    original_attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 1)
    original_attempt.mkdir(parents=True)
    observation_window = tmp_path / "original-window.json"
    observation_window.write_text(
        json.dumps(
            {
                "phase_sources": {"fault": "virtualservice_api_bracket_estimated"},
                "phases": {
                    "fault": {
                        "start": "2026-10-19T00:00:00Z",
                        "end": "2026-10-19T00:02:00.025Z",
                    },
                    "post": {
                        "start": "2026-10-19T00:02:00Z",
                        "end": "2026-10-19T00:04:00Z",
                    },
                },
            }
        )
    )
    original_run_path = original_attempt / "run.json"
    original_run_path.write_text(json.dumps({
        "run_id": "original", "status": "completed",
        "summary": {
            "controlled_status": "completed",
            "controlled_gates": {
                "recovery": {"passed": True}, "cleanup": {"passed": True},
                "load_stopped": {"stopped": True},
            },
            "artifact_paths": {"observation_window": str(observation_window)},
        },
    }))
    campaign.append_ledger(tmp_path / "ledger.jsonl", {
        **cell, "status": "completed", "attempt": 1, "run_id": "original",
        "gates": {
            "submitted": True, "recovery": {"passed": True},
            "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
        },
    })

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url
            self.spec = None

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return []
            assert path == "/api/runs/retry-run"
            return {
                "run_id": "retry-run", "status": "completed",
                "summary": {
                    "controlled_status": "completed",
                    "controlled_gates": {
                        "submitted": True, "recovery": {"passed": True},
                        "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
                    },
                },
            }

        def post(self, path, body):
            if path == "/api/experiments":
                self.spec = body["spec"]
                assert self.spec["experiment"]["id"].endswith("-attempt-2")
                assert self.spec["controlled_campaign"]["superseded_observation_window"]["overlap_ms"] == 25.0
                return {"version": 1}
            assert path == "/api/runs/start"
            assert body["idempotency_key"].endswith("-attempt-2")
            return {"run_id": "retry-run"}

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data")))

    assert campaign.execute(
        tmp_path, resume=True, retry_invalid_window={cell["cell_id"]},
    ) == 0

    entries = campaign.read_ledger(tmp_path / "ledger.jsonl")
    assert entries[0]["run_id"] == "original"
    assert entries[-1]["attempt"] == 2
    assert entries[-1]["supersedes_run_id"] == "original"
    assert entries[-1]["run_id"] == "retry-run"
    assert json.loads(original_run_path.read_text())["run_id"] == "original"
    exported = list(csv.DictReader((tmp_path / "runs.csv").open()))
    assert exported[0]["run_id"] == "retry-run"
    events = [json.loads(line) for line in (tmp_path / "campaign_events.jsonl").read_text().splitlines()]
    assert any(event["event"] == "observation_window_attempt_invalidated" for event in events)


def test_reviewed_container_kill_retry_requires_confirmed_clean_attempt(tmp_path):
    cell = {
        "service": "carts", "chaos_type": "container-kill", "replica_idx": 0,
        "cell_id": "carts-container-kill-r0",
    }
    previous = {"cell_id": cell["cell_id"], "status": "aborted", "attempt": 1, "run_id": "old-kill"}
    attempt = campaign.attempt_directory(tmp_path, cell["cell_id"], 1)
    attempt.mkdir(parents=True)
    run_path = attempt / "run.json"
    run_path.write_text(json.dumps({
        "status": "failed",
        "summary": {
            "controlled_status": "aborted",
            "controlled_gates": {
                "submitted": True,
                "mechanism_observed": {"passed": True, "restart_count_delta": 1},
                "recovery": {"passed": False},
                "cleanup": {"passed": True},
                "load_stopped": {"stopped": True},
                "application_stability": {"healthy": True},
            },
        },
    }))

    guard = campaign.reviewed_container_kill_retry_guard(tmp_path, cell, previous)
    assert guard["previous_run_id"] == "old-kill"
    assert guard["retry_authorized_by_user"] is True
    assert guard["current_run_preflight_required"] is True

    record = json.loads(run_path.read_text())
    record["summary"]["controlled_gates"]["cleanup"]["passed"] = False
    run_path.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="does not prove one restart"):
        campaign.reviewed_container_kill_retry_guard(tmp_path, cell, previous)


def test_unresolved_abort_blocks_resume_before_any_request(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    campaign.append_ledger(tmp_path / "ledger.jsonl", {"cell_id": "x", "status": "aborted", "reason": "cleanup failed"})
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    with pytest.raises(RuntimeError, match="unresolved abort"):
        campaign.execute(tmp_path, resume=True)


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        campaign.build_plan("unknown")


def test_cpu_candidate_yaml_covers_all_services_with_minimum_quota_gate():
    values = yaml.safe_load((campaign.HERE / "cpu-calibration.yaml").read_text())
    assert set(values) - {"policy"} == set(campaign.SERVICES)
    for service in campaign.SERVICES:
        profile = values[service]["cpu"]
        assert profile["cores"] == 2
        assert profile["load"] == 100
        assert profile["minimum_quota_fraction"] == .80
        assert "target_quota_fraction" not in profile
        assert "target_tolerance_fraction" not in profile
        assert profile["calibration_status"] == "candidate"
        assert profile["rationale"]


def test_campaign_lock_refuses_a_second_executor_before_requests(tmp_path):
    with (tmp_path / ".campaign.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another executor"):
            campaign.execute(tmp_path, resume=True)


@pytest.mark.parametrize("late_recovery", [False, True])
def test_lost_start_response_reconnects_without_posting_and_exports_effect(monkeypatch, tmp_path, late_recovery):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    cell = cfg["test_experiments"][0]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    attempt = tmp_path / "attempts" / cell["cell_id"]
    attempt.mkdir(parents=True)
    spec = campaign.build_spec(cfg, cell, attempt)
    (attempt / "spec.json").write_text(json.dumps(spec))
    campaign.append_ledger(tmp_path / "ledger.jsonl", {**cell, "status": "prepared"})
    gates = {
        "submitted": True, "intervention_confirmed_on_target": True,
        "mechanism_observed": {"passed": True}, "impact_observed": {"errors_increased": False},
        "recovery": {"passed": True}, "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
    }
    if late_recovery:
        gates = recovered_entry()["gates"]
    run = {
        "run_id": "original", "experiment_id": spec["experiment"]["id"], "status": "running",
    }

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return [run]
            assert path == "/api/runs/original"
            return {**run, "status": "failed" if late_recovery else "completed", "summary": {
                "controlled_status": "aborted" if late_recovery else "completed", "controlled_gates": gates,
                "reason": recovered_entry()["reason"] if late_recovery else "",
            }}

        def post(self, *a, **kw):
            pytest.fail("lost-response run must not be resubmitted")

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", lambda *a, **kw: (Path("report"), Path("data")))
    assert campaign.execute(tmp_path, resume=True) == (1 if late_recovery else 0)
    assert campaign.read_ledger(tmp_path / "ledger.jsonl")[-1]["run_id"] == "original"
    effect = json.loads((tmp_path / "effect_validation.json").read_text())[0]
    assert effect["passed"] is True
    if late_recovery:
        assert effect["result_status"] == "aborted"
        assert effect["checks"]["recovery"]["passed"] is False
        assert campaign.execute(tmp_path, resume=True) == 1
        assert campaign.read_ledger(tmp_path / "ledger.jsonl")[-1]["status"] == "aborted"
    else:
        assert effect["checks"]["impact_observed"]["errors_increased"] is False


def test_legacy_backend_is_rejected_before_run_submission(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url

        def get(self, path):
            assert path == "/api/controlled-campaign/capabilities"
            raise RuntimeError("HTTP 404 on GET /api/controlled-campaign/capabilities")

        def post(self, *a, **kw):
            pytest.fail("legacy backend must be rejected before any run submission")

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    with pytest.raises(RuntimeError, match="must be restarted.*No new run was submitted"):
        campaign.execute(tmp_path, resume=False)


def test_missing_scrape_metadata_tool_blocks_campaign_before_run_submission(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            pytest.fail(f"unexpected API request before tool preflight: {path}")

        def post(self, *a, **kw):
            pytest.fail("missing MCP tool must prevent run submission")

    class MCP:
        def __init__(self, sse_url, timeout_seconds):
            assert sse_url == "http://127.0.0.1:18082/sse"

        def list_tools(self):
            return [{"name": "prometheus_instant_query"}]

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "MCPToolClient", MCP)
    with pytest.raises(RuntimeError, match="missing required tool.*No run was submitted"):
        campaign.execute(tmp_path, resume=False)


@pytest.mark.parametrize("capabilities", [
    {"protocol_version": 1}, {"protocol_version": True}, {},
])
def test_campaign_rejects_malformed_or_incompatible_capabilities(capabilities):
    class API:
        def get(self, path):
            assert path == "/api/controlled-campaign/capabilities"
            return capabilities

    with pytest.raises(RuntimeError, match="controlled backend protocol mismatch"):
        campaign.require_controlled_backend(API())


def test_campaign_rejects_stale_code_even_with_matching_protocol():
    capabilities = campaign.controlled_backend_capabilities()
    capabilities["implementation_sha256"] = "0" * 64
    api = Mock()
    api.get.return_value = capabilities

    with pytest.raises(RuntimeError, match="implementation mismatch.*restart"):
        campaign.require_controlled_backend(api)


def test_campaign_accepts_matching_loaded_implementation():
    api = Mock()
    api.get.return_value = campaign.controlled_backend_capabilities()

    campaign.require_controlled_backend(api)


def test_missing_controlled_status_is_recorded_as_protocol_abort(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:1]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    run = {"run_id": "legacy-run", "status": "running", "experiment_id": ""}

    class API:
        def __init__(self, base_url, **kw):
            self.base_url = base_url

        def get(self, path):
            if path == "/api/controlled-campaign/capabilities":
                return campaign.controlled_backend_capabilities()
            if path == "/api/runs":
                return []
            assert path == "/api/runs/legacy-run"
            return {**run, "status": "completed", "summary": {}}

        def post(self, path, body):
            if path == "/api/experiments":
                run["experiment_id"] = body["spec"]["experiment"]["id"]
                return {"version": 1}
            assert path == "/api/runs/start"
            return {"run_id": "legacy-run"}

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "MCPToolClient", MCPWithScrapeTargets)
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    with pytest.raises(RuntimeError, match="backend protocol mismatch.*controlled_status"):
        campaign.execute(tmp_path, resume=False)

    entry = campaign.read_ledger(tmp_path / "ledger.jsonl")[-1]
    assert entry["status"] == "aborted"
    assert "controlled_status" in entry["reason"]
    attempt = tmp_path / "attempts" / cfg["test_experiments"][0]["cell_id"]
    assert json.loads((attempt / "run.json").read_text())["run_id"] == "legacy-run"


@pytest.mark.parametrize("policy", [
    {"sustained_seconds": 0}, {"sustained_ratio": 1},
    {"sustained_seconds": float("nan")}, {"unsafe_auto_escalation": True},
])
def test_policy_is_explicit_and_validated_before_execution(tmp_path, policy):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    with pytest.raises(ValueError, match="policy"):
        campaign.build_config(source, tmp_path, "small", 42, {"policy": policy})


def test_default_cli_loads_candidate_for_entire_medium_plan(monkeypatch, tmp_path):
    captured = {}

    def execute(directory, **kwargs):
        captured.update(yaml.safe_load((directory / "config.yaml").read_text()))
        return 0

    monkeypatch.setattr(campaign, "HERE", tmp_path)
    source = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text())
    candidate = yaml.safe_load((Path(__file__).parents[1] / "cpu-calibration.yaml").read_text())
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(source))
    (tmp_path / "cpu-calibration.yaml").write_text(yaml.safe_dump(candidate))
    monkeypatch.setattr(campaign, "execute", execute)
    assert campaign.main(["--level", "medium", "--run"]) == 0
    assert set(captured["controlled_campaign"]["calibrations"]) - {"policy"} == set(campaign.SERVICES)
    assert captured["load_profile"]["users"] == 30
    assert captured["load_profile"]["spawn_rate"] == 5
    assert captured["controlled_campaign"]["recovery_timeout_seconds"] == 300
    assert captured["controlled_campaign"]["resilience"]["stable_seconds"] == 120
    assert captured["controlled_campaign"]["resilience"]["late_operational_timeout_seconds"] == 600
    assert captured["controlled_campaign"]["time_budget"]["stages"]["late_operational_recovery"] == 600
    assert captured["platform"]["run_timeout_seconds"] == (
        captured["controlled_campaign"]["time_budget"]["poll_seconds"]
    )
    assert captured["controlled_campaign"]["resilience"]["pre_intervention_retry"] == {
        "version": campaign.PRE_INTERVENTION_RETRY_VERSION,
        "max_attempts": 2,
        "backoff_seconds": 60,
    }
    assert len(captured["test_experiments"]) == 35


@pytest.mark.parametrize("args", [["--preflight", "--run"], ["--preflight", "--resume", "old"]])
def test_preflight_cannot_start_or_resume_campaign(args):
    with pytest.raises(SystemExit) as exc:
        campaign.main(args)
    assert exc.value.code == 2


def test_preflight_cli_does_not_create_files_or_submit_runs(monkeypatch):
    api = Mock(base_url="http://127.0.0.1:8010")
    api.get.side_effect = lambda path: {
        "/api/runs": [], "/api/llm/health": {"ok": True},
    }[path]
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(campaign, "require_controlled_backend", Mock())
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", Mock())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "environment_preflight", Mock(return_value={"passed": True}))
    monkeypatch.setattr(campaign.Path, "mkdir", Mock(side_effect=AssertionError("preflight wrote files")))
    monkeypatch.setattr(campaign, "execute", Mock(side_effect=AssertionError("preflight executed campaign")))
    assert campaign.main(["--level", "medium", "--preflight"]) == 0
    api.post.assert_not_called()


@pytest.fixture
def infrastructure(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = [{"service": "payment", "chaos_type": "cpu-hog", "replica_idx": 0}]
    cfg["controlled_campaign"]["calibrations"] = {
        "payment": {"cpu": {"cores": 1, "load": 100, "evidence": "reviewed-calibration"}},
    }
    node = {"metadata": {"name": "node", "uid": "node-uid"}, "status": {
        "conditions": [{"type": key, "status": value} for key, value in {
            "Ready": "True", "MemoryPressure": "False", "DiskPressure": "False", "PIDPressure": "False",
        }.items()], "allocatable": {"cpu": "16"}, "nodeInfo": {},
    }}
    deployments = [{"metadata": {"name": service, "generation": 1},
                    "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                        "name": service, "image": f"sockshop/{service}:test", "resources": {},
                    }]}}},
                    "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 1}}
                   for service in campaign.SERVICES]
    pod = {"metadata": {"name": "payment-pod", "uid": "pod-uid"}, "spec": {"containers": [{
        "name": "payment", "resources": {"limits": {"cpu": "300m", "memory": "512Mi"}},
    }]}, "status": {"containerStatuses": [
        {"name": "payment", "ready": True, "containerID": "docker://id", "imageID": "sha256:image"},
        {"name": "istio-proxy", "ready": True},
    ]}}
    workflows = [{"metadata": {"name": "payment-cpu-hog"}}]
    health = {"services": {"prometheus": "ok", "loki": "ok", "tempo": "ok"}}
    load = []

    def kubectl(*args):
        if "nodes" in args:
            return {"items": [node]}
        if "deployments" in args:
            return {"items": deployments}
        if "pods" in args:
            return {"items": [pod]}
        if "workflows.argoproj.io" in args:
            return {"items": workflows}
        assert "virtualservices" in args
        return {"items": []}

    def exec_command(*args):
        if "--raw" in args:
            return Mock(returncode=0, stdout="ready")
        if "loadtest" in args:
            return Mock(returncode=0, stdout=json.dumps({"headless_processes": load, "web_user_count": 0}))
        return Mock(returncode=0, stdout=(
            "__cpu_max__\n30000 100000\n"
            "__cpu_stat__\nusage_usec 1\nnr_throttled 0\nthrottled_usec 0\n"
            "__memory_current__\n100\n__memory_max__\n536870912\n"
            "__memory_events__\noom 0\noom_kill 0\n"
        ))

    monkeypatch.setattr(campaign, "kubectl", kubectl)
    monkeypatch.setattr(campaign.runner, "_kubectl", exec_command)
    monkeypatch.setattr(campaign, "validate_workflow_template", Mock(return_value={"runner": ["reviewed"]}))
    client = Mock()
    client.call_tool_json.return_value = health
    client.list_tools.return_value = [
        {"name": "health_check"},
        {"name": "get_run_golden_signals"},
    ]
    monkeypatch.setattr(campaign, "MCPToolClient", Mock(return_value=client))
    return cfg, node, pod, workflows, health, load


def test_read_only_environment_preflight_records_container_conditions(infrastructure):
    cfg, *_ = infrastructure
    result = campaign.environment_preflight(cfg)
    assert result["passed"]
    assert result["load_profile"] == {"users": 30, "spawn_rate": 5}
    assert result["environment_profile"]["target:payment"]["resources"]["limits"]["cpu"] == "300m"
    campaign.validate_workflow_template.assert_called_once()


@pytest.mark.parametrize("failure,check", [
    ("node", "nodes"), ("container", "target:payment"),
    ("limits", "target:payment"), ("template", "reviewed_templates"),
    ("cpu", "cpu_profile:payment"), ("tempo", "observability_backends"),
    ("orphan", "load_isolation"),
])
def test_preflight_reports_explicit_failures_without_injection(infrastructure, failure, check):
    cfg, node, pod, workflows, health, load = infrastructure
    if failure == "node":
        node["status"]["conditions"][0]["status"] = "Unknown"
    elif failure == "container":
        pod["status"]["containerStatuses"][0]["ready"] = False
    elif failure == "limits":
        pod["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "500m"
    elif failure == "template":
        workflows.clear()
    elif failure == "cpu":
        cfg["controlled_campaign"]["calibrations"].clear()
    elif failure == "tempo":
        health["services"]["tempo"] = "unavailable"
    elif failure == "orphan":
        load.append("42")
    result = campaign.environment_preflight(cfg)
    assert result["passed"] is False
    failed = next(c for c in result["checks"] if c["name"] == check)
    assert failed["passed"] is False
    assert failed["error"]


def test_environment_change_blocks_resume_and_records_failed_preflight(monkeypatch, tmp_path, infrastructure):
    cfg, _, pod, *_ = infrastructure
    api = Mock()
    api.get.side_effect = lambda path: [] if path == "/api/runs" else {"ok": True}
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    require_environment(cfg, tmp_path)
    pod["spec"]["containers"][0]["env"] = [{"name": "JAVA_OPTS", "value": "changed"}]
    with pytest.raises(RuntimeError, match="start a new campaign"):
        require_environment(cfg, tmp_path)
    reports = campaign.read_ledger(tmp_path / "preflight.jsonl")
    assert reports[0]["passed"] is True
    assert reports[1]["passed"] is False
    api.post.assert_not_called()


@pytest.mark.parametrize("probe", ["startupProbe", "livenessProbe", "readinessProbe"])
def test_probe_change_blocks_resume_without_rewriting_baseline(
    monkeypatch, tmp_path, infrastructure, probe,
):
    cfg, *_ = infrastructure
    api = Mock()
    api.get.side_effect = lambda path: [] if path == "/api/runs" else {"ok": True}
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    require_environment(cfg, tmp_path)
    deployment = campaign.kubectl("get", "deployments")["items"][0]
    deployment["spec"]["template"]["spec"]["containers"][0][probe] = {
        "httpGet": {"path": "/health", "port": 80},
    }
    with pytest.raises(RuntimeError, match="probes changed"):
        require_environment(cfg, tmp_path)
    reports = campaign.read_ledger(tmp_path / "preflight.jsonl")
    first = reports[0]["environment_profile"]["deployments"][deployment["metadata"]["name"]][0]
    assert first["probes"][probe] is None
    assert reports[-1]["passed"] is False
    api.post.assert_not_called()


def test_missing_probe_baseline_does_not_silently_certify_old_campaign(
    monkeypatch, tmp_path, infrastructure,
):
    cfg, *_ = infrastructure
    api = Mock()
    api.get.side_effect = lambda path: [] if path == "/api/runs" else {"ok": True}
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    report = campaign.environment_preflight(cfg)
    for containers in report["environment_profile"]["deployments"].values():
        for container in containers:
            container.pop("probes")
    campaign.append_ledger(tmp_path / "preflight.jsonl", report)
    with pytest.raises(RuntimeError, match="probe baseline missing"):
        require_environment(cfg, tmp_path)
    assert campaign.read_ledger(tmp_path / "preflight.jsonl")[0] == report
    api.post.assert_not_called()


def test_preflight_failure_is_persisted_before_raising(monkeypatch, tmp_path, infrastructure):
    cfg, _, _, workflows, *_ = infrastructure
    workflows.clear()
    api = Mock()
    api.get.side_effect = lambda path: [] if path == "/api/runs" else {"ok": True}
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    with pytest.raises(RuntimeError, match="missing registered template"):
        require_environment(cfg, tmp_path)
    report = campaign.read_ledger(tmp_path / "preflight.jsonl")[0]
    assert report["passed"] is False
    assert any(c["name"] == "reviewed_templates" and not c["passed"] for c in report["checks"])


def test_failed_preflight_stops_before_registering_or_starting_run(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    api = Mock(base_url=cfg["platform"]["api_base"])
    api.get.side_effect = lambda path: [] if path == "/api/runs" else campaign.controlled_backend_capabilities()
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", Mock())
    monkeypatch.setattr(campaign, "require_environment", Mock(side_effect=RuntimeError("environment failed")))
    with pytest.raises(RuntimeError, match="environment failed"):
        campaign.execute(tmp_path, resume=False)
    api.post.assert_not_called()


@pytest.mark.parametrize("case", ["success", "module_missing", "invalid_interface", "malformed_qdisc", "existing_netem"])
def test_network_preflight_covers_success_and_failures(monkeypatch, infrastructure, case):
    cfg, _, _, workflows, *_ = infrastructure
    cfg["test_experiments"][0]["chaos_type"] = "network-latency"
    workflows[0]["metadata"]["name"] = "payment-network-latency"
    if case == "invalid_interface":
        cfg["controlled_campaign"]["calibrations"]["payment"]["interface"] = "eth0;bad"
    original_exec, original_kube = campaign.runner._kubectl, campaign.kubectl

    def execute(*args):
        if "/proc/modules" in args:
            return Mock(returncode=0, stdout="" if case == "module_missing" else "sch_netem 100 0\n")
        return original_exec(*args)

    def kube(*args):
        if "qdisc" in args:
            if case == "malformed_qdisc":
                return {"unexpected": "object"}
            return [{"kind": "netem" if case == "existing_netem" else "noqueue"}]
        return original_kube(*args)

    monkeypatch.setattr(campaign.runner, "_kubectl", execute)
    monkeypatch.setattr(campaign, "kubectl", kube)
    report = campaign.environment_preflight(cfg)
    check = next(c for c in report["checks"] if c["name"] == "network:payment")
    assert check["passed"] is (case == "success")
    if case != "success":
        assert check["error"]


def test_backend_readiness_failure_is_not_hidden_by_successful_mcp_search(monkeypatch, infrastructure):
    cfg, *_ = infrastructure
    original = campaign.runner._kubectl

    def execute(*args):
        if any("tempo:3200" in arg for arg in args):
            return Mock(returncode=1, stdout="", stderr="HTTP 503")
        return original(*args)

    monkeypatch.setattr(campaign.runner, "_kubectl", execute)
    report = campaign.environment_preflight(cfg)
    assert next(c for c in report["checks"] if c["name"] == "observability_backends")["passed"]
    assert not next(c for c in report["checks"] if c["name"] == "telemetry_readiness")["passed"]
    assert report["passed"] is False


def test_active_web_load_prevents_new_headless_load(monkeypatch, infrastructure):
    cfg, *_ = infrastructure
    original = campaign.runner._kubectl

    def execute(*args):
        if "loadtest" in args:
            return Mock(returncode=0, stdout=json.dumps({"headless_processes": [], "web_user_count": 30}))
        return original(*args)

    monkeypatch.setattr(campaign.runner, "_kubectl", execute)
    result = campaign.environment_preflight(cfg)
    assert not next(c for c in result["checks"] if c["name"] == "load_isolation")["passed"]


def test_preflight_is_rechecked_before_each_new_submission(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["test_experiments"] = cfg["test_experiments"][:2]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    api = Mock(base_url=cfg["platform"]["api_base"])
    api.get.side_effect = lambda path: (
        campaign.controlled_backend_capabilities()
        if path == "/api/controlled-campaign/capabilities" else []
        if path == "/api/runs" else {"status": "completed", "summary": {
            "controlled_status": "completed", "controlled_gates": {
                "submitted": True, "recovery": {"passed": True},
                "cleanup": {"passed": True}, "load_stopped": {"stopped": True},
            },
        }}
    )
    api.post.side_effect = lambda path, body: {"version": 1} if path == "/api/experiments" else {
        "run_id": body["experiment_id"],
    }
    monkeypatch.setattr(campaign.runner, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(campaign, "require_controlled_backend", Mock())
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", Mock())
    monkeypatch.setattr(campaign, "_get_strategy_info", lambda **kw: {
        "strategy_version": campaign.STRATEGY_VERSION, "prompt_version": campaign.PROMPT_VERSION,
    })
    monkeypatch.setattr(campaign, "evaluate_campaign", Mock(return_value=("report", "data")))
    assert campaign.execute(tmp_path, resume=False) == 0
    assert campaign.require_environment.call_count == 2
