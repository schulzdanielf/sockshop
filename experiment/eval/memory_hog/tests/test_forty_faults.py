from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from experiment.eval.memory_hog import run_forty_faults as campaign


def source_config():
    return yaml.safe_load(campaign.SOURCE_CONFIG.read_text(encoding="utf-8"))


def test_forty_unique_pairs_vary_service_and_fault_on_each_run():
    assert len(campaign.CELLS) == 40
    assert len(
        {
            (cell["service"], cell["chaos_type"])
            for cell in campaign.CELLS
        }
    ) == 40
    assert {cell["service"] for cell in campaign.CELLS} == set(
        campaign.diverse.SERVICES
    )
    assert {cell["chaos_type"] for cell in campaign.CELLS} == set(
        campaign.diverse.FAULTS
    )
    assert all(
        left["service"] != right["service"]
        and left["chaos_type"] != right["chaos_type"]
        for left, right in zip(campaign.CELLS, campaign.CELLS[1:])
    )
    assert not any(
        cell["service"] not in campaign.diverse.HTTP_SERVICES
        and cell["chaos_type"] == "http-status-code"
        for cell in campaign.CELLS
    )


def test_campaign_config_uses_40_distinct_faults_with_180_second_injections(
    tmp_path,
):
    source = source_config()
    original = deepcopy(source)
    config = campaign.build_config(source, "forty-test", tmp_path)
    assert source == original
    assert config["train_experiments"] == []
    assert config["test_experiments"] == campaign.CELLS
    assert len(config["test_experiments"]) == 40
    assert config["replicas"] == 1
    assert config["resource_chaos"]["duration_seconds"] == 180
    selected = {cell["chaos_type"] for cell in campaign.CELLS}
    assert all(
        entry["env"]["total_chaos_duration_seconds"] == 180
        for entry in config["chaos_types"]
        if entry["name"] in selected
    )
    assert config["timeline"]["fault_duration_seconds"] == 270
    assert config["timeline"]["post_recovery_observation_seconds"] == 120
    assert config["platform"]["inter_run_cooldown_seconds"] == 60
    assert config["ordering"]["shuffle"] is False
    assert config["ordering"]["start_jitter_max_seconds"] == 0
    assert config["platform"]["initiated_by"] == "forty-faults"
    assert config["output"]["results_dir"] == str(tmp_path)


def test_specs_and_manifests_use_actual_injection_duration(tmp_path, monkeypatch):
    config = campaign.build_config(source_config(), "forty-test", tmp_path)
    manifests = campaign.generate_chaos_manifests.generate(
        config, tmp_path / "manifests"
    )
    assert len(manifests) == 40
    monkeypatch.setattr(
        campaign.runner,
        "_target_container_limits",
        lambda *_: {"cpu": "300m", "memory": "384Mi"},
    )
    for cell in campaign.CELLS:
        spec = campaign.runner.build_spec(
            config, cell["service"], 0, cell["chaos_type"]
        )
        assert spec["chaos_profile"]["fault_duration_seconds"] == 180
        if cell["chaos_type"] in {"cpu-hog", "memory-hog"}:
            assert (
                spec["chaos_profile"]["resource_chaos"]["environment"][
                    "TOTAL_CHAOS_DURATION"
                ]
                == "180"
            )
        if cell["chaos_type"] == "http-status-code":
            assert spec["chaos_profile"]["manifest_path"]
            assert spec["timeline"]["fault_duration_seconds"] == 180


def test_preflight_requires_existing_templates_without_priming(tmp_path, monkeypatch):
    config = campaign.build_config(source_config(), "forty-test", tmp_path)
    monkeypatch.setattr(campaign.diverse, "preflight", lambda _: ["healthy"])
    monkeypatch.setattr(campaign.runner, "existing_workflow_bases", lambda _: set())
    with pytest.raises(RuntimeError, match="missing Litmus templates"):
        campaign.preflight(config)
    suffixes = {
        entry["name"]: entry["template_suffix"] for entry in config["chaos_types"]
    }
    templates = {
        f"{cell['service']}-{suffixes[cell['chaos_type']]}"
        for cell in campaign.CELLS
        if cell["chaos_type"] not in campaign.runner.MANIFEST_CHAOS_TYPES
    }
    monkeypatch.setattr(campaign.runner, "existing_workflow_bases", lambda _: templates)
    assert "healthy" in campaign.preflight(config)


def test_default_only_prints_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(campaign, "CAMPAIGN_DIR", tmp_path)
    monkeypatch.setattr(campaign.sys, "argv", ["run_forty_faults"])
    monkeypatch.setattr(
        campaign, "preflight", lambda _: pytest.fail("dry-run must not check cluster")
    )
    assert campaign.main() == 0
    assert not list(tmp_path.iterdir())
    output = capsys.readouterr().out
    assert "40 unique sequential runs; 180s per fault" in output
    assert "POST /api/runs/{run_id}/rca" in output


