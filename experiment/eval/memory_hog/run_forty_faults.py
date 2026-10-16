#!/usr/bin/env python3
"""Plan 40 varied service/fault pairs; execute only with --run."""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

from experiment.eval.memory_hog import generate_chaos_manifests
from experiment.eval.memory_hog import run_diverse_campaign as diverse
from experiment.eval.memory_hog import run_targeted_training as targeted
from experiment.eval.memory_hog import runner

SOURCE_CONFIG = diverse.SOURCE_CONFIG
ROOT = diverse.ROOT
CAMPAIGN_DIR = SOURCE_CONFIG.parent / "out" / "forty_faults"
RUN_COUNT = 40
FAULT_SECONDS = 180
RCA_TIMEOUT_SECONDS = 300

_CAUSE_ALIASES = {
    "memory-exhaustion": ("memory-hog", "memory pressure", "out of memory", "oom-kill"),
    "cpu-exhaustion": ("cpu-hog", "cpu saturation"),
    "pod-failure": (
        "pod-delete",
        "container-kill",
        "pod failure",
        "pod deletion",
        "container failure",
        "container restart",
        "pod restart",
    ),
    "network-latency": ("network-latency", "network latency", "network delay"),
    "network-loss": ("network-loss", "network loss", "packet loss"),
    "http-error": ("http-status-code", "http 500", "5xx", "status code"),
}


def build_cells(run_count: int = RUN_COUNT) -> list[dict[str, str]]:
    """Build a deterministic matrix with unique pairs and varied adjacent runs."""
    services = diverse.SERVICES
    faults = diverse.FAULTS
    available: list[dict[str, str]] = []
    for round_index in range(len(faults)):
        for service_index, service in enumerate(services):
            chaos_type = faults[(service_index + round_index) % len(faults)]
            if chaos_type == "http-status-code" and service not in diverse.HTTP_SERVICES:
                continue
            available.append({"service": service, "chaos_type": chaos_type})

    if run_count < 1 or run_count > len(available):
        raise ValueError(
            f"run_count must be between 1 and {len(available)}; got {run_count}"
        )
    cells = available[:run_count]
    pairs = {(cell["service"], cell["chaos_type"]) for cell in cells}
    if len(pairs) != run_count:
        raise ValueError("campaign schedule contains repeated service/fault pairs")
    for previous, current in zip(cells, cells[1:]):
        if (
            previous["service"] == current["service"]
            or previous["chaos_type"] == current["chaos_type"]
        ):
            raise ValueError("adjacent runs must vary both service and fault")
    return cells


CELLS = build_cells()


def build_config(
    source: dict[str, Any], campaign_id: str, campaign_dir: Path
) -> dict[str, Any]:
    config = diverse.build_campaign_config(
        source, campaign_id=campaign_id, campaign_dir=campaign_dir
    )
    config["train_experiments"] = []
    config["test_experiments"] = [dict(cell) for cell in CELLS]
    config["replicas"] = 1
    config["platform"].update(
        {
            "initiated_by": "forty-faults",
            "poll_interval_seconds": 10,
            "run_timeout_seconds": 1800,
            "inter_run_cooldown_seconds": 60,
        }
    )
    config["resource_chaos"]["duration_seconds"] = FAULT_SECONDS
    selected = {cell["chaos_type"] for cell in CELLS}
    for entry in config["chaos_types"]:
        if entry["name"] in selected:
            entry.setdefault("env", {})["total_chaos_duration_seconds"] = FAULT_SECONDS
    config["timeline"].update(
        {
            "baseline_seconds": 60,
            "warmup_seconds": 30,
            "fault_duration_seconds": FAULT_SECONDS + 90,
            "post_recovery_observation_seconds": 120,
        }
    )
    config["ordering"].update({"shuffle": False, "start_jitter_max_seconds": 0})
    return config


def preflight(config: dict[str, Any]) -> list[str]:
    checks = diverse.preflight(config)
    existing = runner.existing_workflow_bases(config["cluster"]["litmus_namespace"])
    suffixes = {
        entry["name"]: entry["template_suffix"] for entry in config["chaos_types"]
    }
    missing = [
        f"{cell['service']}-{suffixes[cell['chaos_type']]}"
        for cell in config["test_experiments"]
        if cell["chaos_type"] not in runner.MANIFEST_CHAOS_TYPES
        and f"{cell['service']}-{suffixes[cell['chaos_type']]}" not in existing
    ]
    if missing:
        raise RuntimeError(
            "missing Litmus templates: "
            + ", ".join(missing)
            + "; register them separately before retrying. Automatic priming is "
            "disabled because applying templates would inject additional faults."
        )
    return checks + ["all required Litmus templates already registered"]


