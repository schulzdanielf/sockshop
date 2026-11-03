from __future__ import annotations

import csv
import json
from unittest.mock import Mock

import pytest
import yaml
from experiment.eval.memory_hog import run_targeted_training as targeted
from experiment.eval.memory_hog import runner as runner_module
from experiment.eval.memory_hog.runner import build_spec


def test_training_config_isolated_and_has_two_replicas_for_all_runnable_chaos():
    source = {
        "experiment_campaign_id": "default",
        "platform": {"initiated_by": "default"},
        "output": {"results_dir": "out/default"},
        "train_experiments": [{"service": "shipping", "chaos_type": "pod-delete"}],
        "test_experiments": [{"service": "user", "chaos_type": "cpu-hog"}],
        "replicas": 3,
        "resource_chaos": {"duration_seconds": 120},
        "timeline": {
            "baseline_seconds": 30,
            "warmup_seconds": 30,
            "fault_duration_seconds": 90,
            "post_recovery_observation_seconds": 180,
        },
    }

    config = targeted.build_training_config(source)

    assert config["train_experiments"] == targeted.TARGET_CELLS
    assert len({cell["service"] for cell in config["train_experiments"]}) == len(
        targeted.TARGET_CELLS
    )
    assert config["test_experiments"] == []
    assert config["replicas"] == 2
    assert config["output"]["results_dir"].endswith("all_chaos_training")
    assert source["train_experiments"] == [{"service": "shipping", "chaos_type": "pod-delete"}]
    assert config["timeline"]["fault_duration_seconds"] == 210
    assert targeted.estimate_duration_seconds(config) == 6300
    assert set(targeted.EXCLUDED_CHAOS) == {"dns-error", "io-stress"}


def test_generated_http_fault_manifest_is_used_by_run_spec(tmp_path):
    base_config = yaml.safe_load(targeted.BASE_CONFIG.read_text(encoding="utf-8"))
    config = targeted.build_training_config(base_config)
    config["output"]["results_dir"] = str(tmp_path)
    manifests_dir = tmp_path / config["output"]["manifests_dir"]
    targeted.generate_chaos_manifests.generate(config, manifests_dir)

    spec = build_spec(config, "orders", 0, "http-status-code")

    assert spec["chaos_profile"]["manifest_path"] == str(
        manifests_dir / "orders-http-status-code.yaml"
    )
    assert spec["chaos_profile"]["chaos_engine"] is None
    assert spec["timeline"]["fault_duration_seconds"] == spec["chaos_profile"][
        "fault_duration_seconds"
    ]


def test_resource_chaos_spec_exposes_active_injection_duration(monkeypatch):
    config = yaml.safe_load(targeted.BASE_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        runner_module,
        "_target_container_limits",
        lambda service, namespace: {"cpu": "300m", "memory": "500Mi"},
    )

    for chaos_type, service in (("cpu-hog", "payment"), ("memory-hog", "catalogue")):
        spec = build_spec(config, service, 0, chaos_type)
        assert spec["chaos_profile"]["fault_duration_seconds"] == 120


