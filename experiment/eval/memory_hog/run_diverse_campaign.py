#!/usr/bin/env python3
"""Prepare or explicitly run a roughly 10-hour Sock Shop chaos campaign.

By default this script only prints the run matrix and estimated duration. Pass
``--run`` to perform preflight checks, write a dedicated config/ledger, and
invoke the existing harness. The campaign does not run the LLM evaluation.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any
from urllib.request import urlopen

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE_CONFIG = HERE / "config.yaml"
CAMPAIGN_DIR = HERE / "out" / "diverse_campaign"
CAMPAIGN_ID_PREFIX = "long-chaos-eval"
PYTHON = sys.executable

SERVICES = (
    "carts",
    "catalogue",
    "front-end",
    "orders",
    "payment",
    "shipping",
    "user",
)
FAULTS = (
    "container-kill",
    "cpu-hog",
    "memory-hog",
    "network-latency",
    "network-loss",
    "pod-delete",
    "http-status-code",
)
HTTP_SERVICES = tuple(service for service in SERVICES if service != "front-end")
FAULT_DURATION_SECONDS = 300
FAULT_PHASE_SECONDS = 390
BASELINE_SECONDS = 30
WARMUP_SECONDS = 30
POST_RECOVERY_SECONDS = 60
OBSERVED_NON_PHASE_OVERHEAD_SECONDS = 210
DEFAULT_JITTER_MAX_SECONDS = 15
DEFAULT_COOLDOWN_SECONDS = 30
SETUP_ALLOWANCE_SECONDS = 600


def campaign_cells(seed: int = 20260930) -> list[dict[str, str]]:
    """Return every runnable fault/service pair, excluding HTTP on front-end."""
    cells = [
        {"service": service, "chaos_type": fault}
        for fault in FAULTS
        for service in (HTTP_SERVICES if fault == "http-status-code" else SERVICES)
    ]
    random.Random(seed).shuffle(cells)
    return cells


def duration_estimate(
    run_count: int,
    *,
    timeline_seconds: int = (
        BASELINE_SECONDS + WARMUP_SECONDS + FAULT_PHASE_SECONDS + POST_RECOVERY_SECONDS
    ),
    non_phase_overhead_seconds: int = OBSERVED_NON_PHASE_OVERHEAD_SECONDS,
    jitter_max_seconds: int = DEFAULT_JITTER_MAX_SECONDS,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    setup_seconds: int = SETUP_ALLOWANCE_SECONDS,
) -> tuple[int, int]:
    """Return lower/upper wall-time estimates including setup and jitter."""
    per_run = timeline_seconds + non_phase_overhead_seconds + cooldown_seconds
    lower = run_count * per_run + setup_seconds
    upper = lower + run_count * jitter_max_seconds
    return lower, upper


def build_campaign_config(
    source: dict[str, Any],
    *,
    seed: int = 20260930,
    campaign_id: str,
    campaign_dir: Path,
) -> dict[str, Any]:
    """Create an isolated one-phase config without mutating the source config."""
    config = json.loads(json.dumps(source))
    config["experiment_campaign_id"] = campaign_id
    config["train_experiments"] = campaign_cells(seed)
    config["test_experiments"] = []
    config["replicas"] = 1
    config["resource_chaos"]["duration_seconds"] = FAULT_DURATION_SECONDS
    selected_faults = set(FAULTS)
    for entry in config.get("chaos_types", []):
        if entry.get("name") in selected_faults:
            entry.setdefault("env", {})["total_chaos_duration_seconds"] = FAULT_DURATION_SECONDS
    config["timeline"].update(
        {
            "baseline_seconds": BASELINE_SECONDS,
            "warmup_seconds": WARMUP_SECONDS,
            "fault_duration_seconds": FAULT_PHASE_SECONDS,
            "post_recovery_observation_seconds": POST_RECOVERY_SECONDS,
            "sampling_interval_seconds": 5,
        }
    )
    config["ordering"] = {
        "shuffle": False,
        "seed": seed,
        "start_jitter_max_seconds": DEFAULT_JITTER_MAX_SECONDS,
    }
    config["platform"] = {
        **source["platform"],
        "inter_run_cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
    }
    config["output"] = {
        **source["output"],
        "results_dir": str(campaign_dir),
        "runs_csv": "runs.csv",
        "evaluation_csv": "evaluation.csv",
        "manifests_dir": "manifests",
    }
    return config


def _active_runs(api_base: str) -> list[dict[str, Any]]:
    try:
        with urlopen(f"{api_base.rstrip('/')}/api/runs", timeout=5) as response:
            runs = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot check active runs through {api_base}: {exc}") from exc
    if not isinstance(runs, list):
        raise RuntimeError("the platform API returned an invalid runs list")
    return [
        run
        for run in runs
        if str(run.get("status", "")).lower() in {"queued", "pending", "running", "starting"}
    ]


def _active_local_harnesses() -> list[str]:
    active = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        try:
            command = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if "experiment.eval.memory_hog.run_quick_campaign" in command or (
            "experiment.eval.memory_hog.runner" in command and "--phase chaos" in command
        ):
            active.append(f"pid={process_dir.name}")
    return active


def wait_for_idle(api_base: str, *, wait: bool) -> None:
    while True:
        active = _active_runs(api_base)
        local_harnesses = _active_local_harnesses()
        if not active and not local_harnesses:
            return
        run_ids = ", ".join(str(run.get("run_id", "unknown")) for run in active)
        active_details = [detail for detail in (run_ids, ", ".join(local_harnesses)) if detail]
        description = "; ".join(active_details)
        if not wait:
            raise RuntimeError(f"active experiment work exists ({description}); use --wait-for-idle")
        print(f"Waiting for active experiment work to finish: {description}", flush=True)
        time.sleep(30)


def _kubectl_json(*args: str) -> dict[str, Any]:
    result = subprocess.run(
        ["kubectl", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"kubectl {' '.join(args)} failed")
    return json.loads(result.stdout)


def preflight(config: dict[str, Any]) -> list[str]:
    """Fail before any experiment if the local cluster cannot run the matrix."""
    namespace = config["cluster"]["app_namespace"]
    nodes = _kubectl_json("get", "nodes", "-o", "json").get("items", [])
    ready_nodes = [
        node
        for node in nodes
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
    ]
    if not ready_nodes:
        raise RuntimeError("Kubernetes has no Ready nodes")

    deployments = _kubectl_json(
        "get", "deployments", "-n", namespace, "-o", "json"
    ).get("items", [])
    by_name = {deployment["metadata"]["name"]: deployment for deployment in deployments}
    missing = [service for service in SERVICES if service not in by_name]
    unavailable = [
        service
        for service in SERVICES
        if service in by_name
        and by_name[service].get("status", {}).get("availableReplicas", 0) < 1
    ]
    if missing or unavailable:
        details = []
        if missing:
            details.append(f"missing deployments: {', '.join(missing)}")
        if unavailable:
            details.append(f"not available: {', '.join(unavailable)}")
        raise RuntimeError(f"Sock Shop preflight failed ({'; '.join(details)})")

    for service in SERVICES:
        containers = (
            by_name[service]
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        app = next((container for container in containers if container.get("name") == service), None)
        limits = (app or {}).get("resources", {}).get("limits", {})
        if not limits.get("cpu") or not limits.get("memory"):
            raise RuntimeError(
                f"deployment/{service} application container needs CPU and memory limits"
            )

    try:
        modules = Path("/proc/modules").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect /proc/modules for sch_netem: {exc}") from exc
    if not any(line.split(" ", 1)[0] == "sch_netem" for line in modules):
        raise RuntimeError(
            "sch_netem is not loaded in the WSL2 host kernel; run `make chaos-enable-netem` "
            "before starting the campaign"
        )

    return [
        f"Kubernetes Ready nodes: {len(ready_nodes)}",
        f"Sock Shop available deployments: {len(SERVICES)}",
        "CPU/memory limits present on all target containers",
        "sch_netem loaded",
    ]


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run after preflight; otherwise report only.")
    parser.add_argument("--skip-prime", action="store_true", help="Do not generate/apply missing Litmus templates.")
    parser.add_argument("--wait-for-idle", action="store_true", help="Wait for active platform runs before starting.")
    parser.add_argument("--seed", type=int, default=20260930)
    args = parser.parse_args()

    source = yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8"))
    campaign_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    campaign_id = f"{CAMPAIGN_ID_PREFIX}-{campaign_stamp}"
    campaign_dir = CAMPAIGN_DIR / campaign_id
    config = build_campaign_config(
        source,
        seed=args.seed,
        campaign_id=campaign_id,
        campaign_dir=campaign_dir,
    )
    cells = config["train_experiments"]
    lower, upper = duration_estimate(
        len(cells),
        timeline_seconds=sum(
            int(config["timeline"][key])
            for key in (
                "baseline_seconds",
                "warmup_seconds",
                "fault_duration_seconds",
                "post_recovery_observation_seconds",
            )
        ),
        jitter_max_seconds=config["ordering"]["start_jitter_max_seconds"],
        cooldown_seconds=config["platform"]["inter_run_cooldown_seconds"],
    )
    counts = {fault: sum(c["chaos_type"] == fault for c in cells) for fault in FAULTS}
    print(f"Campaign: {campaign_id}; seed={args.seed}; {len(cells)} runs")
    print("Matrix: " + ", ".join(f"{fault}={count}" for fault, count in counts.items()))
    print(
        "Estimated wall time: "
        f"{_format_duration(lower)}-{_format_duration(upper)} "
        f"(includes {_format_duration(SETUP_ALLOWANCE_SECONDS)} setup allowance, "
        f"{FAULT_DURATION_SECONDS}s fault per run, and 0-{config['ordering']['start_jitter_max_seconds']}s jitter; "
        f"{config['platform']['inter_run_cooldown_seconds']}s cooldown)"
    )
    print("Excluded: dns-error and io-stress (incompatible with this WSL2 cluster)")
    if not args.run:
        print("Report only. Pass --run to execute; no cluster changes were made.")
        return 0

    try:
        wait_for_idle(config["platform"]["api_base"], wait=args.wait_for_idle)
        checks = preflight(config)
    except (RuntimeError, FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2
    print("Preflight passed: " + "; ".join(checks))

    campaign_dir.mkdir(parents=True, exist_ok=False)
    config_path = campaign_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    command = [
        PYTHON,
        "-m",
        "experiment.eval.memory_hog.runner",
        "--config",
        str(config_path),
        "--phase",
        "chaos",
        "--retry-failed",
    ]
    if args.skip_prime:
        command.append("--skip-prime")
    print("Starting campaign:", " ".join(command), flush=True)
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
