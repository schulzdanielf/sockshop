#!/usr/bin/env python3
"""Run one short, target-diverse sample of every WSL2-runnable fault type.

Default mode prints the seven-run matrix and estimate. Pass ``--run`` to
preflight, create an isolated config/ledger, execute the chaos phase and write
target-specific effect checks. DNS and IO faults are excluded because they are
unsupported by the current Docker Desktop/WSL2 environment.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from experiment.eval.memory_hog import generate_chaos_manifests
from experiment.eval.memory_hog import run_targeted_training as targeted

target_container_limits = getattr(
    __import__("experiment.eval.memory_hog.runner", fromlist=["_target_container_limits"]),
    "_target_container_limits",
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE_CONFIG = HERE / "config.yaml"
CAMPAIGN_DIR = HERE / "out" / "quick_campaign"
CAMPAIGN_ID = "quick-chaos-eval-20261001"
SEED = 20261001
PER_RUN_OVERHEAD_SECONDS = 180
PRIMING_ALLOWANCE_SECONDS = 300


def build_quick_config(
    source: dict[str, Any],
    campaign_id: str = CAMPAIGN_ID,
    campaign_dir: Path = CAMPAIGN_DIR,
    cells: Optional[List[Dict[str, str]]] = None,
) -> dict[str, Any]:
    """Return a short, held-out config without changing the main campaign."""
    cells = targeted.TARGET_CELLS if cells is None else cells
    config = json.loads(json.dumps(source))
    config["experiment_campaign_id"] = campaign_id
    config["platform"]["initiated_by"] = "quick-chaos-evaluation"
    config["platform"]["poll_interval_seconds"] = 10
    config["platform"]["run_timeout_seconds"] = 900
    config["platform"]["inter_run_cooldown_seconds"] = 15
    config["resource_chaos"]["duration_seconds"] = 60
    config["timeline"].update(
        {
            "baseline_seconds": 30,
            "warmup_seconds": 30,
            # Startup took up to 68s in the previous run; allow the full 60s
            # injection duration plus a margin for the workflow to finalize.
            "fault_duration_seconds": 150,
            "post_recovery_observation_seconds": 60,
            "sampling_interval_seconds": 5,
        }
    )
    selected_faults = {cell["chaos_type"] for cell in cells}
    for entry in config.get("chaos_types", []):
        if entry.get("name") in selected_faults:
            entry.setdefault("env", {})["total_chaos_duration_seconds"] = 60
    config["train_experiments"] = []
    config["test_experiments"] = [dict(cell) for cell in cells]
    config["replicas"] = 1
    config["ordering"] = {
        "shuffle": True,
        "seed": SEED,
        "start_jitter_max_seconds": 0,
    }
    config["output"].update(
        {
            "results_dir": str(campaign_dir),
            "runs_csv": "runs.csv",
            "evaluation_csv": "evaluation.csv",
            "manifests_dir": "manifests",
        }
    )
    return config


def duration_estimate_seconds(config: dict[str, Any]) -> int:
    timeline = config["timeline"]
    phase_seconds = sum(
        int(timeline[key])
        for key in (
            "baseline_seconds",
            "warmup_seconds",
            "fault_duration_seconds",
            "post_recovery_observation_seconds",
        )
    )
    count = len(config["test_experiments"])
    return (
        count
        * (
            phase_seconds
            + PER_RUN_OVERHEAD_SECONDS
            + config["platform"]["inter_run_cooldown_seconds"]
        )
        + PRIMING_ALLOWANCE_SECONDS
    )


def preflight(
    config: dict[str, Any],
    cells: Optional[List[Dict[str, str]]] = None,
) -> list[str]:
    cells = targeted.TARGET_CELLS if cells is None else cells
    uses_netem = any(cell["chaos_type"] in targeted.NETEM_TYPES for cell in cells)
    if uses_netem and not targeted.netem_is_loaded():
        raise RuntimeError(
            "sch_netem is not loaded; run `make chaos-enable-netem` before the campaign"
        )
    result = subprocess.run(
        ["kubectl", "get", "deployments", "-n", config["cluster"]["app_namespace"], "-o", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "cannot list Sock Shop deployments")
    deployments = {
        item["metadata"]["name"]: item
        for item in json.loads(result.stdout).get("items", [])
    }
    services = sorted({cell["service"] for cell in cells})
    unavailable = [
        service
        for service in services
        if deployments.get(service, {}).get("status", {}).get("availableReplicas", 0) < 1
    ]
    if unavailable:
        raise RuntimeError(f"target deployments are missing or unavailable: {', '.join(unavailable)}")

    resource_targets = [
        cell for cell in cells if cell["chaos_type"] in {"cpu-hog", "memory-hog"}
    ]
    for cell in resource_targets:
        target_container_limits(cell["service"], config["cluster"]["app_namespace"])
    checks = [f"all {len(services)} target deployments available"]
    if resource_targets:
        checks.append("resource limits present for CPU/memory targets")
    if uses_netem:
        checks.append("sch_netem loaded")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Execute after preflight; default only prints the plan.")
    parser.add_argument("--skip-prime", action="store_true", help="Skip automatic priming of missing Litmus templates.")
    args = parser.parse_args()

    source = yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8"))
    campaign_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    campaign_id = f"quick-chaos-eval-{campaign_stamp}"
    campaign_dir = CAMPAIGN_DIR / campaign_id
    config = build_quick_config(source, campaign_id, campaign_dir)
    cells = config["test_experiments"]
    estimate = duration_estimate_seconds(config)
    print(f"Campaign: {campaign_id}; {len(cells)} runs; seed={SEED}")
    for cell in cells:
        print(f"  {cell['chaos_type']:<18} -> {cell['service']}")
    print(f"Estimated wall time: about {(estimate + 59) // 60} min, plus cluster variability")
    print("Excluded: dns-error (unsupported target netns), io-stress (WSL2 overlayfs/O_DIRECT)")
    if not args.run:
        print("Report only. Pass --run to execute; no cluster changes were made.")
        return 0

    try:
        checks = preflight(config)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2
    print("Preflight passed: " + "; ".join(checks))

    campaign_dir.mkdir(parents=True, exist_ok=False)
    config_path = campaign_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    manifests_dir = campaign_dir / config["output"]["manifests_dir"]
    generate_chaos_manifests.generate(config, manifests_dir)

    command = [
        sys.executable,
        "-m",
        "experiment.eval.memory_hog.runner",
        "--phase",
        "chaos",
        "--config",
        str(config_path),
        "--retry-failed",
    ]
    if args.skip_prime:
        command.append("--skip-prime")
    print("Starting campaign:", " ".join(command), flush=True)
    runner_result = subprocess.run(command, cwd=ROOT, check=False)

    reports = targeted.validate_runs(
        campaign_dir / config["output"]["runs_csv"],
        cells=cells,
        replicas=1,
        phase="test",
    )
    report_path = campaign_dir / "effect_validation.json"
    report_path.write_text(
        json.dumps(targeted.json_safe(reports), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    for report in reports:
        print(json.dumps(targeted.json_safe(report), ensure_ascii=False, allow_nan=False))
    print(f"Effect report: {report_path.relative_to(ROOT)}")
    return 0 if runner_result.returncode == 0 and reports and all(item.get("passed") for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