def call_online_rca(
    api_base: str, run_id: str, timeout_seconds: int = RCA_TIMEOUT_SECONDS
) -> dict[str, Any]:
    url = (
        f"{api_base.rstrip('/')}/api/runs/"
        f"{quote(run_id, safe='')}/rca"
    )
    request = Request(
        url,
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("platform RCA endpoint returned a non-object response")
    return result


def _answer_field(answer: str, label: str) -> str | None:
    expected = label.casefold()
    for line in answer.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().strip("*").casefold() == expected:
            return value.strip() or None
    return None


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _cause_matches_category(cause: str | None, category: str) -> bool | None:
    if not cause or not category:
        return None
    normalized_cause = _normalize_label(cause)
    aliases = (category, *_CAUSE_ALIASES.get(category, ()))
    return any(_normalize_label(alias) in normalized_cause for alias in aliases)


def _accuracy(results: list[dict[str, Any]], field: str) -> dict[str, Any]:
    evaluated = [result[field] for result in results if isinstance(result.get(field), bool)]
    correct = sum(value is True for value in evaluated)
    return {
        "correct": correct,
        "evaluated": len(evaluated),
        "accuracy": round(correct / len(evaluated), 4) if evaluated else None,
    }


def evaluate_rca_campaign(
    api_base: str,
    runs_csv: Path,
    cells: list[dict[str, str]],
    fault_categories: dict[str, str],
    campaign_id: str,
    timeout_seconds: int = RCA_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Call the platform RCA endpoint for each completed run and score its labels."""
    rows_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    if runs_csv.exists():
        with runs_csv.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = (
                    row.get("service", ""),
                    row.get("chaos_type", ""),
                    str(row.get("replica_idx") or "0"),
                )
                rows_by_key[key] = row

    results: list[dict[str, Any]] = []
    for order_index, cell in enumerate(cells, start=1):
        service = cell["service"]
        chaos_type = cell["chaos_type"]
        category = fault_categories.get(chaos_type, "")
        row = rows_by_key.get((service, chaos_type, "0"))
        result: dict[str, Any] = {
            "order": order_index,
            "service": service,
            "chaos_type": chaos_type,
            "ground_truth_service": service,
            "ground_truth_fault_category": category,
            "phase": (row or {}).get("phase", "test"),
            "run_id": (row or {}).get("run_id", ""),
            "run_status": (row or {}).get("status", "missing"),
        }
        if row is None or row.get("phase") != "test":
            result.update(
                {
                    "rca_status": "skipped_missing_run",
                    "error": "no matching test run was recorded",
                }
            )
            results.append(result)
            continue
        if row.get("status") != "completed" or not row.get("run_id"):
            result.update(
                {
                    "rca_status": "skipped_incomplete_run",
                    "error": row.get("notes") or "run did not complete",
                }
            )
            results.append(result)
            continue

        try:
            response = call_online_rca(
                api_base, row["run_id"], timeout_seconds=timeout_seconds
            )
        except HTTPError as exc:
            result.update(
                {
                    "rca_status": "http_error",
                    "error": f"HTTP {exc.code}: {exc.reason}",
                }
            )
            results.append(result)
            continue
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            result.update(
                {
                    "rca_status": "request_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            results.append(result)
            continue

        answer = response.get("answer")
        answer_text = answer if isinstance(answer, str) else ""
        evidence_bundle = response.get("evidence_bundle")
        if not isinstance(evidence_bundle, dict):
            evidence_bundle = {}
        localized_service = evidence_bundle.get("localized_service") or _answer_field(
            answer_text, "Localização provável"
        )
        cause = _answer_field(answer_text, "Causa provável")
        coverage = evidence_bundle.get("coverage")
        if not isinstance(coverage, dict):
            coverage = {}
        result.update(
            {
                "rca_status": "completed",
                "localized_service": localized_service,
                "localization_correct": (
                    str(localized_service).casefold() == service.casefold()
                    if localized_service
                    else None
                ),
                "cause": cause,
                "fault_category_correct": _cause_matches_category(cause, category),
                "answer": answer_text,
                "tools_used": response.get("tools_used", []),
                "iterations": response.get("iterations"),
                "stop_reason": response.get("stop_reason"),
                "artifact_path": response.get("artifact_path"),
                "evidence": {
                    "source_windows": evidence_bundle.get("source_windows", {}),
                    "coverage": {
                        tool: item.get("status", "unknown")
                        for tool, item in coverage.items()
                        if isinstance(item, dict)
                    },
                    "limitations": evidence_bundle.get("limitations", []),
                    "timeline_item_count": len(evidence_bundle.get("timeline") or []),
                    "evidence_id_count": len(evidence_bundle.get("evidence_ids") or []),
                },
            }
        )
        results.append(result)

    successful = [result for result in results if result["rca_status"] == "completed"]
    coverage_counts: dict[str, Counter[str]] = {}
    tool_counts: Counter[str] = Counter()
    for result in successful:
        tool_counts.update(
            tool for tool in result.get("tools_used", []) if isinstance(tool, str)
        )
        for tool, status in result.get("evidence", {}).get("coverage", {}).items():
            counts = coverage_counts.setdefault(tool, Counter())
            counts[status] += 1
    localization = _accuracy(results, "localization_correct")
    fault_classification = _accuracy(results, "fault_category_correct")
    both_evaluated = [
        result
        for result in results
        if isinstance(result.get("localization_correct"), bool)
        and isinstance(result.get("fault_category_correct"), bool)
    ]
    summary = {
        "requested_runs": len(cells),
        "run_records_found": sum(
            result["run_status"] != "missing" for result in results
        ),
        "completed_runs": sum(
            result["run_status"] == "completed" and bool(result["run_id"])
            for result in results
        ),
        "rca_attempted": sum(
            result["rca_status"]
            not in {"skipped_missing_run", "skipped_incomplete_run"}
            for result in results
        ),
        "rca_completed": len(successful),
        "service_localization": localization,
        "fault_classification": fault_classification,
        "both_correct": {
            "correct": sum(
                result["localization_correct"] is True
                and result["fault_category_correct"] is True
                for result in both_evaluated
            ),
            "evaluated": len(both_evaluated),
        },
        "tool_call_counts": dict(sorted(tool_counts.items())),
        "coverage_status_counts": {
            tool: dict(sorted(counts.items()))
            for tool, counts in sorted(coverage_counts.items())
        },
    }
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "summary": summary,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Execute; default only prints the plan."
    )
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    campaign_id = f"forty-faults-{stamp}"
    campaign_dir = CAMPAIGN_DIR / campaign_id
    source = yaml.safe_load(SOURCE_CONFIG.read_text(encoding="utf-8"))
    config = build_config(source, campaign_id, campaign_dir)
    lower, _ = diverse.duration_estimate(
        RUN_COUNT,
        timeline_seconds=sum(
            config["timeline"][key]
            for key in (
                "baseline_seconds",
                "warmup_seconds",
                "fault_duration_seconds",
                "post_recovery_observation_seconds",
            )
        ),
        cooldown_seconds=config["platform"]["inter_run_cooldown_seconds"],
        jitter_max_seconds=0,
        setup_seconds=0,
    )
    print(
        f"Campaign: {campaign_id}; {RUN_COUNT} unique sequential runs; "
        f"{FAULT_SECONDS}s per fault"
    )
    for order, cell in enumerate(CELLS, start=1):
        print(
            f"  {order:02d}. {cell['chaos_type']:<18} -> {cell['service']}"
        )
    print(
        f"Estimated wall time: about {(lower + 59) // 60} min; cluster variability applies"
    )
    print(
        "After the campaign, the script calls POST /api/runs/{run_id}/rca "
        "for each completed run."
    )
    if not args.run:
        print(
            "Plan only. Pass --run to execute; no files or cluster resources changed."
        )
        return 0

    try:
        diverse.wait_for_idle(config["platform"]["api_base"], wait=False)
        checks = preflight(config)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2
    print("Preflight passed: " + "; ".join(checks))
    campaign_dir.mkdir(parents=True, exist_ok=False)
    config_path = campaign_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    generate_chaos_manifests.generate(
        config, campaign_dir / config["output"]["manifests_dir"]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "experiment.eval.memory_hog.runner",
            "--config",
            str(config_path),
            "--phase",
            "chaos",
            "--skip-prime",
        ],
        cwd=ROOT,
        check=False,
    )
    effect_reports = targeted.validate_runs(
        campaign_dir / config["output"]["runs_csv"],
        cells=config["test_experiments"],
        replicas=1,
        phase="test",
    )
    effect_report_path = campaign_dir / "effect_validation.json"
    effect_report_path.write_text(
        json.dumps(effect_reports, indent=2), encoding="utf-8"
    )
    for report in effect_reports:
        print(json.dumps(report, ensure_ascii=False))
    fault_categories = {
        entry["name"]: entry.get("fault_category", "")
        for entry in config["chaos_types"]
    }
    rca_report = evaluate_rca_campaign(
        config["platform"]["api_base"],
        campaign_dir / config["output"]["runs_csv"],
        config["test_experiments"],
        fault_categories,
        campaign_id,
    )
    rca_report_path = campaign_dir / "rca_evaluation.json"
    rca_report_path.write_text(
        json.dumps(rca_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = rca_report["summary"]
    print(
        "Online RCA: "
        f"{summary['rca_completed']}/{summary['requested_runs']} completed; "
        f"service localization accuracy="
        f"{summary['service_localization']['accuracy']}; "
        f"fault classification accuracy="
        f"{summary['fault_classification']['accuracy']}"
    )
    print(f"Results: {campaign_dir}")
    if result.returncode != 0:
        return result.returncode
    reports_passed = bool(effect_reports) and all(
        report["passed"] for report in effect_reports
    )
    rca_passed = (
        summary["run_records_found"] == RUN_COUNT
        and summary["completed_runs"] == RUN_COUNT
        and summary["rca_completed"] == RUN_COUNT
    )
    return 0 if reports_passed and rca_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
