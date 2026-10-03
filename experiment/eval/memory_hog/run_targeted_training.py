#!/usr/bin/env python3
"""Train on every chaos type currently runnable in the WSL2 environment.

From the repository root:
    python -m experiment.eval.memory_hog.run_targeted_training

The script runs two replicas of each of seven supported chaos types on distinct
services, writes results under out/all_chaos_training, and requires a passing
Litmus ChaosResult plus target-specific metric evidence for every run. The
WSL2-incompatible dns-error and io-stress types are intentionally excluded.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from experiment.eval.memory_hog import generate_chaos_manifests

ROOT = Path(__file__).resolve().parents[3]
BASE_CONFIG = ROOT / "experiment/eval/memory_hog/config.yaml"
OBJECT_STORE = ROOT / "experiment/platform/data/object_store"
TARGET_CELLS = [
    {"service": "catalogue", "chaos_type": "memory-hog"},
    {"service": "payment", "chaos_type": "cpu-hog"},
    {"service": "queue-master", "chaos_type": "pod-delete"},
    {"service": "front-end", "chaos_type": "network-latency"},
    {"service": "carts", "chaos_type": "network-loss"},
    {"service": "orders", "chaos_type": "http-status-code"},
    {"service": "user", "chaos_type": "container-kill"},
]
NETEM_TYPES = {"network-latency", "network-loss"}
EXCLUDED_CHAOS = {
    "dns-error": "dns_interceptor is incompatible with the WSL2 target netns",
    "io-stress": "stress-ng O_DIRECT is unsupported by the WSL2 overlayfs",
}
REPLICAS = 2


def build_training_config(base_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a private train-only config; leave the default campaign untouched."""
    config = json.loads(json.dumps(base_config))
    config["experiment_campaign_id"] = "all-runnable-chaos-training-v1"
    config["platform"]["initiated_by"] = "all-runnable-chaos-training"
    config["train_experiments"] = [dict(cell) for cell in TARGET_CELLS]
    config["test_experiments"] = []
    config["replicas"] = REPLICAS
    config["timeline"]["fault_duration_seconds"] = max(
        int(config["timeline"]["fault_duration_seconds"]),
        int(config["resource_chaos"]["duration_seconds"]) + 90,
    )
    config["output"]["results_dir"] = "experiment/eval/memory_hog/out/all_chaos_training"
    config["output"]["runs_csv"] = "runs.csv"
    config["output"]["evaluation_csv"] = "evaluation.csv"
    return config


def _float(row: Dict[str, Any], field: str) -> Optional[float]:
    try:
        return float(row[field])
    except (KeyError, TypeError, ValueError):
        return None


def _target_row(features: Dict[str, Any], metric_id: str, service: str) -> Optional[Dict[str, Any]]:
    hotspots = features.get("metric_hotspots") or {}
    entry = hotspots.get(metric_id) or {}
    for row in entry.get("top") or []:
        if isinstance(row, dict) and row.get("label") == service:
            return row
    return None