def test_call_online_rca_posts_to_the_platform_run_rca_route(monkeypatch):
    calls = []
    expected = {"run_id": "run-1", "answer": "ok"}

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(json.dumps(expected).encode("utf-8"))

    monkeypatch.setattr(campaign, "urlopen", fake_urlopen)
    assert campaign.call_online_rca("http://127.0.0.1:8010/", "run-1", 17) == expected
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8010/api/runs/run-1/rca"
    assert request.method == "POST"
    assert request.data == b""
    assert timeout == 17


def test_rca_evaluation_scores_location_and_fault_category(tmp_path, monkeypatch):
    runs_csv = tmp_path / "runs.csv"
    with runs_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "service",
                "chaos_type",
                "replica_idx",
                "phase",
                "run_id",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "service": "carts",
                "chaos_type": "network-loss",
                "replica_idx": 0,
                "phase": "test",
                "run_id": "run-1",
                "status": "completed",
            }
        )

    def call_online_rca(api_base, run_id, *, timeout_seconds):
        assert api_base == "http://127.0.0.1:8010"
        assert run_id == "run-1"
        assert timeout_seconds == campaign.RCA_TIMEOUT_SECONDS
        return {
            "answer": (
                "Localização provável: carts\n"
                "Causa provável: network-loss (packet loss)"
            ),
            "tools_used": ["loki_range_query"],
            "evidence_bundle": {
                "localized_service": "carts",
                "coverage": {"loki_range_query": {"status": "ok"}},
                "limitations": [],
                "timeline": [{"phase": "fault"}],
                "evidence_ids": ["log-1"],
            },
        }

    monkeypatch.setattr(campaign, "call_online_rca", call_online_rca)
    report = campaign.evaluate_rca_campaign(
        "http://127.0.0.1:8010",
        runs_csv,
        [{"service": "carts", "chaos_type": "network-loss"}],
        {"network-loss": "network-loss"},
        "forty-test",
    )
    assert report["summary"]["rca_completed"] == 1
    assert report["summary"]["service_localization"] == {
        "correct": 1,
        "evaluated": 1,
        "accuracy": 1.0,
    }
    assert report["summary"]["fault_classification"]["accuracy"] == 1.0
    assert report["summary"]["both_correct"]["evaluated"] == 1
    assert report["results"][0]["evidence"]["coverage"] == {
        "loki_range_query": "ok"
    }


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
def test_execution_skips_priming_validates_effects_and_runs_rca(
    tmp_path, monkeypatch, passed, exit_code
):
    monkeypatch.setattr(campaign, "CAMPAIGN_DIR", tmp_path)
    monkeypatch.setattr(campaign.sys, "argv", ["run_forty_faults", "--run"])

    def wait_for_idle(api_base, *, wait):
        assert api_base == "http://127.0.0.1:8010"
        assert wait is False

    monkeypatch.setattr(campaign.diverse, "wait_for_idle", wait_for_idle)
    monkeypatch.setattr(campaign, "preflight", lambda _: ["healthy"])

    def generate_manifests(config, output_dir):
        assert len(config["test_experiments"]) == 40
        assert output_dir.parent.parent == tmp_path
        return []

    monkeypatch.setattr(
        campaign.generate_chaos_manifests,
        "generate",
        generate_manifests,
    )
    commands = []

    def run(command, **kwargs):
        assert kwargs == {"cwd": campaign.ROOT, "check": False}
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)

    def validate(path, **kwargs):
        assert path.name == "runs.csv"
        assert kwargs == {
            "cells": campaign.CELLS,
            "replicas": 1,
            "phase": "test",
        }
        return [{"passed": passed}]

    monkeypatch.setattr(campaign.targeted, "validate_runs", validate)

    def evaluate(api_base, runs_csv, cells, categories, campaign_id):
        assert api_base == "http://127.0.0.1:8010"
        assert runs_csv.name == "runs.csv"
        assert cells == campaign.CELLS
        assert categories["network-loss"] == "network-loss"
        assert campaign_id.startswith("forty-faults-")
        return {
            "summary": {
                "requested_runs": 40,
                "run_records_found": 40,
                "completed_runs": 40,
                "rca_completed": 40,
                "service_localization": {"accuracy": 1.0},
                "fault_classification": {"accuracy": 1.0},
            }
        }

    monkeypatch.setattr(campaign, "evaluate_rca_campaign", evaluate)
    assert campaign.main() == exit_code
    assert len(commands) == 1
    assert "--skip-prime" in commands[0]
    assert commands[0][commands[0].index("--phase") + 1] == "chaos"
    config_path = Path(commands[0][commands[0].index("--config") + 1])
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert len(config["test_experiments"]) == 40
    assert len(list(tmp_path.glob("*/effect_validation.json"))) == 1
    assert len(list(tmp_path.glob("*/rca_evaluation.json"))) == 1
