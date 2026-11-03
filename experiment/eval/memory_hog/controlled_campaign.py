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
from experiment.eval.memory_hog.resource_intensity import _CPU_SUFFIXES, _MEMORY_SUFFIXES, _parse_quantity
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.eval.memory_hog import runner
from experiment.eval.memory_hog import generate_chaos_manifests as manifests
from experiment.eval.memory_hog.evaluate_multiagent_rca import (
    PROMPT_VERSION, STRATEGY_VERSION, _get_strategy_info, evaluate_campaign,
)

HERE = Path(__file__).resolve().parent
SEED = 20261018
RAG_REVIEWED_RETRY_PROTOCOL_VERSION = "skill-rag-ab-reviewed-retry-v1"
RAG_REVIEWED_RETRY_PROTOCOL_FILE = "rag_reviewed_retry_protocol.json"
PRE_INTERVENTION_RETRY_VERSION = "blocked-before-injection-v1"
PRE_INTERVENTION_RETRY_MODE = "automatic_pre_intervention"
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
    if level not in {"small", "medium", "large"}:
        raise ValueError("level must be small, medium, large or rag")
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
    if level == "rag":
        config["knowledge_campaign"] = {
            "version": "skill-rag-ab-v1", "corpus_id": directory.name,
            "top_k": 3, "min_score": 0.05, "max_rounds": 2,
            "max_model_calls": 12, "timeout_seconds": 600,
            "max_total_tokens": 120000, "history_char_budget": 24000,
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
    return bool(
        isinstance(policy, dict) and policy.get("version") == 1
        and entry.get("status") == "aborted"
        and entry.get("reason") == "recovery not confirmed; campaign must stop before next service"
        and gates.get("submitted") is True
        and gates.get("intervention_confirmed_on_target") is True
        and (gates.get("mechanism_observed") or {}).get("passed") is True
        and (gates.get("recovery") or {}).get("passed") is False
        and operational.get("passed") is True
        and operational.get("policy_version") == 1
        and operational.get("source") == "same_load_same_baseline_unchanged_recovery_thresholds"
        and operational.get("stable_seconds", 0) >= policy["stable_seconds"]
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
    cells = config["test_experiments"]
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
) -> int:
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    pre_intervention_policy = pre_intervention_retry_policy(config)
    cells = campaign_cells(config)
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
        if entry.get("status") in {"completed", "blocked", "failed", "stopped", "aborted"} and not retrying:
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
        if validation_cell == cell["cell_id"]:
            require_continuation_validation(config, latest)
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "continuation_validation_passed",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": cell["cell_id"], "run_id": entry["run_id"],
                "parent_campaign_id": config["continuation"]["parent_campaign_id"],
            })
            print(f"{cell['cell_id']}: validation passed; remaining cells authorized", flush=True)
        if state not in {"completed", "blocked"}:
            if not operationally_recovered(config, entry):
                raise RuntimeError(f"run failed/aborted; stop before next target: {entry['reason']}")
            append_ledger(directory / "campaign_events.jsonl", {
                "event": "continuation_after_operational_recovery",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": cell["cell_id"], "run_id": entry["run_id"],
                "experimental_status_unchanged": state,
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
    if rag:
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
    parser.add_argument("--level", choices=["small", "medium", "large", "rag"], default="small")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--rag-top-k", type=int, help="Maximum retrieved training pairs (rag profile only).")
    parser.add_argument("--rag-min-score", type=float, help="Minimum retrieval score (rag profile only).")
    parser.add_argument("--rag-max-rounds", type=int, help="Maximum extra read-only checks (rag profile only).")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Read-only infrastructure checks; no load, injection or campaign files.")
    parser.add_argument("--load-check", action="store_true",
                        help="Single 30-user load-only rehearsal across all services; no fault or campaign evaluation.")
    parser.add_argument("--resume", type=Path, help="Existing campaign; never re-inject terminal cells.")
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
    if args.resume:
        if not args.run:
            parser.error("--resume requires --run")
        return execute(
            args.resume.resolve(), resume=True, retry_blocked=set(args.retry_blocked),
            retry_unconfirmed_container_kill=set(args.retry_unconfirmed_container_kill),
            retry_invalid_window=set(args.retry_invalid_window),
            retry_reviewed_container_kill=set(args.retry_reviewed_container_kill),
        )
    if (
        args.retry_blocked or args.retry_unconfirmed_container_kill
        or args.retry_invalid_window or args.retry_reviewed_container_kill
    ):
        parser.error("campaign retries require --resume")
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
            require_scrape_metadata_tool(config, config["test_experiments"][0])
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
    return execute(directory, resume=False)


if __name__ == "__main__":
    raise SystemExit(main())
