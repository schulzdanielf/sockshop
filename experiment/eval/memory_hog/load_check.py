"""No-fault, single-load rehearsal using the controlled campaign's actual gates."""
from __future__ import annotations

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
