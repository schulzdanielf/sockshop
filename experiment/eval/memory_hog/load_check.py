"""No-fault, single-load rehearsal using the controlled campaign's actual gates."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from experiment.platform.backend.adapters.noop_notifier import NoopNotificationAdapter
from experiment.platform.backend.controlled_experiment import (
    CONTROLLED_MEASUREMENT_POLICY, DEFAULT_STABILITY_RELATIVE_LIMIT,
    ControlledRun, GateBlocked, SERVICES, resource_environment, stability_metrics,
)
from experiment.platform.backend.engine import OrchestratorEngine
from experiment.platform.backend.plugins.litmus_plugin import LitmusChaosPlugin
from experiment.platform.backend.plugins.locust_plugin import LocustLoadPlugin
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.plugins.mcp_metrics_plugin import MCPPrometheusMetricsPlugin
from experiment.platform.backend.plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage

SAMPLE_INTERVAL_SECONDS = 5
MAX_CONTROL_WINDOW_CLOCK_EXTENSION_SECONDS = 30
DEFAULT_PREPARATION_POLICY = {
    "version": "load-check-default-preparation-v1",
    "warmup_min_seconds": 60,
    "stabilization_window_seconds": 30,
    "stable_windows_required": 1,
    "preparation_timeout_seconds": 600,
    "baseline_seconds": 90,
    "health_check_interval_seconds": 15,
}


def control_acceptance_policy(stability_mode: str = "blocking") -> dict[str, Any]:
    if not isinstance(stability_mode, str) or stability_mode not in {"blocking", "diagnostic"}:
        raise ValueError("control-window stability mode must be blocking or diagnostic")
    return {
        "version": f"no-fault-control-{stability_mode}-v1",
        "long_window_stability": stability_mode,
        "preparation_stability": "blocking",
        "coverage_health_identity_safety_cleanup": "blocking",
        "per_run_gates_changed": False,
    }


def _preparation_policy(config: dict[str, Any]) -> dict[str, Any]:
    configured = config["controlled_campaign"].get("preparation_policy")
    policy = dict(configured or DEFAULT_PREPARATION_POLICY)
    required = (
        "warmup_min_seconds",
        "stabilization_window_seconds",
        "stable_windows_required",
        "preparation_timeout_seconds",
        "baseline_seconds",
        "health_check_interval_seconds",
    )
    if any(
        type(policy.get(key)) is not int or policy[key] <= 0
        for key in required
    ):
        raise ValueError("load-check preparation policy must contain positive integer durations")
    if (
        policy["preparation_timeout_seconds"]
        < policy["warmup_min_seconds"]
        + policy["stable_windows_required"] * policy["stabilization_window_seconds"]
    ):
        raise ValueError("load-check preparation timeout cannot fit warmup and stable windows")
    return policy


def run_late_operational_recovery_check(
    source_run_id: str,
    source_report_path: Path,
    effective_spec_path: Path,
    directory: Path,
    attempt_number: int = 1,
    latency_p95_limit_override_ms: float | None = None,
) -> dict[str, Any]:
    if type(attempt_number) is not int or attempt_number < 1:
        raise ValueError("late recovery attempt number must be a positive integer")
    if latency_p95_limit_override_ms is not None and (
        type(latency_p95_limit_override_ms) not in (int, float)
        or not 0 < latency_p95_limit_override_ms < float("inf")
    ):
        raise ValueError("late recovery latency P95 limit override must be finite and positive")
    source_bytes = source_report_path.read_bytes()
    source = json.loads(source_bytes)
    spec = json.loads(effective_spec_path.read_text(encoding="utf-8"))
    source_gates = source.get("gates") or {}
    operational = source_gates.get("operational_recovery") or {}
    if (
        source.get("status") != "aborted"
        or (source_gates.get("recovery") or {}).get("passed") is not False
        or operational.get("passed") is not False
        or (source_gates.get("mechanism_observed") or {}).get("passed") is not True
        or (source_gates.get("cleanup") or {}).get("passed") is not True
        or (source_gates.get("load_stopped") or {}).get("stopped") is not True
        or spec.get("controlled_campaign", {}).get("fault") != "container-kill"
        or spec.get("controlled_campaign", {}).get("service") != "orders"
        or source.get("fault") != "container-kill"
        or source.get("service") != "orders"
    ):
        raise ValueError("source run is not eligible for a late passive recovery check")
    resilience = spec["controlled_campaign"].get("resilience") or {}
    if (
        resilience.get("operational_timeout_seconds") != 600
        or resilience.get("stable_seconds") != 120
    ):
        raise ValueError("source run recovery policy differs from the authorized late-check limits")

    baseline_observation = next(
        (item for item in source.get("observations", []) if item.get("phase") == "baseline"),
        None,
    )
    baseline = (baseline_observation or {}).get("summary") or {}
    required_metrics = ("traffic", "cpu_cores", "memory_bytes", "errors_percent", "latency_ms")
    if any((baseline.get(key) or {}).get("status") != "ok" for key in required_metrics):
        raise ValueError("source run baseline is missing a required recovery metric")

    source_event = source.get("container_kill_event") or {}
    source_target = source.get("target_before") or {}
    event_target = source_event.get("target") or {}
    if (
        source_event.get("restart_observed") is not True
        or not source_event.get("container_id_after_restart")
        or type(source_event.get("restart_count_after_signal")) is not int
        or not source_target.get("uid")
        or event_target.get("pod_uid") != source_target.get("uid")
        or event_target.get("container_id") != source_target.get("container_id")
        or event_target.get("restart_count") != source_target.get("restart_count")
        or source_event["restart_count_after_signal"] != source_target.get("restart_count", -1) + 1
        or source_event["container_id_after_restart"] == source_target.get("container_id")
        or (operational.get("target") or {}).get("container_id")
        != source_event["container_id_after_restart"]
        or (operational.get("target") or {}).get("restart_count")
        != source_event["restart_count_after_signal"]
    ):
        raise ValueError("source run lacks confirmed container identity for late recovery")

    load_profile = dict(spec["load_profile"])
    if load_profile.get("users") != 30 or load_profile.get("spawn_rate") != 5:
        raise ValueError("late recovery check requires the campaign's 30-user load profile")
    check_id = (
        f"late-recovery-{source_run_id}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    engine = OrchestratorEngine(
        storage=SqliteStorage(directory / "observations.db", directory / "artifacts"),
        chaos=LitmusChaosPlugin(), load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(), traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=Path(__file__).resolve().parents[2] / "platform/data/KILL_SWITCH",
    )
    run = ControlledRun(engine, check_id, spec)
    run.target = source_target
    run.baseline = baseline
    run.report.update({
        "status": "late_recovery_check",
        "run_id": check_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_run_id": source_run_id,
        "source_report_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "late_recovery_policy": {
            "timeout_seconds": 600,
            "stable_seconds": 120,
            "attempt": attempt_number,
            "max_attempts": 3,
            "same_load": True,
            "same_baseline": True,
            "thresholds_unchanged": latency_p95_limit_override_ms is None,
            "other_thresholds_unchanged": True,
            "latency_p95_limit_override_ms": latency_p95_limit_override_ms,
            "fault_injection": False,
            "resource_measurement_source": "cgroup-v2",
            "resource_measurement_scope": "exact application container",
            "amendment_version": (
                "controlled-late-operational-recovery-check-v3"
                if latency_p95_limit_override_ms is not None else None
            ),
        },
        "target_before": source_target,
        "container_kill_event": {
            **source_event,
            "observations": [],
        },
    })
    engine._runtime_handles.setdefault(check_id, {})
    engine._stop_flags[check_id] = threading.Event()
    report_path = directory / "report.json"
    handle: dict[str, Any] | None = None
    failure: Exception | None = None

    def persist() -> None:
        report_path.write_text(json.dumps(run.report, indent=2) + "\n", encoding="utf-8")

    persist()
    try:
        engine.metrics.validate(spec["observability"]["metrics"])
        status = run.application_container_status()
        expected = source_event["container_id_after_restart"]
        expected_restarts = source_event["restart_count_after_signal"]
        if (
            status["container_id"] != expected
            or status["restart_count"] != expected_restarts
            or not status["ready"]
            or not status["running"]
        ):
            raise RuntimeError("current application container differs from the confirmed source restart")
        health = run.health()
        run.report["pre_load_health"] = health
        if not health["healthy"]:
            raise RuntimeError("application or node health failed before late recovery load")

        load = dict(
            load_profile,
            controlled_remote=True,
            run_time_seconds=900,
        )
        engine.load.validate(load)
        run.start_cgroup_sampling()
        run.report["late_recovery_policy"]["resource_sampling_started_before_load"] = True
        persist()
        handle = engine.load.start_load(run.context, load)
        run.load_handle = handle
        run.report["load_handle"] = handle
        persist()
        run.wait(60)
        if latency_p95_limit_override_ms is None:
            passed = run.operational_recovery(
                gate_name="late_operational_recovery",
                timeout_seconds=600,
            )
        else:
            passed = run.operational_recovery(
                gate_name="late_operational_recovery",
                timeout_seconds=600,
                latency_p95_limit_override_ms=latency_p95_limit_override_ms,
            )
        run.report["late_operational_recovery_passed"] = passed
    except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        failure = exc
        run.report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            try:
                stopped = engine.load.stop_load(run.context, handle)
                run.report["load_stopped"] = stopped
            except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
                run.report["load_stop_error"] = f"{type(exc).__name__}: {exc}"
                run.report["load_stopped"] = {"stopped": False}
            else:
                if stopped.get("stopped") is True:
                    try:
                        run.report["load_summary"] = engine.load.collect_summary(run.context, handle)
                    except (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
                        run.report["load_summary_error"] = f"{type(exc).__name__}: {exc}"
        gate = run.report["gates"].get("late_operational_recovery") or {}
        run.report["passed"] = bool(
            failure is None
            and gate.get("passed") is True
            and (run.report.get("load_stopped") or {}).get("stopped") is True
        )
        run.report["ended_at"] = datetime.now(timezone.utc).isoformat()
        persist()
    return run.report


def run_load_check(
    config: dict[str, Any],
    directory: Path,
    *,
    duration_seconds: int | None = None,
    measurement_fault: str = "cpu-hog",
    preflight_config: dict[str, Any] | None = None,
    services: tuple[str, ...] = SERVICES,
    stability_default_relative_limit: float = DEFAULT_STABILITY_RELATIVE_LIMIT,
    stability_relative_limit_overrides: dict[str, dict[str, float]] | None = None,
    control_window_stability_mode: str = "blocking",
) -> dict[str, Any]:
    from .controlled_campaign import build_spec, require_environment

    if duration_seconds is not None and (
        type(duration_seconds) is not int or not 60 <= duration_seconds <= 3600
    ):
        raise ValueError("load-check duration must be an integer from 60 through 3600 seconds")
    if measurement_fault not in {"cpu-hog", "network-latency"}:
        raise ValueError("load-check measurement fault must be cpu-hog or network-latency")
    if not services or any(service not in SERVICES for service in services):
        raise ValueError("load-check services must be a non-empty subset of controlled services")
    if (
        type(stability_default_relative_limit) not in (int, float)
        or not math.isfinite(stability_default_relative_limit)
        or not 0 < stability_default_relative_limit <= 1
    ):
        raise ValueError("load-check default stability relative limit must be finite and in (0, 1]")
    if stability_relative_limit_overrides is None:
        stability_relative_limit_overrides = {}
    if (
        not isinstance(stability_relative_limit_overrides, dict)
        or not set(stability_relative_limit_overrides).issubset(services)
        or any(not isinstance(limits, dict) for limits in stability_relative_limit_overrides.values())
    ):
        raise ValueError("stability relative-limit overrides must target requested services")
    allowed_stability_metrics = {
        "traffic", "cpu_cores", "memory_bytes", "latency_ms",
    }
    if measurement_fault == "network-latency":
        allowed_stability_metrics.update({
            "target_edge_requests", "target_edge_latency_ms",
        })
    if any(
        not set(limits).issubset(allowed_stability_metrics)
        for limits in stability_relative_limit_overrides.values()
    ):
        raise ValueError("stability relative-limit override names an unavailable metric")
    if any(
        type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 1
        for limits in stability_relative_limit_overrides.values()
        for limit in limits.values()
    ):
        raise ValueError("stability relative-limit overrides must be finite and in (0, 1]")
    acceptance_policy = control_acceptance_policy(control_window_stability_mode)
    if control_window_stability_mode == "diagnostic" and duration_seconds is None:
        raise ValueError("diagnostic control-window stability requires a fixed-duration control")
    preparation_policy = _preparation_policy(config)
    directory.mkdir(parents=True, exist_ok=False)
    require_environment(preflight_config or config, directory)
    engine = OrchestratorEngine(
        storage=SqliteStorage(directory / "observations.db", directory / "artifacts"),
        chaos=LitmusChaosPlugin(), load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(), traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=Path(__file__).resolve().parents[2] / "platform/data/KILL_SWITCH",
    )
    rehearsal_config = {key: value for key, value in config.items() if key != "knowledge_campaign"}
    runs = []
    for service in services:
        spec = build_spec(rehearsal_config, {
            "service": service, "chaos_type": measurement_fault, "replica_idx": 0,
            "cell_id": f"load-check-{service}",
        }, directory)
        run = ControlledRun(engine, f"{directory.name}-{service}", spec)
        engine._stop_flags[run.run_id] = threading.Event()
        run.inspect_target()
        run.metadata()
        run.start_cgroup_sampling()
        runs.append(run)
    context = {
        "run_id": f"{directory.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}",
        "experiment_id": "load-only-rehearsal",
    }
    report: dict[str, Any] = {
        "kind": "load_only_rehearsal", "started_at": datetime.now(timezone.utc).isoformat(),
        "injections_submitted": 0, "users": 30, "spawn_rate": 5,
        "measurement_fault": measurement_fault,
        "measurement_policy": dict(CONTROLLED_MEASUREMENT_POLICY),
        "stability_policy": {
            "default_relative_limit": stability_default_relative_limit,
            "relative_limit_overrides": {
                service: dict(limits)
                for service, limits in stability_relative_limit_overrides.items()
            },
        },
        "no_fault_window_seconds": duration_seconds,
        "control_acceptance_policy": acceptance_policy,
        "warnings": [],
        "preparation_policy": preparation_policy,
        "preparation": {
            "policy": preparation_policy,
            "health_checks": [],
            "stabilization_windows": [],
        },
        "services": {}, "limitations": [
            "No restart or fault recovery tested; this is not a completed campaign cell.",
            "Baseline trace coverage does not guarantee fault-window trace coverage.",
        ], "passed": False,
    }
    load = dict(
        config["load_profile"],
        controlled_remote=True,
        run_time_seconds=duration_seconds + 1800 if duration_seconds else 1800,
    )
    handle = None
    stop_sampling = threading.Event()
    sampling_errors: list[Exception] = []
    samplers: list[threading.Thread] = []
    completed = False

    def persist() -> None:
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def sample_continuously(run: ControlledRun) -> None:
        while not stop_sampling.is_set():
            started = time.monotonic()
            try:
                if run.application_container_status()["container_id"] != run.target["container_id"]:
                    raise GateBlocked("container changed during load-only rehearsal")
                run.sample_cgroup()
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                sampling_errors.append(exc)
                stop_sampling.set()
                return
            stop_sampling.wait(max(0, SAMPLE_INTERVAL_SECONDS - (time.monotonic() - started)))

    def require_sampler() -> None:
        if sampling_errors:
            raise RuntimeError(f"rehearsal sampling failed: {sampling_errors[0]}") from sampling_errors[0]
        if any(not sampler.is_alive() for sampler in samplers) and not stop_sampling.is_set():
            raise RuntimeError("rehearsal sampler terminated unexpectedly")

    def sample_wait(
        seconds: int,
        *,
        stage: str,
        deadline: float | None = None,
    ) -> float:
        started = time.monotonic()
        stage_deadline = started + seconds
        if deadline is not None:
            stage_deadline = min(stage_deadline, deadline)
        next_health_check = started
        while time.monotonic() < stage_deadline:
            require_sampler()
            if engine.load.status(context, handle).get("state") != "running":
                raise RuntimeError("rehearsal load stopped before collection finished")
            now = time.monotonic()
            if now >= next_health_check:
                health = runs[0].health()
                report["preparation"]["health_checks"].append({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "stage": stage,
                    "healthy": health["healthy"],
                    "health": health,
                })
                if not health["healthy"]:
                    raise GateBlocked(f"application health failed during {stage}")
                next_health_check = now + preparation_policy["health_check_interval_seconds"]
                persist()
            remaining = stage_deadline - time.monotonic()
            until_health_check = next_health_check - time.monotonic()
            wait_seconds = min(SAMPLE_INTERVAL_SECONDS, remaining)
            if until_health_check > 0:
                wait_seconds = min(wait_seconds, until_health_check)
            engine._sleep_interruptible(
                engine._stop_flags[runs[0].run_id],
                max(0, wait_seconds),
            )
        return time.monotonic() - started

    try:
        engine.load.validate(load)
        handle = engine.load.start_load(context, load)
        report["load_handle"] = handle
        preparation_started = time.monotonic()
        preparation_deadline = (
            preparation_started + preparation_policy["preparation_timeout_seconds"]
        )
        report["preparation"].update({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "deadline_seconds": preparation_policy["preparation_timeout_seconds"],
        })
        persist()
        for run in runs:
            sampler = threading.Thread(target=sample_continuously, args=(run,),
                                       name=f"load-check-cgroup-{run.service}")
            sampler.start()
            samplers.append(sampler)
        sample_wait(
            preparation_policy["warmup_min_seconds"],
            stage="warmup",
            deadline=preparation_deadline,
        )
        stable_windows = 0
        attempt = 0
        while (
            stable_windows < preparation_policy["stable_windows_required"]
            and time.monotonic() + preparation_policy["stabilization_window_seconds"]
            <= preparation_deadline
        ):
            start = datetime.now(timezone.utc).isoformat()
            stage = f"stabilization_{attempt}"
            elapsed = sample_wait(
                preparation_policy["stabilization_window_seconds"],
                stage=stage,
                deadline=preparation_deadline,
            )
            end = datetime.now(timezone.utc).isoformat()
            outcome: dict[str, Any] = {
                "attempt": attempt,
                "start": start,
                "end": end,
                "required_seconds": preparation_policy["stabilization_window_seconds"],
                "observed_seconds": elapsed,
                "services": {},
                "passed": True,
            }
            for run in runs:
                service_outcome: dict[str, Any] = {}
                try:
                    summary = run.collect(stage, start, end)
                    run.require_coverage(
                        summary, preparation_policy["stabilization_window_seconds"],
                    )
                    checks = stability_metrics(
                        run.raw[stage]["data"],
                        include_target_edge=measurement_fault == "network-latency",
                        default_relative_limit=stability_default_relative_limit,
                        relative_limit_overrides=stability_relative_limit_overrides.get(
                            run.service,
                        ),
                    )
                    service_outcome.update(
                        metrics=checks,
                        passed=all(check["passed"] for check in checks.values()),
                    )
                except GateBlocked as exc:
                    service_outcome.update(passed=False, error=str(exc))
                outcome["services"][run.service] = service_outcome
                outcome["passed"] &= service_outcome["passed"]
            if elapsed < preparation_policy["stabilization_window_seconds"]:
                outcome.update(passed=False, error="preparation timeout interrupted the stability window")
            if time.monotonic() > preparation_deadline:
                outcome.update(passed=False, error="preparation timeout exceeded")
            stable_windows = stable_windows + 1 if outcome["passed"] else 0
            outcome["consecutive_stable_windows"] = stable_windows
            report["preparation"]["stabilization_windows"].append(outcome)
            for run in runs:
                report["services"].setdefault(
                    run.service, {"stabilization": []},
                )["stabilization"].append({
                    "attempt": attempt,
                    **outcome["services"][run.service],
                })
            attempt += 1
            require_sampler()
            persist()
        stable = stable_windows >= preparation_policy["stable_windows_required"]
        report["preparation"].update({
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - preparation_started,
            "consecutive_stable_windows": stable_windows,
            "stabilization_passed": bool(stable),
            "timed_out": bool(
                not stable
                and time.monotonic()
                + preparation_policy["stabilization_window_seconds"]
                > preparation_deadline
            ),
        })
        report["stabilization_passed"] = bool(stable)
        start = datetime.now(timezone.utc).isoformat()
        sample_wait(
            preparation_policy["baseline_seconds"],
            stage="baseline",
        )
        end = datetime.now(timezone.utc).isoformat()
        report["baseline"] = {
            "start": start,
            "end": end,
            "duration_seconds": preparation_policy["baseline_seconds"],
            "eligible": bool(stable),
        }
        client = MCPToolClient(runs[0].spec["observability"]["metrics"]["mcp_sse_url"], timeout_seconds=30)
        passed = bool(stable)
        for run in runs:
            result = report["services"][run.service]
            try:
                baseline = run.collect("baseline", start, end)
                result["baseline"] = baseline
                run.require_coverage(baseline, preparation_policy["baseline_seconds"])
                run.baseline = baseline
                result["memory_profile"] = resource_environment("memory-hog", run.target["limits"], baseline, {})
                if baseline["memory_usage_bytes"]["max"] >= .85 * baseline["memory_limit_bytes"]["mean"]:
                    raise GateBlocked("baseline memory already >=85% of application limit")
                traces = client.call_tool_json("tempo_search_traces", {
                    "service_name": run.service, "start": start, "end": end, "limit": 5,
                }, timeout_seconds=30)
                run.save("baseline_trace_search", traces)
                if not isinstance(traces, dict) or traces.get("error") or not traces.get("traces"):
                    raise GateBlocked(f"no fresh baseline traces for {run.service}")
                result["trace_count"] = len(traces["traces"])
                result["passed"] = True
            except GateBlocked as exc:
                result.update(passed=False, error=str(exc))
                passed = False
            persist()
        control_window_passed = True
        if duration_seconds is not None:
            control_start_monotonic = time.monotonic()
            control_start_epoch = time.time()
            control_start = datetime.fromtimestamp(
                control_start_epoch, timezone.utc,
            ).isoformat()
            report["no_fault_window"] = {
                "start": control_start,
                "duration_seconds": duration_seconds,
                "max_wall_clock_extension_seconds": (
                    MAX_CONTROL_WINDOW_CLOCK_EXTENSION_SECONDS
                ),
                "health_checks": [],
                "services": {},
            }
            control_deadline = control_start_monotonic + duration_seconds
            maximum_deadline = (
                control_deadline + MAX_CONTROL_WINDOW_CLOCK_EXTENSION_SECONDS
            )
            while True:
                monotonic_now = time.monotonic()
                wall_elapsed = time.time() - control_start_epoch
                monotonic_elapsed = monotonic_now - control_start_monotonic
                if (
                    monotonic_elapsed >= duration_seconds
                    and wall_elapsed >= duration_seconds
                ):
                    break
                if monotonic_now >= maximum_deadline:
                    raise GateBlocked(
                        "wall-clock no-fault interval did not reach the required "
                        f"{duration_seconds}s within the bounded "
                        f"{MAX_CONTROL_WINDOW_CLOCK_EXTENSION_SECONDS}s extension"
                    )
                require_sampler()
                if engine.load.status(context, handle).get("state") != "running":
                    raise RuntimeError("load stopped before no-fault control window finished")
                health = runs[0].health()
                report["no_fault_window"]["health_checks"].append({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "healthy": health["healthy"],
                    "health": health,
                })
                if not health["healthy"]:
                    raise GateBlocked("application health failed during no-fault control window")
                persist()
                if monotonic_elapsed < duration_seconds:
                    remaining = duration_seconds - monotonic_elapsed
                else:
                    remaining = duration_seconds - wall_elapsed
                engine._sleep_interruptible(
                    engine._stop_flags[runs[0].run_id],
                    min(
                        15,
                        max(0, remaining),
                        max(0, maximum_deadline - time.monotonic()),
                    ),
                )
            control_end_monotonic = time.monotonic()
            control_end_epoch = time.time()
            control_end = datetime.fromtimestamp(
                control_end_epoch, timezone.utc,
            ).isoformat()
            monotonic_observed = (
                control_end_monotonic - control_start_monotonic
            )
            report["no_fault_window"].update({
                "end": control_end,
                "observed_seconds": control_end_epoch - control_start_epoch,
                "monotonic_observed_seconds": monotonic_observed,
                "wall_clock_extension_seconds": max(
                    0, monotonic_observed - duration_seconds,
                ),
            })
            for run in runs:
                result = report["services"][run.service]
                try:
                    summary = run.collect("no_fault_control", control_start, control_end)
                    run.require_coverage(summary, duration_seconds)
                    checks = stability_metrics(
                        run.raw["no_fault_control"]["data"],
                        include_target_edge=measurement_fault == "network-latency",
                        default_relative_limit=stability_default_relative_limit,
                        relative_limit_overrides=stability_relative_limit_overrides.get(
                            run.service,
                        ),
                    )
                    stability_passed = all(check["passed"] for check in checks.values())
                    service_passed = (
                        stability_passed or control_window_stability_mode == "diagnostic"
                    )
                    if not stability_passed and control_window_stability_mode == "diagnostic":
                        report["warnings"].append({
                            "service": run.service,
                            "phase": "no_fault_control",
                            "reason": "long-window variability exceeded the diagnostic limits",
                            "metrics": [key for key, check in checks.items() if not check["passed"]],
                        })
                    report["no_fault_window"]["services"][run.service] = {
                        "summary": summary,
                        "stability": checks,
                        "stability_passed": stability_passed,
                        "passed": service_passed,
                    }
                    control_window_passed &= service_passed
                except GateBlocked as exc:
                    report["no_fault_window"]["services"][run.service] = {
                        "passed": False,
                        "stability_passed": False,
                        "error": str(exc),
                    }
                    control_window_passed = False
                result["control_window_passed"] = (
                    report["no_fault_window"]["services"][run.service]["passed"]
                )
            report["no_fault_window"]["stability_passed"] = all(
                item["stability_passed"]
                for item in report["no_fault_window"]["services"].values()
            )
            persist()
        report["passed"] = bool(
            passed and control_window_passed and runs[0].health()["healthy"]
        )
        require_sampler()
        completed = True
    except (RuntimeError, ValueError, OSError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        candidate_passed = report["passed"] and completed
        report["passed"] = False
        stop_sampling.set()
        for sampler in samplers:
            sampler.join()
        try:
            if handle is not None:
                stopped = engine.load.stop_load(context, handle)
                report["load_stopped"] = stopped
                if stopped.get("stopped") is not True:
                    report["passed"] = False
                    raise RuntimeError(f"rehearsal load cleanup unconfirmed: {stopped}")
                report["load_summary"] = engine.load.collect_summary(context, handle)
            if sampling_errors:
                raise RuntimeError(f"rehearsal sampling failed: {sampling_errors[0]}") from sampling_errors[0]
            report["passed"] = bool(candidate_passed)
        finally:
            report["ended_at"] = datetime.now(timezone.utc).isoformat()
            persist()
    return report
