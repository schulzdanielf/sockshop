#!/usr/bin/env python3
"""Evaluate multiagent RCA against the ground truth of an existing campaign."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

from experiment.platform.backend.analysis.edge_latency import EDGE_LATENCY_QUERY_SPECS
from experiment.platform.backend.multiagent_rca import PROMPT_VERSION, STRATEGY_VERSION
from experiment.eval.memory_hog.rca_evaluation_common import (
    campaign_fault_types,
    compare_skill_and_multiagent,
    index_evaluation_runs,
    validate_paired_report_context,
    validate_shared_run_labels,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_API_BASE = "http://127.0.0.1:8010"
REQUEST_TIMEOUT_SECONDS = 1800
VALID_STATUSES = {"confirmed", "inconclusive"}


def _edge_latency_coverage(calls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    coverage = {
        spec["metric_name"]: {
            "status": "missing",
            "fault_observation_count": 0,
            "regressed_edges": 0,
        }
        for spec in EDGE_LATENCY_QUERY_SPECS
    }
    for call in calls:
        result = call.get("result") or {}
        if not isinstance(result, dict):
            continue
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            digest = call.get("digest") or {}
            metrics = digest.get("metrics") if isinstance(digest, dict) else None
        if not isinstance(metrics, dict):
            continue
        for metric_name, metric in metrics.items():
            if metric_name not in coverage or not isinstance(metric, dict):
                continue
            coverage[metric_name] = {
                "status": metric.get("status", call.get("status", "unknown")),
                "fault_observation_count": int(
                    call.get("fault_observation_count") or 0
                ),
                "regressed_edges": sum(
                    isinstance(edge, dict) and edge.get("direction") == "increase"
                    for edge in metric.get("top_delta") or []
                ),
            }
    return coverage


def score_diagnosis(
    response: Optional[Dict[str, Any]],
    expected_service: str,
    expected_fault_type: str | None = None,
) -> Dict[str, Any]:
    diagnosis = (response or {}).get("diagnosis") or {}
    predicted_service = diagnosis.get("primary_service")
    predicted_fault_type = diagnosis.get("fault_type")
    return {
        "predicted_service": predicted_service,
        "service_correct": predicted_service == expected_service,
        "predicted_fault_type": predicted_fault_type,
        "fault_type_correct": (
            predicted_fault_type == expected_fault_type
            if expected_fault_type is not None
            else None
        ),
    }


def assess_run_quality(
    response: Optional[Dict[str, Any]],
    expected_service: str,
    *,
    effect_validated: Optional[bool],
    expected_fault_type: str | None = None,
    possible_fault_types: list[str] | None = None,
) -> Dict[str, Any]:
    result = response or {}
    diagnosis = result.get("diagnosis") or {}
    scored = score_diagnosis(
        response, expected_service, expected_fault_type
    )
    scope = result.get("investigation_scope") or {}
    candidates = scope.get("metric_candidates") or []
    scoped_services = scope.get("services") or []
    scope_snapshots = {
        tuple(sorted(item.get("services") or [])) for item in scope.get("history") or []
    }
    if not scope_snapshots and scoped_services:
        scope_snapshots.add(tuple(sorted(scoped_services)))
    evidence = result.get("evidence") or []
    calls_by_evidence = {
        item.get("evidence_id"): item
        for item in result.get("calls") or []
        if item.get("evidence_id")
    }
    observed_fault_evidence = [
        item
        for item in evidence
        if item.get("status") in {"ok", "partial"}
        and calls_by_evidence.get(item.get("evidence_id"), {}).get(
            "fault_observation_count", 0
        )
        > 0
    ]
    complete_fault_evidence = [
        item for item in observed_fault_evidence if item.get("status") == "ok"
    ]
    family_coverage: Dict[str, int] = {}
    partial_family_coverage: Dict[str, int] = {}
    complete_family_coverage: Dict[str, int] = {}
    for item in observed_fault_evidence:
        family = item.get("family")
        if isinstance(family, str):
            family_coverage[family] = family_coverage.get(family, 0) + 1
            if item.get("status") == "partial":
                partial_family_coverage[family] = (
                    partial_family_coverage.get(family, 0) + 1
                )
    for item in complete_fault_evidence:
        family = item.get("family")
        if isinstance(family, str):
            complete_family_coverage[family] = (
                complete_family_coverage.get(family, 0) + 1
            )

    calls = result.get("calls") or []
    edge_latency_coverage = _edge_latency_coverage(calls)
    scope_violations = 0
    unverified_scope_calls = 0
    for call in calls:
        if call.get("family") not in {"logs", "traces"}:
            continue
        authorized = call.get("scope_services")
        service = call.get("service")
        if authorized is None:
            unverified_scope_calls += 1
            continue
        authorized_set = set(authorized)
        appears_in_authorized_snapshot = any(
            authorized_set.issubset(set(snapshot)) for snapshot in scope_snapshots
        )
        if (
            (service is not None and service not in authorized_set)
            or not authorized_set
            or len(authorized_set) > 4
            or not appears_in_authorized_snapshot
        ):
            scope_violations += 1

    model_attempts = result.get("model_attempts") or []
    specialists = result.get("specialists") or []
    budget = result.get("budget") or {}
    supported_ids = set(diagnosis.get("supporting_evidence") or [])
    evidence_by_id = {
        item.get("evidence_id"): item
        for item in complete_fault_evidence
        if item.get("evidence_id")
    }
    supporting_families = {
        evidence_by_id[reference].get("family")
        for reference in supported_ids
        if reference in evidence_by_id
    }
    valid_response = result.get("status") in VALID_STATUSES
    return {
        **scored,
        "category": diagnosis.get("category"),
        "fault_type_in_options": (
            scored["predicted_fault_type"] in possible_fault_types
            if possible_fault_types
            else None
        ),
        "valid_response": valid_response,
        "effect_validated": effect_validated,
        "target_in_metric_candidates": (
            expected_service in candidates if valid_response else None
        ),
        "target_in_investigation_scope": (
            expected_service in scoped_services if valid_response else None
        ),
        "scope_revision_count": len(scope.get("revisions") or []),
        "scope_violations": scope_violations,
        "unverified_scope_calls": unverified_scope_calls,
        "fault_evidence_by_family": family_coverage,
        "partial_fault_evidence_by_family": partial_family_coverage,
        "complete_fault_evidence_by_family": complete_family_coverage,
        "query_statuses_by_family": {
            family: (details.get("statuses") or {})
            for family, details in (
                (result.get("telemetry_coverage") or {}).get("families") or {}
            ).items()
        },
        "trace_search_probes": (
            (result.get("telemetry_coverage") or {}).get("trace_search_probes")
            or {}
        ),
        "edge_latency_coverage": edge_latency_coverage,
        "missing_fault_families": (
            (result.get("telemetry_coverage") or {}).get("missing_fault_families", [])
        ),
        "repair_calls": int(
            budget.get("repair_calls_used")
            or sum(int(attempt.get("repair_count") or 0) for attempt in model_attempts)
        ),
        "model_attempt_failures": sum(
            attempt.get("status") == "error" for attempt in model_attempts
        ),
        "coordinator_failures": sum(
            attempt.get("status") == "error"
            and str(attempt.get("stage", "")).startswith("coordinator_")
            for attempt in model_attempts
        ),
        "validation_failures": int(budget.get("validation_failures") or 0),
        "specialist_failures": sum(
            report.get("status") == "error" for report in specialists
        ),
        "model_calls": budget.get("model_calls_used"),
        "tool_calls": budget.get("tool_calls_used"),
        "tokens": budget.get("tokens_used"),
        "elapsed_seconds": budget.get("elapsed_seconds"),
        "confirmed_incorrect": (
            diagnosis.get("status") == "confirmed"
            and (
                not scored["service_correct"]
                or (
                    isinstance(scored["fault_type_correct"], bool)
                    and not scored["fault_type_correct"]
                )
            )
        ),
        "confirmed_without_independent_family_support": (
            diagnosis.get("status") == "confirmed" and len(supporting_families) < 2
        ),
    }


def compare_evaluations(
    current: Dict[str, Any], baseline: Dict[str, Any]
) -> Dict[str, Any]:
    campaign_id, possible_fault_types = validate_paired_report_context(
        current, baseline
    )
    current_runs = index_evaluation_runs(current, "per_run")
    baseline_runs = index_evaluation_runs(baseline, "per_run")
    common = sorted(set(current_runs) & set(baseline_runs))
    if not common:
        raise ValueError("evaluation reports have no completed run IDs in common")
    for run_id in common:
        validate_shared_run_labels(
            run_id,
            current_runs[run_id],
            baseline_runs[run_id],
            left_service_key="expected_service",
            right_service_key="expected_service",
            possible_fault_types=possible_fault_types,
        )
    deltas: Dict[str, Any] = {}
    binary_metrics = {
        "service_correct": True,
        "fault_type_correct": True,
        "target_in_metric_candidates": True,
        "target_in_investigation_scope": True,
        "confirmed_incorrect": False,
        "confirmed_without_independent_family_support": False,
        "fault_metrics_covered": True,
        "fault_logs_covered": True,
        "fault_traces_covered": True,
    }

    def metric_value(run: Dict[str, Any], key: str) -> Any:
        if key.startswith("fault_") and key.endswith("_covered"):
            family = key.removeprefix("fault_").removesuffix("_covered")
            coverage = run.get("fault_evidence_by_family")
            if not isinstance(coverage, dict):
                return None
            return bool(coverage.get(family, 0))
        return run.get(key)

    per_run: list[dict[str, Any]] = []
    for key, higher_is_better in binary_metrics.items():
        improved = sum(
            metric_value(current_runs[run_id], key) is higher_is_better
            and metric_value(baseline_runs[run_id], key) is (not higher_is_better)
            for run_id in common
        )
        regressed = sum(
            metric_value(baseline_runs[run_id], key) is higher_is_better
            and metric_value(current_runs[run_id], key) is (not higher_is_better)
            for run_id in common
        )
        paired_count = sum(
            isinstance(metric_value(current_runs[run_id], key), bool)
            and isinstance(metric_value(baseline_runs[run_id], key), bool)
            for run_id in common
        )
        deltas[key] = {
            "improved": improved,
            "regressed": regressed,
            "net_change": improved - regressed,
            "paired_observations": paired_count,
        }
    count_metrics = (
        "scope_violations",
        "scope_revision_count",
        "unverified_scope_calls",
        "repair_calls",
        "validation_failures",
        "specialist_failures",
        "model_attempt_failures",
        "coordinator_failures",
    )
    count_deltas: Dict[str, Any] = {}
    for key in count_metrics:
        observed = [
            (baseline_runs[run_id].get(key), current_runs[run_id].get(key))
            for run_id in common
            if isinstance(baseline_runs[run_id].get(key), int)
            and not isinstance(baseline_runs[run_id].get(key), bool)
            and isinstance(current_runs[run_id].get(key), int)
            and not isinstance(current_runs[run_id].get(key), bool)
        ]
        count_deltas[key] = {
            "paired_observations": len(observed),
            "baseline_total": sum(pair[0] for pair in observed),
            "current_total": sum(pair[1] for pair in observed),
            "net_change": sum(pair[1] - pair[0] for pair in observed),
        }
    for run_id in common:
        per_run.append(
            {
                "run_id": run_id,
                **{
                    key: {
                        "baseline": metric_value(baseline_runs[run_id], key),
                        "current": metric_value(current_runs[run_id], key),
                    }
                    for key in binary_metrics
                },
                **{
                    key: {
                        "baseline": baseline_runs[run_id].get(key),
                        "current": current_runs[run_id].get(key),
                    }
                    for key in count_metrics
                },
            }
        )
    return {
        "campaign_id": campaign_id,
        "possible_fault_types": possible_fault_types,
        "baseline_strategy_version": baseline.get("strategy_version"),
        "current_strategy_version": current.get("strategy_version"),
        "paired_run_count": len(common),
        "deltas": deltas,
        "count_deltas": count_deltas,
        "per_run": per_run,
    }


def _get_strategy_info(*, api_base: str, timeout_seconds: int) -> Dict[str, Any]:
    request = Request(
        f"{api_base.rstrip('/')}/api/multiagent-rca/strategy",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            strategy = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot verify loaded multiagent RCA strategy: {exc}"
        ) from exc
    if (
        not isinstance(strategy, dict)
        or not isinstance(strategy.get("strategy_version"), str)
        or not isinstance(strategy.get("prompt_version"), str)
    ):
        raise RuntimeError("Strategy preflight returned an invalid version response")
    return strategy


def request_multiagent_rca(
    run_id: str,
    *,
    api_base: str = DEFAULT_API_BASE,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    expected_strategy_version: str = STRATEGY_VERSION,
    expected_prompt_version: str = PROMPT_VERSION,
    possible_fault_types: list[str] | None = None,
) -> Dict[str, Any]:
    request = Request(
        f"{api_base.rstrip('/')}/api/runs/{quote(run_id, safe='')}/multiagent-rca",
        data=json.dumps(
            {"possible_fault_types": possible_fault_types or []}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            result = json.load(response)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body
        return {"status": "error", "http_status": exc.code, "error": detail}
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": str(exc)}

    if (
        not isinstance(result, dict)
        or result.get("status") not in VALID_STATUSES
        or not isinstance(result.get("diagnosis"), dict)
    ):
        return {
            "status": "error",
            "error": "multiagent endpoint returned an invalid diagnosis response",
            "response": result,
        }
    if (
        result.get("strategy_version") != expected_strategy_version
        or result.get("prompt_version") != expected_prompt_version
    ):
        return {
            "status": "error",
            "error": "multiagent endpoint response strategy or prompt version did not match the verified version",
            "expected_strategy_version": expected_strategy_version,
            "expected_prompt_version": expected_prompt_version,
            "response_strategy_version": result.get("strategy_version"),
            "response_prompt_version": result.get("prompt_version"),
        }
    if (
        possible_fault_types
        and result["diagnosis"].get("fault_type") not in possible_fault_types
    ):
        return {
            "status": "error",
            "error": (
                "fault_type must exactly match one of: "
                + ", ".join(possible_fault_types)
            ),
            "response": result,
        }
    return result


def _read_campaign(campaign_dir: Path) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    config_path = campaign_dir / "config.yaml"
    runs_path = campaign_dir / "runs.csv"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    with runs_path.open("r", encoding="utf-8", newline="") as handle:
        runs = list(csv.DictReader(handle))
    return config, runs


def _markdown_cell(value: Any) -> str:
    return (
        str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")
    )


def _read_effect_reports(campaign_dir: Path) -> Dict[str, Dict[str, Any]]:
    path = campaign_dir / "effect_validation.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("runs", [])
    return {
        row["run_id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("run_id"), str)
    }


def _rate(count: int, denominator: int) -> Optional[float]:
    return round(count / denominator, 4) if denominator else None


def _summarize_runs(per_run: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(per_run)
    valid = [item for item in per_run if item["valid_response"]]
    effect_validated = [item for item in per_run if item["effect_validated"] is True]
    edge_latency_fault_coverage = {
        spec["metric_name"]: sum(
            item.get("edge_latency_coverage", {})
            .get(spec["metric_name"], {})
            .get("fault_observation_count", 0)
            > 0
            for item in per_run
        )
        for spec in EDGE_LATENCY_QUERY_SPECS
    }
    edge_latency_regression_runs = {
        spec["metric_name"]: sum(
            item.get("edge_latency_coverage", {})
            .get(spec["metric_name"], {})
            .get("regressed_edges", 0)
            > 0
            for item in per_run
        )
        for spec in EDGE_LATENCY_QUERY_SPECS
    }
    return {
        "expected_runs": total,
        "valid_multiagent_responses": len(valid),
        "errors_or_missing_runs": total - len(valid),
        "confirmed": sum(item["status"] == "confirmed" for item in per_run),
        "inconclusive": sum(item["status"] == "inconclusive" for item in per_run),
        "services_correct": sum(item["service_correct"] for item in per_run),
        "services_accuracy": _rate(
            sum(item["service_correct"] for item in per_run),
            total,
        ),
        "fault_types_correct": sum(
            item.get("fault_type_correct") is True for item in per_run
        ),
        "fault_types_accuracy": _rate(
            sum(item.get("fault_type_correct") is True for item in per_run),
            total,
        ),
        "fault_type_option_compliance_correct": sum(
            item.get("fault_type_in_options") is True for item in per_run
        ),
        "fault_type_option_compliance_accuracy": _rate(
            sum(item.get("fault_type_in_options") is True for item in per_run),
            total,
        ),
        "target_in_metric_candidates": sum(
            item["target_in_metric_candidates"] is True for item in valid
        ),
        "metric_candidate_recall": _rate(
            sum(item["target_in_metric_candidates"] is True for item in per_run),
            total,
        ),
        "valid_response_metric_candidate_recall": _rate(
            sum(item["target_in_metric_candidates"] is True for item in valid),
            len(valid),
        ),
        "target_in_investigation_scope": sum(
            item["target_in_investigation_scope"] is True for item in per_run
        ),
        "scope_recall": _rate(
            sum(item["target_in_investigation_scope"] is True for item in per_run),
            total,
        ),
        "valid_response_scope_recall": _rate(
            sum(item["target_in_investigation_scope"] is True for item in valid),
            len(valid),
        ),
        "effect_validated_runs": len(effect_validated),
        "effect_validated_service_accuracy": _rate(
            sum(item["service_correct"] for item in effect_validated),
            len(effect_validated),
        ),
        "effect_validated_fault_type_accuracy": _rate(
            sum(item.get("fault_type_correct") is True for item in effect_validated),
            len(effect_validated),
        ),
        "scope_violations": sum(item["scope_violations"] for item in valid),
        "unverified_scope_calls": sum(item["unverified_scope_calls"] for item in valid),
        "scope_revisions": sum(item["scope_revision_count"] for item in valid),
        "specialist_failures": sum(item["specialist_failures"] for item in valid),
        "model_attempt_failures": sum(item["model_attempt_failures"] for item in valid),
        "coordinator_failures": sum(item["coordinator_failures"] for item in valid),
        "repair_calls": sum(item["repair_calls"] for item in valid),
        "validation_failures": sum(item["validation_failures"] for item in valid),
        "confirmed_incorrect": sum(item["confirmed_incorrect"] for item in valid),
        "confirmed_without_independent_family_support": sum(
            item["confirmed_without_independent_family_support"] for item in valid
        ),
        "fault_family_coverage": {
            family: sum(
                item["fault_evidence_by_family"].get(family, 0) > 0 for item in valid
            )
            for family in ("metrics", "logs", "traces")
        },
        "partial_fault_family_runs": {
            family: sum(
                item.get("partial_fault_evidence_by_family", {}).get(family, 0) > 0
                for item in valid
            )
            for family in ("metrics", "logs", "traces")
        },
        "trace_search_probes": {
            key: sum(
                int(item.get("trace_search_probes", {}).get(key, 0) or 0)
                for item in valid
            )
            for key in ("attempted", "traces_present", "empty_windows", "tool_errors")
        },
        "edge_latency_fault_coverage_runs": edge_latency_fault_coverage,
        "edge_latency_regression_runs": edge_latency_regression_runs,
        "mean_model_calls": (
            round(sum(item["model_calls"] or 0 for item in valid) / len(valid), 2)
            if valid
            else None
        ),
        "mean_tool_calls": (
            round(sum(item["tool_calls"] or 0 for item in valid) / len(valid), 2)
            if valid
            else None
        ),
        "mean_tokens": (
            round(sum(item["tokens"] or 0 for item in valid) / len(valid), 2)
            if valid
            else None
        ),
        "mean_elapsed_seconds": (
            round(sum(item["elapsed_seconds"] or 0 for item in valid) / len(valid), 2)
            if valid
            else None
        ),
    }


def evaluate_campaign(
    campaign_dir: Path,
    *,
    api_base: str = DEFAULT_API_BASE,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    expected_strategy_version: str = STRATEGY_VERSION,
    expected_prompt_version: str = PROMPT_VERSION,
    compare_evaluation_path: Optional[Path] = None,
    compare_skill_evaluation_path: Optional[Path] = None,
) -> Tuple[Path, Path]:
    config, run_rows = _read_campaign(campaign_dir)
    loaded_strategy = _get_strategy_info(
        api_base=api_base,
        timeout_seconds=min(timeout_seconds, 10),
    )
    if (
        loaded_strategy["strategy_version"] != expected_strategy_version
        or loaded_strategy["prompt_version"] != expected_prompt_version
    ):
        raise RuntimeError(
            "Loaded multiagent strategy does not match the expected version: "
            f"loaded={loaded_strategy}, expected={{'strategy_version': "
            f"{expected_strategy_version!r}, 'prompt_version': {expected_prompt_version!r}}}"
        )
    expected_cells = config.get("test_experiments") or []
    possible_fault_types = campaign_fault_types(config, run_rows)
    test_rows = [row for row in run_rows if row.get("phase") == "test"]
    effect_reports = _read_effect_reports(campaign_dir)
    rows_by_cell: Dict[Tuple[str, str, int], Dict[str, str]] = {}
    for row in test_rows:
        key = (row.get("chaos_type", ""), row.get("service", ""), int(row.get("replica_idx") or 0))
        if key in rows_by_cell:
            raise ValueError(f"duplicate test ledger entry for {key}")
        rows_by_cell[key] = row

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = campaign_dir / "multiagent_rca" / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    per_run: List[Dict[str, Any]] = []

    for cell in expected_cells:
        chaos_type = cell["chaos_type"]
        service = cell["service"]
        replica_idx = int(cell.get("replica_idx", 0))
        row = rows_by_cell.get((chaos_type, service, replica_idx))
        run_id = row.get("run_id", "") if row else ""
        effect_report = effect_reports.get(run_id, {})
        effect_validated = (
            bool(effect_report["passed"])
            if isinstance(effect_report.get("passed"), bool)
            else None
        )
        if not row or row.get("status") != "completed" or not run_id:
            result: Dict[str, Any] = {
                "status": "not_run",
                "error": "campaign run was missing or did not complete",
            }
        else:
            print(f"Multiagent RCA: {run_id}", flush=True)
            result = request_multiagent_rca(
                run_id,
                api_base=api_base,
                timeout_seconds=timeout_seconds,
                expected_strategy_version=expected_strategy_version,
                expected_prompt_version=expected_prompt_version,
                possible_fault_types=possible_fault_types,
            )
            response_path = output_dir / f"{run_id}.json"
            response_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n",
                encoding="utf-8",
            )

        quality = assess_run_quality(
            result if result.get("status") in VALID_STATUSES else None,
            service,
            effect_validated=effect_validated,
            expected_fault_type=chaos_type,
            possible_fault_types=possible_fault_types,
        )
        per_run.append(
            {
                "run_id": run_id or None,
                "chaos_type": chaos_type,
                "replica_idx": replica_idx,
                "status": result.get("status"),
                "error": result.get("error"),
                "expected_service": service,
                **quality,
            }
        )

    summary = _summarize_runs(per_run)
    report = {
        "campaign_id": config.get("experiment_campaign_id"),
        "method": "multiagent_rca",
        "strategy_version": loaded_strategy["strategy_version"],
        "prompt_version": loaded_strategy["prompt_version"],
        "possible_fault_types": possible_fault_types,
        "expected_runs": summary["expected_runs"],
        "completed_campaign_runs": sum(
            item["status"] in VALID_STATUSES or item["status"] == "error"
            for item in per_run
        ),
        **summary,
        "services_denominator": len(per_run),
        "per_run": per_run,
        "cause_scoring": (
            "Exact match of diagnosis.fault_type against the campaign chaos_type; "
            "missing/invalid responses are incorrect."
        ),
    }
    if compare_evaluation_path is not None:
        baseline = json.loads(compare_evaluation_path.read_text(encoding="utf-8"))
        report["paired_comparison"] = compare_evaluations(report, baseline)
    if compare_skill_evaluation_path is not None:
        skill_report = json.loads(
            compare_skill_evaluation_path.read_text(encoding="utf-8")
        )
        report["skill_vs_multiagent_comparison"] = compare_skill_and_multiagent(
            skill_report, report
        )
    json_path = output_dir / "evaluation.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Avaliação de RCA multiagente",
        "",
        f"- Campanha: `{report['campaign_id']}`",
        f"- Estratégia/prompt: `{report['strategy_version']}` / `{report['prompt_version']}`",
        f"- Runs avaliados: {summary['expected_runs']}",
        f"- Respostas válidas: {summary['valid_multiagent_responses']}/{summary['expected_runs']}",
        f"- Confirmados: {summary['confirmed']}; inconclusivos: {summary['inconclusive']}; erros/ausentes: {summary['errors_or_missing_runs']}",
        f"- Serviço correto: **{summary['services_correct']}/{summary['expected_runs']}**; recall do alvo no ranking métrico: **{summary['metric_candidate_recall']}** (condicional a resposta válida: {summary['valid_response_metric_candidate_recall']})",
        f"- Tipo de falha exato: **{summary['fault_types_correct']}/{summary['expected_runs']}**; opções apresentadas: `{', '.join(possible_fault_types)}`",
        f"- Saída dentro das opções: **{summary['fault_type_option_compliance_correct']}/{summary['expected_runs']}**",
        f"- Recall do alvo no escopo: **{summary['scope_recall']}**; violações: **{summary['scope_violations']}**; revisões: **{summary['scope_revisions']}**",
        f"- Chamadas de logs/traces sem snapshot de autorização verificável: **{summary['unverified_scope_calls']}**",
        f"- Runs com efeito validado: **{summary['effect_validated_runs']}**; acurácia serviço/tipo de falha: **{summary['effect_validated_service_accuracy']} / {summary['effect_validated_fault_type_accuracy']}**",
        f"- Cobertura de fault por família: `{json.dumps(summary['fault_family_coverage'], ensure_ascii=False)}`",
        f"- Cobertura fault por perfil de latência de aresta: `{json.dumps(summary['edge_latency_fault_coverage_runs'], ensure_ascii=False)}`",
        f"- Runs com regressão por perfil de latência de aresta: `{json.dumps(summary['edge_latency_regression_runs'], ensure_ascii=False)}`",
        f"- Runs com evidência parcial em fault por família: `{json.dumps(summary['partial_fault_family_runs'], ensure_ascii=False)}`",
        f"- Sondagens Tempo: `{json.dumps(summary['trace_search_probes'], ensure_ascii=False)}`",
        f"- Reparos estruturais: {summary['repair_calls']}; falhas de validação: {summary['validation_failures']}; falhas do coordenador/especialistas: {summary['coordinator_failures']} / {summary['specialist_failures']}; tentativas de modelo com erro: {summary['model_attempt_failures']}",
        f"- Confirmações incorretas / sem suporte independente: {summary['confirmed_incorrect']} / {summary['confirmed_without_independent_family_support']}",
        "",
        "Runs sem resposta válida contam como incorretos nos placares globais. "
        "A acurácia do subconjunto com efeito validado é exibida separadamente.",
        "",
        "| Run | Causa | Estado | Serviço real | Serviço previsto | Serviço correto | Causa prevista | Causa correta | Candidato | No escopo | Efeito validado | Revisões | Erros de escopo |",
        "|---|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in per_run:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    item["run_id"],
                    item["chaos_type"],
                    item["status"],
                    item["expected_service"],
                    item["predicted_service"],
                    "sim" if item["service_correct"] else "não",
                    item["predicted_fault_type"],
                    "sim" if item["fault_type_correct"] else "não",
                    "sim" if item["target_in_metric_candidates"] else "não",
                    "sim" if item["target_in_investigation_scope"] else "não",
                    (
                        "sim"
                        if item["effect_validated"] is True
                        else ("não" if item["effect_validated"] is False else "?")
                    ),
                    item["scope_revision_count"],
                    item["scope_violations"],
                )
            )
            + " |"
        )
    if "skill_vs_multiagent_comparison" in report:
        comparison = report["skill_vs_multiagent_comparison"]
        lines.extend(
            [
                "",
                "## Comparação pareada: Skill RCA vs multiagentes",
                "",
                f"- Runs em comum: {comparison['paired_run_count']}; falhas/ausências contadas como incorretas.",
                f"- Opções de tipo de falha compartilhadas: `{', '.join(comparison['possible_fault_types'])}`",
                "| Métrica | Skill RCA | Multiagentes | Ganhos multiagentes | Ganhos Skill |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for metric, values in comparison["metrics"].items():
            lines.append(
                f"| {metric} | {values['skill_correct']}/{values['denominator']} "
                f"({values['skill_accuracy']}) | {values['multiagent_correct']}/"
                f"{values['denominator']} ({values['multiagent_accuracy']}) | "
                f"{values['multiagent_only_correct']} | {values['skill_only_correct']} |"
            )
        lines.append("")
        lines.append(
            "Acurácia do tipo por falha: `"
            + json.dumps(comparison["fault_type_accuracy_by_cause"], ensure_ascii=False)
            + "`"
        )
    if "paired_comparison" in report:
        comparison = report["paired_comparison"]
        lines.extend(
            [
                "",
                "## Comparação pareada",
                "",
                f"- Runs em comum: {comparison['paired_run_count']}",
            ]
        )
        for metric, counts in comparison["deltas"].items():
            lines.append(
                f"- {metric}: melhoraram {counts['improved']}, pioraram "
                f"{counts['regressed']}, variação líquida {counts['net_change']}"
            )
        for metric, counts in comparison["count_deltas"].items():
            lines.append(
                f"- {metric}: total baseline {counts['baseline_total']}, "
                f"atual {counts['current_total']}, variação líquida "
                f"{counts['net_change']} ({counts['paired_observations']} pares)"
            )
    markdown_path = output_dir / "evaluation.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument(
        "--compare-evaluation",
        type=Path,
        help="Optional previous evaluation.json to compare on shared run IDs.",
    )
    parser.add_argument(
        "--compare-skill-evaluation",
        type=Path,
        help="Optional Skill RCA evaluation JSON for a paired method comparison.",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    campaign_dir = args.campaign_dir
    if not campaign_dir.is_absolute():
        campaign_dir = ROOT / campaign_dir
    compare_path = args.compare_evaluation
    if compare_path is not None and not compare_path.is_absolute():
        compare_path = ROOT / compare_path
    compare_skill_path = args.compare_skill_evaluation
    if compare_skill_path is not None and not compare_skill_path.is_absolute():
        compare_skill_path = ROOT / compare_skill_path
    markdown_path, json_path = evaluate_campaign(
        campaign_dir,
        api_base=args.api_base,
        timeout_seconds=args.timeout,
        compare_evaluation_path=compare_path,
        compare_skill_evaluation_path=compare_skill_path,
    )
    print(f"Relatório: {markdown_path.relative_to(ROOT)}")
    print(f"Dados: {json_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
