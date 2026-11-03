#!/usr/bin/env python3
"""Compatibility entry point for the controlled application campaign.

Legacy Skill RCA scoring helpers remain available for historical reports.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from experiment.eval.memory_hog import run_quick_campaign as quick
from experiment.eval.memory_hog.controlled_campaign import SMALL_CELLS

FIVE_RUN_CELLS = SMALL_CELLS
SKILL_RCA_TIMEOUT_SECONDS = 900


def build_quality_summary(
    *,
    campaign_id: str,
    expected_runs: int,
    run_rows: List[Dict[str, str]],
    effect_reports: List[Dict[str, Any]],
    responses: Dict[str, Dict[str, Any]],
    failed_skill_run_ids: List[str],
    skill_failure_reasons: Dict[str, str],
    fault_categories: Dict[str, str],
) -> Dict[str, Any]:
    completed = [
        row
        for row in run_rows
        if row.get("phase") == "test" and row.get("status") == "completed"
    ]
    scored: List[Dict[str, Any]] = []
    for row in completed:
        run_id = row.get("run_id", "")
        response = responses.get(run_id)
        if response is None:
            continue
        diagnosis = response.get("diagnosis") or {}
        expected_category = fault_categories.get(row.get("chaos_type", ""), "")
        predicted_service = diagnosis.get("primary_service")
        predicted_category = diagnosis.get("category", "unknown")
        predicted_fault_type = diagnosis.get("fault_type")
        service_correct = predicted_service == row.get("service")
        category_correct = predicted_category == expected_category
        fault_type_correct = predicted_fault_type == row.get("chaos_type")
        scored.append(
            {
                "run_id": run_id,
                "chaos_type": row.get("chaos_type"),
                "status": response.get("status"),
                "expected_service": row.get("service"),
                "predicted_service": predicted_service,
                "service_correct": service_correct,
                "expected_category": expected_category,
                "predicted_category": predicted_category,
                "category_correct": category_correct,
                "predicted_fault_type": predicted_fault_type,
                "fault_type_correct": fault_type_correct,
                "both_correct": service_correct and category_correct,
            }
        )

    denominator = len(scored)
    service_correct = sum(item["service_correct"] for item in scored)
    category_correct = sum(item["category_correct"] for item in scored)
    fault_type_correct = sum(item["fault_type_correct"] for item in scored)
    both_correct = sum(item["both_correct"] for item in scored)

    def rate(count: int) -> Optional[float]:
        return round(count / denominator, 4) if denominator else None

    return {
        "campaign_id": campaign_id,
        "expected_runs": expected_runs,
        "completed_runs": len(completed),
        "effect_checks_passed": sum(bool(report.get("passed")) for report in effect_reports),
        "effect_checks_total": len(effect_reports),
        "skill_rca_responses": denominator,
        "skill_rca_failures": failed_skill_run_ids,
        "skill_rca_failure_reasons": skill_failure_reasons,
        "confirmed": sum(item["status"] == "confirmed" for item in scored),
        "inconclusive": sum(item["status"] == "inconclusive" for item in scored),
        "scored_service_correct": service_correct,
        "service_accuracy": rate(service_correct),
        "scored_category_correct": category_correct,
        "fault_category_accuracy": rate(category_correct),
        "scored_fault_type_correct": fault_type_correct,
        "fault_type_accuracy": rate(fault_type_correct),
        "scored_both_correct": both_correct,
        "joint_accuracy": rate(both_correct),
        "score_denominator": denominator,
        "per_run": scored,
        "note": (
            "Accuracy is computed only for valid Skill RCA responses; inconclusive "
            "responses remain in the denominator and are scored against both labels."
        ),
    }


def _read_run_rows(runs_csv: Path) -> List[Dict[str, str]]:
    if not runs_csv.exists():
        return []
    with runs_csv.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _run_skill_rca(
    completed_runs: List[Dict[str, str]],
    output_dir: Path,
    possible_fault_types: List[str] | None = None,
) -> tuple[Dict[str, Dict[str, Any]], List[str], Dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    possible_fault_types = possible_fault_types or sorted(
        {row["chaos_type"] for row in completed_runs if row.get("chaos_type")}
    )
    responses: Dict[str, Dict[str, Any]] = {}
    failures: List[str] = []
    failure_reasons: Dict[str, str] = {}
    for row in completed_runs:
        run_id = row["run_id"]
        output = output_dir / f"{run_id}.json"
        log_path = output_dir / f"{run_id}.log"
        print(f"Skill RCA: {run_id}", flush=True)
        try:
            command = [
                sys.executable,
                "-m",
                "experiment.platform.rca_cli",
                "--run-id",
                run_id,
                "--timeout",
                str(SKILL_RCA_TIMEOUT_SECONDS),
                "--output",
                str(output),
            ]
            for fault_type in possible_fault_types:
                command.extend(["--possible-fault-type", fault_type])
            result = subprocess.run(
                command,
                cwd=quick.ROOT,
                check=False,
                timeout=SKILL_RCA_TIMEOUT_SECONDS + 30,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            log_path.write_text(stdout + "\n" + stderr, encoding="utf-8")
            reason = f"timed out after {SKILL_RCA_TIMEOUT_SECONDS + 30}s"
            print(f"Skill RCA failed for {run_id}: {reason}", file=sys.stderr)
            failures.append(run_id)
            failure_reasons[run_id] = reason
            continue
        log_path.write_text(
            result.stdout + ("\n" if result.stderr else "") + result.stderr,
            encoding="utf-8",
        )
        if result.returncode != 0:
            reason = (result.stderr or result.stdout).strip()
            reason = reason.splitlines()[-1][:500] if reason else "non-zero exit status"
            print(f"Skill RCA failed for {run_id}: {reason}", file=sys.stderr)
            failures.append(run_id)
            failure_reasons[run_id] = reason
            continue
        try:
            responses[run_id] = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reason = f"cannot read result JSON: {exc}"
            print(f"Skill RCA failed for {run_id}: {reason}", file=sys.stderr)
            failures.append(run_id)
            failure_reasons[run_id] = reason
    return responses, failures, failure_reasons


def run_campaign(*, execute: bool, skip_prime: bool = False) -> int:
    from experiment.eval.memory_hog.controlled_campaign import main as controlled_main

    if skip_prime:
        raise ValueError("--skip-prime is obsolete: controlled campaigns never prime templates")
    return controlled_main(["--run"] if execute else [])


def main() -> int:
    from experiment.eval.memory_hog.controlled_campaign import main as controlled_main

    return controlled_main()


if __name__ == "__main__":
    raise SystemExit(main())
