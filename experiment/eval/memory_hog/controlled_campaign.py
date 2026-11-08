"""Versioned, resumable application-only campaigns. Dry-run is the default."""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import random
import re
import subprocess
import time
import urllib.error
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from experiment.platform.backend.controlled_experiment import (
    ACTIVE_SECONDS, CONTROLLED_CAMPAIGN_PROTOCOL_VERSION, FAULTS, SERVICES,
    CGROUP_READ_COMMAND, GateBlocked, kubectl, parse_cgroup_v2,
    validate_cpu_profile, validate_workflow_template,
    build_time_budget, controlled_time_budget,
)
from experiment.platform.backend.history_config import (
    DEFAULT_HISTORICAL_MCP_SSE_URL,
)
from experiment.platform.backend.plugins.locust_plugin import LocustLoadPlugin
from experiment.eval.memory_hog.resource_intensity import _CPU_SUFFIXES, _MEMORY_SUFFIXES, _parse_quantity
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.eval.memory_hog import runner
from experiment.eval.memory_hog import generate_chaos_manifests as manifests
from experiment.eval.memory_hog.evaluate_multiagent_rca import (
    PROMPT_VERSION, STRATEGY_VERSION, _get_strategy_info, evaluate_campaign,
)

HERE = Path(__file__).resolve().parent
SEED = 20261018
TRAIN_ONLY_PLAN_VERSION = "controlled-train-only-35-v1"
TRAIN_ONLY_TRANCHES = (1, 5, 10, 20, 35)
RAG_REVIEWED_RETRY_PROTOCOL_VERSION = "skill-rag-ab-reviewed-retry-v1"
RAG_REVIEWED_RETRY_PROTOCOL_FILE = "rag_reviewed_retry_protocol.json"
PRE_INTERVENTION_RETRY_VERSION = "blocked-before-injection-v1"
PRE_INTERVENTION_RETRY_MODE = "automatic_pre_intervention"
SAFE_PRE_INTERVENTION_SKIP_VERSION = "clean-pre-intervention-block-skip-v1"
TECHNICAL_FAILURE_CONTINUATION_VERSION = (
    "controlled-train-only-technical-failure-continuation-v2"
)
CPU_MEAN80_AMENDMENT_VERSION = "controlled-cpu-fault-mean80-v1"
CPU_MEAN80_AMENDMENT_FILE = "cpu-mean80-amendment.yaml"
CPU_MEAN80_REVALIDATION_FILE = "cpu_mean80_revalidation.jsonl"
PASSIVE_RECOVERY_EXTENSION_VERSION = "controlled-passive-recovery-extension-v1"
PASSIVE_RECOVERY_EXTENSION_FILE = "passive-recovery-extension-amendment.yaml"
LATE_RECOVERY_CHECK_VERSION = "controlled-late-operational-recovery-check-v3"
LATE_RECOVERY_CHECK_FILE = "late-operational-recovery-check-amendment-v3.yaml"
LATE_RECOVERY_CHECK_MAX_ATTEMPTS = 4
LATE_RECOVERY_CHECK_CAMPAIGN_ID = "controlled-20261103T234452388707Z"
LATE_RECOVERY_AMENDMENT_VERSION = "controlled-campaign-late-recovery-v1"
LATE_RECOVERY_AMENDMENT_FILE = "late_recovery_amendment.json"
UNSUBMITTED_ABORT_AMENDMENT_VERSION = "controlled-campaign-unsubmitted-abort-v1"
UNSUBMITTED_ABORT_AMENDMENT_FILE = "unsubmitted_abort_amendment.json"
SMALL_CELLS = [
    {"service": "payment", "chaos_type": "cpu-hog"},
    {"service": "catalogue", "chaos_type": "memory-hog"},
    {"service": "shipping", "chaos_type": "network-latency"},
    {"service": "user", "chaos_type": "container-kill"},
    {"service": "orders", "chaos_type": "http-status-code"},
]


def build_plan(level: str, seed: int = SEED) -> list[dict[str, Any]]:
    if level == "rag":
        return [
            {**cell, "replica_idx": repetition,
             "cell_id": f"{cell['service']}-{cell['chaos_type']}-r{repetition}",
             "phase": phase}
            for repetition, phase in enumerate(("train", "test"))
            for cell in build_plan("medium", seed + repetition)
        ]
    if level == "train-only":
        priority = build_plan("small", seed)
        priority_order = {
            (cell["service"], cell["chaos_type"]): index
            for index, cell in enumerate(SMALL_CELLS)
        }
        priority.sort(
            key=lambda cell: priority_order[(cell["service"], cell["chaos_type"])]
        )
        remaining = build_plan("medium", seed)
        priority_ids = {cell["cell_id"] for cell in priority}
        remaining = [cell for cell in remaining if cell["cell_id"] not in priority_ids]
        ordered = [{**cell, "phase": "train"} for cell in priority]
        while remaining:
            index = next(
                (i for i, cell in enumerate(remaining)
                 if cell["service"] != ordered[-1]["service"]),
                0,
            )
            ordered.append({**remaining.pop(index), "phase": "train"})
        return ordered
    if level not in {"small", "medium", "large"}:
        raise ValueError("level must be small, medium, large, rag or train-only")
    base = SMALL_CELLS if level == "small" else [
        {"service": service, "chaos_type": fault} for service in SERVICES for fault in FAULTS
    ]
    cells = [
        {**cell, "replica_idx": replica,
         "cell_id": f"{cell['service']}-{cell['chaos_type']}-r{replica}"}
        for replica in range(5 if level == "large" else 1) for cell in base
    ]
    rng = random.Random(seed)
    rng.shuffle(cells)
    ordered = []
    while cells:
        index = next((i for i, cell in enumerate(cells)
                      if not ordered or cell["service"] != ordered[-1]["service"]), 0)
        ordered.append(cells.pop(index))
    return ordered


def build_config(
    source: dict[str, Any], directory: Path, level: str, seed: int,
    calibrations: dict[str, Any],
) -> dict[str, Any]:
    config = json.loads(json.dumps(source))
    config["experiment_campaign_id"] = directory.name
    historical = config.get("historical_observability") or {}
    historical["mcp_sse_url"] = (
        historical.get("mcp_sse_url") or DEFAULT_HISTORICAL_MCP_SSE_URL
    )
    historical["required"] = True
    historical["mode"] = "dual_write"
    historical.setdefault("timeout_seconds", 90)
    historical.setdefault("visibility_timeout_seconds", 600)
    config["historical_observability"] = historical
    plan = build_plan(level, seed)
    config["test_experiments"] = [c for c in plan if c.get("phase", "test") == "test"]
    config["train_experiments"] = [c for c in plan if c.get("phase") == "train"]
    if level == "train-only":
        config["train_only_campaign"] = {
            "version": TRAIN_ONLY_PLAN_VERSION,
            "tranches": list(TRAIN_ONLY_TRANCHES),
            "automatic_advance": False,
            "test_split_included": False,
        }
    if level == "rag":
        config["knowledge_campaign"] = {
            "version": "skill-rag-ab-v1", "corpus_id": directory.name,
            "top_k": 3, "min_score": 0.05, "max_rounds": 4,
            "max_model_calls": 30, "timeout_seconds": 600,
            "max_total_tokens": 200000, "history_char_budget": 24000,
        }
    config["replicas"] = 1  # Repetition is explicit in each cell/ledger key.
    config["ordering"] = {"shuffle": False, "seed": seed}
    config["controlled_campaign"] = {
        "enabled": True,
        "version": "application-pressure-v1",
        "level": level,
        "seed": seed,
        "calibrations": calibrations,
        "minimum_cpu_delta_cores": 0.05,
        "sustained_ratio": 1.25,
        "sustained_seconds": 60,
        "recovery_timeout_seconds": 300,
        "workflow_timeout_seconds": 600,
        "memory_cutoff_limit_fraction": 0.90,
        "metrics_mcp_timeout_seconds": 30,
        "resilience": {
            "version": 1,
            "operational_timeout_seconds": 600,
            "late_operational_timeout_seconds": 600,
            "stable_seconds": 120,
            "max_consecutive_recoveries": 2,
            "max_total_recoveries": 5,
            "poll_retries": 3,
            "pre_intervention_retry": {
                "version": PRE_INTERVENTION_RETRY_VERSION,
                "max_attempts": 2,
                "backoff_seconds": 60,
            },
        },
    }
    for key, value in (calibrations.get("policy") or {}).items():
        if key not in {
            "minimum_cpu_delta_cores", "sustained_ratio", "sustained_seconds",
            "baseline_max_error_percent", "recovery_timeout_seconds", "workflow_timeout_seconds",
        }:
            raise ValueError(f"unsupported controlled policy setting: {key}")
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0
            or (key == "sustained_ratio" and value <= 1)
        ):
            raise ValueError(f"invalid controlled policy value: {key}")
        config["controlled_campaign"][key] = value
    config["controlled_campaign"]["time_budget"] = build_time_budget(config["controlled_campaign"])
    config["timeline"] = {
        "warmup_seconds": 60, "baseline_seconds": 90,
        "fault_duration_seconds": 180, "post_recovery_observation_seconds": 120,
        "sampling_interval_seconds": 5,
    }
    config["platform"].update({
        "initiated_by": "controlled-application-campaign",
        "inter_run_cooldown_seconds": 30,
        "run_timeout_seconds": controlled_time_budget(config["controlled_campaign"])["poll_seconds"],
    })
    config["load_profile"].update({"users": 30, "spawn_rate": 5})
    config["output"].update({
        "results_dir": str(directory), "runs_csv": "runs.csv",
        "manifests_dir": "manifests", "evaluation_csv": "evaluation.csv",
    })
    for item in config["chaos_types"]:
        fault = item["name"]
        if fault in FAULTS:
            item.setdefault("env", {})["total_chaos_duration_seconds"] = ACTIVE_SECONDS[fault]
            if fault == "network-latency":
                item["env"]["network_latency_ms"] = 200
    config["resource_chaos"]["duration_seconds"] = 180
    return config


def campaign_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list(config.get("train_experiments") or []) + list(config["test_experiments"])


def validate_train_only_plan(
    config: dict[str, Any], cells: list[dict[str, Any]]
) -> None:
    protocol = config.get("train_only_campaign") or {}
    seed = (config.get("ordering") or {}).get("seed")
    if type(seed) is not int:
        raise RuntimeError("train-only campaign is missing its integer ordering seed")
    expected = build_plan("train-only", seed)
    if (
        protocol.get("version") != TRAIN_ONLY_PLAN_VERSION
        or protocol.get("tranches") != list(TRAIN_ONLY_TRANCHES)
        or protocol.get("automatic_advance") is not False
        or protocol.get("test_split_included") is not False
        or config.get("train_experiments") != expected
        or config.get("test_experiments") != []
        or cells != expected
        or len(cells) != 35
        or any(cell.get("phase") != "train" for cell in cells)
    ):
        raise RuntimeError(
            "train-only campaign must preserve the versioned 35-cell train matrix "
            "with no test cells"
        )


def train_history_validation_passed(validation: Any) -> bool:
    if not isinstance(validation, dict):
        return False
    telemetry = validation.get("telemetry")
    source_counts = validation.get("source_counts")
    return bool(
        validation.get("status") == "complete"
        and validation.get("mode") == "dual_write"
        and validation.get("source") == "historical_mcp"
        and isinstance(telemetry, dict)
        and all(
            isinstance(telemetry.get(family), dict)
            and telemetry[family].get("status") == "complete"
            for family in ("metrics", "logs", "traces")
        )
        and isinstance(source_counts, dict)
        and all(
            isinstance(source_counts.get(family), int)
            and source_counts[family] > 0
            for family in ("metrics", "logs", "traces")
        )
    )


def cpu_mean80_revalidation_record(
    cell: dict[str, Any], entry: dict[str, Any], amendment: dict[str, Any],
) -> dict[str, Any] | None:
    if cell.get("chaos_type") != "cpu-hog" or entry.get("status") != "completed":
        return None
    if amendment.get("version") != CPU_MEAN80_AMENDMENT_VERSION:
        raise RuntimeError("CPU mean amendment version is unsupported")
    threshold = amendment.get("minimum_quota_fraction")
    if type(threshold) not in (int, float) or not math.isfinite(threshold):
        raise RuntimeError("CPU mean amendment threshold is invalid")

    gates = entry.get("gates") or {}
    mechanism = gates.get("mechanism_observed") or {}
    baseline = mechanism.get("baseline") or {}
    intervention = mechanism.get("intervention") or {}
    mean_fraction = mechanism.get("intervention_mean_quota_fraction")
    nested_mean = (mechanism.get("target_attainment") or {}).get(
        "observed_mean_fraction"
    )
    valid_mean = type(mean_fraction) in (int, float) and math.isfinite(mean_fraction)
    nested_mean_valid = type(nested_mean) in (int, float) and math.isfinite(nested_mean)
    mean_consistent = (
        not nested_mean_valid
        or valid_mean and math.isclose(mean_fraction, nested_mean, rel_tol=0, abs_tol=1e-6)
    )
    measurement_valid = bool(
        gates.get("submitted") is True
        and mechanism.get("metric") == "cpu_cores"
        and baseline.get("status") == "ok"
        and intervention.get("status") == "ok"
        and valid_mean
        and mean_consistent
    )
    mean_passed = bool(measurement_valid and mean_fraction >= threshold)
    checks = {
        "completed": entry.get("status") == "completed",
        "submitted": gates.get("submitted") is True,
        "target_cpu_measurement_valid": measurement_valid,
        "fault_phase_mean_at_least_minimum": mean_passed,
        "recovery": (gates.get("recovery") or {}).get("passed") is True,
        "cleanup": (gates.get("cleanup") or {}).get("passed") is True,
        "load_stopped": (gates.get("load_stopped") or {}).get("stopped") is True,
        "historical_readback": train_history_validation_passed(
            entry.get("historical_validation")
        ),
    }
    eligible = all(checks.values())
    target_attainment = mechanism.get("target_attainment") or {}
    return {
        "version": amendment["version"],
        "criterion": "fault-phase arithmetic mean CPU/quota fraction >= minimum",
        "threshold": threshold,
        "observed_mean_fraction": mean_fraction if valid_mean else None,
        "mean_passed": mean_passed,
        "measurement_valid": measurement_valid,
        "diagnostic_seconds_above_legacy_threshold": target_attainment.get(
            "observed_seconds"
        ),
        "original_backend_gates": {
            "intervention_confirmed_on_target": gates.get(
                "intervention_confirmed_on_target"
            ),
            "mechanism_observed_passed": mechanism.get("passed"),
            "legacy_target_attainment_passed": target_attainment.get("passed"),
        },
        "checks": checks,
        "eligible_for_training": eligible,
    }


def train_cell_gate_passed(entry: dict[str, Any]) -> bool:
    gates = entry.get("gates") or {}
    mechanism = gates.get("mechanism_observed") or {}
    cpu_revalidation = entry.get("cpu_mean80_revalidation") or {}
    if cpu_revalidation.get("version") == CPU_MEAN80_AMENDMENT_VERSION:
        effect_passed = cpu_revalidation.get("eligible_for_training") is True
    else:
        effect_passed = (
            gates.get("intervention_confirmed_on_target") is True
            and mechanism.get("passed") is True
        )
    return bool(
        entry.get("status") == "completed"
        and gates.get("submitted") is True
        and effect_passed
        and (gates.get("recovery") or {}).get("passed") is True
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
        and train_history_validation_passed(entry.get("historical_validation"))
    )


def train_cell_technical_failure_continuable(entry: dict[str, Any]) -> bool:
    if entry.get("status") != "completed":
        return False
    gates = entry.get("gates") or {}
    mechanism = gates.get("mechanism_observed") or {}
    recovery = gates.get("recovery")
    cleanup = gates.get("cleanup")
    load_stopped = gates.get("load_stopped")
    if (
        not isinstance(mechanism, dict)
        or not isinstance(recovery, dict)
        or not isinstance(cleanup, dict)
        or not isinstance(load_stopped, dict)
        or gates.get("submitted") is not True
        or (
            gates.get("intervention_confirmed_on_target") is not False
            and mechanism.get("passed") is not False
        )
        or recovery.get("passed") is not True
        or cleanup.get("passed") is not True
        or load_stopped.get("stopped") is not True
        or not train_history_validation_passed(entry.get("historical_validation"))
        or mechanism.get("status") in {"no_data", "missing_baseline", "not_checked"}
    ):
        return False
    baseline = mechanism.get("baseline")
    intervention = mechanism.get("intervention")
    if baseline is not None or intervention is not None:
        return bool(
            isinstance(baseline, dict)
            and baseline.get("status") == "ok"
            and isinstance(intervention, dict)
            and intervention.get("status") == "ok"
        )
    return bool(
        mechanism.get("status") == "not_confirmed"
        and type(mechanism.get("spec_matches_submitted")) is bool
    )


def apply_train_continuation_calibration(
    config: dict[str, Any], calibration_path: Path,
) -> dict[str, Any]:
    if not calibration_path.is_file():
        raise RuntimeError(f"CPU calibration file is missing: {calibration_path}")
    raw_bytes = calibration_path.read_bytes()
    values = yaml.safe_load(raw_bytes)
    if not isinstance(values, dict) or set(values) - {"policy"} != set(SERVICES):
        raise RuntimeError("continuation calibration must define every campaign service")
    controlled = config.get("controlled_campaign") or {}
    calibrations = controlled.get("calibrations")
    if not isinstance(calibrations, dict):
        raise RuntimeError("saved campaign has no per-service calibration profiles")
    profiles = {}
    for service in SERVICES:
        service_values = values.get(service)
        cpu_profile = service_values.get("cpu") if isinstance(service_values, dict) else None
        if not isinstance(cpu_profile, dict):
            raise RuntimeError(f"continuation calibration is missing CPU profile for {service}")
        validate_cpu_profile({"cpu": cpu_profile})
        if (
            cpu_profile.get("minimum_quota_fraction") != .8
            or "target_quota_fraction" in cpu_profile
            or "target_tolerance_fraction" in cpu_profile
        ):
            raise RuntimeError("continuation CPU calibration must use only the approved 80% minimum")
        previous = calibrations.get(service)
        previous_cpu = previous.get("cpu") if isinstance(previous, dict) else None
        if not isinstance(previous_cpu, dict) or any(
            cpu_profile.get(key) != previous_cpu.get(key)
            for key in ("cores", "load")
        ):
            raise RuntimeError(
                f"continuation calibration would change the saved CPU load for {service}"
            )
        profiles[service] = dict(cpu_profile)
        calibrations[service] = {**previous, "cpu": dict(cpu_profile)}
    amendment = {
        "version": TECHNICAL_FAILURE_CONTINUATION_VERSION,
        "scope": "remaining train-only cells in the saved campaign",
        "continue_after": "completed technical gate failures with valid measured evidence",
        "exclude_failed_runs_from_training": True,
        "hard_stops": [
            "safety",
            "recovery",
            "cleanup",
            "load_not_stopped",
            "invalid_or_incomplete_historical_telemetry",
            "unconfirmed_or_unresolved_run_state",
        ],
        "cpu_calibration_source": str(calibration_path.resolve()),
        "cpu_calibration_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "cpu_minimum_quota_fraction": .8,
        "cpu_acceptance_mode": "fault_phase_arithmetic_mean",
        "cpu_sustained_duration_required": False,
        "cpu_profiles": profiles,
    }
    controlled["calibrations"] = calibrations
    controlled["continuation_amendment"] = {
        key: value for key, value in amendment.items()
        if key != "cpu_profiles"
    }
    config["controlled_campaign"] = controlled
    return amendment


def record_train_continuation_authorization(
    directory: Path, amendment: dict[str, Any],
) -> None:
    path = directory / "campaign_events.jsonl"
    existing = [
        row for row in read_ledger(path)
        if row.get("event") == "technical_failure_continuation_authorized"
    ]
    matching_version = [
        row for row in existing
        if (row.get("amendment") or {}).get("version") == amendment.get("version")
    ]
    if matching_version:
        if any(row.get("amendment") != amendment for row in matching_version):
            raise RuntimeError(
                "saved technical-failure amendment differs from the requested continuation"
            )
        return
    append_ledger(path, {
        "event": "technical_failure_continuation_authorized",
        "at": datetime.now(timezone.utc).isoformat(),
        "user_authorization": "continue_technical_failures_with_cpu_mean80",
        "amendment": amendment,
    })