def test_generated_queries_have_service_labels_and_30_second_windows(tmp_path):
    base_config = yaml.safe_load(targeted.BASE_CONFIG.read_text(encoding="utf-8"))
    config = targeted.build_training_config(base_config)
    config["output"]["results_dir"] = str(tmp_path)
    manifests_dir = tmp_path / config["output"]["manifests_dir"]
    targeted.generate_chaos_manifests.generate(config, manifests_dir)

    spec = build_spec(config, "orders", 0, "http-status-code")
    historical_url = config["historical_observability"]["mcp_sse_url"]
    assert spec["historical_observability"]["required"] is True
    assert spec["historical_observability"]["mode"] == "dual_write"
    assert spec["observability"]["metrics"]["mcp_sse_url"] == historical_url
    assert spec["observability"]["traces"]["mcp_sse_url"] == historical_url
    assert spec["observability"]["metrics"]["timeout_seconds"] == 90
    assert spec["observability"]["traces"]["timeout_seconds"] == 90
    queries = {
        query["id"]: query["query"]
        for query in spec["observability"]["metrics"]["queries"]
    }

    for metric_id in (
        "traffic",
        "error_rate",
        "latency_p50",
        "latency_p95",
        "latency_p99",
        "network_receive_dropped_rate",
        "cpu_saturation_pct",
        "cpu_throttled",
        "pod_restarts",
        "http_5xx_by_edge",
    ):
        assert "[30s]" in queries[metric_id]
        assert "[5m]" not in queries[metric_id]
    assert 'reporter="source"' in queries["http_5xx_by_edge"]
    assert 'response_code=~"5.."' in queries["http_5xx_by_edge"]
    assert (
        "by (source_workload,destination_workload,response_code)"
        in queries["http_5xx_by_edge"]
    )
    assert "sum by (pod)" in queries["network_receive_dropped_rate"]
    assert "sum by (pod)" in queries["pod_ready"]
    cpu_query = queries["cpu_saturation_pct"]
    assert cpu_query.count("sum by (pod)") == 2
    assert cpu_query.count('job="kubernetes-cadvisor"') == 3
    assert 'cpu="total"' in cpu_query
    assert "on(pod)" in cpu_query
    memory_query = queries["memory_saturation_pct"]
    assert memory_query.count("sum by (pod)") == 2
    assert memory_query.count('job="kubernetes-cadvisor"') == 2
    assert "on(pod)" in memory_query
    assert "container!=" not in memory_query


def test_http_run_spec_rejects_manifest_without_probe_exclusion(tmp_path):
    base_config = yaml.safe_load(targeted.BASE_CONFIG.read_text(encoding="utf-8"))
    config = targeted.build_training_config(base_config)
    config["output"]["results_dir"] = str(tmp_path)
    manifests_dir = tmp_path / config["output"]["manifests_dir"]
    targeted.generate_chaos_manifests.generate(config, manifests_dir)

    manifest_path = manifests_dir / "orders-http-status-code.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["http"] = manifest["spec"]["http"][1:]
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="probe bypass rule"):
        build_spec(config, "orders", 0, "http-status-code")


def test_http_run_spec_requires_generated_virtualservice(tmp_path):
    base_config = yaml.safe_load(targeted.BASE_CONFIG.read_text(encoding="utf-8"))
    config = targeted.build_training_config(base_config)
    config["output"]["results_dir"] = str(tmp_path)

    with pytest.raises(FileNotFoundError, match="probe exclusions"):
        build_spec(config, "orders", 0, "http-status-code")


def test_netem_preflight_detects_missing_module(monkeypatch):
    monkeypatch.setattr(
        targeted.subprocess,
        "run",
        lambda *args, **kwargs: Mock(returncode=0, stdout="Module\n", stderr=""),
    )
    assert not targeted.netem_is_loaded()

    monkeypatch.setattr(
        targeted.subprocess,
        "run",
        lambda *args, **kwargs: Mock(
            returncode=0, stdout="sch_netem 32768 0 - Live 0x0\n", stderr=""
        ),
    )
    assert targeted.netem_is_loaded()


def _valid_metric_hotspots(chaos_type, service):
    row = {
        "label": service,
        "baseline_mean": 10,
        "fault_mean": 80,
        "fault_p95": 120,
        "delta_abs": 70,
    }
    metric_by_type = {
        "memory-hog": "memory_saturation_pct",
        "cpu-hog": "cpu_saturation_pct",
        "network-latency": "latency_p95",
        "network-loss": "network_receive_dropped_rate",
        "http-status-code": "http_5xx_by_edge",
        "pod-delete": "pod_restarts_total",
        "container-kill": "pod_restarts_total",
    }
    metric_id = metric_by_type[chaos_type]
    if chaos_type == "http-status-code":
        row.update({
            "source_workload": "front-end",
            "destination_workload": service,
            "response_code": "500",
        })
    metrics = {metric_id: {"top": [row]}}
    if chaos_type == "http-status-code":
        metrics[metric_id]["top_edges"] = [row]
    for optional_metric in (
        "cpu_throttled",
        "oom_killed",
        "pod_restarts_total",
        "pod_ready",
        "error_rate",
        "network_transmit_dropped_rate",
        "latency_p99",
    ):
        metrics.setdefault(optional_metric, {"top": []})
    return metrics


