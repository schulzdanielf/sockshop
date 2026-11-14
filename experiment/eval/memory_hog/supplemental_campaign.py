"""Versioned, gated collection for the six planned supplemental cases."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import load_check
from experiment.eval.memory_hog import runner
from experiment.platform.backend.controlled_experiment import (
    DEFAULT_STABILITY_RELATIVE_LIMIT, SUPPLEMENTAL_PREPARATION_POLICY,
    build_time_budget,
    controlled_time_budget,
    CONTROLLED_MEASUREMENT_POLICY, GateBlocked, validate_cgroup_timing,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.tools.history_dual_write import set_dual_write

HERE = Path(__file__).resolve().parent
REFERENCE_DIR = HERE / "out" / "knowledge_evaluation" / (
    "test-base-v6-20261107T215646147881Z"
)
REFERENCE_TRAINING_BASE = REFERENCE_DIR / "rag_base.json"
REFERENCE_TEST_COHORT = REFERENCE_DIR / "test_cohort.json"
REFERENCE_TRAINING_SHA256 = (
    "854624b4991db4fe8856d864e9d68ef0f11f88c692f2ec0262db46ff3f4092b2"
)
REFERENCE_TEST_SHA256 = (
    "c192e82f4107c9c86bad32b5405d36cb1892becf3f049d4edfd707eb1690ea17"
)
PROTOCOL_VERSION = "controlled-supplemental-six-v1"
SEED = 20261108
CONTROL_SERVICES = ("front-end", "orders", "user")
CONTROL_SECONDS = 600
CLOCK_PREFLIGHT_SECONDS = 600
CONTROL_ACCEPTANCE_POLICY = load_check.control_acceptance_policy("diagnostic")
SUPPLEMENTAL_USER_LATENCY_STABILITY_POLICY = {
    "version": "supplemental-user-latency-25pct-v1",
    "default_relative_limit": DEFAULT_STABILITY_RELATIVE_LIMIT,
    "relative_limit_overrides": {"user": {"latency_ms": .25}},
    "scope": "no-fault calibration preparation and control windows only",
    "floors_unchanged": True,
    "effect_and_recovery_thresholds_unchanged": True,
}
SUPPLEMENTAL_STABILITY_POLICY = {
    "version": "supplemental-all-stability-25pct-v1",
    "default_relative_limit": .25,
    "relative_limit_overrides": {},
    "scope": "all no-fault calibration preparation and control stability metrics",
    "floors_unchanged": True,
    "effect_and_recovery_thresholds_unchanged": True,
}
SUPPORTED_SUPPLEMENTAL_STABILITY_POLICIES = {
    policy["version"]: policy
    for policy in (
        SUPPLEMENTAL_USER_LATENCY_STABILITY_POLICY,
        SUPPLEMENTAL_STABILITY_POLICY,
    )
}
CASES = (
    {
        "cell_id": "front-end-memory-hog-r0",
        "service": "front-end",
        "chaos_type": "memory-hog",
        "replica_idx": 0,
        "phase": "train",
        "previous_run_ids": ["run-5d259c38e864"],
    },
    {
        "cell_id": "front-end-network-latency-r0",
        "service": "front-end",
        "chaos_type": "network-latency",
        "replica_idx": 0,
        "phase": "train",
        "previous_run_ids": ["run-06336496e6b3", "run-b285c310e17b"],
    },
    {
        "cell_id": "orders-container-kill-r0",
        "service": "orders",
        "chaos_type": "container-kill",
        "replica_idx": 0,
        "phase": "train",
        "previous_run_ids": ["run-503646080ca5"],
    },
    {
        "cell_id": "user-network-latency-r1",
        "service": "user",
        "chaos_type": "network-latency",
        "replica_idx": 1,
        "phase": "test",
        "previous_run_ids": ["run-efa23736624d"],
    },
    {
        "cell_id": "front-end-network-latency-r1",
        "service": "front-end",
        "chaos_type": "network-latency",
        "replica_idx": 1,
        "phase": "test",
        "previous_run_ids": ["run-0b3bc77bceb9"],
    },
    {
        "cell_id": "orders-container-kill-r1",
        "service": "orders",
        "chaos_type": "container-kill",
        "replica_idx": 1,
        "phase": "test",
        "previous_run_ids": ["run-9e16c0dacae2"],
    },
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def validate_reference_inputs() -> dict[str, str]:
    actual = {
        "training_base_sha256": _sha256(REFERENCE_TRAINING_BASE),
        "test_cohort_sha256": _sha256(REFERENCE_TEST_COHORT),
    }
    expected = {
        "training_base_sha256": REFERENCE_TRAINING_SHA256,
        "test_cohort_sha256": REFERENCE_TEST_SHA256,
    }
    if actual != expected:
        mismatches = ", ".join(
            f"{key}: expected {expected[key]}, got {actual[key]}"
            for key in expected
            if actual[key] != expected[key]
        )
        raise ValueError(f"the frozen v6 training base or test cohort changed ({mismatches})")
    return actual


def _configured_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected_by_id = {
        case["cell_id"]: {
            key: value for key, value in case.items() if key != "previous_run_ids"
        }
        for case in CASES
    }
    ids = [cell.get("cell_id") for cell in cells]
    ordered_ids = [case["cell_id"] for case in CASES]
    if (
        not ids
        or any(cell != expected_by_id.get(cell_id) for cell_id, cell in zip(ids, cells))
        or ids != [cell_id for cell_id in ordered_ids if cell_id in set(ids)]
    ):
        raise ValueError("supplemental plan differs from the frozen six-case matrix")
    return cells


def build_config(
    source: dict[str, Any],
    directory: Path,
    cells: list[dict[str, Any]],
    *,
    history_campaign_id: str | None = None,
) -> dict[str, Any]:
    config = campaign.build_config(source, directory, "medium", SEED, {})
    historical = config.setdefault("historical_observability", {})
    historical["campaign_id"] = history_campaign_id or directory.name
    cells = _configured_cells([
        {key: value for key, value in cell.items() if key != "previous_run_ids"}
        for cell in cells
    ])
    config["train_experiments"] = [
        cell for cell in cells if cell["phase"] == "train"
    ]
    config["test_experiments"] = [
        cell for cell in cells if cell["phase"] == "test"
    ]
    policy = config["controlled_campaign"]
    policy["level"] = "supplemental-6"
    policy["preparation_policy"] = dict(SUPPLEMENTAL_PREPARATION_POLICY)
    policy["recovery_timeout_by_service"] = {"orders": 600}
    policy["time_budget"] = build_time_budget(policy)
    config["timeline"].update({
        "warmup_seconds": 180,
        "baseline_seconds": 180,
        "fault_duration_seconds": 180,
        "post_recovery_observation_seconds": 120,
    })
    config["platform"]["run_timeout_seconds"] = (
        controlled_time_budget(policy)["poll_seconds"]
    )
    return config


def _set_campaign_dual_write(root: Path, enabled: bool) -> None:
    set_dual_write(
        "enable" if enabled else "disable",
        campaign_id=root.name,
    )


def _disable_dual_write_if_terminal(root: Path) -> None:
    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    if state.get("status") not in {
        "awaiting_calibration_review",
        "awaiting_training_base_review",
    }:
        _set_campaign_dual_write(root, False)


def _wait_for_history_visibility(
    config: dict[str, Any],
    root: Path,
    services: tuple[str, ...] = CONTROL_SERVICES,
) -> dict[str, dict[str, Any]]:
    historical = config.get("historical_observability") or {}
    campaign_id = historical.get("campaign_id")
    if campaign_id != root.name:
        raise ValueError("history campaign ID differs from the supplemental campaign")
    url = historical.get("mcp_sse_url")
    if not isinstance(url, str) or not url:
        raise ValueError("historical MCP URL is required for visibility checks")
    timeout = historical.get("visibility_timeout_seconds", 600)
    if type(timeout) is not int or timeout < 1:
        raise ValueError("historical visibility timeout must be a positive integer")

    namespace = config["cluster"]["app_namespace"]
    pods: dict[str, str] = {}
    for service in services:
        result = runner._kubectl(
            "-n", namespace, "get", "pods", "-l", f"name={service}", "-o", "json",
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(
                f"could not resolve {service} pod for historical visibility: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        items = json.loads(result.stdout).get("items")
        active = [
            pod for pod in items or []
            if not pod.get("metadata", {}).get("deletionTimestamp")
            and pod.get("status", {}).get("phase") == "Running"
        ]
        if len(active) != 1:
            raise RuntimeError(
                f"historical visibility requires exactly one running {service} pod"
            )
        pods[service] = active[0]["metadata"]["name"]

    client = MCPToolClient(url, timeout_seconds=30)
    deadline = time.monotonic() + timeout
    latest: dict[str, dict[str, Any]] = {}
    while True:
        all_fresh = True
        latest.clear()
        for service, pod in pods.items():
            query = (
                "time() - timestamp(container_memory_working_set_bytes"
                f'{{namespace={json.dumps(namespace)},pod={json.dumps(pod)},'
                f"history_campaign={json.dumps(campaign_id)}}})"
            )
            response = client.call_tool_json(
                "prometheus_instant_query", {"query": query},
            )
            if not isinstance(response, dict) or response.get("status") != "success":
                raise RuntimeError(
                    f"historical sample-age query failed for {service}: {response!r}"
                )
            data = response.get("data")
            series = data.get("result") if isinstance(data, dict) else None
            if not isinstance(series, list):
                raise RuntimeError(
                    f"historical sample-age response is malformed for {service}"
                )
            if not series:
                latest[service] = {"pod": pod, "status": "no_sample"}
                all_fresh = False
                continue
            if len(series) != 1:
                raise RuntimeError(
                    f"historical sample-age query returned {len(series)} series for {service}"
                )
            value = series[0].get("value")
            if not isinstance(value, list) or len(value) < 2:
                raise RuntimeError(
                    f"historical sample-age result is malformed for {service}"
                )
            age = float(value[1])
            if not math.isfinite(age) or age < 0:
                raise RuntimeError(
                    f"historical sample age is invalid for {service}: {age!r}"
                )
            latest[service] = {"pod": pod, "sample_age_seconds": age}
            if age > 45:
                all_fresh = False
        if all_fresh:
            return dict(latest)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"fresh historical samples did not arrive within {timeout}s: {latest}"
            )
        time.sleep(min(5.0, remaining))


def _source_config(directory: Path) -> dict[str, Any]:
    return yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))


def _case_directory(root: Path, case: dict[str, Any]) -> Path:
    return root / "cases" / f"{root.name}-{case['cell_id']}"


def _prepare_case_campaign(
    root: Path,
    source: dict[str, Any],
    case: dict[str, Any],
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    directory = _case_directory(root, case)
    cell = {key: value for key, value in case.items() if key != "previous_run_ids"}
    config = build_config(
        source, directory, [cell], history_campaign_id=root.name,
    )
    plan = [cell]
    if directory.exists():
        existing = _source_config(directory)
        if existing != config:
            raise RuntimeError(f"saved config differs from the frozen plan: {case['cell_id']}")
        if json.loads((directory / "plan.json").read_text(encoding="utf-8")) != plan:
            raise RuntimeError(f"saved plan differs from the frozen cell: {case['cell_id']}")
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "config.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8",
        )
        (directory / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_json(directory / "supplemental_cell.json", {
            "protocol_version": PROTOCOL_VERSION,
            "cell": case,
        })
    return directory, config, plan


def _latest_entry(directory: Path, cell_id: str) -> dict[str, Any]:
    entries = campaign.read_ledger(directory / "ledger.jsonl")
    matches = [entry for entry in entries if entry.get("cell_id") == cell_id]
    return matches[-1] if matches else {}


def _record_state(root: Path, state: dict[str, Any]) -> None:
    _write_json(root / "campaign_state.json", state)


def _recoverable_completed_training_case(
    root: Path,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        state.get("status") != "blocked"
        or state.get("block_reason") != "supplemental training case did not complete"
    ):
        return None
    cell_id = state.get("blocked_at_cell")
    case = next(
        (
            candidate for candidate in CASES
            if candidate["cell_id"] == cell_id and candidate["phase"] == "train"
        ),
        None,
    )
    if case is None:
        return None
    entry = _latest_entry(_case_directory(root, case), cell_id)
    gates = entry.get("gates") or {}
    if not (
        entry.get("status") == "completed"
        and entry.get("run_id") == state.get("blocked_run_id")
        and gates.get("submitted") is True
        and gates.get("intervention_confirmed_on_target") is True
        and (gates.get("mechanism_observed") or {}).get("passed") is True
        and (gates.get("recovery") or {}).get("passed") is True
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
    ):
        return None
    events_path = root / "campaign_events.jsonl"
    if not events_path.is_file():
        return None
    events = campaign.read_ledger(events_path)
    run_id = entry["run_id"]
    stopped_after_evaluation_error = any(
        event.get("event") == "supplemental_case_stopped"
        and event.get("cell_id") == cell_id
        and event.get("run_id") == run_id
        and event.get("status") == "completed"
        and event.get("execution_error")
        == "ValueError: campaign contains no test fault types"
        for event in events
    )
    already_validated = any(
        event.get("event") == "supplemental_case_validated"
        and event.get("cell_id") == cell_id
        and event.get("run_id") == run_id
        for event in events
    )
    return entry if stopped_after_evaluation_error and not already_validated else None


def clock_preflight(duration_seconds: int = CLOCK_PREFLIGHT_SECONDS) -> dict[str, Any]:
    if type(duration_seconds) is not int or duration_seconds < 1:
        raise ValueError("clock preflight duration must be a positive integer")
    report: dict[str, Any] = {
        "kind": "read_only_clock_preflight",
        "duration_seconds": duration_seconds,
        "max_clock_drift_seconds": CONTROLLED_MEASUREMENT_POLICY["max_clock_drift_seconds"],
        "load_started": False,
        "injections_submitted": 0,
        "samples": [],
        "passed": False,
        "limitation": "Past clock stability does not guarantee future stability under load.",
    }
    started = time.monotonic()
    deadline = started + duration_seconds
    while True:
        before = time.monotonic()
        wall = time.time()
        after = time.monotonic()
        sample = {
            "timestamp": wall,
            "monotonic_timestamp": (before + after) / 2,
            "pair_duration_seconds": after - before,
        }
        samples = report["samples"]
        samples.append(sample)
        try:
            if sample["pair_duration_seconds"] > report["max_clock_drift_seconds"]:
                raise GateBlocked("clock read uncertainty exceeds the clock drift limit")
            if len(samples) > 1:
                validate_cgroup_timing(samples[-2:])
                if len(samples) > 2:
                    validate_cgroup_timing([samples[0], sample])
        except GateBlocked as exc:
            report["error"] = str(exc)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report["passed"] = True
            break
        time.sleep(min(1, remaining))
    report["observed_monotonic_seconds"] = time.monotonic() - started
    report["observed_wall_seconds"] = samples[-1]["timestamp"] - samples[0]["timestamp"]
    return report


def readiness_preflight(config: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "load_started": False,
        "injections_submitted": 0,
    }
    try:
        api = runner.ApiClient(
            config["platform"]["api_base"], role=config["platform"]["user_role"],
        )
        campaign.require_controlled_backend(api)
        active = [
            run["run_id"] for run in api.get("/api/runs")
            if run["status"] in {"pending", "running", "pending_approval"}
        ]
        if active:
            raise RuntimeError(f"active runs prevent isolated readiness preflight: {active}")
    except (RuntimeError, ValueError, OSError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["backend"] = campaign.controlled_backend_capabilities()
    report["environment"] = campaign.environment_preflight(config)
    if not report["environment"]["passed"]:
        return report
    report["clock"] = clock_preflight()
    report["passed"] = report["clock"]["passed"]
    return report


def _require_readiness(root: Path, config: dict[str, Any], phase: str) -> bool:
    report = readiness_preflight(config)
    path = root / f"{phase}_readiness.json"
    _write_json(path, report)
    if not report["passed"]:
        state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
        state.update({
            "status": "blocked",
            "reason": f"{phase} readiness preflight failed before load or injection",
            "readiness_report": str(path),
            "readiness_sha256": _sha256(path),
        })
        _record_state(root, state)
    return report["passed"]


def _run_load_controls(
    root: Path,
    source: dict[str, Any],
    full_config: dict[str, Any],
) -> bool:
    safe_cell = {
        "cell_id": "front-end-memory-hog-r0",
        "service": "front-end",
        "chaos_type": "memory-hog",
        "replica_idx": 0,
        "phase": "train",
    }
    preflight_config = build_config(
        source, root / "control-preflight", [safe_cell],
        history_campaign_id=root.name,
    )
    all_passed = True
    for control_index in (1, 2):
        directory = root / "calibrations" / f"no-fault-{control_index:02d}"
        if directory.exists():
            report_path = directory / "report.json"
            if not report_path.is_file():
                raise RuntimeError(
                    f"no-fault control {control_index} was interrupted; do not rerun it in place"
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            try:
                report = load_check.run_load_check(
                    full_config,
                    directory,
                    duration_seconds=CONTROL_SECONDS,
                    measurement_fault="network-latency",
                    preflight_config=preflight_config,
                    services=CONTROL_SERVICES,
                    stability_default_relative_limit=(
                        SUPPLEMENTAL_STABILITY_POLICY["default_relative_limit"]
                    ),
                    stability_relative_limit_overrides=(
                        SUPPLEMENTAL_STABILITY_POLICY["relative_limit_overrides"]
                    ),
                    control_window_stability_mode=CONTROL_ACCEPTANCE_POLICY["long_window_stability"],
                )
            except (RuntimeError, ValueError, OSError) as exc:
                _append_jsonl(root / "campaign_events.jsonl", {
                    "event": "no_fault_control_aborted",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "index": control_index,
                    "error": f"{type(exc).__name__}: {exc}",
                    "path": str(directory),
                })
                raise
        _append_jsonl(root / "campaign_events.jsonl", {
            "event": "no_fault_control_completed",
            "at": datetime.now(timezone.utc).isoformat(),
            "index": control_index,
            "passed": report.get("passed") is True,
            "control_acceptance_policy": report.get("control_acceptance_policy"),
            "warnings": report.get("warnings", []),
            "path": str(directory),
        })
        all_passed &= report.get("passed") is True
        if report.get("control_acceptance_policy") != CONTROL_ACCEPTANCE_POLICY:
            raise RuntimeError("no-fault control acceptance policy differs from the new campaign")
        control_window = report.get("no_fault_window") or {}
        health_checks = control_window.get("health_checks") or []
        if (
            report.get("error") is not None
            or report.get("injections_submitted") != 0
            or report.get("load_stopped", {}).get("stopped") is not True
            or control_window.get("observed_seconds", 0) < CONTROL_SECONDS
            or not health_checks
            or not all(check.get("healthy") is True for check in health_checks)
        ):
            return False
    return all_passed


def _write_case_files(
    root: Path,
    source: dict[str, Any],
    case: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    directory, config, _ = _prepare_case_campaign(root, source, case)
    return directory, config


def _run_case(
    root: Path,
    source: dict[str, Any],
    case: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    directory, config = _write_case_files(root, source, case)
    has_ledger = (directory / "ledger.jsonl").is_file()
    execution_error = None
    try:
        return_code = campaign.execute(directory, resume=has_ledger)
    except (RuntimeError, ValueError, OSError) as exc:
        return_code = 1
        execution_error = f"{type(exc).__name__}: {exc}"
    entry = _latest_entry(directory, case["cell_id"])
    if return_code != 0 or entry.get("status") != "completed":
        reason = (
            entry.get("reason") or execution_error
            or f"executor returned {return_code} with status {entry.get('status')}"
        )
        _append_jsonl(root / "campaign_events.jsonl", {
            "event": "supplemental_case_stopped",
            "at": datetime.now(timezone.utc).isoformat(),
            "cell_id": case["cell_id"],
            "run_id": entry.get("run_id"),
            "status": entry.get("status"),
            "reason": reason,
            "execution_error": execution_error,
            "previous_run_ids": case["previous_run_ids"],
        })
        return entry, False
    api = runner.ApiClient(
        config["platform"]["api_base"],
        role=config["platform"]["user_role"],
    )
    entry = campaign.ensure_train_history_validation(
        directory, config, case, entry, api,
    )
    passed = campaign.train_cell_gate_passed(entry)
    _append_jsonl(root / "campaign_events.jsonl", {
        "event": "supplemental_case_validated",
        "at": datetime.now(timezone.utc).isoformat(),
        "cell_id": case["cell_id"],
        "phase": case["phase"],
        "run_id": entry.get("run_id"),
        "passed": passed,
        "previous_run_ids": case["previous_run_ids"],
    })
    return entry, passed


def _new_manifest(root: Path, source: dict[str, Any]) -> dict[str, Any]:
    references = validate_reference_inputs()
    full_config = build_config(
        source, root / "preflight", list(CASES),
        history_campaign_id=root.name,
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": root.name,
        "history_campaign_id": root.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "references": references,
        "seed": SEED,
        "measurement_policy": dict(load_check.CONTROLLED_MEASUREMENT_POLICY),
        "stability_policy": dict(SUPPLEMENTAL_STABILITY_POLICY),
        "control_acceptance_policy": dict(CONTROL_ACCEPTANCE_POLICY),
        "calibration": {
            "runs": 2,
            "duration_seconds": CONTROL_SECONDS,
            "services": list(CONTROL_SERVICES),
            "users": 30,
            "spawn_rate": 5,
            "fault_injections": 0,
            "history_visibility_timeout_seconds": full_config[
                "historical_observability"
            ]["visibility_timeout_seconds"],
        },
        "readiness_policy": {
            "clock_seconds": CLOCK_PREFLIGHT_SECONDS,
            "max_clock_drift_seconds": CONTROLLED_MEASUREMENT_POLICY["max_clock_drift_seconds"],
            "backend": campaign.controlled_backend_capabilities(),
        },
        "policy": full_config["controlled_campaign"],
        "cases": list(CASES),
        "execution_order": [case["cell_id"] for case in CASES],
        "automatic_retry_after_submission": False,
        "calibration_review_required_before_training": True,
    }


def _run_training_phase(root: Path, source: dict[str, Any]) -> bool:
    for case in CASES:
        if case["phase"] != "train":
            continue
        entry, passed = _run_case(root, source, case)
        if not passed:
            state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
            state.update({
                "status": "blocked",
                "blocked_at_cell": case["cell_id"],
                "blocked_run_id": entry.get("run_id"),
                "block_reason": (
                    entry.get("reason") or "supplemental training case did not complete"
                ),
            })
            _record_state(root, state)
            return False
    run_ids = [
        _latest_entry(_case_directory(root, case), case["cell_id"])["run_id"]
        for case in CASES if case["phase"] == "train"
    ]
    source_base = json.loads(REFERENCE_TRAINING_BASE.read_text(encoding="utf-8"))
    _write_json(root / "training_source_addendum.json", {
        "protocol_version": PROTOCOL_VERSION,
        "parent_training_base_sha256": REFERENCE_TRAINING_SHA256,
        "parent_training_run_ids": source_base["training_run_ids"],
        "supplemental_training_run_ids": run_ids,
        "supplemental_cells": [
            case["cell_id"] for case in CASES if case["phase"] == "train"
        ],
        "status": "requires_new_35_pair_base_before_test_phase",
    })
    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    state.update({
        "status": "awaiting_training_base_review",
        "training_source_addendum_sha256": _sha256(
            root / "training_source_addendum.json"
        ),
    })
    _record_state(root, state)
    return True


def _validate_training_base(path: Path, root: Path) -> dict[str, Any]:
    base = json.loads(path.read_text(encoding="utf-8"))
    parent = json.loads(REFERENCE_TRAINING_BASE.read_text(encoding="utf-8"))
    pairs = base.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != 35:
        raise ValueError("the reviewed supplemental training base must contain exactly 35 pairs")
    by_run = {
        pair.get("source_run_id"): pair for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get("source_run_id"), str)
    }
    if len(by_run) != 35:
        raise ValueError("the supplemental training base must map 35 unique source runs")
    if any(by_run.get(pair["source_run_id"]) != pair for pair in parent["pairs"]):
        raise ValueError("the reviewed base changed one or more frozen v6 training pairs")
    addendum = json.loads((root / "training_source_addendum.json").read_text(encoding="utf-8"))
    expected_ids = set(parent["training_run_ids"]) | set(
        addendum["supplemental_training_run_ids"]
    )
    if set(by_run) != expected_ids or set(base.get("training_run_ids") or []) != expected_ids:
        raise ValueError("the reviewed base does not contain exactly the 35 authorized training runs")
    expected_cells = {
        (case["service"], case["chaos_type"])
        for case in CASES if case["phase"] == "train"
    }
    new_pairs = [
        by_run[run_id] for run_id in addendum["supplemental_training_run_ids"]
    ]
    observed_cells = {
        ((pair.get("labels") or {}).get("service"),
         (pair.get("labels") or {}).get("fault_type"))
        for pair in new_pairs
    }
    if observed_cells != expected_cells:
        raise ValueError("new training pairs do not match the three approved train cells")
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "pair_count": len(pairs),
        "training_run_ids": sorted(expected_ids),
    }


def _run_test_phase(
    root: Path,
    source: dict[str, Any],
    training_base: Path,
) -> bool:
    reviewed_base = _validate_training_base(training_base, root)
    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    if state.get("status") != "awaiting_training_base_review":
        raise RuntimeError("test phase requires all three train gates and a reviewed training base")
    state.update({"reviewed_training_base": reviewed_base})
    _record_state(root, state)
    for case in CASES:
        if case["phase"] != "test":
            continue
        entry, passed = _run_case(root, source, case)
        if not passed:
            state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
            state.update({
                "status": "blocked",
                "blocked_at_cell": case["cell_id"],
                "blocked_run_id": entry.get("run_id"),
                "block_reason": (
                    entry.get("reason") or "supplemental test case did not complete"
                ),
            })
            _record_state(root, state)
            return False
    old_cohort = json.loads(REFERENCE_TEST_COHORT.read_text(encoding="utf-8"))
    derived_runs = list(old_cohort["runs"])
    for case in CASES:
        if case["phase"] != "test":
            continue
        entry = _latest_entry(_case_directory(root, case), case["cell_id"])
        cell = {key: value for key, value in case.items() if key != "previous_run_ids"}
        derived_runs.append({
            "cell": cell,
            "run_id": entry["run_id"],
            "status": entry["status"],
        })
    if len(derived_runs) != 35 or len({item["run_id"] for item in derived_runs}) != 35:
        raise RuntimeError("derived test cohort is not 35 unique runs")
    _write_json(root / "derived_test_source_cohort.json", {
        "protocol_version": PROTOCOL_VERSION,
        "parent_test_cohort_sha256": REFERENCE_TEST_SHA256,
        "training_base_sha256": reviewed_base["sha256"],
        "test_run_ids": [item["run_id"] for item in derived_runs],
        "training_run_ids": reviewed_base["training_run_ids"],
        "runs": derived_runs,
    })
    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    state["status"] = "six_cases_completed"
    state["derived_test_cohort_sha256"] = _sha256(
        root / "derived_test_source_cohort.json"
    )
    _record_state(root, state)
    return True


def _run_new(root: Path) -> int:
    source = _source_config(HERE)
    manifest = _new_manifest(root, source)
    root.mkdir(parents=True, exist_ok=False)
    _write_json(root / "campaign_manifest.json", manifest)
    _record_state(root, {
        "status": "preflighting",
        "history_campaign_id": root.name,
    })
    full_config = build_config(
        source, root / "preflight", list(CASES),
        history_campaign_id=root.name,
    )
    if not _require_readiness(root, full_config, "initial"):
        return 1
    _record_state(root, {
        "status": "calibrating",
        "history_campaign_id": root.name,
    })
    _set_campaign_dual_write(root, True)
    try:
        try:
            visible_samples = _wait_for_history_visibility(full_config, root)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            _record_state(root, {
                "status": "blocked",
                "reason": "historical telemetry visibility failed",
                "error": f"{type(exc).__name__}: {exc}",
                "history_campaign_id": root.name,
            })
            raise
        _append_jsonl(root / "campaign_events.jsonl", {
            "event": "historical_visibility_ready",
            "at": datetime.now(timezone.utc).isoformat(),
            "history_campaign_id": root.name,
            "samples": visible_samples,
        })
        try:
            controls_passed = _run_load_controls(root, source, full_config)
        except (RuntimeError, ValueError, OSError) as exc:
            _record_state(root, {
                "status": "blocked",
                "reason": "no-fault control aborted",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        if not controls_passed:
            _record_state(root, {"status": "blocked", "reason": "no-fault control failed"})
            return 1
        preflight = campaign.environment_preflight(full_config)
        _write_json(root / "preflight.json", preflight)
        if not preflight.get("passed"):
            _record_state(root, {
                "status": "blocked",
                "reason": "full six-case environment preflight failed",
                "preflight_sha256": _sha256(root / "preflight.json"),
            })
            return 1
        _record_state(root, {
            "status": "awaiting_calibration_review",
            "history_campaign_id": root.name,
            "preflight_sha256": _sha256(root / "preflight.json"),
        })
        return 0
    finally:
        _disable_dual_write_if_terminal(root)


def _continue_training(root: Path) -> int:
    validate_reference_inputs()
    manifest = json.loads((root / "campaign_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported supplemental campaign version")
    stability_policy = manifest.get("stability_policy")
    acceptance_policy = manifest.get("control_acceptance_policy")
    if acceptance_policy is not None and (
        not isinstance(acceptance_policy, dict)
        or acceptance_policy != load_check.control_acceptance_policy(
            acceptance_policy.get("long_window_stability"),
        )
    ):
        raise ValueError("unsupported control acceptance policy")
    if stability_policy is not None and (
        not isinstance(stability_policy, dict)
        or SUPPORTED_SUPPLEMENTAL_STABILITY_POLICIES.get(
            stability_policy.get("version"),
        ) != stability_policy
    ):
        raise ValueError("unsupported supplemental stability policy")
    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    recoverable_entry = _recoverable_completed_training_case(root, state)
    if (
        state.get("status") != "awaiting_calibration_review"
        and recoverable_entry is None
    ):
        raise RuntimeError("training requires a campaign awaiting calibration review")
    preflight_path = root / "preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if (
        preflight.get("passed") is not True
        or state.get("preflight_sha256") != _sha256(preflight_path)
    ):
        raise RuntimeError("saved six-case preflight is missing, failed, or changed")
    for index in (1, 2):
        report_path = root / "calibrations" / f"no-fault-{index:02d}" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            report.get("passed") is not True
            or report.get("injections_submitted") != 0
            or report.get("no_fault_window_seconds") != CONTROL_SECONDS
        ):
            raise RuntimeError(f"no-fault control {index} is not approved for training")
        expected_report_policy = None if stability_policy is None else {
            "default_relative_limit": stability_policy["default_relative_limit"],
            "relative_limit_overrides": stability_policy["relative_limit_overrides"],
        }
        if (
            expected_report_policy is not None
            and report.get("stability_policy") != expected_report_policy
        ):
            raise RuntimeError(
                f"no-fault control {index} stability policy does not match campaign manifest"
            )
        report_acceptance = report.get("control_acceptance_policy")
        if (
            acceptance_policy is not None and report_acceptance != acceptance_policy
            or acceptance_policy is None
            and report_acceptance not in (None, load_check.control_acceptance_policy())
        ):
            raise RuntimeError(
                f"no-fault control {index} acceptance policy does not match campaign manifest"
            )

    source = _source_config(HERE)
    config = build_config(
        source, root / "training_preflight", list(CASES),
        history_campaign_id=root.name,
    )
    if not _require_readiness(root, config, "training"):
        _set_campaign_dual_write(root, False)
        return 1
    _set_campaign_dual_write(root, True)
    try:
        try:
            visible_samples = _wait_for_history_visibility(config, root)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            _record_state(root, {
                "status": "blocked",
                "reason": "historical telemetry visibility failed before training",
                "error": f"{type(exc).__name__}: {exc}",
                "history_campaign_id": root.name,
            })
            raise
        _append_jsonl(root / "campaign_events.jsonl", {
            "event": "historical_visibility_ready",
            "at": datetime.now(timezone.utc).isoformat(),
            "history_campaign_id": root.name,
            "phase": "train",
            "samples": visible_samples,
        })
        training_preflight = campaign.environment_preflight(config)
        _write_json(root / "training_preflight.json", training_preflight)
        if not training_preflight.get("passed"):
            _record_state(root, {
                "status": "blocked",
                "reason": "fresh training preflight failed",
                "training_preflight_sha256": _sha256(root / "training_preflight.json"),
            })
            return 1
        _record_state(root, {
            "status": "collecting_training",
            "history_campaign_id": root.name,
            "preflight_sha256": _sha256(preflight_path),
            "training_preflight_sha256": _sha256(root / "training_preflight.json"),
            "calibration_reviewed_at": datetime.now(timezone.utc).isoformat(),
        })
        if recoverable_entry is not None:
            _append_jsonl(root / "campaign_events.jsonl", {
                "event": "resuming_completed_training_case_after_postprocessing_error",
                "at": datetime.now(timezone.utc).isoformat(),
                "cell_id": state["blocked_at_cell"],
                "run_id": recoverable_entry["run_id"],
                "original_execution_error": (
                    "ValueError: campaign contains no test fault types"
                ),
                "fault_injection_repeated": False,
            })
        return 0 if _run_training_phase(root, source) else 1
    finally:
        _disable_dual_write_if_terminal(root)


def _continue_tests(root: Path, training_base: Path) -> int:
    validate_reference_inputs()
    manifest = json.loads((root / "campaign_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported supplemental campaign version")
    source = _source_config(HERE)
    config = build_config(
        source, root / "preflight", list(CASES),
        history_campaign_id=root.name,
    )
    if not _require_readiness(root, config, "test"):
        _set_campaign_dual_write(root, False)
        return 1
    _set_campaign_dual_write(root, True)
    try:
        try:
            visible_samples = _wait_for_history_visibility(config, root)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            _record_state(root, {
                "status": "blocked",
                "reason": "historical telemetry visibility failed before tests",
                "error": f"{type(exc).__name__}: {exc}",
                "history_campaign_id": root.name,
            })
            raise
        _append_jsonl(root / "campaign_events.jsonl", {
            "event": "historical_visibility_ready",
            "at": datetime.now(timezone.utc).isoformat(),
            "history_campaign_id": root.name,
            "phase": "test",
            "samples": visible_samples,
        })
        preflight = campaign.environment_preflight(config)
        _write_json(root / "test_preflight.json", preflight)
        if not preflight.get("passed"):
            _record_state(root, {
                "status": "blocked",
                "reason": "test-phase environment preflight failed",
                "preflight_sha256": _sha256(root / "test_preflight.json"),
            })
            return 1
        return 0 if _run_test_phase(root, source, training_base) else 1
    finally:
        _disable_dual_write_if_terminal(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the frozen plan only")
    mode.add_argument("--preflight", action="store_true", help="read-only six-case readiness and 600-second clock check; no load or injection")
    mode.add_argument("--run", action="store_true", help="run controls and preflight, then pause for calibration review")
    mode.add_argument("--continue-training", action="store_true", help="start training after calibration review")
    mode.add_argument("--continue-tests", action="store_true", help="continue only after a reviewed 35-pair base exists")
    parser.add_argument("--resume", type=Path, help="existing supplemental campaign directory")
    parser.add_argument("--training-base", type=Path, help="reviewed derived 35-pair rag_base.json")
    args = parser.parse_args(argv)
    if args.continue_tests and (args.resume is None or args.training_base is None):
        parser.error("--continue-tests requires --resume and --training-base")
    if args.continue_training and (args.resume is None or args.training_base is not None):
        parser.error("--continue-training requires --resume and does not take --training-base")
    if not args.continue_tests and not args.continue_training and (
        args.resume is not None or args.training_base is not None
    ):
        parser.error("--resume and --training-base require a continuation mode")
    references = validate_reference_inputs()
    if args.preflight:
        source = _source_config(HERE)
        config = build_config(
            source, HERE / "out" / "supplemental-readiness", list(CASES),
        )
        report = readiness_preflight(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    if args.plan or not (
        args.run or args.continue_training or args.continue_tests
    ):
        print(json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "references": references,
            "controls": {
                "count": 2,
                "duration_seconds": CONTROL_SECONDS,
                "services": list(CONTROL_SERVICES),
                "fault_injections": 0,
                "acceptance_policy": CONTROL_ACCEPTANCE_POLICY,
            },
            "cases": list(CASES),
            "preparation_policy": SUPPLEMENTAL_PREPARATION_POLICY,
            "stability_policy": SUPPLEMENTAL_STABILITY_POLICY,
            "no_automatic_retry_after_submission": True,
            "calibration_review_required_before_training": True,
        }, ensure_ascii=False, indent=2))
        return 0
    if args.run:
        root = HERE / "out" / "supplemental_campaign" / (
            "supplemental-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        root.parent.mkdir(parents=True, exist_ok=True)
        with (root.parent / ".supplemental.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another supplemental campaign is active") from exc
            return _run_new(root)
    assert args.resume is not None
    root = args.resume.resolve()
    with (root / ".supplemental.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another supplemental campaign is active") from exc
        if args.continue_training:
            return _continue_training(root)
        assert args.training_base is not None
        return _continue_tests(root, args.training_base.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
