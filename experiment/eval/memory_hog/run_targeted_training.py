#!/usr/bin/env python3
"""Train on every chaos type currently runnable in the WSL2 environment.

From the repository root:
    python -m experiment.eval.memory_hog.run_targeted_training

The script runs two replicas of each of seven supported chaos types on distinct
services, writes results under out/all_chaos_training, and requires a passing
Litmus ChaosResult plus target-specific metric evidence for every run. Reports
separate the observed signal, mechanism confidence, and service impact; a
passing signal gate is not causal proof. The WSL2-incompatible dns-error and
io-stress types are intentionally excluded.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
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


def json_safe(value: Any) -> Any:
    """Replace non-finite floats with JSON null, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


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


def _target_row(
    features: Dict[str, Any], metric_id: str, service: str
) -> Optional[Dict[str, Any]]:
    hotspots = features.get("metric_hotspots") or {}
    entry = hotspots.get(metric_id) or {}
    rows = entry.get("top_edges") or entry.get("top") or []
    for row in rows:
        if isinstance(row, dict) and (
            row.get("label") == service or row.get("destination_workload") == service
        ):
            return row
    return None


def _service_impact(features: Dict[str, Any], service: str) -> Dict[str, Any]:
    error_rate = _target_row(features, "error_rate", service)
    latency = _target_row(features, "latency_p95", service)
    traffic = _target_row(features, "traffic", service)
    error_delta = _float(error_rate or {}, "delta_abs")
    error_peak = _float(error_rate or {}, "fault_p95")
    latency_delta = _float(latency or {}, "delta_abs")
    latency_peak = _float(latency or {}, "fault_p95")
    traffic_baseline = _float(traffic or {}, "baseline_mean")
    traffic_fault = _float(traffic or {}, "fault_mean")
    error_increase = (
        error_delta is not None
        and error_peak is not None
        and error_delta >= 5.0
        and error_peak >= 5.0
    )
    latency_increase = (
        latency_delta is not None
        and latency_peak is not None
        and latency_delta >= 50.0
        and latency_peak >= 100.0
    )
    traffic_drop = (
        traffic_baseline is not None
        and traffic_fault is not None
        and traffic_baseline > 0
        and traffic_fault <= traffic_baseline * 0.8
    )
    return {
        "observed": error_increase or latency_increase or traffic_drop,
        "signals": {
            "error_increase": error_increase,
            "latency_increase": latency_increase,
            "traffic_drop": traffic_drop,
        },
        "metrics": {
            "error_rate": error_rate,
            "latency_p95": latency,
            "traffic": traffic,
        },
    }