def clean_pre_intervention_block_skip_amendment() -> dict[str, Any]:
    return {
        "version": SAFE_PRE_INTERVENTION_SKIP_VERSION,
        "scope": "full train-only campaign after the pre-registered retry limit",
        "skip_only_if": [
            "pre-intervention retry limit exhausted",
            "no fault submission or target intervention",
            "cleanup passed and load stopped",
            "application healthy with no unhealthy services or nodes",
            "fresh environment preflight passed before advancing",
        ],
        "hard_stops": [
            "submitted or ambiguous intervention",
            "cleanup or load-stop failure",
            "unhealthy application or failed preflight",
            "invalid or incomplete historical telemetry",
            "unresolved run state",
        ],
        "skipped_cells_remain_ineligible_for_training": True,
    }


def record_clean_pre_intervention_block_skip_authorization(
    directory: Path,
) -> None:
    path = directory / "campaign_events.jsonl"
    amendment = clean_pre_intervention_block_skip_amendment()
    existing = [
        row for row in read_ledger(path)
        if row.get("event") == "clean_pre_intervention_block_skip_authorized"
    ]
    if existing:
        if any(row.get("amendment") != amendment for row in existing):
            raise RuntimeError(
                "saved clean-block continuation amendment differs from the requested policy"
            )
        return
    append_ledger(path, {
        "event": "clean_pre_intervention_block_skip_authorized",
        "at": datetime.now(timezone.utc).isoformat(),
        "user_authorization": "safe_skip_after_max_pre_intervention_retries",
        "amendment": amendment,
    })


def clean_pre_intervention_block_skip_recorded(
    directory: Path, entry: dict[str, Any],
) -> bool:
    return any(
        row.get("event") == "clean_pre_intervention_block_skipped"
        and row.get("cell_id") == entry.get("cell_id")
        and row.get("run_id") == entry.get("run_id")
        and row.get("attempt") == entry.get("attempt")
        for row in read_ledger(directory / "campaign_events.jsonl")
    )


def record_clean_pre_intervention_block_skip(
    directory: Path, entry: dict[str, Any],
) -> None:
    if clean_pre_intervention_block_skip_recorded(directory, entry):
        return
    append_ledger(directory / "campaign_events.jsonl", {
        "event": "clean_pre_intervention_block_skipped",
        "at": datetime.now(timezone.utc).isoformat(),
        "cell_id": entry["cell_id"],
        "run_id": entry["run_id"],
        "attempt": entry["attempt"],
        "reason": entry.get("reason", ""),
        "status_preserved": "blocked",
        "eligible_for_training": False,
        "retry_policy_version": PRE_INTERVENTION_RETRY_VERSION,
    })


def record_train_technical_failure_exclusion(
    directory: Path, cell: dict[str, Any], entry: dict[str, Any],
) -> None:
    path = directory / "campaign_events.jsonl"
    run_id = entry.get("run_id")
    if any(
        row.get("event") == "technical_failure_excluded_from_training"
        and row.get("cell_id") == cell["cell_id"]
        and row.get("run_id") == run_id
        for row in read_ledger(path)
    ):
        return
    gates = entry.get("gates") or {}
    failed_gates = []
    if gates.get("intervention_confirmed_on_target") is not True:
        failed_gates.append("intervention_confirmed_on_target")
    if (gates.get("mechanism_observed") or {}).get("passed") is not True:
        failed_gates.append("mechanism_observed")
    append_ledger(path, {
        "event": "technical_failure_excluded_from_training",
        "at": datetime.now(timezone.utc).isoformat(),
        "cell_id": cell["cell_id"],
        "run_id": run_id,
        "failed_gates": failed_gates,
        "recovery_passed": gates.get("recovery", {}).get("passed") is True,
        "cleanup_passed": gates.get("cleanup", {}).get("passed") is True,
        "load_stopped": gates.get("load_stopped", {}).get("stopped") is True,
        "historical_readback_passed": train_history_validation_passed(
            entry.get("historical_validation")
        ),
        "eligible_for_training": False,
        "campaign_continued_under": TECHNICAL_FAILURE_CONTINUATION_VERSION,
    })


def ensure_train_history_validation(
    directory: Path, config: dict[str, Any], cell: dict[str, Any],
    entry: dict[str, Any], api: Any, *, wait_for_completion: bool = True,
) -> dict[str, Any]:
    if train_history_validation_passed(entry.get("historical_validation")):
        return entry
    run_id = entry.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError(
            f"train cell {cell['cell_id']} has no run_id for historical read-back"
        )
    try:
        if wait_for_completion:
            from experiment.eval.memory_hog.rag_campaign import wait_for_history_archive

            validation = wait_for_history_archive(config, run_id, api)
        else:
            validation = api.get(
                f"/api/runs/{quote(run_id, safe='')}/history/validation"
            )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        validation = {
            "status": "failed",
            "run_id": run_id,
            "error": f"{type(exc).__name__}: {exc}",
        }
        history_path = directory / "history_validation" / f"{cell['cell_id']}.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        failed_entry = {**entry, "historical_validation": validation}
        append_ledger(directory / "ledger.jsonl", failed_entry)
        raise
    history_path = directory / "history_validation" / f"{cell['cell_id']}.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    validated_entry = {**entry, "historical_validation": validation}
    append_ledger(directory / "ledger.jsonl", validated_entry)
    if not train_history_validation_passed(validation):
        raise RuntimeError(
            f"train cell {cell['cell_id']} did not pass historical metrics/logs/traces read-back"
        )
    return validated_entry


def build_spec(config: dict[str, Any], cell: dict[str, Any], attempt: Path) -> dict[str, Any]:
    service, fault = cell["service"], cell["chaos_type"]
    # Reuse the existing observability/governance surface, not the legacy
    # resource-intensity calculation or its baseline-before-warmup lifecycle.
    spec = runner.build_spec(config, service, cell["replica_idx"], "container-kill")
    metrics_timeout = config["controlled_campaign"].get("metrics_mcp_timeout_seconds", 10)
    if not isinstance(metrics_timeout, int) or isinstance(metrics_timeout, bool) or metrics_timeout < 1:
        raise ValueError("controlled_campaign.metrics_mcp_timeout_seconds must be a positive integer")
    spec["observability"]["metrics"]["timeout_seconds"] = metrics_timeout
    spec["experiment"].update({
        "id": f"{config['experiment_campaign_id']}-{cell['cell_id']}",
        "name": f"Controlled {fault} on {service}",
        "description_manual": "Controlled application-only intervention with measured baseline and recovery gates.",
        "hypothesis": "Measurable intervention need not cause HTTP errors or unavailability.",
        "tags": ["harness:controlled", f"target:{service}", f"fault_category:{runner._fault_category_for(config, fault)}"],
    })
    spec["controlled_campaign"] = {
        **config["controlled_campaign"], "service": service, "fault": fault,
        "calibration": config["controlled_campaign"]["calibrations"].get(service, {}),
    }
    if config.get("knowledge_campaign"):
        spec["knowledge_campaign"] = {
            "corpus_id": config["knowledge_campaign"]["corpus_id"],
            "split": cell["phase"],
        }
    historical = config.get("historical_observability")
    if historical:
        spec["historical_observability"] = {
            "mcp_sse_url": historical.get("mcp_sse_url"),
            "required": bool(historical.get("required")),
        }
        for key in (
            "mode",
            "campaign_id",
            "timeout_seconds",
            "visibility_timeout_seconds",
        ):
            if key in historical:
                spec["historical_observability"][key] = historical[key]
    spec["chaos_profile"].update({
        "chaos_engine": f"{service}-{fault}", "manifest_path": None,
        "fault_duration_seconds": ACTIVE_SECONDS[fault], "resource_chaos": None,
    })
    spec["timeline"]["fault_duration_seconds"] = (
        0 if fault == "container-kill" else max(ACTIVE_SECONDS[fault], 120)
    )
    if fault == "http-status-code":
        text = manifests._render_http_status_code(service, {"status_code": 500})
        manifest = yaml.safe_load(text)
        manifest["metadata"]["name"] = f"controlled-{service}-{attempt.name}"[:63]
        path = attempt / "intervention.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        spec["chaos_profile"].update({"chaos_engine": None, "manifest_path": str(path)})
        spec["controlled_campaign"]["http_manifest"] = manifest
    return spec


def attempt_directory(directory: Path, cell_id: str, attempt_number: int) -> Path:
    base = directory / "attempts" / cell_id
    return base if attempt_number == 1 else base / f"attempt-{attempt_number}"


def operationally_recovered(config: dict[str, Any], entry: dict[str, Any]) -> bool:
    policy = config["controlled_campaign"].get("resilience")
    gates = entry.get("gates") or {}
    operational = gates.get("operational_recovery") or {}
    late_operational = gates.get("late_operational_recovery") or {}
    original_target = operational.get("target") or {}
    late_target = late_operational.get("target") or {}
    stable_seconds = policy.get("stable_seconds") if isinstance(policy, dict) else None
    late_timeout = (
        policy.get("late_operational_timeout_seconds", 0)
        if isinstance(policy, dict) else 0
    )
    amended_latency_source = (
        entry.get("cell_id") == "orders-container-kill-r0"
        and entry.get("run_id") == "run-503646080ca5"
        and late_operational.get("source")
        == "same_load_same_baseline_user_authorized_latency_p95_limit_override"
        and late_operational.get("latency_p95_limit_override_ms") == 370.0
        and late_operational.get("late_check_amendment_version")
        == LATE_RECOVERY_CHECK_VERSION
        and late_operational.get("late_check_attempt") == 4
        and isinstance(late_operational.get("late_check_amendment_sha256"), str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            late_operational.get("late_check_amendment_sha256", ""),
        ) is not None
    )
    late_passed = bool(
        type(late_timeout) is int and 120 <= late_timeout <= 600
        and type(stable_seconds) is int and 120 <= stable_seconds < late_timeout
        and late_operational.get("passed") is True
        and late_operational.get("policy_version") == 1
        and (
            late_operational.get("source")
            == "same_load_same_baseline_unchanged_recovery_thresholds"
            or amended_latency_source
        )
        and late_operational.get("stable_seconds", 0) >= stable_seconds
        and original_target.get("container_id")
        and late_target.get("container_id") == original_target.get("container_id")
        and late_target.get("restart_count") == original_target.get("restart_count")
        and (
            late_operational.get("late_check_id") is None
            or (
                isinstance(late_operational.get("late_check_id"), str)
                and late_operational.get("late_check_cleanup_passed") is True
                and late_operational.get("late_check_load_stopped") is True
            )
        )
    )
    return bool(
        isinstance(policy, dict) and policy.get("version") == 1
        and type(stable_seconds) is int
        and entry.get("status") == "aborted"
        and entry.get("reason") == "recovery not confirmed; campaign must stop before next service"
        and gates.get("submitted") is True
        and gates.get("intervention_confirmed_on_target") is True
        and (gates.get("mechanism_observed") or {}).get("passed") is True
        and (gates.get("recovery") or {}).get("passed") is False
        and (operational.get("passed") is True or late_passed)
        and operational.get("policy_version") == 1
        and operational.get("source") == "same_load_same_baseline_unchanged_recovery_thresholds"
        and (
            operational.get("passed") is not True
            or operational.get("stable_seconds", 0) >= stable_seconds
        )
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
        and (gates.get("cgroup_telemetry") or {}).get("passed") is not False
        and (gates.get("partial_telemetry") or {}).get("passed") is not False
    )


def pre_intervention_retry_policy(config: dict[str, Any]) -> dict[str, Any] | None:
    resilience = config.get("controlled_campaign", {}).get("resilience") or {}
    policy = resilience.get("pre_intervention_retry")
    if policy is None:
        return None
    expected = {
        "version": PRE_INTERVENTION_RETRY_VERSION,
        "max_attempts": 2,
        "backoff_seconds": 60,
    }
    if policy != expected:
        raise RuntimeError("unsupported or modified pre-intervention retry policy")
    return policy


def clean_unsubmitted_block(entry: dict[str, Any]) -> bool:
    gates = entry.get("gates") or {}
    return bool(
        entry.get("status") == "blocked"
        and isinstance(entry.get("run_id"), str)
        and entry["run_id"]
        and gates.get("submitted") is False
        and gates.get("intervention_confirmed_on_target") in (None, False)
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
    )


