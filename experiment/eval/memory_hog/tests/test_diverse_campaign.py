from __future__ import annotations

from experiment.eval.memory_hog import run_diverse_campaign as campaign


def test_campaign_matrix_is_balanced_and_seeded():
    first = campaign.campaign_cells(123)
    assert first == campaign.campaign_cells(123)
    assert len(first) == 48
    for fault in campaign.FAULTS:
        expected = len(campaign.HTTP_SERVICES) if fault == "http-status-code" else len(campaign.SERVICES)
        assert sum(cell["chaos_type"] == fault for cell in first) == expected
    assert len({(cell["service"], cell["chaos_type"]) for cell in first}) == 48
    assert not any(
        cell["service"] == "front-end" and cell["chaos_type"] == "http-status-code"
        for cell in first
    )
    service_counts = [sum(cell["service"] == service for cell in first) for service in campaign.SERVICES]
    assert max(service_counts) - min(service_counts) <= 1
    assert first != campaign.campaign_cells(124)


def test_duration_estimate_includes_setup_jitter_and_cooldown():
    lower, upper = campaign.duration_estimate(48)
    assert lower == 36_600
    assert upper == 37_320


def test_campaign_config_isolated_from_source_config():
    source = {
        "platform": {"poll_interval_seconds": 10},
        "output": {"results_dir": "old", "runs_csv": "old.csv"},
        "train_experiments": [{"service": "old", "chaos_type": "old"}],
        "test_experiments": [{"service": "old", "chaos_type": "old"}],
        "replicas": 3,
        "ordering": {"shuffle": True},
    }
    source["chaos_types"] = [
        {"name": fault, "env": {"total_chaos_duration_seconds": 120}}
        for fault in campaign.FAULTS
    ]
    source["resource_chaos"] = {"duration_seconds": 120}
    source["timeline"] = {
        "baseline_seconds": 1,
        "warmup_seconds": 1,
        "fault_duration_seconds": 1,
        "post_recovery_observation_seconds": 1,
        "sampling_interval_seconds": 1,
    }
    config = campaign.build_campaign_config(
        source,
        seed=17,
        campaign_id="long-chaos-test",
        campaign_dir=campaign.CAMPAIGN_DIR / "long-chaos-test",
    )
    assert source["train_experiments"][0]["service"] == "old"
    assert source["platform"] == {"poll_interval_seconds": 10}
    assert len(config["train_experiments"]) == 48
    assert config["test_experiments"] == []
    assert config["replicas"] == 1
    assert config["ordering"] == {
        "shuffle": False,
        "seed": 17,
        "start_jitter_max_seconds": campaign.DEFAULT_JITTER_MAX_SECONDS,
    }
    assert config["platform"]["inter_run_cooldown_seconds"] == campaign.DEFAULT_COOLDOWN_SECONDS
    assert config["output"]["runs_csv"] == "runs.csv"
    assert config["resource_chaos"]["duration_seconds"] == 300
    assert config["timeline"]["fault_duration_seconds"] == 390
    assert all(
        entry["env"]["total_chaos_duration_seconds"] == 300
        for entry in config["chaos_types"]
        if entry["name"] in campaign.FAULTS
    )