def test_cpu_validation_requires_signal_on_the_injected_service():
    target_features = {
        "metric_hotspots": {
            "cpu_saturation_pct": {
                "top": [
                    {
                        "label": "payment",
                        "baseline_mean": 5,
                        "fault_mean": 72,
                        "fault_p95": 95,
                        "delta_abs": 67,
                    }
                ]
            },
            "cpu_throttled": {"top": []},
            "error_rate": {"top": []},
            "latency_p95": {"top": []},
            "traffic": {
                "top": [
                    {
                        "label": "payment",
                        "baseline_mean": 20,
                        "fault_mean": 20,
                        "fault_p95": 20,
                        "delta_abs": 0,
                    }
                ]
            },
        }
    }
    result = targeted.validate_metric_effect("cpu-hog", "payment", target_features)
    assert result["passed"]
    assert result["mechanism_signal_observed"]
    assert not result["impact_observed"]

    other_service_only = {
        "metric_hotspots": {
            "cpu_saturation_pct": {
                "top": [
                    {
                        "label": "catalogue",
                        "baseline_mean": 5,
                        "fault_mean": 90,
                        "fault_p95": 100,
                        "delta_abs": 85,
                    }
                ]
            },
            "cpu_throttled": {"top": []},
        }
    }
    result = targeted.validate_metric_effect("cpu-hog", "payment", other_service_only)
    assert not result["passed"]


def test_cpu_throttling_can_confirm_effect_when_saturation_is_muted():
    features = {
        "metric_hotspots": {
            "cpu_saturation_pct": {"top": []},
            "cpu_throttled": {
                "top": [
                    {
                        "label": "payment",
                        "baseline_mean": 0.001,
                        "fault_mean": 0.025,
                        "fault_p95": 0.04,
                        "delta_abs": 0.024,
                    }
                ]
            },
        }
    }
    assert targeted.validate_metric_effect("cpu-hog", "payment", features)["passed"]


def test_memory_validation_accepts_pressure_or_oom_but_not_restart_alone():
    saturation_features = {
        "metric_hotspots": {
            "memory_saturation_pct": {
                "top": [
                    {
                        "label": "catalogue",
                        "baseline_mean": 25,
                        "fault_mean": 78,
                        "fault_p95": 92,
                        "delta_abs": 53,
                    }
                ]
            }
        }
    }
    assert targeted.validate_metric_effect(
        "memory-hog", "catalogue", saturation_features
    )["passed"]
    assert (
        targeted.validate_metric_effect("memory-hog", "catalogue", saturation_features)[
            "mechanism_status"
        ]
        == "memory_pressure_observed"
    )

    oom_features = {
        "metric_hotspots": {
            "memory_saturation_pct": {"top": []},
            "oom_killed": {
                "top": [
                    {
                        "label": "catalogue",
                        "baseline_mean": 0,
                        "fault_mean": 1,
                        "fault_p95": 1,
                        "delta_abs": 1,
                    }
                ]
            },
            "pod_restarts_total": {"top": []},
        }
    }
    assert targeted.validate_metric_effect("memory-hog", "catalogue", oom_features)[
        "passed"
    ]

    restart_only = {
        "metric_hotspots": {
            "memory_saturation_pct": {"top": []},
            "oom_killed": {"top": []},
            "pod_restarts_total": {
                "top": [
                    {
                        "label": "catalogue",
                        "baseline_mean": 0,
                        "fault_mean": 1,
                        "fault_p95": 1,
                        "delta_abs": 1,
                    }
                ]
            },
        }
    }
    result = targeted.validate_metric_effect("memory-hog", "catalogue", restart_only)
    assert not result["passed"]
    assert result["mechanism_status"] == "not_observed"


def test_lifecycle_symptoms_and_impact_are_reported_without_confirming_mechanism():
    features = {
        "metric_hotspots": {
            "pod_restarts_total": {
                "top": [
                    {
                        "label": "user",
                        "baseline_mean": 0,
                        "fault_mean": 1,
                        "fault_p95": 1,
                        "delta_abs": 1,
                    }
                ]
            },
            "pod_ready": {"top": []},
            "error_rate": {"top": []},
            "latency_p95": {"top": []},
        }
    }
    result = targeted.validate_metric_effect("container-kill", "user", features)
    assert result["signal_observed"]
    assert result["passed"]
    assert not result["mechanism_signal_observed"]
    assert result["mechanism_status"] == "lifecycle_or_impact_symptom_only"