def validate_metric_effect(
    chaos_type: str, service: str, features: Dict[str, Any]
) -> Dict[str, Any]:
    """Require the expected target-labeled signal in the fault window."""
    if chaos_type == "cpu-hog":
        saturation = _target_row(features, "cpu_saturation_pct", service)
        throttling = _target_row(features, "cpu_throttled", service)
        sat_delta = _float(saturation or {}, "delta_abs")
        sat_peak = _float(saturation or {}, "fault_p95")
        throttle_delta = _float(throttling or {}, "delta_abs")
        saturation_observed = (
            sat_delta is not None and sat_peak is not None
            and sat_delta >= 10.0 and sat_peak >= 50.0
        )
        throttling_observed = throttle_delta is not None and throttle_delta >= 0.01
        return {
            "passed": saturation_observed or throttling_observed,
            "metrics": {
                "cpu_saturation_pct": saturation,
                "cpu_throttled": throttling,
            },
            "reason": (
                "target CPU saturation/throttling increased"
                if saturation_observed or throttling_observed
                else "no target CPU saturation or throttling increase met threshold"
            ),
        }

    if chaos_type == "memory-hog":
        saturation = _target_row(features, "memory_saturation_pct", service)
        oom = _target_row(features, "oom_killed", service)
        restarts = _target_row(features, "pod_restarts_total", service)
        mem_delta = _float(saturation or {}, "delta_abs")
        mem_peak = _float(saturation or {}, "fault_p95")
        oom_delta = _float(oom or {}, "delta_abs")
        restart_delta = _float(restarts or {}, "delta_abs")
        saturation_observed = (
            mem_delta is not None and mem_peak is not None
            and mem_delta >= 10.0 and mem_peak >= 60.0
        )
        termination_observed = (
            (oom_delta is not None and oom_delta >= 0.4)
            or (restart_delta is not None and restart_delta >= 1.0)
        )
        return {
            "passed": saturation_observed or termination_observed,
            "metrics": {
                "memory_saturation_pct": saturation,
                "oom_killed": oom,
                "pod_restarts_total": restarts,
            },
            "reason": (
                "target memory saturation/OOM/restart observed"
                if saturation_observed or termination_observed
                else "no target memory saturation, OOM, or restart signal met threshold"
            ),
        }

    if chaos_type in {"pod-delete", "container-kill"}:
        restarts = _target_row(features, "pod_restarts_total", service)
        ready = _target_row(features, "pod_ready", service)
        error_rate = _target_row(features, "error_rate", service)
        restart_delta = _float(restarts or {}, "delta_abs")
        ready_delta = _float(ready or {}, "delta_abs")
        ready_fault_mean = _float(ready or {}, "fault_mean")
        error_delta = _float(error_rate or {}, "delta_abs")
        error_peak = _float(error_rate or {}, "fault_p95")
        restart_observed = restart_delta is not None and restart_delta >= 0.2
        readiness_observed = (
            ready_delta is not None and ready_fault_mean is not None
            and ready_delta <= -0.25 and ready_fault_mean <= 0.75
        )
        service_impact_observed = (
            error_delta is not None and error_peak is not None
            and error_delta >= 5.0 and error_peak >= 5.0
        )
        passed = restart_observed or readiness_observed or service_impact_observed
        return {
            "passed": passed,
            "metrics": {
                "pod_restarts_total": restarts,
                "pod_ready": ready,
                "error_rate": error_rate,
            },
            "reason": "target pod restart/readiness or service error signal observed"
            if passed else "no target pod or service-impact signal met threshold",
        }

    if chaos_type == "network-latency":
        latency = _target_row(features, "latency_p95", service)
        latency_delta = _float(latency or {}, "delta_abs")
        latency_peak = _float(latency or {}, "fault_p95")
        passed = (
            latency_delta is not None and latency_peak is not None
            and latency_delta >= 50.0 and latency_peak >= 100.0
        )
        return {
            "passed": passed,
            "metrics": {"latency_p95": latency},
            "reason": "target p95 latency increased"
            if passed else "target p95 latency increase did not meet threshold",
        }

    if chaos_type == "network-loss":
        receive_drops = _target_row(features, "network_receive_dropped_rate", service)
        transmit_drops = _target_row(features, "network_transmit_dropped_rate", service)
        error_rate = _target_row(features, "error_rate", service)
        drop_deltas = [
            _float(receive_drops or {}, "delta_abs"),
            _float(transmit_drops or {}, "delta_abs"),
        ]
        error_delta = _float(error_rate or {}, "delta_abs")
        error_peak = _float(error_rate or {}, "fault_p95")
        drops_observed = any(delta is not None and delta >= 0.1 for delta in drop_deltas)
        errors_observed = (
            error_delta is not None and error_peak is not None
            and error_delta >= 5.0 and error_peak >= 5.0
        )
        return {
            "passed": drops_observed or errors_observed,
            "metrics": {
                "network_receive_dropped_rate": receive_drops,
                "network_transmit_dropped_rate": transmit_drops,
                "error_rate": error_rate,
            },
            "reason": "target packet drops or service errors increased"
            if drops_observed or errors_observed
            else "no target packet-drop or service-error signal met threshold",
        }

    if chaos_type == "http-status-code":
        error_rate = _target_row(features, "error_rate", service)
        error_delta = _float(error_rate or {}, "delta_abs")
        error_peak = _float(error_rate or {}, "fault_p95")
        passed = (
            error_delta is not None and error_peak is not None
            and error_delta >= 15.0 and error_peak >= 20.0
        )
        return {
            "passed": passed,
            "metrics": {"error_rate": error_rate},
            "reason": "target HTTP error rate increased"
            if passed else "target HTTP error rate increase did not meet threshold",
        }

    return {
        "passed": False,
        "metrics": {},
        "reason": f"unsupported chaos type: {chaos_type}",
    }


