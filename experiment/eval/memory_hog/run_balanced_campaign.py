#!/usr/bin/env python3
"""Prepare and run the balanced overnight chaos campaign.

The campaign is intentionally separate from the historical main ledger.  Its
ledger is seeded with the current completed records, so only missing cells are
executed while the new runs receive a new campaign id.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCE_CONFIG = HERE / "config.yaml"
CAMPAIGN_DIR = HERE / "out" / "balanced_campaign"
CAMPAIGN_LEDGER = CAMPAIGN_DIR / "runs.csv"
CAMPAIGN_ID = "balanced-chaos-eval-20260923"
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
)

# Third replicas reserved for the low-count fault/service combinations.
REINFORCEMENT_CELLS = (
    {"service": "carts", "chaos_type": "network-loss"},
    {"service": "catalogue", "chaos_type": "network-loss"},
    {"service": "front-end", "chaos_type": "network-loss"},
    {"service": "orders", "chaos_type": "network-loss"},
    {"service": "shipping", "chaos_type": "network-loss"},
    {"service": "user", "chaos_type": "network-loss"},
    {"service": "catalogue", "chaos_type": "pod-delete"},
    {"service": "front-end", "chaos_type": "pod-delete"},
    {"service": "orders", "chaos_type": "pod-delete"},
    {"service": "payment", "chaos_type": "pod-delete"},
    {"service": "shipping", "chaos_type": "pod-delete"},
    {"service": "user", "chaos_type": "pod-delete"},
    {"service": "carts", "chaos_type": "memory-hog"},
    {"service": "front-end", "chaos_type": "memory-hog"},
    {"service": "shipping", "chaos_type": "memory-hog"},
    {"service": "catalogue", "chaos_type": "container-kill"},
    {"service": "orders", "chaos_type": "container-kill"},
)


def all_cells() -> list[dict[str, str]]:
    return [
        {"service": service, "chaos_type": fault}
        for fault in FAULTS
        for service in SERVICES
    ]


def _config(stage: int) -> tuple[dict[str, Any], Path]:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8"))
    config["experiment_campaign_id"] = CAMPAIGN_ID
    config["train_experiments"] = all_cells() if stage == 1 else list(REINFORCEMENT_CELLS)
    config["test_experiments"] = []
    config["replicas"] = 2 if stage == 1 else 3
    config["output"] = {
        **config["output"],
        "results_dir": str(CAMPAIGN_DIR),
        "runs_csv": "runs.csv",
        "evaluation_csv": "evaluation.csv",
    }
    path = CAMPAIGN_DIR / f"config.stage{stage}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config, path


def _seed_ledger() -> None:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    if CAMPAIGN_LEDGER.exists():
        return
    source = HERE / "out" / "runs.csv"
    if not source.exists():
        raise FileNotFoundError(f"Current ledger not found: {source}")
    shutil.copy2(source, CAMPAIGN_LEDGER)
    print(f"Seeded campaign ledger from {source}")


def _validate() -> None:
    stage1, _ = _config(1)
    stage2, _ = _config(2)
    assert len(stage1["train_experiments"]) == 42
    assert stage1["replicas"] == 2
    assert len(stage2["train_experiments"]) == 17
    assert stage2["replicas"] == 3
    pairs = {(c["chaos_type"], c["service"]) for c in stage1["train_experiments"]}
    assert len(pairs) == 42
    assert not stage1["test_experiments"] and not stage2["test_experiments"]
    print("Validated: stage 1 = 42 cells x 2 replicas; stage 2 = 17 cells x third replica")
    print(f"Configs written under {CAMPAIGN_DIR}")


def _run(stage: int, skip_prime: bool) -> int:
    _seed_ledger()
    _, config_path = _config(stage)
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
    if skip_prime:
        command.append("--skip-prime")
    print("Running:", " ".join(command))
    return subprocess.call(command, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("prepare", "1", "2", "all"),
        default="prepare",
        help="prepare configs, run coverage stage 1, reinforcement stage 2, or both",
    )
    parser.add_argument(
        "--skip-prime",
        action="store_true",
        help="Do not generate/apply Argo templates before running chaos.",
    )
    args = parser.parse_args()

    if args.stage == "prepare":
        _validate()
        return 0
    if args.stage == "all":
        _validate()
        status = _run(1, args.skip_prime)
        if status != 0:
            return status
        return _run(2, args.skip_prime)
    return _run(int(args.stage), args.skip_prime)


if __name__ == "__main__":
    raise SystemExit(main())