def validate_metric_effect(
    chaos_type: str, service: str, features: Dict[str, Any]
) -> Dict[str, Any]:
    """Report target signals, service impact, and whether the mechanism is specific."""
    impact = _service_impact(features, service)
    if chaos_type == "cpu-hog":
        saturation = _target_row(features, "cpu_saturation_pct", service)
        throttling = _target_row(features, "cpu_throttled", service)
        sat_delta = _float(saturation or {}, "delta_abs")
        sat_peak = _float(saturation or {}, "fault_p95")
        throttle_delta = _float(throttling or {}, "delta_abs")
        saturation_observed = (
            sat_delta is not None
            and sat_peak is not None
            and sat_delta >= 10.0
            and sat_peak >= 50.0
        )
        throttling_observed = throttle_delta is not None and throttle_delta >= 0.01
        signal_observed = saturation_observed or throttling_observed
        return {
            "passed": signal_observed,
            "signal_observed": signal_observed,
            "mechanism_signal_observed": signal_observed,
            "mechanism_status": (
                "cpu_contention_observed" if signal_observed else "not_observed"
            ),
            "impact_observed": impact["observed"],
            "metrics": {
                "cpu_saturation_pct": saturation,
                "cpu_throttled": throttling,
                **impact["metrics"],
            },
            "reason": (
                "target CPU saturation/throttling increased; useful application work is not proven"
                if signal_observed
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
        saturation_observed = (
            mem_delta is not None
            and mem_peak is not None
            and mem_delta >= 10.0
            and mem_peak >= 60.0
        )
        oom_observed = oom_delta is not None and oom_delta >= 0.4
        signal_observed = saturation_observed or oom_observed
        return {
            "passed": signal_observed,
            "signal_observed": signal_observed,
            "mechanism_signal_observed": signal_observed,
            "mechanism_status": (
                "oom_killed_observed"
                if oom_observed
                else (
                    "memory_pressure_observed"
                    if saturation_observed
                    else "not_observed"
                )
            ),
            "impact_observed": impact["observed"],
            "metrics": {
                "memory_saturation_pct": saturation,
                "oom_killed": oom,
                "pod_restarts_total": restarts,
                **impact["metrics"],
            },
            "reason": (
                "target memory pressure/OOM observed; restart alone is not evidence of exhaustion"
                if signal_observed
                else "no target memory pressure or OOM signal met threshold; restart alone is insufficient"
            ),
        }

    if chaos_type in {"pod-delete", "container-kill"}:
        restarts = _target_row(features, "pod_restarts_total", service)
        ready = _target_row(features, "pod_ready", service)
        restart_delta = _float(restarts or {}, "delta_abs")
        ready_delta = _float(ready or {}, "delta_abs")
        ready_fault_mean = _float(ready or {}, "fault_mean")
        restart_observed = restart_delta is not None and restart_delta >= 0.2
        readiness_observed = (
            ready_delta is not None
            and ready_fault_mean is not None
            and ready_delta <= -0.25
            and ready_fault_mean <= 0.75
        )
        signal_observed = restart_observed or readiness_observed or impact["observed"]
        return {
            "passed": signal_observed,
            "signal_observed": signal_observed,
            "mechanism_signal_observed": False,
            "mechanism_status": (
                "lifecycle_or_impact_symptom_only"
                if signal_observed
                else "not_observed"
            ),
            "impact_observed": impact["observed"],
            "metrics": {
                "pod_restarts_total": restarts,
                "pod_ready": ready,
                **impact["metrics"],
            },
            "reason": (
                "target lifecycle/impact signal observed; pod identity or termination reason is needed to distinguish the mechanism"
                if signal_observed
                else "no target lifecycle or service-impact signal met threshold"
            ),
        }

    if chaos_type == "network-latency":
        latency = _target_row(features, "latency_p95", service)
        latency_delta = _float(latency or {}, "delta_abs")
        latency_peak = _float(latency or {}, "fault_p95")
        passed = (
            latency_delta is not None
            and latency_peak is not None
            and latency_delta >= 50.0
            and latency_peak >= 100.0
        )
        return {
            "passed": passed,
            "signal_observed": passed,
            "mechanism_signal_observed": False,
            "mechanism_status": "service_latency_only" if passed else "not_observed",
            "impact_observed": passed or impact["observed"],
            "metrics": {"latency_p95": latency, **impact["metrics"]},
            "reason": "target p95 latency increased"
            + (
                "; affected path is not identified"
                if passed
                else " did not meet threshold"
            ),
        }

    if chaos_type == "network-loss":
        receive_drops = _target_row(features, "network_receive_dropped_rate", service)
        transmit_drops = _target_row(features, "network_transmit_dropped_rate", service)
        drop_deltas = [
            _float(receive_drops or {}, "delta_abs"),
            _float(transmit_drops or {}, "delta_abs"),
        ]
        drops_observed = any(
            delta is not None and delta >= 0.1 for delta in drop_deltas
        )
        return {
            "passed": drops_observed,
            "signal_observed": drops_observed,
            "mechanism_signal_observed": False,
            "mechanism_status": (
                "interface_drop_signal_ambiguous" if drops_observed else "not_observed"
            ),
            "impact_observed": impact["observed"],
            "metrics": {
                "network_receive_dropped_rate": receive_drops,
                "network_transmit_dropped_rate": transmit_drops,
                **impact["metrics"],
            },
            "reason": (
                "interface drop counter increased; netem rule, direction and path remain unverified"
                if drops_observed
                else "no target interface drop signal met threshold; 5xx alone does not prove packet loss"
            ),
        }

    if chaos_type == "http-status-code":
        edge = _target_row(features, "http_5xx_by_edge", service)
        error_delta = _float(edge or {}, "delta_abs")
        error_peak = _float(edge or {}, "fault_p95")
        passed = (
            error_delta is not None
            and error_peak is not None
            and error_delta >= 0.01
            and error_peak >= 0.01
            and edge.get("source_workload") not in (None, "", "unknown")
        )
        return {
            "passed": passed,
            "signal_observed": passed,
            "mechanism_signal_observed": passed,
            "mechanism_status": (
                "client_reported_5xx_on_target_edge"
                if passed
                else "target_http_edge_not_observed"
            ),
            "impact_observed": passed or impact["observed"],
            "metrics": {"http_5xx_by_edge": edge, **impact["metrics"]},
            "reason": (
                "5xx observed by the caller on an identified edge to the target"
                if passed
                else "no caller-reported target-edge 5xx met threshold; destination aggregate alone is insufficient"
            ),
        }

    return {
        "passed": False,
        "signal_observed": False,
        "mechanism_signal_observed": False,
        "mechanism_status": "unsupported_chaos_type",
        "impact_observed": False,
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
    """Validate run count and report injection, target signal, and causal limits."""
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
        if not report["checks"]["injection"]["passed"]:
            report["result_status"] = "injection_failed"
        elif not metric_check["signal_observed"]:
            report["result_status"] = (
                "impact_observed_without_target_signal"
                if metric_check["impact_observed"]
                else "target_signal_not_observed"
            )
        elif not metric_check["mechanism_signal_observed"]:
            report["result_status"] = "target_signal_without_mechanism_signal"
        else:
            report["result_status"] = "target_signal_with_mechanism_signal"
        report["passed"] = bool(
            report["checks"]["injection"]["passed"] and metric_check["passed"]
        )
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