def automatic_pre_intervention_retry_authorized(
    config: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    policy = pre_intervention_retry_policy(config)
    attempt = entry.get("attempt", 1)
    return bool(
        policy
        and clean_unsubmitted_block(entry)
        and type(attempt) is int
        and attempt < policy["max_attempts"]
    )


def clean_pre_intervention_block_skip_authorized(
    config: dict[str, Any],
    entry: dict[str, Any],
) -> bool:
    policy = pre_intervention_retry_policy(config)
    attempt = entry.get("attempt", 1)
    gates = entry.get("gates") or {}
    application = gates.get("application_stability") or {}
    return bool(
        policy
        and clean_unsubmitted_block(entry)
        and type(attempt) is int
        and attempt == policy["max_attempts"]
        and application.get("healthy") is True
        and application.get("unhealthy_services") == []
        and application.get("unhealthy_nodes") == []
    )


def train_cell_failure_continuation_authorized(
    config: dict[str, Any], entry: dict[str, Any], *,
    allow_clean_pre_intervention_skip: bool = False,
) -> bool:
    return bool(
        train_cell_technical_failure_continuable(entry)
        or operationally_recovered(config, entry)
        or late_recovery_authorized(config, entry)
        or automatic_pre_intervention_retry_authorized(config, entry)
        or (
            allow_clean_pre_intervention_skip
            and clean_pre_intervention_block_skip_authorized(config, entry)
        )
    )


def train_only_prior_cell_continuation_authorized(
    config: dict[str, Any], entry: dict[str, Any], *,
    continue_after_technical_failure: bool,
) -> bool:
    return bool(
        continue_after_technical_failure
        and train_cell_failure_continuation_authorized(config, entry)
    )


def late_recovery_authorized(config: dict[str, Any], entry: dict[str, Any]) -> bool:
    policy = config["controlled_campaign"].get("resilience")
    gates = entry.get("gates") or {}
    late = gates.get("late_recovery") or {}
    return bool(
        isinstance(policy, dict) and policy.get("version") == 1
        and entry.get("cell_id") == "orders-container-kill-r1"
        and entry.get("run_id") == "run-065ac29ef2ca"
        and entry.get("status") == "aborted"
        and entry.get("reason") == "recovery not confirmed; campaign must stop before next service"
        and gates.get("submitted") is True
        and gates.get("intervention_confirmed_on_target") is True
        and (gates.get("mechanism_observed") or {}).get("passed") is True
        and (gates.get("recovery") or {}).get("passed") is False
        and (gates.get("operational_recovery") or {}).get("passed") is False
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
        and late.get("version") == LATE_RECOVERY_AMENDMENT_VERSION
        and late.get("passed") is True
        and late.get("user_approved") is True
        and late.get("skip_only") is True
        and late.get("original_status_preserved") is True
        and late.get("thresholds_unchanged") is True
        and late.get("fresh_baseline_for_next_run") is True
        and isinstance(late.get("window_seconds"), (int, float))
        and late["window_seconds"] >= policy["stable_seconds"]
        and isinstance(late.get("amendment_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", late["amendment_sha256"]) is not None
    )


def unsubmitted_abort_skip_authorized(
    config: dict[str, Any], entry: dict[str, Any],
) -> bool:
    policy = config["controlled_campaign"].get("resilience")
    gates = entry.get("gates") or {}
    skip = gates.get("unsubmitted_abort_skip") or {}
    return bool(
        isinstance(policy, dict) and policy.get("version") == 1
        and config.get("experiment_campaign_id") == "exploratory-rag-20261028T191703038725Z"
        and config.get("knowledge_campaign", {}).get("exploratory") is True
        and entry.get("cell_id") == "front-end-network-latency-r1"
        and entry.get("run_id") == "run-4dbd8ae472af"
        and entry.get("attempt", 1) == 1
        and entry.get("status") == "aborted"
        and entry.get("reason") == "application unhealthy after blocked attempt; stop campaign"
        and gates.get("submitted") is False
        and gates.get("intervention_confirmed_on_target") is None
        and (gates.get("recovery") or {}).get("status") == "not_checked"
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
        and skip.get("version") == UNSUBMITTED_ABORT_AMENDMENT_VERSION
        and skip.get("passed") is True
        and skip.get("user_approved") is True
        and skip.get("skip_only") is True
        and skip.get("original_status_preserved") is True
        and skip.get("fresh_baseline_for_next_run") is True
        and isinstance(skip.get("amendment_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", skip["amendment_sha256"]) is not None
    )


def check_recovery_budget(config: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    policy = config["controlled_campaign"].get("resilience")
    if not isinstance(policy, dict):
        return
    attempts = {(entry["cell_id"], entry.get("attempt", 1)): entry for entry in entries}
    terminal = [
        entry for entry in attempts.values()
        if entry.get("status") in {"completed", "aborted", "failed", "stopped"}
    ]
    recovered = [
        operationally_recovered(config, entry)
        or late_recovery_authorized(config, entry)
        for entry in terminal
    ]
    consecutive = 0
    for value in recovered:
        consecutive = consecutive + 1 if value else 0
    if (
        sum(recovered) >= policy["max_total_recoveries"]
        or consecutive >= policy["max_consecutive_recoveries"]
    ):
        raise RuntimeError("operational recovery circuit breaker reached; manual review required")


def poll_run(api: runner.ApiClient, run_id: str, directory: Path, retries: int) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            return api.get(f"/api/runs/{run_id}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as exc:
            cause = exc.__cause__ if isinstance(exc, RuntimeError) else exc
            transient = (
                isinstance(cause, urllib.error.HTTPError) and cause.code in {502, 503, 504}
            ) or (
                isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError))
                and not isinstance(cause, urllib.error.HTTPError)
            )
            if not transient or attempt >= retries:
                raise
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "run_poll_retry", "at": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id, "retry": attempt + 1, "error": str(exc),
            })
            time.sleep(min(5 * 2**attempt, 30))
    raise RuntimeError("run polling exhausted its retry budget")


def unconfirmed_container_kill_retry_guard(
    directory: Path, cell: dict[str, Any], previous: dict[str, Any],
) -> dict[str, Any]:
    gates = previous.get("gates") or {}
    if (
        cell["chaos_type"] != "container-kill"
        or previous.get("status") != "aborted"
        or gates.get("submitted") not in {True, False}
    ):
        raise RuntimeError(
            f"{cell['cell_id']} is not an aborted container-kill attempt with known submission state"
        )
    previous_attempt = int(previous.get("attempt", 1))
    run_path = attempt_directory(directory, cell["cell_id"], previous_attempt) / "run.json"
    if not run_path.is_file():
        raise RuntimeError(f"previous container-kill run record is missing: {run_path}")
    result = json.loads(run_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("previous run record is malformed; refusing an unconfirmed retry")
    summary = result.get("summary")
    artifacts = summary.get("artifact_paths") if isinstance(summary, dict) else None
    report_file = artifacts.get("controlled_report") if isinstance(artifacts, dict) else None
    pods_after_file = artifacts.get("pods_after") if isinstance(artifacts, dict) else None
    if not isinstance(report_file, str) or not report_file or not isinstance(pods_after_file, str) or not pods_after_file:
        raise RuntimeError("previous run lacks controlled/pod readback artifacts; refusing an unconfirmed retry")
    report_path, pods_after_path = Path(report_file), Path(pods_after_file)
    if not report_path.is_file() or not pods_after_path.is_file():
        raise RuntimeError("previous controlled/pod readback artifact is missing; refusing an unconfirmed retry")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pods_after = json.loads(pods_after_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(pods_after, dict):
        raise RuntimeError("previous controlled/pod readback artifact is malformed; refusing an unconfirmed retry")
    event = report.get("container_kill_event") or {}
    target = report.get("target_before") or {}
    event_target = event.get("target") or {}
    report_gates = report.get("gates") or {}
    summary_gates = summary.get("controlled_gates") if isinstance(summary, dict) else {}
    if (
        summary.get("controlled_status") != "aborted"
        or (summary_gates or {}).get("submitted") is not gates["submitted"]
        or (summary_gates or {}).get("cleanup", {}).get("passed") is not True
        or (summary_gates or {}).get("load_stopped", {}).get("stopped") is not True
        or (summary_gates or {}).get("application_stability", {}).get("healthy") is not True
    ):
        raise RuntimeError("previous run did not prove safe cleanup and healthy application state")
    identity_pairs = (
        ("pod", "pod"), ("uid", "pod_uid"), ("container", "container"),
        ("container_id", "container_id"), ("restart_count", "restart_count"),
    )
    submitted_but_unconfirmed = (
        gates["submitted"] is True
        and report_gates.get("intervention_confirmed_on_target") is False
        and (gates.get("mechanism_observed") or {}).get("status") == "restart_not_confirmed"
        and event.get("event_count_submitted") == 1
        and event.get("restart_observed") is False
        and not any(target.get(left) != event_target.get(right) for left, right in identity_pairs)
    )
    aborted_before_submission = (
        gates["submitted"] is False
        and report_gates.get("submission_attempted") is not True
        and "container_kill_event" not in report
    )
    containers = [
        status for item in pods_after.get("items", [])
        for status in item.get("status", {}).get("containerStatuses", [])
        if item.get("metadata", {}).get("name") == target.get("pod")
        and item.get("metadata", {}).get("uid") == target.get("uid")
        and status.get("name") == target.get("container")
    ]
    after = containers[0] if len(containers) == 1 else {}
    if (
        report.get("status") != "aborted"
        or not (submitted_but_unconfirmed or aborted_before_submission)
        or any(
            not isinstance(target.get(key), str) or not target[key]
            for key in ("pod", "uid", "container", "container_id")
        )
        or not isinstance(target.get("restart_count"), int)
        or isinstance(target.get("restart_count"), bool)
        or len(pods_after.get("items", [])) != 1
        or after.get("containerID") != target.get("container_id")
        or after.get("restartCount") != target.get("restart_count")
        or after.get("ready") is not True
        or not isinstance(after.get("state", {}).get("running"), dict)
    ):
        raise RuntimeError("previous reports do not prove the target stayed stable without a restart")
    return {
        "previous_run_id": previous.get("run_id"),
        "pod": target["pod"],
        "pod_uid": target["uid"],
        "container": target["container"],
        "container_id": target["container_id"],
        "restart_count": target["restart_count"],
    }


def overlapping_observation_window_retry_guard(
    directory: Path, cell: dict[str, Any], previous: dict[str, Any],
) -> dict[str, Any]:
    if cell["chaos_type"] != "http-status-code" or previous.get("status") != "completed":
        raise RuntimeError(
            f"{cell['cell_id']} is not a completed HTTP experiment eligible for window invalidation"
        )
    attempt_number = int(previous.get("attempt", 1))
    run_path = attempt_directory(directory, cell["cell_id"], attempt_number) / "run.json"
    if not run_path.is_file():
        raise RuntimeError(f"previous run record is missing: {run_path}")
    result = json.loads(run_path.read_text(encoding="utf-8"))
    summary = result.get("summary") if isinstance(result, dict) else None
    if (
        not isinstance(summary, dict)
        or result.get("status") != "completed"
        or summary.get("controlled_status") != "completed"
    ):
        raise RuntimeError("previous HTTP experiment is not a completed controlled run")
    gates = summary.get("controlled_gates") or {}
    if (
        gates.get("recovery", {}).get("passed") is not True
        or gates.get("cleanup", {}).get("passed") is not True
        or gates.get("load_stopped", {}).get("stopped") is not True
    ):
        raise RuntimeError("previous HTTP experiment did not prove recovery, cleanup and load stop")
    artifacts = summary.get("artifact_paths") or {}
    window_file = artifacts.get("observation_window")
    if not isinstance(window_file, str) or not window_file:
        raise RuntimeError("previous HTTP experiment lacks its observation-window artifact")
    window_path = Path(window_file)
    if not window_path.is_file():
        raise RuntimeError(f"previous observation-window artifact is missing: {window_path}")
    window = json.loads(window_path.read_text(encoding="utf-8"))
    if not isinstance(window, dict) or (
        (window.get("phase_sources") or {}).get("fault") != "virtualservice_api_bracket_estimated"
    ):
        raise RuntimeError("previous window was not produced by the affected HTTP timestamp path")
    phases = window.get("phases") or {}
    fault, post = phases.get("fault"), phases.get("post")
    if not isinstance(fault, dict) or not isinstance(post, dict):
        raise RuntimeError("previous observation window lacks fault or post boundaries")
    try:
        fault_end = datetime.fromisoformat(str(fault["end"]).replace("Z", "+00:00"))
        post_start = datetime.fromisoformat(str(post["start"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("previous observation-window boundaries are malformed") from exc
    if fault_end.tzinfo is None or post_start.tzinfo is None:
        raise RuntimeError("previous observation-window boundaries must include timezones")
    overlap_ms = (fault_end - post_start).total_seconds() * 1000
    if overlap_ms <= 0:
        raise RuntimeError("previous observation window does not contain the known overlap defect")
    return {
        "previous_run_id": previous.get("run_id"),
        "fault_end": fault["end"],
        "post_start": post["start"],
        "overlap_ms": round(overlap_ms, 3),
        "phase_source": window["phase_sources"]["fault"],
    }


def reviewed_container_kill_retry_guard(
    directory: Path, cell: dict[str, Any], previous: dict[str, Any],
) -> dict[str, Any]:
    if cell["chaos_type"] != "container-kill" or previous.get("status") != "aborted":
        raise RuntimeError(f"{cell['cell_id']} is not an aborted container-kill attempt")
    attempt_number = int(previous.get("attempt", 1))
    run_path = attempt_directory(directory, cell["cell_id"], attempt_number) / "run.json"
    if not run_path.is_file():
        raise RuntimeError(f"previous container-kill run record is missing: {run_path}")
    result = json.loads(run_path.read_text(encoding="utf-8"))
    summary = result.get("summary") if isinstance(result, dict) else None
    if not isinstance(summary, dict):
        raise RuntimeError("previous container-kill run record has no controlled summary")
    gates = summary.get("controlled_gates") if isinstance(summary, dict) else None
    gates = gates or {}
    mechanism = gates.get("mechanism_observed") or {}
    recovery = gates.get("recovery") or {}
    cleanup = gates.get("cleanup") or {}
    load_stopped = gates.get("load_stopped") or {}
    application_stability = gates.get("application_stability") or {}
    if (
        result.get("status") != "failed"
        or summary.get("controlled_status") != "aborted"
        or gates.get("submitted") is not True
        or mechanism.get("passed") is not True
        or mechanism.get("restart_count_delta") != 1
        or recovery.get("passed") is not False
        or cleanup.get("passed") is not True
        or load_stopped.get("stopped") is not True
        or application_stability.get("healthy") is not True
    ):
        raise RuntimeError(
            "previous container-kill attempt does not prove one restart, failed recovery, "
            "successful cleanup/load stop and healthy application stability"
        )
    return {
        "previous_run_id": previous.get("run_id"),
        "previous_restart_count_delta": mechanism["restart_count_delta"],
        "previous_recovery_passed": False,
        "retry_authorized_by_user": True,
        "current_run_preflight_required": True,
    }


def validate_rag_reviewed_retry_protocol(
    directory: Path,
    config: dict[str, Any],
    cells: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
) -> dict[str, Any] | None:
    protocol_path = directory / RAG_REVIEWED_RETRY_PROTOCOL_FILE
    all_retry_rows = [row for row in ledger if int(row.get("attempt", 1)) > 1]
    automatic_retry_rows = [
        row
        for row in all_retry_rows
        if row.get("retry_mode") == PRE_INTERVENTION_RETRY_MODE
    ]
    for row in automatic_retry_rows:
        policy = pre_intervention_retry_policy(config)
        prior_attempt = int(row.get("attempt", 1)) - 1
        previous_rows = [
            item
            for item in ledger
            if item.get("cell_id") == row.get("cell_id")
            and int(item.get("attempt", 1)) == prior_attempt
        ]
        previous = previous_rows[-1] if previous_rows else {}
        if (
            policy is None
            or row.get("retry_policy_version") != policy["version"]
            or row.get("attempt") != 2
            or row.get("retry_of_run_id") != previous.get("run_id")
            or not automatic_pre_intervention_retry_authorized(config, previous)
        ):
            raise RuntimeError(
                "RAG automatic retry is not authorized by the saved pre-intervention policy"
            )
    retry_rows = [
        row
        for row in all_retry_rows
        if row.get("retry_mode") != PRE_INTERVENTION_RETRY_MODE
    ]
    if not protocol_path.exists():
        if retry_rows:
            raise RuntimeError(
                "RAG campaign has additional attempts without a reviewed protocol amendment"
            )
        return None

    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    plan_path = directory / "plan.json"
    expected = {
        "version": RAG_REVIEWED_RETRY_PROTOCOL_VERSION,
        "parent_protocol_version": config["knowledge_campaign"]["version"],
        "campaign_id": config["experiment_campaign_id"],
        "config_sha256": hashlib.sha256((directory / "config.yaml").read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "additional_attempt_budget": 1,
        "retry_attempt": 2,
        "authorized_by_user": True,
    }
    if any(protocol.get(key) != value for key, value in expected.items()):
        raise RuntimeError("RAG reviewed retry amendment does not match the saved campaign protocol")

    cells_by_id = {cell["cell_id"]: cell for cell in cells}
    cell_id = protocol.get("cell_id")
    cell = cells_by_id.get(cell_id)
    if (
        cell is None
        or cell.get("phase") != "train"
        or cell.get("chaos_type") != "container-kill"
        or protocol.get("phase") != "train"
        or protocol.get("previous_attempt") != 1
        or protocol.get("previous_run_id") is None
    ):
        raise RuntimeError("RAG reviewed retry amendment must identify one training container-kill cell")
    if any(
        row["cell_id"] != cell_id and int(row.get("attempt", 1)) > 1
        for row in ledger
    ):
        raise RuntimeError("RAG reviewed retry amendment permits only one additional campaign attempt")

    cell_attempts = [
        row for row in ledger
        if row.get("cell_id") == cell_id
    ]
    original_rows = [
        row for row in cell_attempts
        if int(row.get("attempt", 1)) == 1
    ]
    original_latest = original_rows[-1] if original_rows else {}
    if (
        original_latest.get("status") != "aborted"
        or original_latest.get("run_id") != protocol["previous_run_id"]
    ):
        raise RuntimeError("RAG reviewed retry amendment no longer matches the original aborted run")

    retry_attempts = [
        row for row in retry_rows
        if row.get("cell_id") == cell_id and int(row.get("attempt", 1)) == 2
    ]
    if any(
        row.get("retry_of_run_id") != protocol["previous_run_id"]
        for row in retry_attempts
    ):
        raise RuntimeError("RAG replacement attempt is not linked to the reviewed original run")
    if len({row["run_id"] for row in retry_attempts if row.get("run_id")}) > 1:
        raise RuntimeError("RAG reviewed retry created more than one replacement run")
    if any(int(row.get("attempt", 1)) > 2 for row in cell_attempts):
        raise RuntimeError("RAG reviewed retry attempt budget is exhausted")
    return protocol


def authorize_rag_reviewed_retry(
    directory: Path, config: dict[str, Any], cells: list[dict[str, Any]],
    ledger: list[dict[str, Any]], cell: dict[str, Any], previous: dict[str, Any],
) -> dict[str, Any]:
    if (
        cell.get("phase") != "train"
        or cell.get("chaos_type") != "container-kill"
        or int(previous.get("attempt", 1)) != 1
        or previous.get("status") != "aborted"
        or not previous.get("run_id")
    ):
        raise RuntimeError(
            "RAG reviewed retry is limited to one aborted training container-kill attempt"
        )
    if (directory / "rag_base.json").exists():
        raise RuntimeError("RAG training retries are forbidden after the knowledge base is frozen")
    test_ids = {item["cell_id"] for item in cells if item.get("phase") == "test"}
    if any(row.get("cell_id") in test_ids for row in ledger):
        raise RuntimeError("RAG training retries are forbidden after test execution has started")

    protocol_path = directory / RAG_REVIEWED_RETRY_PROTOCOL_FILE
    existing = validate_rag_reviewed_retry_protocol(directory, config, cells, ledger)
    if existing is not None:
        if existing["cell_id"] != cell["cell_id"]:
            raise RuntimeError("RAG campaign already has a reviewed retry for another cell")
        if any(
            row.get("cell_id") == cell["cell_id"]
            and int(row.get("attempt", 1)) > 1
            for row in ledger
        ):
            raise RuntimeError(
                "RAG reviewed retry has already started; resume without requesting another retry"
            )
        return existing

    from experiment.platform.backend.rca_knowledge import write_once

    protocol = {
        "version": RAG_REVIEWED_RETRY_PROTOCOL_VERSION,
        "parent_protocol_version": config["knowledge_campaign"]["version"],
        "campaign_id": config["experiment_campaign_id"],
        "config_sha256": hashlib.sha256((directory / "config.yaml").read_bytes()).hexdigest(),
        "plan_sha256": hashlib.sha256((directory / "plan.json").read_bytes()).hexdigest(),
        "cell_id": cell["cell_id"],
        "phase": "train",
        "previous_attempt": 1,
        "previous_run_id": previous["run_id"],
        "retry_attempt": 2,
        "additional_attempt_budget": 1,
        "authorized_by_user": True,
        "authorization_source": "explicit reviewed retry command after user authorization",
        "original_attempt_retained": True,
        "original_attempt_excluded_from_training_cohort": True,
        "split_unchanged": {"train": 35, "test": 35},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    write_once(protocol_path, protocol)
    return validate_rag_reviewed_retry_protocol(directory, config, cells, ledger) or protocol


def append_ledger(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_cpu_mean80_amendment(
    path: Path | None = None,
) -> dict[str, Any]:
    amendment_path = path or HERE / CPU_MEAN80_AMENDMENT_FILE
    amendment = yaml.safe_load(amendment_path.read_text(encoding="utf-8"))
    if (
        not isinstance(amendment, dict)
        or amendment.get("version") != CPU_MEAN80_AMENDMENT_VERSION
        or not isinstance(amendment.get("campaign_id"), str)
        or type(amendment.get("minimum_quota_fraction")) not in (int, float)
        or amendment.get("minimum_quota_fraction") != .8
        or amendment.get("aggregation") != "arithmetic_mean"
        or amendment.get("window") != "fault"
        or amendment.get("sustained_duration_required") is not False
        or amendment.get("upper_bound") is not None
    ):
        raise RuntimeError(f"invalid CPU mean amendment: {amendment_path}")
    return amendment


def load_passive_recovery_extension_amendment(
    path: Path | None = None,
) -> dict[str, Any]:
    amendment_path = path or HERE / PASSIVE_RECOVERY_EXTENSION_FILE
    amendment = yaml.safe_load(amendment_path.read_text(encoding="utf-8"))
    if (
        not isinstance(amendment, dict)
        or amendment.get("version") != PASSIVE_RECOVERY_EXTENSION_VERSION
        or amendment.get("campaign_id") != LATE_RECOVERY_CHECK_CAMPAIGN_ID
        or amendment.get("user_authorized") is not True
        or amendment.get("scope") != "future_runs_only"
        or amendment.get("operational_timeout_seconds") != 600
        or amendment.get("late_operational_timeout_seconds") != 600
        or amendment.get("stable_seconds") != 120
        or amendment.get("thresholds_unchanged") is not True
        or amendment.get("automatic_restart") is not False
        or amendment.get("aborted_run_status_preserved") is not True
        or amendment.get("skip_only_after_recovery_and_cleanup") is not True
    ):
        raise RuntimeError(f"invalid passive recovery extension amendment: {amendment_path}")
    return amendment


def apply_passive_recovery_extension_amendment(
    directory: Path, config: dict[str, Any],
) -> bool:
    amendment = load_passive_recovery_extension_amendment()
    if config.get("experiment_campaign_id") != amendment["campaign_id"]:
        return False
    policy = config["controlled_campaign"].get("resilience")
    if (
        not isinstance(policy, dict)
        or policy.get("version") != 1
        or policy.get("operational_timeout_seconds") != amendment["operational_timeout_seconds"]
        or policy.get("stable_seconds") != amendment["stable_seconds"]
        or type(policy.get("late_operational_timeout_seconds", 0)) is not int
        or policy.get("late_operational_timeout_seconds", 0)
        not in (0, amendment["late_operational_timeout_seconds"])
    ):
        raise RuntimeError("saved campaign recovery policy conflicts with its amendment")

    content = (HERE / PASSIVE_RECOVERY_EXTENSION_FILE).read_bytes()
    amendment_hash = hashlib.sha256(content).hexdigest()
    policy["late_operational_timeout_seconds"] = amendment["late_operational_timeout_seconds"]
    config["controlled_campaign"]["time_budget"] = build_time_budget(
        config["controlled_campaign"],
    )
    config["platform"]["run_timeout_seconds"] = (
        config["controlled_campaign"]["time_budget"]["poll_seconds"]
    )
    events_path = directory / "campaign_events.jsonl"
    events = read_ledger(events_path)
    prior = [
        event for event in events
        if event.get("event") == "passive_recovery_extension_amendment_applied"
    ]
    if prior and any(event.get("amendment_sha256") != amendment_hash for event in prior):
        raise RuntimeError("campaign has a conflicting passive recovery amendment")
    if not prior:
        append_ledger(events_path, {
            "event": "passive_recovery_extension_amendment_applied",
            "at": datetime.now(timezone.utc).isoformat(),
            "version": amendment["version"],
            "campaign_id": amendment["campaign_id"],
            "amendment_sha256": amendment_hash,
            "scope": amendment["scope"],
            "operational_timeout_seconds": amendment["operational_timeout_seconds"],
            "late_operational_timeout_seconds": amendment["late_operational_timeout_seconds"],
            "stable_seconds": amendment["stable_seconds"],
            "thresholds_unchanged": True,
            "automatic_restart": False,
            "aborted_run_status_preserved": True,
        })
    return True


def load_late_operational_recovery_check_amendment(
    path: Path | None = None,
) -> dict[str, Any]:
    amendment_path = path or HERE / LATE_RECOVERY_CHECK_FILE
    amendment = yaml.safe_load(amendment_path.read_text(encoding="utf-8"))
    target = amendment.get("target_run") if isinstance(amendment, dict) else None
    if (
        not isinstance(amendment, dict)
        or amendment.get("version") != LATE_RECOVERY_CHECK_VERSION
        or amendment.get("campaign_id") != "controlled-20261103T234452388707Z"
        or amendment.get("user_authorized") is not True
        or amendment.get("scope") != "single_aborted_run_only"
        or target != {
            "cell_id": "orders-container-kill-r0",
            "run_id": "run-503646080ca5",
        }
        or amendment.get("timeout_seconds") != 600
        or amendment.get("stable_seconds") != 120
        or amendment.get("same_load") is not True
        or amendment.get("same_baseline") is not True
        or amendment.get("thresholds_unchanged") is not False
        or amendment.get("other_thresholds_unchanged") is not True
        or amendment.get("latency_p95_limit_override_ms") != 370.0
        or amendment.get("fault_injection") is not False
        or amendment.get("automatic_restart") is not False
        or amendment.get("original_run_status_preserved") is not True
        or amendment.get("continue_only_after_recovery_and_cleanup") is not True
        or amendment.get("max_attempts") != LATE_RECOVERY_CHECK_MAX_ATTEMPTS
        or amendment.get("retry_after_cgroup_sampling_fix_only") is not True
        or amendment.get("retry_after_marginal_latency_stability_failure_only") is not True
    ):
        raise RuntimeError(f"invalid late operational recovery check amendment: {amendment_path}")
    return amendment


def cleanup_interrupted_late_recovery_load(
    check_report: dict[str, Any],
) -> dict[str, Any]:
    check_id = check_report.get("run_id")
    if not isinstance(check_id, str) or not check_id:
        raise RuntimeError("interrupted late recovery report has no check identifier")
    context = {"run_id": check_id}
    handle = check_report.get("load_handle")
    if handle:
        if not isinstance(handle, dict) or handle.get("controlled_remote") is not True:
            raise RuntimeError("interrupted late recovery has an unsupported load handle")
        plugin = LocustLoadPlugin()
        before = plugin.status(context, handle)
        stop_result = None
        if before.get("state") == "running":
            stop_result = plugin.stop_load(context, handle)
            if stop_result.get("stopped") is not True:
                raise RuntimeError("interrupted late recovery load stop was not confirmed")
        after = plugin.status(context, handle)
        if after.get("state") != "completed":
            raise RuntimeError("interrupted late recovery load termination could not be verified")
        return {
            "found": True,
            "stopped": after.get("stopped") is True,
            "remote_pid": handle.get("remote_pid"),
            "pod_uid": handle.get("pod_uid"),
            "container_id": handle.get("container_id"),
            "status_before": before,
            "stop_result": stop_result,
            "status_after": after,
            "identity_source": "persisted_load_handle",
        }
    return LocustLoadPlugin().stop_orphaned_remote_load(context, check_id)


def late_recovery_resource_telemetry_retry_authorized(
    check_report: dict[str, Any], prior_gate: dict[str, Any],
) -> bool:
    if (
        prior_gate.get("passed") is not False
        or prior_gate.get("late_check_cleanup_passed") is not True
        or check_report.get("passed") is not False
        or check_report.get("late_operational_recovery_passed") is not False
        or check_report.get("error") is not None
        or check_report.get("load_stop_error") is not None
        or (check_report.get("load_stopped") or {}).get("stopped") is not True
        or (check_report.get("pre_load_health") or {}).get("healthy") is not True
    ):
        return False
    gate = (check_report.get("gates") or {}).get("late_operational_recovery") or {}
    health = gate.get("health") or {}
    target = gate.get("target") or {}
    event = check_report.get("container_kill_event") or {}
    checks = gate.get("metric_checks") or {}
    failed = {
        key for key, check in checks.items()
        if check.get("passed") is not True
    }
    if (
        gate.get("passed") is not False
        or gate.get("stable_seconds") != 0
        or gate.get("required_stable_seconds") != 120
        or health.get("healthy") is not True
        or target.get("running") is not True
        or target.get("ready") is not True
        or target.get("container_id") != event.get("container_id_after_restart")
        or target.get("restart_count") != event.get("restart_count_after_signal")
        or failed != {"cpu_cores", "memory_bytes"}
    ):
        return False
    return all(
        checks[key].get("reason") == "missing_or_insufficient_samples"
        and checks[key].get("observed_status") == "no_data"
        and checks[key].get("sample_count") == 0
        and checks[key].get("minimum_samples") == 4
        for key in ("cpu_cores", "memory_bytes")
    )


def late_recovery_marginal_latency_retry_authorized(
    check_report: dict[str, Any],
    prior_gate: dict[str, Any],
    source_report: dict[str, Any],
    amendment: dict[str, Any],
) -> bool:
    if (
        prior_gate.get("passed") is not False
        or prior_gate.get("late_check_cleanup_passed") is not True
        or prior_gate.get("late_check_attempt") != 3
        or check_report.get("passed") is not False
        or check_report.get("late_operational_recovery_passed") is not False
        or check_report.get("error") is not None
        or check_report.get("load_stop_error") is not None
        or (check_report.get("load_stopped") or {}).get("stopped") is not True
        or (check_report.get("pre_load_health") or {}).get("healthy") is not True
        or (check_report.get("late_recovery_policy") or {}).get("attempt") != 3
        or amendment.get("latency_p95_limit_override_ms") != 370.0
    ):
        return False

    gate = (check_report.get("gates") or {}).get("late_operational_recovery") or {}
    checks = gate.get("metric_checks") or {}
    stable_seconds = gate.get("stable_seconds")
    target = gate.get("target") or {}
    event = check_report.get("container_kill_event") or {}
    cgroup = ((check_report.get("telemetry") or {}).get("cgroup_v2") or {})
    if (
        gate.get("passed") is not False
        or gate.get("required_stable_seconds") != 120
        or type(stable_seconds) not in (int, float)
        or not 90 <= stable_seconds < 120
        or (gate.get("health") or {}).get("healthy") is not True
        or target.get("running") is not True
        or target.get("ready") is not True
        or target.get("container_id") != event.get("container_id_after_restart")
        or target.get("restart_count") != event.get("restart_count_after_signal")
        or set(checks) != {
            "traffic", "cpu_cores", "memory_bytes", "errors_percent", "latency_ms",
        }
        or any(check.get("passed") is not True for check in checks.values())
        or cgroup.get("source") != "kubectl_exec_inside_application_container"
        or cgroup.get("scope") != "exact application container; sidecars excluded"
        or cgroup.get("container_id_after_restart") != target.get("container_id")
        or type(cgroup.get("sample_count")) is not int
        or cgroup["sample_count"] < 4
        or (check_report.get("telemetry") or {}).get("cgroup_v2_read_failures")
    ):
        return False

    baseline_observation = next(
        (
            observation for observation in source_report.get("observations", [])
            if observation.get("phase") == "baseline"
        ),
        None,
    )
    baseline = (baseline_observation or {}).get("summary") or {}
    metric_names = ("traffic", "cpu_cores", "memory_bytes", "errors_percent", "latency_ms")
    if any((baseline.get(name) or {}).get("status") != "ok" for name in metric_names):
        return False
    latency_baseline = baseline["latency_ms"]
    original_latency_limit = max(
        latency_baseline["p95"] * 1.2,
        latency_baseline["p95"] + 50,
    )
    recorded_latency_limit = checks["latency_ms"].get("threshold")
    amended_latency_limit = amendment["latency_p95_limit_override_ms"]
    if (
        type(recorded_latency_limit) not in (int, float)
        or abs(recorded_latency_limit - original_latency_limit) > 1e-6
        or amended_latency_limit <= original_latency_limit
        or checks["latency_ms"].get("observed", float("inf")) > original_latency_limit
    ):
        return False

    margins = {
        "cpu_cores": .05,
        "memory_bytes": 16 * 2**20,
        "errors_percent": 1,
        "latency_ms": 50,
    }

    def observation_passes(summary: dict[str, Any]) -> bool:
        for name in metric_names:
            observed = summary.get(name) or {}
            reference = baseline[name]
            if observed.get("status") != "ok" or observed.get("samples", 0) < 4:
                return False
            if name == "traffic":
                if observed.get("mean", float("-inf")) < reference["mean"] * .8:
                    return False
                continue
            threshold = max(reference["p95"] * 1.2, reference["p95"] + margins[name])
            if name == "latency_ms":
                threshold = amended_latency_limit
            if observed.get("p95", float("inf")) > threshold:
                return False
        return True

    observations = check_report.get("observations") or []
    if not observations:
        return False
    last_failure = max(
        (index for index, observation in enumerate(observations)
         if not observation_passes(observation.get("summary") or {})),
        default=-1,
    )
    tail = observations[last_failure + 1:]
    if len(tail) < 2:
        return False
    first_tail_latency = (
        (tail[0].get("summary") or {}).get("latency_ms") or {}
    ).get("p95")
    if (
        type(first_tail_latency) not in (int, float)
        or not original_latency_limit < first_tail_latency <= amended_latency_limit
        or any(
            ((observation.get("summary") or {}).get("latency_ms") or {}).get(
                "p95", float("inf"),
            ) > original_latency_limit
            for observation in tail[1:]
        )
    ):
        return False
    try:
        tail_start = datetime.fromisoformat(tail[0]["end"].replace("Z", "+00:00"))
        tail_end = datetime.fromisoformat(observations[-1]["end"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    return (tail_end - tail_start).total_seconds() >= amendment["stable_seconds"]


def apply_late_operational_recovery_check(
    directory: Path, config: dict[str, Any], ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if config.get("experiment_campaign_id") != LATE_RECOVERY_CHECK_CAMPAIGN_ID:
        return ledger
    amendment = load_late_operational_recovery_check_amendment()
    target = amendment["target_run"]
    latest = {row["cell_id"]: row for row in ledger}
    entry = latest.get(target["cell_id"])
    if entry is None:
        return ledger
    if entry.get("run_id") != target["run_id"]:
        raise RuntimeError("late recovery amendment does not match the saved campaign run")
    if entry.get("status") != "aborted":
        return ledger
    prior_late_gate = (entry.get("gates") or {}).get("late_operational_recovery")
    if prior_late_gate:
        if prior_late_gate.get("passed") is True:
            return ledger
        if prior_late_gate.get("passed") is not False:
            raise RuntimeError("saved late recovery gate has no explicit result")

    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    planned = next((cell for cell in plan if cell.get("cell_id") == target["cell_id"]), None)
    if (
        planned is None
        or planned.get("service") != "orders"
        or planned.get("chaos_type") != "container-kill"
    ):
        raise RuntimeError("late recovery amendment does not match the immutable campaign plan")

    object_store = HERE.parents[1] / "platform" / "data" / "object_store" / target["run_id"]
    source_report_path = object_store / "controlled_report.json"
    effective_spec_path = object_store / "effective_spec.json"
    if not source_report_path.is_file() or not effective_spec_path.is_file():
        raise RuntimeError("late recovery source report or effective spec is missing")
    source_report_sha256 = hashlib.sha256(source_report_path.read_bytes()).hexdigest()
    effective_spec = json.loads(effective_spec_path.read_text(encoding="utf-8"))
    saved_load_profile = effective_spec.get("load_profile", {})
    campaign_load_profile = config.get("load_profile", {})
    for key in ("command", "locust_file", "host", "users", "spawn_rate", "grace_seconds", "extra_args"):
        saved_value = saved_load_profile.get(key)
        campaign_value = campaign_load_profile.get(key)
        if key == "extra_args":
            saved_value = [] if saved_value is None else saved_value
            campaign_value = [] if campaign_value is None else campaign_value
        if saved_value != campaign_value:
            raise RuntimeError(f"late recovery load profile differs from the original run: {key}")

    late_directory = directory / "late_recoveries" / target["run_id"]
    report_path = late_directory / "report.json"
    report_paths: list[Path] = []
    if late_directory.exists():
        if not report_path.is_file():
            raise RuntimeError("late recovery check exists without a final report; refusing to rerun load")
        attempt_directories = sorted(
            (path for path in late_directory.glob("attempt-*") if path.is_dir()),
            key=lambda path: path.name,
        )
        attempt_reports = [path / "report.json" for path in attempt_directories]
        if any(not path.is_file() for path in attempt_reports):
            raise RuntimeError("late recovery retry exists without a report; refusing to start another load")
        report_paths = [report_path, *attempt_reports]
        report_path = report_paths[-1]
        check_report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            check_report.get("source_run_id") != target["run_id"]
            or check_report.get("source_report_sha256") != source_report_sha256
        ):
            raise RuntimeError("late recovery check is incomplete or does not match its source")
        if not check_report.get("ended_at"):
            cleanup = cleanup_interrupted_late_recovery_load(check_report)
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "late_recovery_interrupted_attempt_cleanup_verified",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": target["cell_id"],
                "run_id": target["run_id"],
                "late_check_id": check_report.get("run_id"),
                "late_check_attempt": len(report_paths),
                "cleanup": cleanup,
                "late_check_report_path": str(report_path),
                "original_status_preserved": "aborted",
            })
            if prior_late_gate:
                raise RuntimeError(
                    "saved late recovery gate points to an incomplete later attempt"
                )
            if (
                check_report.get("late_operational_recovery_passed") is not None
                or (check_report.get("gates") or {}).get("late_operational_recovery")
            ):
                raise RuntimeError(
                    "late recovery observation already produced a gate; refusing to repeat it"
                )
            max_attempts = amendment["max_attempts"]
            if len(report_paths) >= max_attempts:
                raise RuntimeError("late recovery check retry limit reached")
            attempt_number = len(report_paths) + 1
            late_directory = late_directory / f"attempt-{attempt_number}"
            require_environment(config, directory)
            from experiment.eval.memory_hog.load_check import run_late_operational_recovery_check

            check_report = run_late_operational_recovery_check(
                target["run_id"],
                source_report_path,
                effective_spec_path,
                late_directory,
                attempt_number=attempt_number,
            )
            report_path = late_directory / "report.json"
        elif (
            check_report.get("load_handle")
            and (check_report.get("load_stopped") or {}).get("stopped") is not True
        ):
            cleanup = cleanup_interrupted_late_recovery_load(check_report)
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "late_recovery_completed_attempt_cleanup_verified",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": target["cell_id"],
                "run_id": target["run_id"],
                "late_check_id": check_report.get("run_id"),
                "cleanup": cleanup,
                "late_check_report_path": str(report_path),
            })
            raise RuntimeError(
                "late recovery check completed without recorded load cleanup; refusing to continue"
            )
    else:
        if prior_late_gate:
            raise RuntimeError("saved late recovery gate has no corresponding report")
        require_environment(config, directory)
        from experiment.eval.memory_hog.load_check import run_late_operational_recovery_check

        check_report = run_late_operational_recovery_check(
            target["run_id"],
            source_report_path,
            effective_spec_path,
            late_directory,
            attempt_number=1,
        )
        report_paths = [report_path]

    amendment_hash: str | None = None
    latency_limit_override: float | None = None
    if prior_late_gate:
        if not report_paths:
            raise RuntimeError("late recovery retry has no attempt history")
        prior_report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        if (
            prior_late_gate.get("late_check_report_path") != str(report_path)
            or prior_late_gate.get("late_check_report_sha256") != prior_report_sha256
        ):
            raise RuntimeError("saved late recovery gate does not match the latest check report")
        retry_kind = None
        if (
            len(report_paths) == 2
            and late_recovery_resource_telemetry_retry_authorized(
                check_report, prior_late_gate,
            )
        ):
            retry_kind = "cgroup_sampling"
        elif len(report_paths) == 3 and late_recovery_marginal_latency_retry_authorized(
            check_report,
            prior_late_gate,
            json.loads(source_report_path.read_text(encoding="utf-8")),
            amendment,
        ):
            retry_kind = "latency_threshold"
            latency_limit_override = amendment["latency_p95_limit_override_ms"]
        if retry_kind is None:
            raise RuntimeError(
                "late recovery retry is not covered by the scoped telemetry or latency amendment"
            )
        max_attempts = amendment["max_attempts"]
        if len(report_paths) >= max_attempts:
            raise RuntimeError("late recovery check retry limit reached")
        require_environment(config, directory)
        attempt_number = len(report_paths) + 1
        events_path = directory / "campaign_events.jsonl"
        if retry_kind == "latency_threshold":
            amendment_bytes = (HERE / LATE_RECOVERY_CHECK_FILE).read_bytes()
            amendment_hash = hashlib.sha256(amendment_bytes).hexdigest()
            prior_amendments = [
                event for event in read_ledger(events_path)
                if event.get("event") == "late_recovery_latency_amendment_applied"
            ]
            if any(event.get("amendment_sha256") != amendment_hash for event in prior_amendments):
                raise RuntimeError("campaign has a conflicting late recovery latency amendment")
            if not prior_amendments:
                append_ledger(events_path, {
                    "event": "late_recovery_latency_amendment_applied",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "version": amendment["version"],
                    "campaign_id": amendment["campaign_id"],
                    "amendment_sha256": amendment_hash,
                    "scope": amendment["scope"],
                    "target_run": target["run_id"],
                    "latency_p95_limit_override_ms": latency_limit_override,
                    "previous_latency_p95_limit_ms": check_report["gates"][
                        "late_operational_recovery"
                    ]["metric_checks"]["latency_ms"]["threshold"],
                    "same_load": True,
                    "same_baseline": True,
                    "timeout_seconds": amendment["timeout_seconds"],
                    "stable_seconds": amendment["stable_seconds"],
                    "other_thresholds_unchanged": True,
                    "fault_injection": False,
                    "original_run_status_preserved": "aborted",
                })
        append_ledger(events_path, {
            "event": (
                "late_recovery_cgroup_sampler_retry_authorized"
                if retry_kind == "cgroup_sampling"
                else "late_recovery_latency_threshold_retry_authorized"
            ),
            "at": datetime.now(timezone.utc).isoformat(),
            "cell_id": target["cell_id"],
            "run_id": target["run_id"],
            "prior_late_check_id": check_report.get("run_id"),
            "prior_report_path": str(report_path),
            "prior_report_sha256": prior_report_sha256,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "reason": (
                "only CPU and memory were no_data; exact-container cgroup-v2 sampling is now enabled"
                if retry_kind == "cgroup_sampling"
                else "the prior check missed 120 seconds only after one marginal latency window"
            ),
            "latency_p95_limit_override_ms": latency_limit_override,
            "amendment_sha256": amendment_hash,
            "other_thresholds_unchanged": True,
            "fault_injection": False,
        })
        late_directory = late_directory / f"attempt-{attempt_number}"
        from experiment.eval.memory_hog.load_check import run_late_operational_recovery_check

        check_report = run_late_operational_recovery_check(
            target["run_id"],
            source_report_path,
            effective_spec_path,
            late_directory,
            attempt_number=attempt_number,
            latency_p95_limit_override_ms=latency_limit_override,
        )
        report_path = late_directory / "report.json"

    check_gate = check_report.get("gates", {}).get("late_operational_recovery") or {}
    check_load_stopped = (
        bool(check_report.get("load_handle"))
        and (check_report.get("load_stopped") or {}).get("stopped") is True
    )
    late_gate = {
        **check_gate,
        "passed": (
            check_report.get("passed") is True
            and check_gate.get("passed") is True
            and check_load_stopped
        ),
        "late_check_id": check_report.get("run_id"),
        "late_check_attempt": (check_report.get("late_recovery_policy") or {}).get("attempt"),
        "late_check_amendment_version": (check_report.get("late_recovery_policy") or {}).get(
            "amendment_version"
        ),
        "late_check_amendment_sha256": amendment_hash,
        "latency_p95_limit_override_ms": (
            check_report.get("late_recovery_policy") or {}
        ).get("latency_p95_limit_override_ms"),
        "late_check_cleanup_passed": check_load_stopped,
        "late_check_load_stopped": check_load_stopped,
        "source_run_id": target["run_id"],
        "source_report_sha256": source_report_sha256,
        "late_check_report_path": str(report_path),
        "late_check_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    updated = {**entry, "gates": {**(entry.get("gates") or {}), "late_operational_recovery": late_gate}}
    append_ledger(directory / "ledger.jsonl", updated)
    append_ledger(directory / "campaign_events.jsonl", {
        "event": "late_operational_recovery_check_completed",
        "at": datetime.now(timezone.utc).isoformat(),
        "cell_id": target["cell_id"],
        "run_id": target["run_id"],
        "late_check_id": check_report.get("run_id"),
        "late_check_attempt": (check_report.get("late_recovery_policy") or {}).get("attempt"),
        "latency_p95_limit_override_ms": (
            check_report.get("late_recovery_policy") or {}
        ).get("latency_p95_limit_override_ms"),
        "amendment_sha256": amendment_hash,
        "passed": late_gate["passed"],
        "late_check_cleanup_passed": check_load_stopped,
        "source_report_sha256": source_report_sha256,
        "late_check_report_path": str(report_path),
        "late_check_report_sha256": late_gate["late_check_report_sha256"],
        "original_status_preserved": "aborted",
        "eligible_for_training": False,
    })
    return [*ledger, updated]


def load_cpu_mean80_revalidation_records(
    directory: Path,
) -> dict[tuple[str, int], dict[str, Any]]:
    path = directory / CPU_MEAN80_REVALIDATION_FILE
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for row in read_ledger(path):
        run_id = row.get("run_id")
        attempt = row.get("attempt")
        assessment = row.get("assessment")
        if (
            row.get("event") != "cpu_mean80_revalidation"
            or not isinstance(run_id, str)
            or type(attempt) is not int
            or not isinstance(assessment, dict)
            or assessment.get("version") != CPU_MEAN80_AMENDMENT_VERSION
        ):
            raise RuntimeError(f"invalid CPU mean revalidation record in {path}")
        key = (run_id, attempt)
        existing = records.get(key)
        if existing is not None and existing != assessment:
            raise RuntimeError(f"conflicting CPU mean revalidation records for {run_id}")
        records[key] = assessment
    return records


def attach_cpu_mean80_revalidation(
    entry: dict[str, Any], assessment: dict[str, Any],
) -> dict[str, Any]:
    existing = entry.get("cpu_mean80_revalidation")
    if existing is not None and existing != assessment:
        raise RuntimeError(
            f"conflicting in-ledger CPU mean revalidation for {entry.get('run_id')}"
        )
    return {**entry, "cpu_mean80_revalidation": assessment}


def record_cpu_mean80_revalidation(
    directory: Path, entry: dict[str, Any], assessment: dict[str, Any],
    campaign_id: str,
) -> None:
    path = directory / CPU_MEAN80_REVALIDATION_FILE
    run_id = entry.get("run_id")
    attempt = int(entry.get("attempt", 1))
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("CPU mean revalidation requires a run_id")
    existing = load_cpu_mean80_revalidation_records(directory).get((run_id, attempt))
    if existing is not None:
        if existing != assessment:
            raise RuntimeError(
                f"CPU mean revalidation differs from the saved result for {run_id}"
            )
        return
    ledger_path = directory / "ledger.jsonl"
    ledger_sha256 = (
        hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        if ledger_path.is_file() else None
    )
    append_ledger(path, {
        "event": "cpu_mean80_revalidation",
        "at": datetime.now(timezone.utc).isoformat(),
        "campaign_id": campaign_id,
        "cell_id": entry.get("cell_id"),
        "run_id": run_id,
        "attempt": attempt,
        "source_ledger_sha256": ledger_sha256,
        "assessment": assessment,
    })


def reconcile_exploratory_manifest(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> None:
    if not (config.get("knowledge_campaign") or {}).get("exploratory"):
        return
    from experiment.eval.memory_hog.rag_campaign import (
        reconcile_exploratory_test_runs,
    )

    reconciliation = reconcile_exploratory_test_runs(directory, config, ledger)
    if reconciliation:
        append_ledger(directory / "campaign_events.jsonl", {
            "event": "exploratory_manifest_reconciled",
            "at": datetime.now(timezone.utc).isoformat(),
            "previous_test_runs_started": reconciliation["previous"],
            "test_runs_started": reconciliation["reconciled"],
            "source": "unique_test_cell_attempt_run_id_entries_in_ledger",
        })


def _read_amendment_artifact(
    root: Path, artifact: dict[str, Any],
) -> dict[str, Any]:
    relative = artifact.get("path")
    expected_hash = artifact.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_hash, str):
        raise RuntimeError("late recovery amendment has an invalid artifact reference")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError("late recovery artifact escapes the repository") from exc
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise RuntimeError(f"late recovery evidence changed: {relative}")
    document = json.loads(content)
    if not isinstance(document, dict):
        raise RuntimeError(f"late recovery evidence is not an object: {relative}")
    return document


def _amendment_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("late recovery amendment has an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("late recovery amendment has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("late recovery amendment timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_late_recovery_amendment(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> tuple[dict[str, Any], str] | None:
    path = directory / LATE_RECOVERY_AMENDMENT_FILE
    if not path.exists():
        if any((row.get("gates") or {}).get("late_recovery") for row in ledger):
            raise RuntimeError("late recovery ledger entry has no amendment artifact")
        return None

    content = path.read_bytes()
    amendment = json.loads(content)
    amendment_hash = hashlib.sha256(content).hexdigest()
    target = amendment.get("target") or {}
    validation = amendment.get("validation") or {}
    evidence = amendment.get("evidence") or {}
    if (
        amendment.get("version") != LATE_RECOVERY_AMENDMENT_VERSION
        or amendment.get("campaign_id") != config.get("experiment_campaign_id")
        or amendment.get("approval") != "explicit_user_confirmation"
        or amendment.get("action") != "skip_aborted_cell_only"
        or amendment.get("thresholds_unchanged") is not True
        or amendment.get("fresh_baseline_for_next_run") is not True
        or target.get("cell_id") != "orders-container-kill-r1"
        or target.get("run_id") != "run-065ac29ef2ca"
        or target.get("attempt") != 1
        or target.get("status") != "aborted"
        or config.get("knowledge_campaign", {}).get("exploratory") is not True
    ):
        raise RuntimeError("late recovery amendment does not match the authorized RAG case")

    plan = {cell["cell_id"]: cell for cell in campaign_cells(config)}
    cell = plan.get(target["cell_id"])
    if not cell or cell.get("phase") != "test" or cell.get("chaos_type") != "container-kill":
        raise RuntimeError("late recovery amendment target is not the planned test cell")
    latest = {row["cell_id"]: row for row in ledger}
    entry = latest.get(target["cell_id"], {})
    gates = entry.get("gates") or {}
    if (
        entry.get("status") != "aborted"
        or entry.get("run_id") != target["run_id"]
        or entry.get("attempt", 1) != target["attempt"]
        or entry.get("reason") != "recovery not confirmed; campaign must stop before next service"
        or (gates.get("recovery") or {}).get("passed") is not False
        or (gates.get("operational_recovery") or {}).get("passed") is not False
        or (gates.get("cleanup") or {}).get("passed") is not True
        or (gates.get("load_stopped") or {}).get("stopped") is not True
    ):
        raise RuntimeError("late recovery amendment no longer matches the original aborted result")

    root = HERE.parents[2].resolve()
    original_path = (
        f"experiment/platform/data/object_store/{target['run_id']}/controlled_report.json"
    )
    load_id = validation.get("load_check_id")
    if load_id != "load-check-20261028T235524328934Z":
        raise RuntimeError("late recovery amendment references an unexpected load check")
    load_root = f"experiment/eval/memory_hog/out/load_checks/{load_id}"
    original = _read_amendment_artifact(
        root, {"path": original_path, "sha256": evidence.get("original_report_sha256")},
    )
    load_report = _read_amendment_artifact(
        root, {
            "path": f"{load_root}/report.json",
            "sha256": evidence.get("load_check_report_sha256"),
        },
    )
    load_controlled = _read_amendment_artifact(
        root, {
            "path": f"{load_root}/artifacts/{load_id}-orders/controlled_report.json",
            "sha256": evidence.get("orders_report_sha256"),
        },
    )
    if (
        original.get("status") != "aborted"
        or (original.get("gates") or {}).get("recovery", {}).get("passed") is not False
        or (original.get("gates") or {}).get("operational_recovery", {}).get("passed") is not False
        or load_report.get("passed") is not True
        or load_report.get("load_stopped", {}).get("stopped") is not True
        or load_report.get("injections_submitted") != 0
        or load_report.get("users") != 30
        or load_report.get("spawn_rate") != 5
        or load_report.get("stabilization_passed") is not True
    ):
        raise RuntimeError("late recovery source reports do not satisfy the amendment")

    original_times = original.get("timestamps") or {}
    exhausted = _amendment_time(
        (original_times.get("operational_recovery_exhausted") or {}).get("at")
    )
    load_start = _amendment_time(load_report.get("started_at"))
    load_end = _amendment_time(load_report.get("ended_at"))
    if load_start <= exhausted:
        raise RuntimeError("late recovery evidence must be separate from the original recovery window")

    phases = {item.get("phase"): item for item in load_controlled.get("observations", [])}
    stabilization = phases.get("stabilization_1") or {}
    baseline = phases.get("baseline") or {}
    stab_start = _amendment_time(stabilization.get("start"))
    stab_end = _amendment_time(stabilization.get("end"))
    baseline_start = _amendment_time(baseline.get("start"))
    baseline_end = _amendment_time(baseline.get("end"))
    window_start = _amendment_time(validation.get("window_start"))
    window_end = _amendment_time(validation.get("window_end"))
    stable_seconds = (window_end - window_start).total_seconds()
    policy = config["controlled_campaign"].get("resilience") or {}
    if (
        type(policy.get("stable_seconds")) is not int
        or stable_seconds < policy["stable_seconds"]
        or window_start < load_start
        or window_end > load_end
        or (stab_start - window_start).total_seconds() > 1
        or window_start > stab_end
        or window_end > baseline_end
        or (baseline_start - stab_end).total_seconds() > 60
        or (baseline_end - stab_start).total_seconds() < policy["stable_seconds"]
    ):
        raise RuntimeError("late recovery evidence does not establish a stable 120-second window")

    original_baseline = next(
        (
            item.get("summary", {})
            for item in original.get("observations", [])
            if item.get("phase") == "baseline"
        ),
        {},
    )
    thresholds: dict[str, float] = {}
    margins = {
        "cpu_cores": 0.05,
        "memory_bytes": 16 * 2**20,
        "errors_percent": 1.0,
        "latency_ms": 50.0,
    }
    for metric, margin in margins.items():
        baseline_metric = original_baseline.get(metric) or {}
        if baseline_metric.get("status") != "ok":
            raise RuntimeError(f"original baseline lacks {metric} for late recovery")
        thresholds[metric] = max(
            float(baseline_metric["p95"]) * 1.2,
            float(baseline_metric["p95"]) + margin,
        )
    baseline_traffic = original_baseline.get("traffic") or {}
    if baseline_traffic.get("status") != "ok":
        raise RuntimeError("original baseline lacks traffic for late recovery")
    thresholds["traffic_min"] = float(baseline_traffic["mean"]) * 0.8

    grafana = validation.get("grafana") or {}
    timestamps = grafana.get("sample_unix_seconds") or []
    latency = grafana.get("latency_p95_ms") or []
    traffic = grafana.get("traffic_rps") or []
    errors = grafana.get("errors_5xx_percent") or []
    if not (
        len(timestamps) == len(latency) == len(traffic) == len(errors) == 9
        and all(type(value) in (int, float) for values in (timestamps, latency, traffic, errors) for value in values)
        and all(left < right and right - left <= 60 for left, right in zip(timestamps, timestamps[1:]))
        and abs(timestamps[0] - window_start.timestamp()) <= 1
        and abs(timestamps[-1] - window_end.timestamp()) <= 1
        and max(latency) <= thresholds["latency_ms"]
        and min(traffic) >= thresholds["traffic_min"]
        and max(errors) <= thresholds["errors_percent"]
    ):
        raise RuntimeError("Grafana evidence does not meet the original recovery thresholds")

    resource_p95: dict[str, float] = {}
    for metric in ("cpu_cores", "memory_bytes"):
        observed = []
        for phase in (stabilization, baseline):
            summary = phase.get("summary", {}).get(metric) or {}
            if summary.get("status") != "ok" or summary.get("samples", 0) < 4:
                raise RuntimeError(f"late recovery load report lacks {metric} samples")
            observed.append(float(summary["p95"]))
        resource_p95[metric] = max(observed)
        if resource_p95[metric] > thresholds[metric]:
            raise RuntimeError(f"late recovery {metric} exceeds its original threshold")
    for phase in (stabilization, baseline):
        ready = phase.get("summary", {}).get("ready") or {}
        if (
            ready.get("status") != "ok"
            or ready.get("samples", 0) < 4
            or ready.get("mean") != 1
            or ready.get("p95") != 1
        ):
            raise RuntimeError("late recovery readiness was not continuously healthy")

    claimed_thresholds = validation.get("thresholds") or {}
    if any(
        not math.isclose(float(claimed_thresholds.get(key, float("nan"))), value, rel_tol=1e-9)
        for key, value in thresholds.items()
    ):
        raise RuntimeError("late recovery amendment thresholds differ from the original baseline")
    return amendment, amendment_hash


def apply_late_recovery_amendment(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validated = validate_late_recovery_amendment(directory, config, ledger)
    if validated is None:
        return ledger
    amendment, amendment_hash = validated
    target = amendment["target"]
    latest = {row["cell_id"]: row for row in ledger}
    original = latest[target["cell_id"]]
    existing = (original.get("gates") or {}).get("late_recovery")
    if existing:
        if (
            existing.get("amendment_sha256") != amendment_hash
            or not late_recovery_authorized(config, original)
        ):
            raise RuntimeError("late recovery ledger entry conflicts with its amendment")
        return ledger

    validation = amendment["validation"]
    gate = {
        "version": LATE_RECOVERY_AMENDMENT_VERSION,
        "passed": True,
        "user_approved": True,
        "skip_only": True,
        "original_status_preserved": True,
        "thresholds_unchanged": True,
        "fresh_baseline_for_next_run": True,
        "amendment_sha256": amendment_hash,
        "load_check_id": validation["load_check_id"],
        "window_seconds": (
            _amendment_time(validation["window_end"])
            - _amendment_time(validation["window_start"])
        ).total_seconds(),
    }
    gates = {**(original.get("gates") or {}), "late_recovery": gate}
    event_path = directory / "campaign_events.jsonl"
    events = read_ledger(event_path)
    if not any(
        event.get("event") == "late_recovery_amendment_applied"
        and event.get("amendment_sha256") == amendment_hash
        for event in events
    ):
        append_ledger(event_path, {
            "event": "late_recovery_amendment_applied",
            "at": datetime.now(timezone.utc).isoformat(),
            "version": LATE_RECOVERY_AMENDMENT_VERSION,
            "cell_id": target["cell_id"],
            "run_id": target["run_id"],
            "amendment_sha256": amendment_hash,
            "original_status_preserved": "aborted",
            "original_operational_recovery_passed": False,
            "action": "skip this cell; continue only with fresh per-run gates",
        })
    append_ledger(directory / "ledger.jsonl", {**original, "gates": gates})
    return read_ledger(directory / "ledger.jsonl")


def validate_unsubmitted_abort_amendment(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> tuple[dict[str, Any], str] | None:
    path = directory / UNSUBMITTED_ABORT_AMENDMENT_FILE
    if not path.exists():
        if any((row.get("gates") or {}).get("unsubmitted_abort_skip") for row in ledger):
            raise RuntimeError("unsubmitted-abort ledger entry has no amendment artifact")
        return None

    content = path.read_bytes()
    amendment = json.loads(content)
    amendment_hash = hashlib.sha256(content).hexdigest()
    target = amendment.get("target") or {}
    if (
        amendment.get("version") != UNSUBMITTED_ABORT_AMENDMENT_VERSION
        or amendment.get("campaign_id") != config.get("experiment_campaign_id")
        or amendment.get("approval") != "explicit_user_confirmation"
        or amendment.get("action") != "skip_unsubmitted_aborted_cell_only"
        or amendment.get("thresholds_unchanged") is not True
        or amendment.get("fresh_baseline_for_next_run") is not True
        or target != {
            "cell_id": "front-end-network-latency-r1",
            "run_id": "run-4dbd8ae472af",
            "attempt": 1,
            "status": "aborted",
            "submitted": False,
        }
        or config.get("knowledge_campaign", {}).get("exploratory") is not True
    ):
        raise RuntimeError("unsubmitted-abort amendment does not match the authorized RAG case")

    plan = {cell["cell_id"]: cell for cell in campaign_cells(config)}
    cell = plan.get(target["cell_id"])
    if (
        not cell or cell.get("phase") != "test"
        or cell.get("service") != "front-end"
        or cell.get("chaos_type") != "network-latency"
    ):
        raise RuntimeError("unsubmitted-abort amendment target is not the planned test cell")
    latest = {row["cell_id"]: row for row in ledger}
    entry = latest.get(target["cell_id"], {})
    if (
        entry.get("status") != "aborted"
        or entry.get("run_id") != target["run_id"]
        or entry.get("attempt", 1) != target["attempt"]
        or entry.get("reason") != "application unhealthy after blocked attempt; stop campaign"
        or (entry.get("gates") or {}).get("submitted") is not False
        or (entry.get("gates") or {}).get("cleanup", {}).get("passed") is not True
        or (entry.get("gates") or {}).get("load_stopped", {}).get("stopped") is not True
    ):
        raise RuntimeError("unsubmitted-abort amendment no longer matches the original aborted result")

    root = HERE.parents[2].resolve()
    expected_artifacts = {
        "run": (
            f"experiment/eval/memory_hog/out/exploratory_campaigns/"
            f"{config['experiment_campaign_id']}/attempts/{target['cell_id']}/run.json"
        ),
        "controlled_report": (
            f"experiment/platform/data/object_store/{target['run_id']}/controlled_report.json"
        ),
        "pods_after": f"experiment/platform/data/object_store/{target['run_id']}/pods_after.json",
        "kubernetes_events": (
            f"experiment/platform/data/object_store/{target['run_id']}/kubernetes_events.json"
        ),
    }
    evidence = amendment.get("evidence") or {}
    if set(evidence) != set(expected_artifacts):
        raise RuntimeError("unsubmitted-abort amendment has an incomplete evidence set")
    artifacts = {
        name: _read_amendment_artifact(
            root, {"path": expected_path, "sha256": evidence[name].get("sha256")},
        )
        for name, expected_path in expected_artifacts.items()
        if evidence[name].get("path") == expected_path
    }
    if len(artifacts) != len(expected_artifacts):
        raise RuntimeError("unsubmitted-abort amendment references unexpected evidence")
    report = artifacts["controlled_report"]
    report_gates = report.get("gates") or {}
    run_summary = (artifacts["run"].get("summary") or {})
    if (
        artifacts["run"].get("run_id") != target["run_id"]
        or artifacts["run"].get("status") != "failed"
        or run_summary.get("controlled_status") != "aborted"
        or (run_summary.get("controlled_gates") or {}).get("submitted") is not False
        or report.get("status") != "aborted"
        or report_gates.get("submitted") is not False
        or report_gates.get("intervention_confirmed_on_target") is not None
        or (report_gates.get("cleanup") or {}).get("passed") is not True
        or (report_gates.get("load_stopped") or {}).get("stopped") is not True
    ):
        raise RuntimeError("unsubmitted-abort source reports do not prove an unsubmitted run")
    return amendment, amendment_hash


def apply_unsubmitted_abort_amendment(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validated = validate_unsubmitted_abort_amendment(directory, config, ledger)
    if validated is None:
        return ledger
    amendment, amendment_hash = validated
    target = amendment["target"]
    latest = {row["cell_id"]: row for row in ledger}
    original = latest[target["cell_id"]]
    existing = (original.get("gates") or {}).get("unsubmitted_abort_skip")
    if existing:
        if (
            existing.get("amendment_sha256") != amendment_hash
            or not unsubmitted_abort_skip_authorized(config, original)
        ):
            raise RuntimeError("unsubmitted-abort ledger entry conflicts with its amendment")
        return ledger

    gate = {
        "version": UNSUBMITTED_ABORT_AMENDMENT_VERSION,
        "passed": True,
        "user_approved": True,
        "skip_only": True,
        "original_status_preserved": True,
        "thresholds_unchanged": True,
        "fresh_baseline_for_next_run": True,
        "amendment_sha256": amendment_hash,
    }
    gates = {**(original.get("gates") or {}), "unsubmitted_abort_skip": gate}
    event_path = directory / "campaign_events.jsonl"
    events = read_ledger(event_path)
    if not any(
        event.get("event") == "unsubmitted_abort_amendment_applied"
        and event.get("amendment_sha256") == amendment_hash
        for event in events
    ):
        append_ledger(event_path, {
            "event": "unsubmitted_abort_amendment_applied",
            "at": datetime.now(timezone.utc).isoformat(),
            "version": UNSUBMITTED_ABORT_AMENDMENT_VERSION,
            "cell_id": target["cell_id"],
            "run_id": target["run_id"],
            "amendment_sha256": amendment_hash,
            "original_status_preserved": "aborted",
            "injection_submitted": False,
            "action": "skip this cell; continue only after fresh per-run gates",
        })
    append_ledger(directory / "ledger.jsonl", {**original, "gates": gates})
    return read_ledger(directory / "ledger.jsonl")


def export_runs(directory: Path, cells: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> None:
    latest = {entry["cell_id"]: entry for entry in ledger}
    fields = ["phase", "chaos_type", "service", "replica_idx", "run_id", "status", "notes"]
    with (directory / "runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for cell in cells:
            entry = latest.get(cell["cell_id"], {})
            writer.writerow({
                "phase": cell.get("phase", "test"), **{key: cell[key] for key in ("chaos_type", "service", "replica_idx")},
                "run_id": entry.get("run_id", ""), "status": entry.get("status", "not_run"),
                "notes": entry.get("reason", ""),
            })


def require_controlled_backend(api: Any) -> None:
    try:
        capabilities = api.get("/api/controlled-campaign/capabilities")
    except RuntimeError as exc:
        raise RuntimeError(
            "controlled backend preflight failed; the API must be restarted with "
            "controlled-campaign support. No new run was submitted."
        ) from exc
    version = capabilities.get("protocol_version") if isinstance(capabilities, dict) else None
    if (
        not isinstance(version, int) or isinstance(version, bool)
        or version != CONTROLLED_CAMPAIGN_PROTOCOL_VERSION
    ):
        raise RuntimeError(
            "controlled backend protocol mismatch: "
            f"expected {CONTROLLED_CAMPAIGN_PROTOCOL_VERSION}, got {version!r}. "
            "No new run was submitted."
        )


def require_scrape_metadata_tool(config: dict[str, Any], cell: dict[str, Any]) -> None:
    probe_spec = runner.build_spec(
        config, cell["service"], cell["replica_idx"], "container-kill",
    )
    metrics = probe_spec.get("observability", {}).get("metrics", {})
    sse_url = metrics.get("mcp_sse_url") if isinstance(metrics, dict) else None
    if not isinstance(sse_url, str) or not sse_url:
        raise RuntimeError("campaign config has no metrics MCP URL; no run was submitted")
    tools = MCPToolClient(
        sse_url=sse_url,
        timeout_seconds=int(metrics.get("timeout_seconds", 10)),
    ).list_tools()
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise RuntimeError("observability MCP returned malformed tool metadata; no run was submitted")
    tool_names = {tool.get("name") for tool in tools}
    if "prometheus_scrape_targets" not in tool_names:
        raise RuntimeError(
            "observability MCP is missing required tool 'prometheus_scrape_targets'; "
            "deploy/restart the MCP observability server before running the campaign. "
            "No run was submitted."
        )


def environment_preflight(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only infrastructure checks; never a substitute for loaded run gates."""
    namespace = config["cluster"]["app_namespace"]
    cells = campaign_cells(config)
    if not cells:
        raise ValueError("campaign preflight requires at least one planned cell")
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(), "checks": checks,
        "load_profile": {"users": config["load_profile"]["users"], "spawn_rate": config["load_profile"]["spawn_rate"]},
        "limitations": [
            "No load or fault submitted. Idle readiness does not prove stability at 30 users.",
            "CPU headroom, memory safety, telemetry coverage and recovery still require per-run gates.",
            "Trace API health does not prove fault-window trace coverage.",
        ],
    }

    def check(name: str, operation: Callable[[], Any]) -> None:
        try:
            evidence = operation()
        except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
            checks.append({"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            checks.append({"name": name, "passed": True, "evidence": evidence})

    def ready_nodes():
        nodes = kubectl("get", "nodes", "-o", "json")["items"]
        if not nodes:
            raise GateBlocked("no Kubernetes nodes")
        for node in nodes:
            conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
            if conditions.get("Ready") != "True" or any(
                conditions.get(key) != "False" for key in ("MemoryPressure", "DiskPressure", "PIDPressure")
            ):
                raise GateBlocked(f"node unhealthy or pressure unknown: {node['metadata']['name']}")
        return [{
            "name": node["metadata"]["name"], "uid": node["metadata"]["uid"],
            "allocatable": node["status"]["allocatable"], "nodeInfo": node["status"]["nodeInfo"],
        } for node in nodes]

    check("nodes", ready_nodes)

    def ready_deployments():
        deployments = kubectl("-n", namespace, "get", "deployments", "-o", "json")["items"]
        by_name = {d["metadata"]["name"]: d for d in deployments}
        if not set(SERVICES).issubset(by_name):
            raise GateBlocked("application deployments missing")
        for deployment in deployments:
            status, metadata = deployment.get("status", {}), deployment["metadata"]
            replicas = deployment["spec"].get("replicas", 1)
            if (
                replicas < 1 or status.get("readyReplicas", 0) != replicas
                or status.get("updatedReplicas", 0) != replicas
                or status.get("observedGeneration", 0) < metadata["generation"]
            ):
                raise GateBlocked(f"deployment not fully ready/current: {metadata['name']}")
        return {
            name: [{
                "name": container["name"], "image": container["image"],
                "resources": container.get("resources", {}),
                "probes": {
                    key: container.get(key)
                    for key in ("startupProbe", "livenessProbe", "readinessProbe")
                },
                "runtime_env": [env for env in container.get("env", [])
                                if env["name"] in {
                                    "JAVA_OPTS", "JAVA_TOOL_OPTIONS", "OTEL_EXPORTER_OTLP_ENDPOINT",
                                    "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_TRACES_EXPORTER",
                                    "OTEL_METRICS_EXPORTER", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG",
                                }],
            } for container in deployment["spec"]["template"]["spec"]["containers"]]
            for name, deployment in by_name.items()
        }

    check("application_and_dependencies", ready_deployments)
    for service in sorted({cell["service"] for cell in cells}):
        def target_check(service=service):
            pods = kubectl("-n", namespace, "get", "pods", "-l", f"name={service}", "-o", "json")["items"]
            if len(pods) != 1 or pods[0]["metadata"].get("deletionTimestamp"):
                raise GateBlocked("exactly one non-terminating target pod required")
            pod = pods[0]
            containers = [c for c in pod["spec"]["containers"] if c["name"] == service]
            if len(containers) != 1:
                raise GateBlocked("exact application container missing or ambiguous")
            container = containers[0]
            statuses = {c["name"]: c for c in pod["status"]["containerStatuses"]}
            if not statuses[service].get("ready") or not statuses.get("istio-proxy", {}).get("ready"):
                raise GateBlocked("application and Istio sidecar must both be ready")
            limits = container.get("resources", {}).get("limits", {})
            cpu = _parse_quantity(limits.get("cpu"), _CPU_SUFFIXES, "CPU")
            memory = _parse_quantity(limits.get("memory"), _MEMORY_SUFFIXES, "memory")
            result = runner._kubectl("-n", namespace, "exec", pod["metadata"]["name"],
                                     "-c", service, "--", "sh", "-c", CGROUP_READ_COMMAND)
            if result.returncode:
                raise GateBlocked(f"cannot read application cgroup: {result.stderr.strip()}")
            observed = parse_cgroup_v2(result.stdout)
            if not math.isclose(observed["cpu_quota_cores"], cpu, rel_tol=.02) or not math.isclose(
                observed["memory_limit_bytes"], memory, rel_tol=.02,
            ):
                raise GateBlocked("live cgroup limits differ from deployment limits")
            return {
                "pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                "container_id": statuses[service].get("containerID"),
                "image_id": statuses[service].get("imageID"),
                "resources": container["resources"], "cgroup": observed,
                "java_opts": [e for e in container.get("env", []) if e["name"] == "JAVA_OPTS"],
            }
        check(f"target:{service}", target_check)
        if any(c["service"] == service and c["chaos_type"] == "cpu-hog" for c in cells):
            check(f"cpu_profile:{service}", lambda service=service: validate_cpu_profile(
                config["controlled_campaign"]["calibrations"].get(service, {}),
            ))
        if any(c["service"] == service and c["chaos_type"] == "network-latency" for c in cells):
            def network_check(service=service):
                calibration = config["controlled_campaign"]["calibrations"].get(service, {})
                interface = calibration.get("interface", "eth0")
                if not isinstance(interface, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
                    raise GateBlocked("invalid network interface")
                modules = runner._kubectl("-n", namespace, "exec", f"deploy/{service}",
                                          "-c", service, "--", "cat", "/proc/modules")
                if modules.returncode or not any(line.startswith("sch_netem ") for line in modules.stdout.splitlines()):
                    raise GateBlocked("target node sch_netem not confirmed; no automatic modprobe")
                qdisc = kubectl("-n", namespace, "exec", f"deploy/{service}", "-c", "istio-proxy",
                                "--", "tc", "-j", "qdisc", "show", "dev", interface)
                if not isinstance(qdisc, list) or any(
                    not isinstance(row, dict) or row.get("kind") == "netem" for row in qdisc
                ):
                    raise GateBlocked("unknown qdisc state or pre-existing netem")
                return {"interface": interface, "qdisc": qdisc, "sch_netem_loaded": True}
            check(f"network:{service}", network_check)

    def templates():
        workflows = kubectl("-n", "litmus", "get", "workflows.argoproj.io", "-o", "json")["items"]
        by_name = {w["metadata"]["name"]: w for w in workflows}
        verified = {}
        for service, fault in sorted({(c["service"], c["chaos_type"]) for c in cells}):
            if fault in {"cpu-hog", "memory-hog", "network-latency"}:
                name = f"{service}-{fault}"
                if name not in by_name:
                    raise GateBlocked(f"missing registered template {name}; no automatic priming")
                verified[name] = validate_workflow_template(by_name[name], namespace, service, fault)
        return verified

    check("reviewed_templates", templates)

    def telemetry():
        spec = runner.build_spec(config, cells[0]["service"], 0, "container-kill")
        metrics = spec["observability"]["metrics"]
        client = MCPToolClient(metrics["mcp_sse_url"], timeout_seconds=30)
        health = client.call_tool_json("health_check")
        if not isinstance(health, dict) or any(
            health.get("services", {}).get(name) != "ok" for name in ("prometheus", "loki", "tempo")
        ):
            raise GateBlocked(f"observability unavailable: {health}")
        return health

    check("observability_backends", telemetry)

    historical = config.get("historical_observability") or {}
    if historical.get("required"):
        def historical_telemetry():
            url = historical.get("mcp_sse_url")
            timeout = historical.get("timeout_seconds", 30)
            if not isinstance(url, str) or not url:
                raise GateBlocked("historical MCP URL is required")
            if type(timeout) is not int or timeout < 1:
                raise GateBlocked("historical MCP timeout must be a positive integer")
            client = MCPToolClient(url, timeout_seconds=timeout)
            tools = {item.get("name") for item in client.list_tools()}
            if "health_check" not in tools or "get_run_golden_signals" not in tools:
                raise GateBlocked("historical MCP is missing required RCA tools")
            health = client.call_tool_json("health_check")
            if not isinstance(health, dict) or any(
                health.get("services", {}).get(name) != "ok"
                for name in ("prometheus", "loki", "tempo")
            ):
                raise GateBlocked(f"historical observability unavailable: {health}")
            return {
                "status": health.get("status"),
                "services": health.get("services"),
                "mcp_sse_url": url,
            }

        check("historical_observability", historical_telemetry)

    def telemetry_readiness():
        endpoints = {
            "prometheus": ("prometheus-v3:9090", "-/ready"),
            "loki": ("loki:3100", "ready"),
            "tempo": ("tempo:3200", "ready"),
            "collector": ("otel-collector:13133", ""),
        }
        evidence = {}
        for name, (service, path) in endpoints.items():
            result = runner._kubectl("get", "--raw",
                                     f"/api/v1/namespaces/monitoring/services/{service}/proxy/{path}")
            if result.returncode:
                raise GateBlocked(f"{name} not ready: {result.stderr.strip() or result.stdout.strip()}")
            evidence[name] = result.stdout.strip()
        return evidence

    check("telemetry_readiness", telemetry_readiness)

    def http_conflicts():
        existing = kubectl("-n", namespace, "get", "virtualservices", "-o", "json")["items"]
        from fnmatch import fnmatchcase
        for service in {c["service"] for c in cells if c["chaos_type"] == "http-status-code"}:
            aliases = (service, f"{service}.{namespace}", f"{service}.{namespace}.svc",
                       f"{service}.{namespace}.svc.cluster.local")
            if any(fnmatchcase(alias, host) for item in existing
                   for host in item.get("spec", {}).get("hosts", []) for alias in aliases):
                raise GateBlocked(f"existing VirtualService conflicts with target {service}")
        return {"conflicts": 0}

    check("http_isolation", http_conflicts)

    def load_isolation():
        result = runner._kubectl(
            "-n", "loadtest", "exec", "deploy/locust-web", "-c", "locust", "--", "python", "-c",
            """import json, pathlib, urllib.request
active = []
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        argv = path.read_bytes().split(bytes([0]))
    except FileNotFoundError:
        continue
    if any(b'locust' in arg for arg in argv[:2]) and b'--headless' in argv:
        active.append(path.parent.name)
with urllib.request.urlopen('http://127.0.0.1:8089/stats/requests', timeout=5) as response:
    web = json.load(response)
print(json.dumps({'headless_processes': active, 'web_user_count': web['user_count']}))
""",
        )
        if result.returncode:
            raise GateBlocked(f"cannot verify remote load isolation: {result.stderr.strip()}")
        state = json.loads(result.stdout)
        if not isinstance(state, dict) or not isinstance(state.get("headless_processes"), list) or type(
            state.get("web_user_count")
        ) is not int:
            raise GateBlocked("malformed remote load-isolation response")
        if state["headless_processes"] or state["web_user_count"]:
            raise GateBlocked(f"Locust load already active: {state}; investigate before loading")
        return state

    check("load_isolation", load_isolation)
    report["environment_profile"] = {
        item["name"]: {
            key: item["evidence"][key] for key in ("image_id", "resources", "java_opts")
        }
        for item in checks if item["passed"] and item["name"].startswith("target:")
    }
    report["environment_profile"]["deployments"] = next(
        (item["evidence"] for item in checks if item["name"] == "application_and_dependencies" and item["passed"]),
        {},
    )
    report["passed"] = all(item["passed"] for item in checks)
    return report


def require_environment(config: dict[str, Any], directory: Path) -> None:
    api = runner.ApiClient(config["platform"]["api_base"], role=config["platform"]["user_role"])
    active = [r["run_id"] for r in api.get("/api/runs")
              if r["status"] in {"pending", "running", "pending_approval"}]
    if active:
        raise RuntimeError(f"another run became active before submission: {active}")
    if api.get("/api/llm/health").get("ok") is not True:
        raise RuntimeError("LLM unavailable; evaluation cannot complete")
    report = environment_preflight(config)
    previous = [entry for entry in read_ledger(directory / "preflight.jsonl") if entry.get("passed")]
    if previous and report["environment_profile"] != previous[0]["environment_profile"]:
        report["passed"] = False
        report["checks"].append({
            "name": "immutable_environment", "passed": False,
            "error": "application image/resources/JAVA_OPTS/probes changed or probe baseline missing; "
                     "start a new campaign, do not resume",
        })
    append_ledger(directory / "preflight.jsonl", report)
    if not report["passed"]:
        failures = "; ".join(f"{c['name']}: {c['error']}" for c in report["checks"] if not c["passed"])
        raise RuntimeError(f"environment preflight failed; no new run submitted: {failures}")


def execute(
    directory: Path, *, resume: bool, retry_blocked: set[str] | None = None,
    retry_unconfirmed_container_kill: set[str] | None = None,
    retry_invalid_window: set[str] | None = None,
    retry_reviewed_container_kill: set[str] | None = None,
    stop_after_first_test: bool = False,
    through_train_count: int | None = None,
    continue_after_technical_failure: bool = False,
    continue_after_safe_blocked_cell: bool = False,
    cpu_calibration_path: Path | None = None,
) -> int:
    with (directory / ".campaign.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another executor holds this campaign; refusing concurrent execution") from exc
        try:
            return _execute(
                directory, resume=resume, retry_blocked=retry_blocked,
                retry_unconfirmed_container_kill=retry_unconfirmed_container_kill,
                retry_invalid_window=retry_invalid_window,
                retry_reviewed_container_kill=retry_reviewed_container_kill,
                stop_after_first_test=stop_after_first_test,
                through_train_count=through_train_count,
                continue_after_technical_failure=continue_after_technical_failure,
                continue_after_safe_blocked_cell=continue_after_safe_blocked_cell,
                cpu_calibration_path=cpu_calibration_path,
            )
        except Exception as exc:
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "execution_interrupted",
                "at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise


def _execute(
    directory: Path,
    *,
    resume: bool,
    retry_blocked: set[str] | None = None,
    retry_unconfirmed_container_kill: set[str] | None = None,
    retry_invalid_window: set[str] | None = None,
    retry_reviewed_container_kill: set[str] | None = None,
    stop_after_first_test: bool = False,
    through_train_count: int | None = None,
    continue_after_technical_failure: bool = False,
    continue_after_safe_blocked_cell: bool = False,
    cpu_calibration_path: Path | None = None,
) -> int:
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    apply_passive_recovery_extension_amendment(directory, config)
    cpu_mean80_amendment = load_cpu_mean80_amendment()
    cpu_mean80_active = (
        config.get("experiment_campaign_id") == cpu_mean80_amendment["campaign_id"]
    )
    pre_intervention_policy = pre_intervention_retry_policy(config)
    cells = campaign_cells(config)
    train_only = (
        (config.get("controlled_campaign") or {}).get("level") == "train-only"
    )
    if train_only:
        validate_train_only_plan(config, cells)
        if (
            type(through_train_count) is not int
            or through_train_count not in TRAIN_ONLY_TRANCHES
        ):
            raise RuntimeError(
                f"train-only execution requires an explicitly approved tranche "
                f"from {TRAIN_ONLY_TRANCHES}"
            )
        if stop_after_first_test:
            raise RuntimeError("train-only campaigns do not contain a test split")
    elif through_train_count is not None:
        raise RuntimeError("--through-train-count is valid only for train-only campaigns")
    if continue_after_technical_failure:
        if (
            not resume
            or not train_only
            or through_train_count != len(cells)
            or cpu_calibration_path is None
        ):
            raise RuntimeError(
                "technical-failure continuation requires a full train-only resume "
                "and an explicit amended CPU calibration"
            )
        amendment = apply_train_continuation_calibration(config, cpu_calibration_path)
        record_train_continuation_authorization(directory, amendment)
    elif cpu_calibration_path is not None:
        raise RuntimeError("CPU calibration override requires technical-failure continuation")
    if continue_after_safe_blocked_cell:
        if (
            not resume
            or not train_only
            or through_train_count != len(cells)
            or not continue_after_technical_failure
        ):
            raise RuntimeError(
                "safe blocked-cell continuation requires a full train-only resume "
                "with technical-failure continuation enabled"
            )
        record_clean_pre_intervention_block_skip_authorization(directory)
    rag = config.get("knowledge_campaign")
    if rag:
        from experiment.platform.backend.rca_knowledge import VERSION, digest, write_once

        if rag["version"] != VERSION:
            raise RuntimeError("saved knowledge campaign version differs from executor")
        expected = build_plan("rag", config["ordering"]["seed"])
        if rag.get("exploratory"):
            expected_train = [cell for cell in expected if cell["phase"] == "train"]
            expected_test = [cell for cell in expected if cell["phase"] == "test"]
            if (
                config["train_experiments"] != expected_train
                or (
                    config["test_experiments"] != expected_test
                    and not (
                        len(config["test_experiments"]) == 1
                        and config["test_experiments"][0] in expected_test
                    )
                )
            ):
                raise RuntimeError(
                    "exploratory campaign must inherit the 35 training cells and select "
                    "either one planned pilot cell or the full 35-cell test split"
                )
        elif cells != expected:
            raise RuntimeError("RAG campaign must preserve the exact 70-run train/test matrix")
        write_once(directory / "rag_protocol.json", {
            "version": VERSION, "config_sha256": digest(config),
        })
    plan_path = directory / "plan.json"
    if plan_path.exists() and cells != json.loads(plan_path.read_text(encoding="utf-8")):
        raise RuntimeError("campaign matrix differs from saved plan; cannot resume")
    validation_cell = (config.get("continuation") or {}).get("validation_cell")
    if config.get("continuation") and (
        not isinstance(validation_cell, str)
        or not config["test_experiments"]
        or config["test_experiments"][0]["cell_id"] != validation_cell
    ):
        raise RuntimeError("continuation must preserve its validation cell at the start of the plan")
    ledger_path = directory / "ledger.jsonl"
    ledger = read_ledger(ledger_path)
    ledger = apply_late_recovery_amendment(directory, config, ledger)
    ledger = apply_unsubmitted_abort_amendment(directory, config, ledger)
    reconcile_exploratory_manifest(directory, config, ledger)
    latest = {entry["cell_id"]: entry for entry in ledger}
    if rag:
        validate_rag_reviewed_retry_protocol(directory, config, cells, ledger)
    retry_blocked = retry_blocked or set()
    retry_unconfirmed_container_kill = retry_unconfirmed_container_kill or set()
    retry_invalid_window = retry_invalid_window or set()
    retry_reviewed_container_kill = retry_reviewed_container_kill or set()
    retry_sets = (
        retry_blocked, retry_unconfirmed_container_kill,
        retry_invalid_window, retry_reviewed_container_kill,
    )
    if any(
        left & right
        for index, left in enumerate(retry_sets)
        for right in retry_sets[index + 1:]
    ):
        raise RuntimeError("a cell cannot use multiple retry modes")
    planned_cell_ids = {cell["cell_id"] for cell in cells}
    cells_by_id = {cell["cell_id"]: cell for cell in cells}
    requested_retries = set().union(*retry_sets)
    if rag and (
        retry_blocked or retry_unconfirmed_container_kill or retry_invalid_window
    ):
        raise RuntimeError(
            "RAG protocol allows only an explicitly reviewed training container-kill replacement"
        )
    if (
        rag
        and retry_reviewed_container_kill
        and len(retry_reviewed_container_kill) != 1
    ):
        raise RuntimeError(
            "RAG reviewed retry amendment permits one replacement cell only"
        )
    unknown_retries = requested_retries - planned_cell_ids
    if unknown_retries:
        raise RuntimeError(
            f"retry cells are not in the immutable campaign plan: {sorted(unknown_retries)}"
        )
    for cell_id in retry_blocked:
        previous = latest.get(cell_id, {})
        if not clean_unsubmitted_block(previous):
            raise RuntimeError(
                f"blocked retry requires no submission, confirmed cleanup and stopped load: {cell_id}"
            )
    invalid_window_guards = {}
    for cell_id in retry_invalid_window:
        previous = latest.get(cell_id, {})
        invalid_window_guards[cell_id] = overlapping_observation_window_retry_guard(
            directory, cells_by_id[cell_id], previous,
        )
    reviewed_container_kill_guards = {}
    for cell_id in retry_reviewed_container_kill:
        previous = latest.get(cell_id, {})
        if rag and (
            cells_by_id[cell_id].get("phase") != "train"
            or int(previous.get("attempt", 1)) != 1
        ):
            raise RuntimeError(
                "RAG reviewed retry can replace only an aborted first-attempt training cell"
            )
        reviewed_container_kill_guards[cell_id] = reviewed_container_kill_retry_guard(
            directory, cells_by_id[cell_id], previous,
        )
    if rag and retry_reviewed_container_kill:
        cell_id = next(iter(retry_reviewed_container_kill))
        protocol = authorize_rag_reviewed_retry(
            directory, config, cells, ledger, cells_by_id[cell_id], latest[cell_id],
        )
        reviewed_container_kill_guards[cell_id]["protocol_amendment"] = (
            protocol["version"]
        )
    retry_guards = {}
    for cell_id in retry_unconfirmed_container_kill:
        previous = latest.get(cell_id, {})
        retry_guards[cell_id] = unconfirmed_container_kill_retry_guard(
            directory, cells_by_id[cell_id], previous,
        )
    if requested_retries and not resume:
        raise RuntimeError("campaign retries require --resume")
    if resume:
        ledger = apply_late_operational_recovery_check(directory, config, ledger)
        latest = {entry["cell_id"]: entry for entry in ledger}
    if cpu_mean80_active:
        saved_revalidations = load_cpu_mean80_revalidation_records(directory)
        for cell in cells:
            entry = latest.get(cell["cell_id"], {})
            if entry.get("status") != "completed" or cell.get("chaos_type") != "cpu-hog":
                continue
            key = (entry.get("run_id"), int(entry.get("attempt", 1)))
            assessment = saved_revalidations.get(key)
            if assessment is None:
                assessment = cpu_mean80_revalidation_record(
                    cell, entry, cpu_mean80_amendment,
                )
            if assessment is not None:
                latest[cell["cell_id"]] = attach_cpu_mean80_revalidation(
                    entry, assessment,
                )
    if any(
        entry.get("status") in {"aborted", "failed", "stopped"}
        and not operationally_recovered(config, entry)
        and not late_recovery_authorized(config, entry)
        and not unsubmitted_abort_skip_authorized(config, entry)
        and entry.get("cell_id")
        not in (retry_unconfirmed_container_kill | retry_reviewed_container_kill)
        for entry in latest.values()
    ):
        raise RuntimeError(
            "campaign has unresolved abort/cleanup/recovery; manual investigation required"
        )
    check_recovery_budget(config, ledger)
    automatic_retry_blocked = {
        cell_id
        for cell_id, entry in latest.items()
        if cell_id in planned_cell_ids
        and cell_id not in retry_blocked
        and automatic_pre_intervention_retry_authorized(config, entry)
    }
    blocked_retry_cells = retry_blocked | automatic_retry_blocked
    pending_cells = [
        cell
        for cell in cells
        if (
            latest.get(cell["cell_id"], {}).get("status") != "completed"
            or cell["cell_id"] in retry_invalid_window
        )
        and (
            latest.get(cell["cell_id"], {}).get("status") != "blocked"
            or cell["cell_id"] in blocked_retry_cells
        )
        and (
            latest.get(cell["cell_id"], {}).get("status")
            not in {"aborted", "failed", "stopped"}
            or cell["cell_id"]
            in (retry_unconfirmed_container_kill | retry_reviewed_container_kill)
        )
    ]
    api = runner.ApiClient(config["platform"]["api_base"], role=config["platform"]["user_role"])
    info = api.get("/api/skill-rag/strategy") if rag else _get_strategy_info(api_base=api.base_url, timeout_seconds=10)
    expected_modes = [
        "annotate", "control", "rag",
        "exploratory_control", "exploratory_rag",
    ]
    expected = {"version": rag["version"], "modes": expected_modes} if rag else {
        "strategy_version": STRATEGY_VERSION, "prompt_version": PROMPT_VERSION
    }
    if info != expected:
        raise RuntimeError(f"loaded RCA version mismatch: {info}")
    if pending_cells:
        require_controlled_backend(api)
        require_scrape_metadata_tool(config, pending_cells[0])
    known_runs = api.get("/api/runs")
    active = [r for r in known_runs if r["status"] in {"pending", "running", "pending_approval"}]
    owned = {entry.get("run_id") for entry in ledger}
    owned_experiments = {
        f"{config['experiment_campaign_id']}-{cell['cell_id']}" for cell in cells
    }
    owned_experiments.update(
        f"{config['experiment_campaign_id']}-{cell_id}-attempt-{entry['attempt']}"
        for cell_id, entry in latest.items()
        if int(entry.get("attempt", 1)) > 1
    )
    if any(r["run_id"] not in owned and r["experiment_id"] not in owned_experiments for r in active):
        raise RuntimeError("another run is active; refusing concurrent load or interventions")
    execution_cells = list(cells)
    if train_only:
        execution_cells = cells[:through_train_count]
    if retry_reviewed_container_kill:
        if rag:
            execution_cells = [
                cells_by_id[next(iter(retry_reviewed_container_kill))]
            ] + [
                cell for cell in execution_cells
                if cell["cell_id"] not in retry_reviewed_container_kill
            ]
        else:
            execution_cells = [
                cell for cell in execution_cells
                if cell["cell_id"] not in retry_reviewed_container_kill
            ] + [
                cell for cell in execution_cells
                if cell["cell_id"] in retry_reviewed_container_kill
            ]
    paused_for_validation = False
    for cell in execution_cells:
        if train_only:
            cell_index = cells.index(cell)
            for prior_cell in cells[:cell_index]:
                prior = latest.get(prior_cell["cell_id"], {})
                if not train_cell_gate_passed(prior):
                    if (
                        train_only_prior_cell_continuation_authorized(
                            config,
                            prior,
                            continue_after_technical_failure=(
                                continue_after_technical_failure
                            ),
                        )
                    ):
                        if train_cell_technical_failure_continuable(prior):
                            record_train_technical_failure_exclusion(
                                directory, prior_cell, prior,
                            )
                        continue
                    if (
                        continue_after_safe_blocked_cell
                        and clean_pre_intervention_block_skip_authorized(
                            config, prior,
                        )
                        and clean_pre_intervention_block_skip_recorded(
                            directory, prior,
                        )
                    ):
                        continue
                    raise RuntimeError(
                        f"train-only gate failed before {cell['cell_id']}; "
                        f"prior cell {prior_cell['cell_id']} is not fully validated"
                    )
        if rag:
            from experiment.eval.memory_hog.rag_campaign import prepare_test, process_cell

            if cell["phase"] == "test":
                prepare_test(directory, config, latest, api)
        if validation_cell and cell["cell_id"] != validation_cell:
            require_continuation_validation(config, latest)
        entry = latest.get(cell["cell_id"], {})
        retrying_blocked = (
            cell["cell_id"] in blocked_retry_cells
            and entry.get("status") == "blocked"
        )
        retrying_auto_blocked = (
            cell["cell_id"] in automatic_retry_blocked and retrying_blocked
        )
        retrying_unconfirmed = (
            cell["cell_id"] in retry_unconfirmed_container_kill
            and entry.get("status") == "aborted"
        )
        retrying_invalid_window = (
            cell["cell_id"] in retry_invalid_window and entry.get("status") == "completed"
        )
        retrying_reviewed_container_kill = (
            cell["cell_id"] in retry_reviewed_container_kill and entry.get("status") == "aborted"
        )
        retrying = (
            retrying_blocked or retrying_unconfirmed
            or retrying_invalid_window or retrying_reviewed_container_kill
        )
        if (
            entry.get("status") == "blocked"
            and not retrying
            and continue_after_safe_blocked_cell
            and clean_pre_intervention_block_skip_authorized(config, entry)
        ):
            require_environment(config, directory)
            record_clean_pre_intervention_block_skip(directory, entry)
        if entry.get("status") in {"completed", "blocked", "failed", "stopped", "aborted"} and not retrying:
            if train_only and entry.get("status") == "completed":
                entry = ensure_train_history_validation(
                    directory, config, cell, entry, api,
                )
                latest[cell["cell_id"]] = entry
                if not train_cell_gate_passed(entry):
                    if (
                        not continue_after_technical_failure
                        or not train_cell_technical_failure_continuable(entry)
                    ):
                        raise RuntimeError(
                            f"train-only gate failed for completed cell {cell['cell_id']}"
                        )
                    record_train_technical_failure_exclusion(
                        directory, cell, entry,
                    )
            if rag and entry.get("status") == "completed":
                process_cell(directory, config, cell, entry, api)
            if stop_after_first_test and cell["phase"] == "test":
                paused_for_validation = True
                append_ledger(directory / "campaign_events.jsonl", {
                    "event": "paused_after_first_test",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "cell_id": cell["cell_id"],
                    "run_id": entry.get("run_id"),
                    "status": entry.get("status"),
                })
                break
            continue
        check_recovery_budget(config, read_ledger(ledger_path))
        if entry and not resume and not retrying_auto_blocked:
            raise RuntimeError("incomplete campaign requires --resume")
        previous_entry = entry
        attempt_number = (
            int(entry.get("attempt", 1)) + 1
            if retrying
            else int(entry.get("attempt", 1))
        )
        attempt = attempt_directory(directory, cell["cell_id"], attempt_number)
        if retrying:
            if retrying_auto_blocked:
                delay_s = pre_intervention_policy["backoff_seconds"]
                append_ledger(
                    directory / "campaign_events.jsonl",
                    {
                        "event": "automatic_pre_intervention_retry_started",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "cell_id": cell["cell_id"],
                        "previous_run_id": previous_entry.get("run_id"),
                        "attempt": attempt_number,
                        "policy_version": pre_intervention_policy["version"],
                        "backoff_seconds": delay_s,
                    },
                )
                print(
                    f"{cell['cell_id']}: waiting {delay_s}s before a fresh "
                    "pre-intervention attempt",
                    flush=True,
                )
                time.sleep(delay_s)
            attempt.mkdir(parents=True, exist_ok=False)
            spec = build_spec(config, cell, attempt)
            if retrying_unconfirmed:
                spec["controlled_campaign"]["previous_attempt_guard"] = retry_guards[cell["cell_id"]]
            if retrying_invalid_window:
                spec["controlled_campaign"]["superseded_observation_window"] = (
                    invalid_window_guards[cell["cell_id"]]
                )
            if retrying_reviewed_container_kill:
                spec["controlled_campaign"]["reviewed_container_kill_retry"] = (
                    reviewed_container_kill_guards[cell["cell_id"]]
                )
            spec["experiment"]["id"] = (
                f"{config['experiment_campaign_id']}-{cell['cell_id']}-attempt-{attempt_number}"
            )
            spec["experiment"]["name"] += f" (attempt {attempt_number})"
            spec["experiment"]["tags"].append(f"attempt:{attempt_number}")
            spec["controlled_campaign"]["attempt"] = attempt_number
            (attempt / "spec.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
            entry = {
                "cell_id": cell["cell_id"],
                "status": "prepared",
                "attempt": attempt_number,
                "retry_of_run_id": previous_entry.get("run_id"),
                "retry_reason": (
                    f"invalid observation-window overlap of "
                    f"{invalid_window_guards[cell['cell_id']]['overlap_ms']} ms"
                    if retrying_invalid_window
                    else (
                        "explicitly authorized new container-kill attempt after manual review"
                        if retrying_reviewed_container_kill
                        else previous_entry.get("reason", "")
                    )
                ),
            }
            if retrying_auto_blocked:
                entry.update(
                    {
                        "retry_mode": PRE_INTERVENTION_RETRY_MODE,
                        "retry_policy_version": pre_intervention_policy["version"],
                        "retry_reason": (
                            "automatic fresh attempt after a clean, unsubmitted block: "
                            f"{previous_entry.get('reason', '')}"
                        ),
                    }
                )
            if retrying_invalid_window:
                entry["supersedes_run_id"] = previous_entry.get("run_id")
            append_ledger(ledger_path, entry)
            if retrying_invalid_window or retrying_reviewed_container_kill:
                append_ledger(
                    directory / "campaign_events.jsonl",
                    {
                        "event": (
                            "observation_window_attempt_invalidated"
                            if retrying_invalid_window
                            else "reviewed_container_kill_retry_authorized"
                        ),
                        "at": datetime.now(timezone.utc).isoformat(),
                        "cell_id": cell["cell_id"],
                        "attempt": attempt_number,
                        "previous_run_id": previous_entry.get("run_id"),
                        "details": (
                            invalid_window_guards[cell["cell_id"]]
                            if retrying_invalid_window
                            else reviewed_container_kill_guards[cell["cell_id"]]
                        ),
                    },
                )
        elif not entry:
            attempt.mkdir(parents=True, exist_ok=False)
            spec = build_spec(config, cell, attempt)
            (attempt / "spec.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
            entry = {"cell_id": cell["cell_id"], "status": "prepared", "attempt": 1}
            append_ledger(ledger_path, entry)
        else:
            spec = json.loads((attempt / "spec.json").read_text(encoding="utf-8"))
        if not entry.get("run_id"):
            matches = [r for r in known_runs if r["experiment_id"] == spec["experiment"]["id"]]
            if len(matches) > 1:
                raise RuntimeError("multiple runs for deterministic experiment; refusing ambiguous resume")
            if matches:
                entry = {**entry, "run_id": matches[0]["run_id"], "status": "submitted"}
                append_ledger(ledger_path, entry)
                reconcile_exploratory_manifest(
                    directory, config, read_ledger(ledger_path),
                )
        if not entry.get("run_id"):
            require_environment(config, directory)
            # Persisted deterministic experiment/key make resuming after a lost
            # HTTP response reuse the same run, never a second injection.
            created = api.post("/api/experiments", body={
                "initiated_by": config["platform"]["initiated_by"], "spec": spec,
            })
            response = api.post("/api/runs/start", body={
                "experiment_id": spec["experiment"]["id"], "version": created["version"],
                "initiated_by": config["platform"]["initiated_by"],
                "idempotency_key": f"controlled:{spec['experiment']['id']}",
                "is_training": cell.get("phase") == "train",
            })
            entry = {**entry, "run_id": response["run_id"], "status": "submitted"}
            append_ledger(ledger_path, entry)
            reconcile_exploratory_manifest(
                directory, config, read_ledger(ledger_path),
            )
        print(f"{cell['cell_id']}: {entry['run_id']}", flush=True)
        deadline = time.monotonic() + controlled_time_budget(config["controlled_campaign"])["poll_seconds"]
        heartbeat_at = time.monotonic()
        while True:
            resilience = config["controlled_campaign"].get("resilience") or {}
            result = poll_run(api, entry["run_id"], directory, resilience.get("poll_retries", 0))
            if result["status"] in {"completed", "failed", "stopped"}:
                break
            if time.monotonic() >= heartbeat_at:
                append_ledger(directory / "campaign_events.jsonl", {
                    "event": "run_heartbeat", "at": datetime.now(timezone.utc).isoformat(),
                    "cell_id": cell["cell_id"], "run_id": entry["run_id"],
                    "status": result["status"],
                })
                heartbeat_at = time.monotonic() + 60
            if time.monotonic() >= deadline:
                append_ledger(ledger_path, {**entry, "status": "aborted", "reason": "timeout; run may still be active"})
                raise RuntimeError("run timeout; stopping campaign, do not reinject")
            time.sleep(5)
        (attempt / "run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        summary = result.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        state = summary.get("controlled_status")
        if state not in {"completed", "blocked", "failed", "aborted"}:
            reason = (
                "backend protocol mismatch: terminal run response has no valid "
                "'summary.controlled_status'; raw response saved to run.json"
            )
            entry = {**entry, "status": "aborted", "reason": reason}
            append_ledger(ledger_path, entry)
            export_runs(directory, cells, read_ledger(ledger_path))
            raise RuntimeError(reason)
        gates = summary.get("controlled_gates") or {}
        reason = summary.get("reason") or ""
        if state in {"failed", "aborted"} and not reason:
            reason = f"controlled run returned {state} without a reason"
        entry = {**entry, "status": state, "reason": reason, "gates": gates}
        append_ledger(ledger_path, entry)
        latest[cell["cell_id"]] = entry
        ledger = read_ledger(ledger_path)
        export_runs(directory, cells, ledger)
        effects = []
        attempt_results = {
            (row["cell_id"], row.get("attempt", 1)): row for row in ledger
        }
        for item in attempt_results.values():
            if not item.get("run_id"):
                continue
            checks = item.get("gates") or {}
            confirmation = checks.get("intervention_confirmed_on_target")
            mechanism = checks.get("mechanism_observed", {}).get("passed")
            passed = (
                confirmation and mechanism
                if isinstance(confirmation, bool) and isinstance(mechanism, bool) else None
            ) if checks.get("submitted") is True else False
            effects.append({
                "run_id": item["run_id"], "cell_id": item["cell_id"],
                "attempt": item.get("attempt", 1),
                "retry_of_run_id": item.get("retry_of_run_id"),
                "passed": passed,
                "result_status": item["status"], "checks": checks,
                "reason": item.get("reason", ""),
            })
        (directory / "effect_validation.json").write_text(
            json.dumps(effects, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        if train_only and state != "blocked":
            entry = ensure_train_history_validation(
                directory, config, cell, entry, api,
                wait_for_completion=state == "completed",
            )
            if cpu_mean80_active and cell.get("chaos_type") == "cpu-hog":
                assessment = cpu_mean80_revalidation_record(
                    cell, entry, cpu_mean80_amendment,
                )
                if assessment is not None:
                    entry = attach_cpu_mean80_revalidation(entry, assessment)
                    record_cpu_mean80_revalidation(
                        directory, entry, assessment,
                        cpu_mean80_amendment["campaign_id"],
                    )
            latest[cell["cell_id"]] = entry
        if validation_cell == cell["cell_id"]:
            require_continuation_validation(config, latest)
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "continuation_validation_passed",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": cell["cell_id"], "run_id": entry["run_id"],
                "parent_campaign_id": config["continuation"]["parent_campaign_id"],
            })
            print(f"{cell['cell_id']}: validation passed; remaining cells authorized", flush=True)
        if (
            train_only
            and continue_after_technical_failure
            and state != "completed"
            and not train_cell_failure_continuation_authorized(
                config,
                entry,
                allow_clean_pre_intervention_skip=continue_after_safe_blocked_cell,
            )
        ):
            raise RuntimeError(
                f"technical-failure continuation stopped on run state {state!r} "
                f"for {cell['cell_id']}; no later cell was started"
            )
        if state not in {"completed", "blocked"}:
            operational_recovery_passed = operationally_recovered(config, entry)
            amendment_recovery_passed = late_recovery_authorized(config, entry)
            if not (operational_recovery_passed or amendment_recovery_passed):
                raise RuntimeError(f"run failed/aborted; stop before next target: {entry['reason']}")
            late_operational = (
                (entry.get("gates") or {}).get("late_operational_recovery") or {}
            )
            recovery_gate = (
                "late_operational_recovery"
                if late_operational.get("passed")
                else "late_recovery_amendment"
                if amendment_recovery_passed
                else "operational_recovery"
            )
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "continuation_after_operational_recovery",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": cell["cell_id"], "run_id": entry["run_id"],
                "experimental_status_unchanged": state,
                "recovery_gate": recovery_gate,
            })
            print(f"{cell['cell_id']}: remains aborted; operational recovery permits next cell", flush=True)
        if state == "completed":
            if (
                not gates.get("recovery", {}).get("passed")
                or not gates.get("cleanup", {}).get("passed")
                or not gates.get("load_stopped", {}).get("stopped")
            ):
                append_ledger(
                    ledger_path,
                    {
                        **entry,
                        "status": "aborted",
                        "reason": "recovery/cleanup not confirmed",
                    },
                )
                raise RuntimeError("recovery/cleanup not confirmed; stopping campaign")
            print(f"{cell['cell_id']}: recovered and cleaned", flush=True)
            if rag:
                process_cell(directory, config, cell, entry, api)
        elif state == "blocked":
            print(f"{cell['cell_id']}: BLOCKED: {entry['reason']}", flush=True)
            if (
                pre_intervention_policy
                and cell["cell_id"] not in automatic_retry_blocked
                and automatic_pre_intervention_retry_authorized(config, entry)
            ):
                automatic_retry_blocked.add(cell["cell_id"])
                blocked_retry_cells.add(cell["cell_id"])
                execution_cells.insert(execution_cells.index(cell) + 1, cell)
                append_ledger(
                    directory / "campaign_events.jsonl",
                    {
                        "event": "automatic_pre_intervention_retry_scheduled",
                        "at": datetime.now(timezone.utc).isoformat(),
                        "cell_id": cell["cell_id"],
                        "previous_run_id": entry.get("run_id"),
                        "attempt": int(entry.get("attempt", 1)) + 1,
                        "policy_version": pre_intervention_policy["version"],
                        "backoff_seconds": pre_intervention_policy["backoff_seconds"],
                        "reason": entry.get("reason", ""),
                    },
                )
            elif (
                continue_after_safe_blocked_cell
                and clean_pre_intervention_block_skip_authorized(config, entry)
            ):
                require_environment(config, directory)
                record_clean_pre_intervention_block_skip(directory, entry)
        if (
            train_only
            and state != "blocked"
            and not train_cell_gate_passed(entry)
        ):
            if (
                not continue_after_technical_failure
                or not train_cell_technical_failure_continuable(entry)
            ):
                raise RuntimeError(
                    f"train-only gate failed for {cell['cell_id']}; "
                    "campaign stopped before the next cell"
                )
            record_train_technical_failure_exclusion(directory, cell, entry)
            print(
                f"{cell['cell_id']}: technical gate failed; run excluded from training, "
                "recovery/read-back gates passed",
                flush=True,
            )
        retry_pending_for_canary = bool(
            state == "blocked"
            and cell["cell_id"] in automatic_retry_blocked
            and pre_intervention_policy
            and int(entry.get("attempt", 1)) < pre_intervention_policy["max_attempts"]
        )
        if (
            stop_after_first_test
            and cell["phase"] == "test"
            and not retry_pending_for_canary
        ):
            paused_for_validation = True
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "paused_after_first_test",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": cell["cell_id"],
                "run_id": entry.get("run_id"),
                "status": entry.get("status"),
            })
            break
    export_runs(directory, cells, read_ledger(ledger_path))
    # Keep all completed runs, not only those with a detectable intervention.
    if train_only:
        assert through_train_count is not None
        prefix = cells[:through_train_count]
        prefix_gates_passed = all(
            train_cell_gate_passed(latest.get(cell["cell_id"], {}))
            for cell in prefix
        )
        excluded_technical_failures = sum(
            not train_cell_gate_passed(latest.get(cell["cell_id"], {}))
            and train_cell_technical_failure_continuable(
                latest.get(cell["cell_id"], {})
            )
            for cell in prefix
        )
        excluded_clean_blocks = sum(
            continue_after_safe_blocked_cell
            and clean_pre_intervention_block_skip_authorized(
                config, latest.get(cell["cell_id"], {}),
            )
            and clean_pre_intervention_block_skip_recorded(
                directory, latest.get(cell["cell_id"], {}),
            )
            for cell in prefix
        )
        continuation_complete = all(
            train_cell_gate_passed(latest.get(cell["cell_id"], {}))
            or (
                continue_after_technical_failure
                and train_cell_technical_failure_continuable(
                    latest.get(cell["cell_id"], {})
                )
            )
            or (
                continue_after_safe_blocked_cell
                and clean_pre_intervention_block_skip_authorized(
                    config, latest.get(cell["cell_id"], {}),
                )
                and clean_pre_intervention_block_skip_recorded(
                    directory, latest.get(cell["cell_id"], {}),
                )
            )
            for cell in prefix
        )
        excluded_cells = excluded_technical_failures + excluded_clean_blocks
        progress = {
            "version": TRAIN_ONLY_PLAN_VERSION,
            "campaign_id": config["experiment_campaign_id"],
            "approved_through_cell_count": through_train_count,
            "planned_cell_count": len(cells),
            "collection_complete": continuation_complete,
            "all_gates_passed": prefix_gates_passed,
            "continue_after_technical_failure": continue_after_technical_failure,
            "continue_after_safe_blocked_cell": continue_after_safe_blocked_cell,
            "completed_and_gated_count": sum(
                train_cell_gate_passed(latest.get(cell["cell_id"], {}))
                for cell in cells
            ),
            "excluded_technical_failure_count": excluded_technical_failures,
            "excluded_clean_pre_intervention_block_count": excluded_clean_blocks,
            "not_started_count": sum(
                cell["cell_id"] not in latest for cell in prefix
            ),
            "prefix_gates_passed": prefix_gates_passed,
            "next_tranche": next(
                (count for count in TRAIN_ONLY_TRANCHES
                 if count > through_train_count),
                None,
            ),
            "cells": [
                {
                    "cell_id": cell["cell_id"],
                    "status": latest.get(cell["cell_id"], {}).get("status", "not_run"),
                    "run_id": latest.get(cell["cell_id"], {}).get("run_id"),
                    "gates_passed": train_cell_gate_passed(
                        latest.get(cell["cell_id"], {})
                    ),
                    "eligible_for_training": train_cell_gate_passed(
                        latest.get(cell["cell_id"], {})
                    ),
                    "cpu_mean80_revalidation": latest.get(
                        cell["cell_id"], {},
                    ).get("cpu_mean80_revalidation"),
                    "training_exclusion_reason": (
                        "clean_pre_intervention_block"
                        if continue_after_safe_blocked_cell
                        and clean_pre_intervention_block_skip_recorded(
                            directory, latest.get(cell["cell_id"], {}),
                        )
                        else (
                            "technical_gate_failure"
                            if (
                                not train_cell_gate_passed(
                                    latest.get(cell["cell_id"], {})
                                )
                                and train_cell_technical_failure_continuable(
                                    latest.get(cell["cell_id"], {})
                                )
                            )
                            else None
                        )
                    ),
                }
                for cell in cells
            ],
        }
        (directory / "train_only_progress.json").write_text(
            json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        append_ledger(directory / "campaign_events.jsonl", {
            "event": (
                "paused_after_train_tranche"
                if continuation_complete and through_train_count < len(cells)
                else "train_tranche_gate_failed"
                if not continuation_complete
                else "train_only_campaign_completed_with_exclusions"
                if excluded_cells
                else "train_only_campaign_completed"
            ),
            "at": datetime.now(timezone.utc).isoformat(),
            "approved_through_cell_count": through_train_count,
            "prefix_gates_passed": prefix_gates_passed,
            "collection_complete": continuation_complete,
            "excluded_technical_failure_count": excluded_technical_failures,
            "excluded_clean_pre_intervention_block_count": excluded_clean_blocks,
            "not_started_count": progress["not_started_count"],
        })
    elif rag:
        from experiment.eval.memory_hog.rag_campaign import write_evaluation

        write_evaluation(directory, config)
        report_name = (
            "exploratory_rag_evaluation.json"
            if rag.get("exploratory")
            else "rag_evaluation.json"
        )
        print(f"RAG report: {directory / report_name}")
    else:
        markdown, data = evaluate_campaign(directory, api_base=api.base_url)
        print(f"Multiagent reports: {markdown}, {data}")
    latest = {entry["cell_id"]: entry for entry in read_ledger(ledger_path)}
    counts = {
        status: sum(latest.get(cell["cell_id"], {}).get("status", "not_run") == status
                    for cell in cells)
        for status in ("completed", "blocked", "aborted", "failed", "not_run")
    }
    print(f"Campaign outcomes ({len(cells)} planned): {counts}", flush=True)
    print(f"Submitted interventions: {sum((entry.get('gates') or {}).get('submitted') is True for entry in latest.values())}", flush=True)
    if train_only:
        if progress["collection_complete"]:
            if progress["excluded_technical_failure_count"]:
                print(
                    "Train-only collection complete with "
                    f"{progress['excluded_technical_failure_count']} technical "
                    "failure(s) explicitly excluded from training; "
                    f"{progress['completed_and_gated_count']} cells are training-eligible.",
                    flush=True,
                )
                return 0
            print(
                f"Train-only campaign paused at approved tranche "
                f"{through_train_count}/{len(cells)}; no next tranche was started.",
                flush=True,
            )
            return 0
        print(
            f"Train-only campaign stopped: tranche {through_train_count} did not pass all gates.",
            flush=True,
        )
        return 1
    if paused_for_validation:
        first_test = next(
            cell for cell in config["test_experiments"] if cell["phase"] == "test"
        )
        canary_status = latest.get(first_test["cell_id"], {}).get("status")
        if canary_status == "completed":
            print(
                f"Paused after successful first test for validation: "
                f"{first_test['cell_id']}",
                flush=True,
            )
            return 0
        return 1
    return 0 if counts["completed"] == len(cells) else 1


def build_continuation(
    parent: Path, directory: Path, validation_cell: str,
) -> dict[str, Any]:
    with (parent / ".campaign.lock").open("r") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("parent campaign is active; cannot create a continuation") from exc
        sources = {
            name: (parent / name).read_bytes()
            for name in ("config.yaml", "plan.json", "ledger.jsonl")
        }
        config = yaml.safe_load(sources["config.yaml"])
        if config.get("knowledge_campaign"):
            raise RuntimeError("RAG train/test protocol cannot continue in a different environment segment")
        plan = json.loads(sources["plan.json"])
        if plan != config["test_experiments"]:
            raise RuntimeError("parent campaign matrix differs from its saved plan")
        latest = {
            row["cell_id"]: row
            for row in (json.loads(line) for line in sources["ledger.jsonl"].splitlines() if line.strip())
        }
        terminal = {"completed", "blocked", "failed", "stopped", "aborted"}
        if any(row.get("status") not in terminal for row in latest.values()):
            raise RuntimeError("parent campaign has unresolved nonterminal runs")
        if set(latest) - {cell["cell_id"] for cell in plan}:
            raise RuntimeError("parent ledger contains cells outside its saved plan")
        pending = [
            cell for cell in plan
            if latest.get(cell["cell_id"], {}).get("status") != "completed"
        ]
        if validation_cell not in {cell["cell_id"] for cell in pending}:
            raise RuntimeError("validation cell must be a pending parent combination")
        pending = (
            [cell for cell in pending if cell["cell_id"] == validation_cell]
            + [cell for cell in pending if cell["cell_id"] != validation_cell]
        )
        config["continuation"] = {
            "parent_campaign_id": config["experiment_campaign_id"],
            "parent_directory": str(parent.resolve()),
            "source_sha256": {
                name: hashlib.sha256(data).hexdigest() for name, data in sources.items()
            },
            "parent_latest_results": latest,
            "inherited_completed_count": sum(row["status"] == "completed" for row in latest.values()),
            "validation_cell": validation_cell,
            "protocol_version": CONTROLLED_CAMPAIGN_PROTOCOL_VERSION,
            "comparability": "new environment segment; do not pool as a homogeneous campaign",
        }
        config["experiment_campaign_id"] = directory.name
        config["test_experiments"] = pending
        config["output"].update({
            "results_dir": str(directory), "manifests_dir": "manifests",
        })
        return config


def require_continuation_validation(config: dict[str, Any], latest: dict[str, Any]) -> None:
    validation = (config.get("continuation") or {}).get("validation_cell")
    if not validation:
        return
    entry = latest.get(validation, {})
    gates = entry.get("gates") or {}
    if (
        entry.get("status") != "completed"
        or gates.get("submitted") is not True
        or gates.get("intervention_confirmed_on_target") is not True
        or gates.get("mechanism_observed", {}).get("passed") is not True
        or gates.get("recovery", {}).get("passed") is not True
        or gates.get("cleanup", {}).get("passed") is not True
        or gates.get("load_stopped", {}).get("stopped") is not True
    ):
        raise RuntimeError(
            f"continuation validation did not pass all gates: {validation}; no next cell authorized"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--level",
        choices=["small", "medium", "large", "rag", "train-only"],
        default="small",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--through-train-count",
        type=int,
        choices=TRAIN_ONLY_TRANCHES,
        help="Explicit approval boundary for train-only campaigns (1, 5, 10, 20 or 35).",
    )
    parser.add_argument("--rag-top-k", type=int, help="Maximum retrieved training pairs (rag profile only).")
    parser.add_argument("--rag-min-score", type=float, help="Minimum retrieval score (rag profile only).")
    parser.add_argument(
        "--rag-max-rounds",
        type=int,
        choices=range(1, 5),
        help="Maximum extra read-only checks shared by original, control and RAG.",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Read-only infrastructure checks; no load, injection or campaign files.")
    parser.add_argument("--load-check", action="store_true",
                        help="Single 30-user load-only rehearsal across all services; no fault or campaign evaluation.")
    parser.add_argument("--resume", type=Path, help="Existing campaign; never re-inject terminal cells.")
    parser.add_argument(
        "--continue-after-technical-failure",
        action="store_true",
        help=(
            "Train-only full-campaign resume: continue only after a completed, measured "
            "technical gate failure with recovery, cleanup and historical read-back passed."
        ),
    )
    parser.add_argument(
        "--continue-after-safe-blocked-cell",
        action="store_true",
        help=(
            "After the pre-registered retry limit, skip only a clean unsubmitted "
            "pre-intervention block after a fresh passing environment preflight."
        ),
    )
    parser.add_argument(
        "--continue-from", type=Path,
        help="New environment segment containing only unfinished parent combinations.",
    )
    parser.add_argument(
        "--validation-cell",
        help="Required with --continue-from; must complete all gates before any other cell.",
    )
    parser.add_argument(
        "--retry-blocked", action="append", default=[], metavar="CELL_ID",
        help="Explicitly retry an unsubmitted blocked cell; preserves the old attempt.",
    )
    parser.add_argument(
        "--retry-unconfirmed-container-kill", action="append", default=[], metavar="CELL_ID",
        help=(
            "Explicitly retry an aborted one-shot container-kill only when the prior "
            "report proves no restart and the target container identity is unchanged."
        ),
    )
    parser.add_argument(
        "--retry-invalid-window", action="append", default=[], metavar="CELL_ID",
        help=(
            "Supersede a completed HTTP cell only when its persisted observation window "
            "contains the known fault/post overlap defect."
        ),
    )
    parser.add_argument(
        "--retry-reviewed-container-kill", action="append", default=[], metavar="CELL_ID",
        help=(
            "Start a separately recorded container-kill attempt after explicit review of "
            "a prior confirmed restart and failed recovery; original gates still apply."
        ),
    )
    parser.add_argument("--calibration", type=Path, default=HERE / "cpu-calibration.yaml",
                        help="Per-service CPU profile (default: bundled candidate, verified during each run).")
    args = parser.parse_args(argv)
    rag_overrides = {
        key: value for key, value in {
            "top_k": args.rag_top_k, "min_score": args.rag_min_score,
            "max_rounds": args.rag_max_rounds,
        }.items() if value is not None
    }
    if rag_overrides and (args.level != "rag" or args.resume or args.continue_from):
        parser.error("RAG overrides require --level rag on a new campaign; resume uses saved options")
    if args.continue_from and (
        args.resume or args.preflight or args.load_check or args.retry_blocked
        or args.retry_unconfirmed_container_kill or args.retry_invalid_window
        or args.retry_reviewed_container_kill
    ):
        parser.error("--continue-from cannot be combined with resume, retries or infrastructure checks")
    if bool(args.continue_from) != bool(args.validation_cell):
        parser.error("--continue-from and --validation-cell must be used together")
    if args.preflight and (args.run or args.resume):
        parser.error("--preflight cannot be combined with --run or --resume")
    if args.load_check and (args.run or args.resume or args.preflight):
        parser.error("--load-check cannot be combined with --run, --resume or --preflight")
    if args.continue_after_technical_failure and not args.resume:
        parser.error("--continue-after-technical-failure requires --resume")
    if args.continue_after_safe_blocked_cell and (
        not args.resume or not args.continue_after_technical_failure
    ):
        parser.error(
            "--continue-after-safe-blocked-cell requires --resume and "
            "--continue-after-technical-failure"
        )
    if args.continue_after_technical_failure and (
        args.retry_blocked or args.retry_unconfirmed_container_kill
        or args.retry_invalid_window or args.retry_reviewed_container_kill
    ):
        parser.error(
            "--continue-after-technical-failure cannot be combined with retry options"
        )
    if args.resume:
        if not args.run:
            parser.error("--resume requires --run")
        try:
            resume_config = yaml.safe_load(
                (args.resume / "config.yaml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            parser.error(f"cannot read resume campaign config: {exc}")
        is_train_only = (
            (resume_config.get("controlled_campaign") or {}).get("level")
            == "train-only"
        )
        if is_train_only != (args.through_train_count is not None):
            parser.error(
                "train-only resume requires --through-train-count; other campaigns "
                "cannot use it"
            )
        return execute(
            args.resume.resolve(), resume=True, retry_blocked=set(args.retry_blocked),
            retry_unconfirmed_container_kill=set(args.retry_unconfirmed_container_kill),
            retry_invalid_window=set(args.retry_invalid_window),
            retry_reviewed_container_kill=set(args.retry_reviewed_container_kill),
            through_train_count=args.through_train_count,
            continue_after_technical_failure=args.continue_after_technical_failure,
            continue_after_safe_blocked_cell=args.continue_after_safe_blocked_cell,
            cpu_calibration_path=(
                args.calibration if args.continue_after_technical_failure else None
            ),
        )
    if (
        args.retry_blocked or args.retry_unconfirmed_container_kill
        or args.retry_invalid_window or args.retry_reviewed_container_kill
    ):
        parser.error("campaign retries require --resume")
    if args.level == "train-only":
        if args.run and args.through_train_count is None:
            parser.error(
                "--run with --level train-only requires an explicit "
                "--through-train-count"
            )
    elif args.through_train_count is not None:
        parser.error("--through-train-count requires --level train-only")
    calibration = {}
    if args.calibration and not args.continue_from:
        if not args.calibration.is_file():
            parser.error(f"calibration file does not exist or is not a file: {args.calibration}")
        try:
            calibration = yaml.safe_load(args.calibration.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            parser.error(f"cannot read calibration YAML {args.calibration}: {exc}")
        if not isinstance(calibration, dict):
            parser.error(f"calibration YAML must contain a mapping at its root: {args.calibration}")
    directory = HERE / "out" / ("load_checks" if args.load_check else "controlled_campaign") / (
        ("load-check-" if args.load_check else "controlled-")
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    source = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    if args.continue_from:
        assert args.validation_cell is not None
        config = build_continuation(args.continue_from.resolve(), directory, args.validation_cell)
    else:
        config = build_config(source, directory, args.level, args.seed, calibration)
    if config.get("knowledge_campaign"):
        from experiment.platform.backend.rca_knowledge import KnowledgeOptions

        config["knowledge_campaign"].update(rag_overrides)
        try:
            KnowledgeOptions(
                **{key: value for key, value in config["knowledge_campaign"].items() if key != "version"},
                mode="rag", possible_fault_types=list(FAULTS),
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.continue_from:
        print(f"New continuation of {config['continuation']['parent_campaign_id']}; "
              f"validation first: {args.validation_cell}; parent outcomes preserved.")
    cells = campaign_cells(config)
    print(f"{config['controlled_campaign']['level']}: "
          f"{len(cells)} planned runs; seed={config['ordering']['seed']}")
    if args.level == "train-only":
        tranche_label = (
            str(args.through_train_count)
            if args.through_train_count is not None
            else "not selected for dry-run"
        )
        print(
            f"Train-only protocol {TRAIN_ONLY_PLAN_VERSION}: "
            f"35 train / 0 test; tranche boundary={tranche_label}."
        )
    if config.get("knowledge_campaign"):
        print("RAG: 35 train -> frozen A/B base -> 35 test; no mandatory human guide approval.")
    print("30 users, spawn=5/s; not a fixed RPS. Warmup >=60s + stable gate; baseline=90s.")
    print(f"CPU profile: {'saved parent configuration' if args.continue_from else args.calibration}; "
          "candidates are NOT empirical calibration.")
    print(f"Orchestration budget: {config['controlled_campaign']['time_budget']}")
    for cell in cells:
        if cell["chaos_type"] == "container-kill":
            print(f"  {cell['cell_id']} single_event; post_observation>=120s")
        else:
            print(f"  {cell['cell_id']} active_candidate={ACTIVE_SECONDS[cell['chaos_type']]}s")
    if not args.run:
        if args.preflight or args.load_check:
            api = runner.ApiClient(config["platform"]["api_base"], role=config["platform"]["user_role"])
            require_controlled_backend(api)
            target_cell = (
                config["test_experiments"] or config["train_experiments"]
            )[0]
            require_scrape_metadata_tool(config, target_cell)
            rag = config.get("knowledge_campaign")
            info = api.get("/api/skill-rag/strategy") if rag else _get_strategy_info(api_base=api.base_url, timeout_seconds=10)
            expected_modes = [
                "annotate", "control", "rag",
                "exploratory_control", "exploratory_rag",
            ]
            expected = {"version": rag["version"], "modes": expected_modes} if rag else {
                "strategy_version": STRATEGY_VERSION, "prompt_version": PROMPT_VERSION
            }
            if info != expected:
                raise RuntimeError(f"loaded RCA version mismatch: {info}")
            active = [r["run_id"] for r in api.get("/api/runs")
                      if r["status"] in {"pending", "running", "pending_approval"}]
            if active:
                raise RuntimeError(f"active runs prevent isolated preflight: {active}")
            if api.get("/api/llm/health").get("ok") is not True:
                raise RuntimeError("LLM unavailable; evaluation cannot complete")
            if args.load_check:
                from .load_check import run_load_check
                report = run_load_check(config, directory)
                print(f"Load-only report: {directory / 'report.json'}; passed={report['passed']}")
                return 0 if report["passed"] else 1
            report = environment_preflight(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        print("Plan only: no files written, no API/cluster calls, no chaos submitted.")
        return 0
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (directory / "plan.json").write_text(json.dumps(cells, indent=2) + "\n", encoding="utf-8")
    print(f"Saved immutable plan: {directory}", flush=True)
    return execute(
        directory, resume=False, through_train_count=args.through_train_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
