"""Append post-hoc CPU mean-80% eligibility assessments without editing the ledger."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
from pathlib import Path
from typing import Any

import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign


def _revalidate_campaign_locked(directory: Path) -> dict[str, Any]:
    config_path = directory / "config.yaml"
    ledger_path = directory / "ledger.jsonl"
    if not config_path.is_file() or not ledger_path.is_file():
        raise FileNotFoundError("campaign config.yaml and ledger.jsonl are required")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    amendment = campaign.load_cpu_mean80_amendment()
    campaign_id = config.get("experiment_campaign_id")
    if campaign_id != amendment["campaign_id"]:
        raise RuntimeError(
            f"CPU mean amendment is scoped to {amendment['campaign_id']}, not {campaign_id}"
        )

    latest = {
        row["cell_id"]: row for row in campaign.read_ledger(ledger_path)
    }
    cells = campaign.campaign_cells(config)
    existing = campaign.load_cpu_mean80_revalidation_records(directory)
    ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    assessed = 0
    added = 0
    eligible = 0
    ineligible = 0

    for cell in cells:
        if cell.get("chaos_type") != "cpu-hog":
            continue
        entry = latest.get(cell["cell_id"])
        if not entry or entry.get("status") != "completed":
            continue
        assessment = campaign.cpu_mean80_revalidation_record(
            cell, entry, amendment,
        )
        if assessment is None:
            continue
        key = (entry.get("run_id"), int(entry.get("attempt", 1)))
        prior = existing.get(key)
        if prior is not None and prior != assessment:
            raise RuntimeError(
                f"CPU mean assessment changed for {entry.get('run_id')}; "
                "preserve the prior record and create a separately reviewed amendment"
            )
        if prior is None:
            campaign.record_cpu_mean80_revalidation(
                directory, entry, assessment, campaign_id,
            )
            added += 1
        assessed += 1
        if assessment["eligible_for_training"]:
            eligible += 1
        else:
            ineligible += 1

    return {
        "campaign_id": campaign_id,
        "amendment_version": amendment["version"],
        "criterion": (
            f"fault-phase mean CPU/quota fraction >= "
            f"{amendment['minimum_quota_fraction']:.0%}"
        ),
        "source_ledger_sha256": ledger_sha256,
        "assessed": assessed,
        "new_records": added,
        "eligible_for_training": eligible,
        "ineligible": ineligible,
        "revalidation_file": str(directory / campaign.CPU_MEAN80_REVALIDATION_FILE),
    }


def revalidate_campaign(directory: Path) -> dict[str, Any]:
    lock_path = directory / ".campaign.lock"
    with lock_path.open(encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "campaign is active; revalidate after the supervisor exits"
            ) from exc
        return _revalidate_campaign_locked(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = revalidate_campaign(args.campaign_dir)
    print(yaml.safe_dump(result, sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