def test_network_loss_rejects_generic_5xx_as_packet_loss_and_marks_drop_ambiguity():
    errors_only = {
        "metric_hotspots": {
            "network_receive_dropped_rate": {"top": []},
            "network_transmit_dropped_rate": {"top": []},
            "error_rate": {
                "top": [
                    {
                        "label": "carts",
                        "baseline_mean": 0,
                        "fault_mean": 10,
                        "fault_p95": 20,
                        "delta_abs": 10,
                    }
                ]
            },
        }
    }
    result = targeted.validate_metric_effect("network-loss", "carts", errors_only)
    assert not result["passed"]
    assert result["impact_observed"]
    assert not result["signal_observed"]

    drops = {
        "metric_hotspots": {
            "network_receive_dropped_rate": {
                "top": [
                    {
                        "label": "carts",
                        "baseline_mean": 0,
                        "fault_mean": 1,
                        "fault_p95": 1,
                        "delta_abs": 1,
                    }
                ]
            },
            "network_transmit_dropped_rate": {"top": []},
            "error_rate": {"top": []},
            "latency_p95": {"top": []},
        }
    }
    result = targeted.validate_metric_effect("network-loss", "carts", drops)
    assert result["passed"]
    assert not result["mechanism_signal_observed"]
    assert result["mechanism_status"] == "interface_drop_signal_ambiguous"


def test_http_status_validation_requires_caller_reported_5xx_on_target_edge():
    aggregate_only = {
        "metric_hotspots": {
            "error_rate": {
                "top": [
                    {
                        "label": "orders",
                        "baseline_mean": 0,
                        "fault_mean": 40,
                        "fault_p95": 50,
                        "delta_abs": 40,
                    }
                ]
            },
            "http_5xx_by_edge": {"top_edges": []},
        }
    }
    aggregate_result = targeted.validate_metric_effect(
        "http-status-code", "orders", aggregate_only
    )
    assert not aggregate_result["passed"]
    assert aggregate_result["impact_observed"]
    assert not aggregate_result["signal_observed"]

    edge_features = {
        "metric_hotspots": {
            "http_5xx_by_edge": {
                "top_edges": [
                    {
                        "label": "orders",
                        "source_workload": "front-end",
                        "destination_workload": "orders",
                        "response_code": "500",
                        "baseline_mean": 0,
                        "fault_mean": 0.5,
                        "fault_p95": 0.5,
                        "delta_abs": 0.5,
                    }
                ]
            }
        }
    }
    result = targeted.validate_metric_effect(
        "http-status-code", "orders", edge_features
    )
    assert result["passed"]
    assert result["mechanism_signal_observed"]
    assert result["mechanism_status"] == "client_reported_5xx_on_target_edge"


def test_validate_runs_requires_litmus_and_metric_evidence(tmp_path):
    rows = []
    for cell in targeted.TARGET_CELLS:
        for replica in range(targeted.REPLICAS):
            run_id = f"run-{cell['service']}-{replica}"
            rows.append(
                {
                    "phase": "train",
                    "chaos_type": cell["chaos_type"],
                    "service": cell["service"],
                    "replica_idx": str(replica),
                    "run_id": run_id,
                    "status": "completed",
                }
            )
            run_dir = tmp_path / run_id
            run_dir.mkdir()
            (run_dir / "run_features.json").write_text(
                json.dumps(
                    {
                        "metric_hotspots": _valid_metric_hotspots(
                            cell["chaos_type"], cell["service"]
                        )
                    }
                ),
                encoding="utf-8",
            )
            if cell["chaos_type"] == "http-status-code":
                artifacts = {
                    "mode": "manifest",
                    "apply": {"returncode": 0},
                    "cleanup": {"returncode": 0},
                }
            else:
                artifacts = {
                    "mode": "argo_workflow",
                    "workflow_name": f"{run_id}-workflow",
                    "final_status": {
                        "chaos_verdict": "Pass",
                        "chaos_phase": "Completed",
                    },
                }
            (run_dir / "chaos_artifacts.json").write_text(
                json.dumps(artifacts), encoding="utf-8"
            )

    runs_csv = tmp_path / "runs.csv"
    with runs_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    reports = targeted.validate_runs(runs_csv, tmp_path)

    assert len(reports) == len(targeted.TARGET_CELLS) * targeted.REPLICAS
    assert all(report["passed"] for report in reports)
    assert all(report["checks"]["injection"]["passed"] for report in reports)
    by_chaos = {report["chaos_type"]: report for report in reports}
    assert by_chaos["container-kill"]["result_status"] == (
        "target_signal_without_mechanism_signal"
    )
    assert by_chaos["network-loss"]["result_status"] == (
        "target_signal_without_mechanism_signal"
    )
    assert by_chaos["http-status-code"]["result_status"] == (
        "target_signal_with_mechanism_signal"
    )


