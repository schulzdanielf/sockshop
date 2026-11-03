from __future__ import annotations

from typing import Any

SCORED_METRICS = (
    "service_correct",
    "fault_type_correct",
)


def campaign_fault_types(
    config: dict[str, Any], run_rows: list[dict[str, Any]]
) -> list[str]:
    declared_options = config.get("possible_fault_types")
    if declared_options is not None:
        if (
            not isinstance(declared_options, list)
            or not declared_options
            or any(not isinstance(item, str) or not item for item in declared_options)
            or len(declared_options) != len(set(declared_options))
        ):
            raise ValueError("possible_fault_types must be a non-empty list of unique strings")
        matrix_labels = {
            cell.get("chaos_type")
            for cell in config.get("test_experiments") or []
            if isinstance(cell, dict) and isinstance(cell.get("chaos_type"), str)
        }
        if not matrix_labels.issubset(declared_options):
            raise ValueError("test matrix contains a fault type outside possible_fault_types")
        return sorted(declared_options)

    cells = config.get("test_experiments") or []
    labels = {
        cell.get("chaos_type")
        for cell in cells
        if isinstance(cell, dict) and isinstance(cell.get("chaos_type"), str)
    }
    if not labels:
        labels = {
            row.get("chaos_type")
            for row in run_rows
            if row.get("phase") == "test" and isinstance(row.get("chaos_type"), str)
        }
    if not labels:
        raise ValueError("campaign contains no test fault types")
    return sorted(labels)


def validate_paired_report_context(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[str, list[str]]:
    campaign_ids = (left.get("campaign_id"), right.get("campaign_id"))
    if any(not isinstance(value, str) or not value.strip() for value in campaign_ids):
        raise ValueError("both evaluation reports must record a campaign_id")
    if campaign_ids[0] != campaign_ids[1]:
        raise ValueError("evaluation reports belong to different campaigns")

    def options_for(report: dict[str, Any]) -> set[str]:
        options = report.get("possible_fault_types")
        if (
            not isinstance(options, list)
            or not options
            or any(not isinstance(item, str) or not item for item in options)
            or len(options) != len(set(options))
        ):
            raise ValueError(
                "both reports must record a non-empty, unique possible_fault_types list"
            )
        return set(options)

    left_options = options_for(left)
    right_options = options_for(right)
    if left_options != right_options:
        raise ValueError("evaluation reports used different fault options")
    return campaign_ids[0], sorted(left_options)


def index_evaluation_runs(
    report: dict[str, Any], key: str
) -> dict[str, dict[str, Any]]:
    rows = report.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"evaluation report has no {key} list")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"evaluation report contains an invalid {key} entry")
        run_id = row.get("run_id")
        if run_id is None:
            continue
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"evaluation report contains an invalid run ID: {run_id!r}")
        if run_id in indexed:
            raise ValueError(f"duplicate run ID in evaluation report: {run_id}")
        indexed[run_id] = row
    return indexed


def validate_shared_run_labels(
    run_id: str,
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_service_key: str,
    right_service_key: str,
    possible_fault_types: list[str],
) -> tuple[str, str]:
    left_fault_type = left.get("chaos_type")
    right_fault_type = right.get("chaos_type")
    if (
        not isinstance(left_fault_type, str)
        or not left_fault_type
        or not isinstance(right_fault_type, str)
        or not right_fault_type
    ):
        raise ValueError(f"run {run_id} is missing its fault-type ground truth")
    if left_fault_type != right_fault_type:
        raise ValueError(f"run {run_id} has inconsistent chaos_type labels")
    if left_fault_type not in possible_fault_types:
        raise ValueError(f"run {run_id} has a fault type outside the campaign options")

    left_service = left.get(left_service_key)
    right_service = right.get(right_service_key)
    if (
        not isinstance(left_service, str)
        or not left_service
        or not isinstance(right_service, str)
        or not right_service
    ):
        raise ValueError(f"run {run_id} is missing its service ground truth")
    if left_service != right_service:
        raise ValueError(f"run {run_id} has inconsistent service labels")
    return left_fault_type, left_service


