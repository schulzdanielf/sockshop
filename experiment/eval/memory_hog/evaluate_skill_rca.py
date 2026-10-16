#!/usr/bin/env python3
"""Evaluate Skill RCA against an existing campaign's completed-run cohort."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_CAMPAIGN_DIR = (
    Path(__file__).resolve().parent
    / "out"
    / "forty_faults"
    / "forty-faults-20261012T190530194018Z"
)
DEFAULT_TIMEOUT_SECONDS = 900


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_cohort(campaign_dir: Path, baseline_path: Path) -> list[dict[str, Any]]:
    """Use the completed run IDs from the prior evaluation as the paired cohort."""
    baseline = _read_json(baseline_path)
    baseline_results = baseline.get("results")
    if not isinstance(baseline_results, list):
        raise ValueError("baseline evaluation has no results list")
    runs_path = campaign_dir / "runs.csv"
    if not runs_path.is_file():
        raise FileNotFoundError(f"campaign run manifest is missing: {runs_path}")

    import csv

    with runs_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows_by_id = {row.get("run_id"): row for row in rows if row.get("run_id")}

    cohort: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in baseline_results:
        if not isinstance(result, dict):
            raise ValueError("baseline contains an invalid result entry")
        if (
            result.get("run_status") != "completed"
            or result.get("rca_status") != "completed"
        ):
            continue
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in seen:
            raise ValueError(
                f"baseline contains a missing or repeated run ID: {run_id!r}"
            )
        row = rows_by_id.get(run_id)
        if (
            row is None
            or row.get("status") != "completed"
            or row.get("service") != result.get("ground_truth_service")
            or row.get("chaos_type") != result.get("chaos_type")
        ):
            raise ValueError(f"baseline and campaign manifest disagree for {run_id}")
        if not result.get("ground_truth_fault_category"):
            raise ValueError(f"baseline has no fault category for {run_id}")
        seen.add(run_id)
        cohort.append(result)
    if not cohort:
        raise ValueError("baseline contains no completed RCA runs to compare")
    return cohort


def _call_skill_rca(api_base: str, run_id: str, timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        f"{api_base.rstrip('/')}/api/runs/{quote(run_id, safe='')}/skill-rca",
        data=b"{}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("Skill RCA endpoint returned a non-object response")
    return result


def _attempt(
    api_base: str, baseline: dict[str, Any], timeout_seconds: int
) -> dict[str, Any]:
    run_id = baseline["run_id"]
    entry: dict[str, Any] = {
        "run_id": run_id,
        "service": baseline["service"],
        "chaos_type": baseline["chaos_type"],
        "ground_truth_service": baseline["ground_truth_service"],
        "ground_truth_fault_category": baseline["ground_truth_fault_category"],
        "legacy": {
            "status": baseline.get("rca_status"),
            "localized_service": baseline.get("localized_service"),
            "localization_correct": baseline.get("localization_correct"),
            "cause": baseline.get("cause"),
            "fault_category_correct": baseline.get("fault_category_correct"),
            "both_correct": (
                baseline.get("localization_correct") is True
                and baseline.get("fault_category_correct") is True
            ),
        },
    }
    try:
        response = _call_skill_rca(api_base, run_id, timeout_seconds)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get(
                "detail", {}
            )
        except (json.JSONDecodeError, AttributeError):
            detail = {}
        entry.update(
            {
                "status": "http_error",
                "error": f"HTTP {exc.code}: {detail.get('error') or exc.reason}",
                "attempt_id": detail.get("attempt_id"),
                "artifact_path": detail.get("artifact_path"),
            }
        )
        return entry
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        entry.update(
            {"status": "request_error", "error": f"{type(exc).__name__}: {exc}"}
        )
        return entry

    diagnosis = response.get("diagnosis")
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    localized = diagnosis.get("primary_service")
    category = diagnosis.get("category")
    localized_correct = (
        localized.casefold() == entry["ground_truth_service"].casefold()
        if isinstance(localized, str) and localized
        else None
    )
    category_correct = (
        category == entry["ground_truth_fault_category"]
        if isinstance(category, str) and category
        else None
    )
    fault_families = sorted(
        {
            item["family"]
            for item in response.get("evidence", [])
            if isinstance(item, dict)
            and item.get("status") == "ok"
            and item.get("fault_observation_count", 0) > 0
            and isinstance(item.get("family"), str)
        }
    )
    entry.update(
        {
            "status": response.get("status", "invalid_response"),
            "localized_service": localized,
            "localization_correct": localized_correct,
            "category": category,
            "fault_category_correct": category_correct,
            "both_correct": (
                localized_correct is True and category_correct is True
                if isinstance(localized_correct, bool)
                and isinstance(category_correct, bool)
                else None
            ),
            "confirmed": response.get("status") == "confirmed",
            "attempt_id": response.get("attempt_id"),
            "artifact_path": response.get("artifact_path"),
            "skill_hash": (response.get("skill") or {}).get("sha256"),
            "model": response.get("model"),
            "tools_used": response.get("tools_used", []),
            "evidence_family_count": len(fault_families),
            "fault_evidence_families": fault_families,
            "evidence_count": len(response.get("evidence") or []),
            "limitations": (
                diagnosis.get("limitations", [])
                if diagnosis
                else [response.get("error", "no diagnosis returned")]
            ),
            "answer": response.get("answer", ""),
        }
    )
    return entry


def _accuracy(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    evaluated = [row[field] for row in results if isinstance(row.get(field), bool)]
    correct = sum(value is True for value in evaluated)
    return {
        "correct": correct,
        "evaluated": len(evaluated),
        "accuracy": round(correct / len(evaluated), 4) if evaluated else None,
    }


def _summarize(
    results: list[dict[str, Any]], cohort: list[dict[str, Any]]
) -> dict[str, Any]:
    baseline_by_id = {row["run_id"]: row for row in cohort}
    paired = [
        row
        for row in results
        if row["run_id"] in baseline_by_id
        and row.get("status") in {"confirmed", "inconclusive"}
    ]
    tool_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    for row in results:
        status_counts[str(row.get("status", "unknown"))] += 1
        tool_counts.update(
            tool for tool in row.get("tools_used", []) if isinstance(tool, str)
        )
        family_counts.update(
            family
            for family in row.get("fault_evidence_families", [])
            if isinstance(family, str)
        )
    gains = {"localization": 0, "fault_category": 0, "both": 0}
    regressions = {"localization": 0, "fault_category": 0, "both": 0}
    for row in paired:
        for key, skill_key, legacy_key in (
            ("localization", "localization_correct", "localization_correct"),
            ("fault_category", "fault_category_correct", "fault_category_correct"),
            ("both", "both_correct", "both_correct"),
        ):
            old = row["legacy"][legacy_key]
            new = row.get(skill_key)
            if isinstance(old, bool) and isinstance(new, bool):
                if new and not old:
                    gains[key] += 1
                elif old and not new:
                    regressions[key] += 1
    completed = [
        row for row in results if row.get("status") in {"confirmed", "inconclusive"}
    ]
    return {
        "cohort_runs": len(cohort),
        "attempted": len(results),
        "completed": len(completed),
        "confirmed": sum(row.get("confirmed") is True for row in completed),
        "inconclusive": sum(row.get("status") == "inconclusive" for row in results),
        "failed": len(results) - len(completed),
        "status_counts": dict(sorted(status_counts.items())),
        "service_localization": _accuracy(results, "localization_correct"),
        "fault_classification": _accuracy(results, "fault_category_correct"),
        "both_correct": _accuracy(results, "both_correct"),
        "paired_completed": len(paired),
        "paired_gains": gains,
        "paired_regressions": regressions,
        "fault_evidence_family_counts": dict(sorted(family_counts.items())),
        "tool_call_counts": dict(sorted(tool_counts.items())),
    }


def _write_report(path: Path, report: dict[str, Any], *, create: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        with path.open("x", encoding="utf-8"):
            pass
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def evaluate(
    *,
    api_base: str,
    cohort: list[dict[str, Any]],
    campaign_id: str,
    output: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite an existing report: {output}")
    report: dict[str, Any] = {
        "schema_version": 1,
        "evaluation": "skill-rca",
        "campaign_id": campaign_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_base": api_base,
        "baseline_evaluation": "rca_evaluation_retry.json",
        "cohort_run_ids": [row["run_id"] for row in cohort],
        "summary": {},
        "results": [],
    }
    _write_report(output, report, create=True)
    for index, baseline in enumerate(cohort, start=1):
        print(
            f"[{index}/{len(cohort)}] Skill RCA {baseline['run_id']} "
            f"({baseline['chaos_type']} -> {baseline['service']})",
            flush=True,
        )
        started = time.monotonic()
        result = _attempt(api_base, baseline, timeout_seconds)
        result["duration_seconds"] = round(time.monotonic() - started, 2)
        report["results"].append(result)
        report["summary"] = _summarize(report["results"], cohort)
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(output, report)
        print(
            f"  status={result.get('status')} service={result.get('localized_service')} "
            f"category={result.get('category')} "
            f"artifact={result.get('artifact_path')}",
            flush=True,
        )
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN_DIR)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Prior RCA evaluation report; defaults to rca_evaluation_retry.json",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8010")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    campaign_dir = args.campaign_dir.resolve()
    baseline_path = args.baseline or campaign_dir / "rca_evaluation_retry.json"
    output = args.output or campaign_dir / "rca_skill_evaluation.json"
    try:
        baseline_doc = _read_json(baseline_path)
        cohort = load_cohort(campaign_dir, baseline_path)
        print(
            f"Campaign {baseline_doc.get('campaign_id')}: "
            f"{len(cohort)} paired completed runs; no fault injection.",
            flush=True,
        )
        report = evaluate(
            api_base=args.api_base,
            cohort=cohort,
            campaign_id=str(baseline_doc.get("campaign_id") or campaign_dir.name),
            output=output,
            timeout_seconds=args.timeout,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Skill RCA evaluation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report saved: {output}")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
