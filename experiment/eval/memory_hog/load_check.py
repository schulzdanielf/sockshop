"""No-fault, single-load rehearsal using the controlled campaign's actual gates."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from experiment.platform.backend.adapters.noop_notifier import NoopNotificationAdapter
from experiment.platform.backend.controlled_experiment import (
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


def run_load_check(config: dict[str, Any], directory: Path) -> dict[str, Any]:
    from .controlled_campaign import build_spec, require_environment

    directory.mkdir(parents=True, exist_ok=False)
    require_environment(config, directory)
    engine = OrchestratorEngine(
        storage=SqliteStorage(directory / "observations.db", directory / "artifacts"),
        chaos=LitmusChaosPlugin(), load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(), traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=Path(__file__).resolve().parents[2] / "platform/data/KILL_SWITCH",
    )
    rehearsal_config = {key: value for key, value in config.items() if key != "knowledge_campaign"}
    runs = []
    for service in SERVICES:
        spec = build_spec(rehearsal_config, {
            "service": service, "chaos_type": "cpu-hog", "replica_idx": 0,
            "cell_id": f"load-check-{service}",
        }, directory)
        run = ControlledRun(engine, f"{directory.name}-{service}", spec)
        engine._stop_flags[run.run_id] = threading.Event()
        run.inspect_target()
        run.metadata()
        run.start_cgroup_sampling()
        runs.append(run)
    context = {"run_id": directory.name, "experiment_id": "load-only-rehearsal"}
    report: dict[str, Any] = {
        "kind": "load_only_rehearsal", "started_at": datetime.now(timezone.utc).isoformat(),
        "injections_submitted": 0, "users": 30, "spawn_rate": 5,
        "services": {}, "limitations": [
            "No restart or fault recovery tested; this is not a completed campaign cell.",
            "Baseline trace coverage does not guarantee fault-window trace coverage.",
        ], "passed": False,
    }
    load = dict(config["load_profile"], controlled_remote=True, run_time_seconds=1800)
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

    def sample_wait(seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            require_sampler()
            if engine.load.status(context, handle).get("state") != "running":
                raise RuntimeError("rehearsal load stopped before collection finished")
            engine._sleep_interruptible(engine._stop_flags[runs[0].run_id],
                                        min(5, max(0, deadline - time.monotonic())))

    try:
        engine.load.validate(load)
        handle = engine.load.start_load(context, load)
        report["load_handle"] = handle
        persist()
        for run in runs:
            sampler = threading.Thread(target=sample_continuously, args=(run,),
                                       name=f"load-check-cgroup-{run.service}")
            sampler.start()
            samplers.append(sampler)
        sample_wait(60)
        stable = False
        for attempt in range(9):
            end = datetime.now(timezone.utc).isoformat()
            start = (datetime.fromisoformat(end) - timedelta(seconds=30)).isoformat()
            stable = True
            for run in runs:
                outcome: dict[str, Any] = {"attempt": attempt}
                try:
                    summary = run.collect(f"stabilization_{attempt}", start, end)
                    run.require_coverage(summary, 30)
                    checks = stability_metrics(run.raw[f"stabilization_{attempt}"]["data"])
                    outcome.update(metrics=checks, passed=all(c["passed"] for c in checks.values()))
                except GateBlocked as exc:
                    outcome.update(passed=False, error=str(exc))
                report["services"].setdefault(run.service, {"stabilization": []})["stabilization"].append(outcome)
                stable &= outcome["passed"]
            stable &= runs[0].health()["healthy"]
            require_sampler()
            persist()
            if stable:
                break
            sample_wait(15)
        report["stabilization_passed"] = bool(stable)
        start = datetime.now(timezone.utc).isoformat()
        sample_wait(90)
        end = datetime.now(timezone.utc).isoformat()
        report["baseline"] = {"start": start, "end": end, "eligible": bool(stable)}
        client = MCPToolClient(runs[0].spec["observability"]["metrics"]["mcp_sse_url"], timeout_seconds=30)
        passed = bool(stable)
        for run in runs:
            result = report["services"][run.service]
            try:
                baseline = run.collect("baseline", start, end)
                result["baseline"] = baseline
                run.require_coverage(baseline, 90)
                run.baseline = baseline
                result["cpu_profile"] = resource_environment(
                    "cpu-hog", run.target["limits"], baseline,
                    config["controlled_campaign"]["calibrations"].get(run.service, {}),
                )
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
        report["passed"] = bool(passed and runs[0].health()["healthy"])
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
