from __future__ import annotations

import yaml

from experiment.eval.memory_hog import run_quick_campaign as quick
from experiment.eval.memory_hog import run_targeted_training as targeted


def test_quick_campaign_has_one_held_out_run_per_runnable_fault():
    source = yaml.safe_load(quick.SOURCE_CONFIG.read_text(encoding="utf-8"))
    config = quick.build_quick_config(source)

    assert config["train_experiments"] == []
    assert config["test_experiments"] == targeted.TARGET_CELLS
    assert len(config["test_experiments"]) == 7
    assert len({cell["chaos_type"] for cell in config["test_experiments"]}) == 7
    assert len({cell["service"] for cell in config["test_experiments"]}) == 7
    assert config["resource_chaos"]["duration_seconds"] == 60
    assert config["timeline"] == {
        **source["timeline"],
        "baseline_seconds": 30,
        "warmup_seconds": 30,
        "fault_duration_seconds": 150,
        "post_recovery_observation_seconds": 60,
        "sampling_interval_seconds": 5,
    }
    selected_faults = {cell["chaos_type"] for cell in targeted.TARGET_CELLS}
    assert all(
        entry["env"]["total_chaos_duration_seconds"] == 60
        for entry in config["chaos_types"]
        if entry["name"] in selected_faults
    )


def test_quick_duration_is_under_60_minutes_with_priming_allowance():
    source = yaml.safe_load(quick.SOURCE_CONFIG.read_text(encoding="utf-8"))
    config = quick.build_quick_config(source)
    assert quick.duration_estimate_seconds(config) < 60 * 60


def test_campaign_config_can_use_a_fresh_ledger_directory(tmp_path):
    source = yaml.safe_load(quick.SOURCE_CONFIG.read_text(encoding="utf-8"))
    campaign_id = "quick-chaos-eval-isolated"
    campaign_dir = tmp_path / campaign_id

    config = quick.build_quick_config(source, campaign_id, campaign_dir)

    assert config["experiment_campaign_id"] == campaign_id
    assert config["output"]["results_dir"] == str(campaign_dir)