def validate_runs(
    runs_csv: Path,
    object_store: Path = OBJECT_STORE,
    *,
    cells: Optional[List[Dict[str, str]]] = None,
    replicas: int = REPLICAS,
    phase: str = "train",
) -> List[Dict[str, Any]]:
    """Validate count, Litmus verdict, and target metric evidence for each row."""
    cells = TARGET_CELLS if cells is None else cells
    if not runs_csv.exists():
        return [{"passed": False, "reason": f"runs file not found: {runs_csv}"}]
    with runs_csv.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    expected = {
        (cell["chaos_type"], cell["service"], replica)
        for cell in cells
        for replica in range(replicas)
    }
    actual = {
        (row.get("chaos_type"), row.get("service"), int(row.get("replica_idx", -1)))
        for row in rows
    }
    reports: List[Dict[str, Any]] = []
    if actual != expected or len(rows) != len(expected):
        reports.append({
            "passed": False,
            "reason": f"expected {len(expected)} unique train runs; got {len(rows)} rows",
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
        })

    for row in rows:
        chaos_type = row.get("chaos_type", "")
        service = row.get("service", "")
        run_id = row.get("run_id", "")
        report: Dict[str, Any] = {
            "run_id": run_id,
            "chaos_type": chaos_type,
            "service": service,
            "replica_idx": row.get("replica_idx"),
            "passed": False,
            "checks": {},
        }
        if row.get("phase") != phase or row.get("status") != "completed" or not run_id:
            report["reason"] = f"run did not complete in phase {phase!r}"
            reports.append(report)
            continue

        run_dir = object_store / run_id
        try:
            features = json.loads((run_dir / "run_features.json").read_text(encoding="utf-8"))
            chaos_artifacts = json.loads((run_dir / "chaos_artifacts.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["reason"] = f"cannot read run artifacts: {exc}"
            reports.append(report)
            continue

        if chaos_artifacts.get("mode") == "manifest":
            apply_result = chaos_artifacts.get("apply") or {}
            cleanup_result = chaos_artifacts.get("cleanup") or {}
            injection_passed = (
                apply_result.get("returncode") == 0
                and cleanup_result.get("returncode") == 0
            )
            report["checks"]["injection"] = {
                "passed": injection_passed,
                "mode": "manifest",
                "apply_returncode": apply_result.get("returncode"),
                "cleanup_returncode": cleanup_result.get("returncode"),
            }
        else:
            workflow_name = chaos_artifacts.get("workflow_name")
            if not workflow_name:
                report["reason"] = "Litmus workflow name missing from chaos artifacts"
                reports.append(report)
                continue
            final_status = chaos_artifacts.get("final_status") or {}
            verdict = final_status.get("chaos_verdict")
            litmus_phase = (
                final_status.get("chaos_phase")
                or final_status.get("workflow_phase")
                or "unknown"
            )
            report["checks"]["injection"] = {
                "passed": verdict == "Pass",
                "mode": "argo_workflow",
                "verdict": verdict,
                "phase": litmus_phase,
                "workflow_name": workflow_name,
            }
        metric_check = validate_metric_effect(chaos_type, service, features)
        report["checks"]["metrics"] = metric_check
        report["passed"] = bool(report["checks"]["injection"]["passed"] and metric_check["passed"])
        reports.append(report)
    return reports


def estimate_duration_seconds(config: Dict[str, Any]) -> int:
    timeline = config["timeline"]
    seconds_per_run = sum(
        int(timeline[key])
        for key in (
            "baseline_seconds",
            "warmup_seconds",
            "fault_duration_seconds",
            "post_recovery_observation_seconds",
        )
    )
    return seconds_per_run * len(TARGET_CELLS) * REPLICAS


def netem_is_loaded() -> bool:
    try:
        proc = subprocess.run(["lsmod"], check=False, capture_output=True, text=True)
    except OSError:
        return False
    return proc.returncode == 0 and any(
        line.split() and line.split()[0] == "sch_netem"
        for line in proc.stdout.splitlines()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    base_config = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    config = build_training_config(base_config)
    if any(cell["chaos_type"] in NETEM_TYPES for cell in TARGET_CELLS) and not netem_is_loaded():
        print(
            "sch_netem is not loaded. Run `make chaos-enable-netem` and retry; "
            "no experiment runs were started.",
            file=sys.stderr,
        )
        return 2
    print(
        "Tipos excluídos neste host: "
        + "; ".join(f"{name}: {reason}" for name, reason in EXCLUDED_CHAOS.items())
    )
    results_dir = ROOT / config["output"]["results_dir"]
    results_dir.mkdir(parents=True, exist_ok=True)
    config_path = results_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    manifests_dir = results_dir / config["output"]["manifests_dir"]
    generate_chaos_manifests.generate(config, manifests_dir)

    expected_seconds = estimate_duration_seconds(config)
    print(
        f"Treino de caos: {len(TARGET_CELLS) * REPLICAS} execuções, "
        f"{expected_seconds // 60} min de janelas configuradas; estime "
        "95-110 min com coleta e espera. Templates ausentes podem acrescentar "
        "alguns minutos de priming."
    )
    command = [
        sys.executable,
        "-m",
        "experiment.eval.memory_hog.runner",
        "--phase",
        "chaos",
        "--config",
        str(config_path),
    ]
    if args.retry_failed:
        command.append("--retry-failed")
    runner_result = subprocess.run(command, cwd=ROOT, check=False)

    reports = validate_runs(results_dir / config["output"]["runs_csv"])
    report_path = results_dir / "effect_validation.json"
    report_path.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    for report in reports:
        print(json.dumps(report, ensure_ascii=False))
    print(f"Relatório de efeito: {report_path.relative_to(ROOT)}")
    return 0 if runner_result.returncode == 0 and reports and all(r.get("passed") for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