def compare_skill_and_multiagent(
    skill_report: dict[str, Any], multiagent_report: dict[str, Any]
) -> dict[str, Any]:
    campaign_id, skill_options = validate_paired_report_context(
        skill_report, multiagent_report
    )
    skill_runs = index_evaluation_runs(skill_report, "results")
    multiagent_runs = index_evaluation_runs(multiagent_report, "per_run")
    common_ids = sorted(set(skill_runs) & set(multiagent_runs))
    if not common_ids:
        raise ValueError("evaluation reports have no completed run IDs in common")

    per_run: list[dict[str, Any]] = []
    for run_id in common_ids:
        skill = skill_runs[run_id]
        multiagent = multiagent_runs[run_id]
        chaos_type, expected_service = validate_shared_run_labels(
            run_id,
            skill,
            multiagent,
            left_service_key="ground_truth_service",
            right_service_key="expected_service",
            possible_fault_types=skill_options,
        )
        skill_service = skill.get("localization_correct", skill.get("service_correct"))
        multiagent_service = multiagent.get("service_correct")
        skill_fault_type = skill.get("fault_type_correct")
        multiagent_fault_type = multiagent.get("fault_type_correct")
        per_run.append(
            {
                "run_id": run_id,
                "chaos_type": chaos_type,
                "expected_service": expected_service,
                "skill_status": skill.get("status"),
                "multiagent_status": multiagent.get("status"),
                "skill_fault_type": skill.get("fault_type"),
                "multiagent_fault_type": multiagent.get("predicted_fault_type"),
                "skill_fault_type_correct": skill_fault_type is True,
                "multiagent_fault_type_correct": multiagent_fault_type is True,
                "skill_service_correct": skill_service is True,
                "multiagent_service_correct": multiagent_service is True,
                "skill_effect_validated": skill.get("effect_validated"),
                "multiagent_effect_validated": multiagent.get("effect_validated"),
            }
        )

    metrics: dict[str, Any] = {}
    for metric in SCORED_METRICS:
        skill_values = [row[f"skill_{metric}"] for row in per_run]
        multiagent_values = [row[f"multiagent_{metric}"] for row in per_run]
        skill_correct = sum(skill_values)
        multiagent_correct = sum(multiagent_values)
        skill_wins = sum(
            skill_value and not multiagent_value
            for skill_value, multiagent_value in zip(skill_values, multiagent_values)
        )
        multiagent_wins = sum(
            multiagent_value and not skill_value
            for skill_value, multiagent_value in zip(skill_values, multiagent_values)
        )
        metrics[metric] = {
            "denominator": len(per_run),
            "skill_correct": skill_correct,
            "skill_accuracy": round(skill_correct / len(per_run), 4),
            "multiagent_correct": multiagent_correct,
            "multiagent_accuracy": round(multiagent_correct / len(per_run), 4),
            "skill_only_correct": skill_wins,
            "multiagent_only_correct": multiagent_wins,
            "net_change_for_multiagent": multiagent_wins - skill_wins,
        }

    by_fault_type: dict[str, Any] = {}
    for fault_type in skill_options:
        rows = [row for row in per_run if row["chaos_type"] == fault_type]
        if not rows:
            continue
        skill_correct = sum(row["skill_fault_type_correct"] for row in rows)
        multiagent_correct = sum(row["multiagent_fault_type_correct"] for row in rows)
        by_fault_type[fault_type] = {
            "denominator": len(rows),
            "skill_correct": skill_correct,
            "skill_accuracy": round(skill_correct / len(rows), 4),
            "multiagent_correct": multiagent_correct,
            "multiagent_accuracy": round(multiagent_correct / len(rows), 4),
        }
    return {
        "campaign_id": campaign_id,
        "possible_fault_types": skill_options,
        "paired_run_count": len(per_run),
        "skill_only_run_count": len(set(skill_runs) - set(common_ids)),
        "multiagent_only_run_count": len(set(multiagent_runs) - set(common_ids)),
        "failed_or_invalid": {
            "skill": sum(
                row["skill_status"] not in {"confirmed", "inconclusive"}
                for row in per_run
            ),
            "multiagent": sum(
                row["multiagent_status"] not in {"confirmed", "inconclusive"}
                for row in per_run
            ),
        },
        "metrics": metrics,
        "fault_type_accuracy_by_cause": by_fault_type,
        "per_run": per_run,
        "scoring_note": (
            "Both methods use the identical candidate set and shared run IDs. "
            "Missing, invalid, or failed classifications count as incorrect."
        ),
    }