def test_validate_runs_fails_when_litmus_or_target_metric_fails(tmp_path):
    rows = []
    for cell in targeted.TARGET_CELLS:
        for replica in range(targeted.REPLICAS):
            run_id = f"run-{cell['service']}-{replica}"
            rows.append({
                "phase": "train",
                "chaos_type": cell["chaos_type"],
                "service": cell["service"],
                "replica_idx": str(replica),
                "run_id": run_id,
                "status": "completed",
            })
            run_dir = tmp_path / run_id
            run_dir.mkdir()
            (run_dir / "run_features.json").write_text(
                json.dumps({"metric_hotspots": {}}), encoding="utf-8"
            )
            if cell["chaos_type"] == "http-status-code":
                artifacts = {
                    "mode": "manifest",
                    "apply": {"returncode": 0},
                    "cleanup": {"returncode": 0},
                }
            else:
                artifacts = {
                    "mode": "argo_workflow",
                    "workflow_name": f"{run_id}-workflow",
                    "final_status": {
                        "chaos_verdict": "Fail",
                        "chaos_phase": "Failed",
                    },
                }
            (run_dir / "chaos_artifacts.json").write_text(
                json.dumps(artifacts), encoding="utf-8"
            )

    runs_csv = tmp_path / "runs.csv"
    with runs_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    reports = targeted.validate_runs(runs_csv, tmp_path)

    assert len(reports) == len(targeted.TARGET_CELLS) * targeted.REPLICAS
    assert all(not report["passed"] for report in reports)
    assert all(not report["checks"]["metrics"]["passed"] for report in reports)


def test_manipulation_check_does_not_treat_top_k_omission_as_no_effect():
    class _API:
        def get(self, path):
            if path.endswith("/features"):
                return {
                    "metric_hotspots": {
                        "memory_saturation_pct": {
                            "top": [
                                {
                                    "label": "catalogue",
                                    "baseline_mean": 20,
                                    "fault_mean": 80,
                                }
                            ]
                        }
                    }
                }
            return {"timings": {}, "verdict": "degraded_recoverable"}

    record = runner_module.RunRecord(
        service="user",
        replica_idx=0,
        phase="train",
        chaos_target="user",
        experiment_id="exp-1",
        run_id="run-1",
        chaos_type="memory-hog",
    )
    manipulation_check = getattr(runner_module, "_manipulation_check")
    observed, reason, _window, verdict = manipulation_check(_API(), record)

    assert observed is None
    assert reason.startswith("target_not_in_top_k")
    assert verdict == "degraded_recoverable"


def test_manipulation_check_reports_no_effect_when_target_signal_was_measured():
    class _API:
        def get(self, path):
            if path.endswith("/features"):
                return {
                    "metric_hotspots": {
                        "memory_saturation_pct": {
                            "top": [
                                {
                                    "label": "user",
                                    "baseline_mean": 40,
                                    "fault_mean": 42,
                                }
                            ]
                        }
                    }
                }
            return {"timings": {}, "verdict": "degraded_recoverable"}

    record = runner_module.RunRecord(
        service="user",
        replica_idx=0,
        phase="train",
        chaos_target="user",
        experiment_id="exp-2",
        run_id="run-2",
        chaos_type="memory-hog",
    )
    manipulation_check = getattr(runner_module, "_manipulation_check")
    observed, reason, _window, _verdict = manipulation_check(_API(), record)

    assert observed is False
    assert reason == "no_expected_increase_on_target memory_saturation_pct"
