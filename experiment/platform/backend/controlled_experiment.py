"""Opt-in campaign lifecycle: warm load, measured baseline and fail-closed gates."""
from __future__ import annotations

import json
import math
import statistics
import subprocess
import time
import re
import threading
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from .domain import RunStatus, utc_now_iso
from .plugins.mcp_client import MCPToolClient
from experiment.eval.memory_hog.resource_intensity import (
    _CPU_SUFFIXES, _MEMORY_SUFFIXES, _parse_quantity,
)

if TYPE_CHECKING:
    from .engine import OrchestratorEngine

SERVICES = ("front-end", "catalogue", "carts", "orders", "payment", "shipping", "user")
FAULTS = ("cpu-hog", "memory-hog", "network-latency", "container-kill", "http-status-code")
ACTIVE_SECONDS = {
    "cpu-hog": 180, "memory-hog": 180, "network-latency": 120,
    "container-kill": 0, "http-status-code": 120,
}
CONTROLLED_CAMPAIGN_PROTOCOL_VERSION = 6
CONTAINER_KILL_RESTART_TIMEOUT_SECONDS = 120
CONTAINER_KILL_POLL_SECONDS = 2
INJECTOR_IMAGE = "litmuschaos.docker.scarf.sh/litmuschaos/go-runner:3.28.0"
CGROUP_RESOURCE_KEYS = frozenset({
    "cpu_cores", "cpu_quota_cores", "memory_bytes", "memory_usage_bytes",
    "memory_limit_bytes", "oom_events", "oom_kill_events",
    "throttled_seconds_per_second", "throttled_periods_per_second",
})
CGROUP_READ_COMMAND = (
    "printf '__cpu_max__\\n'; cat /sys/fs/cgroup/cpu.max; "
    "printf '__cpu_stat__\\n'; cat /sys/fs/cgroup/cpu.stat; "
    "printf '__memory_current__\\n'; cat /sys/fs/cgroup/memory.current; "
    "printf '__memory_max__\\n'; cat /sys/fs/cgroup/memory.max; "
    "printf '__memory_events__\\n'; cat /sys/fs/cgroup/memory.events"
)


class GateBlocked(ValueError):
    """No intervention may be submitted for this combination."""


def controlled_time_budget(policy: dict[str, Any]) -> dict[str, Any]:
    saved = policy.get("time_budget")
    if saved is not None:
        if (
            not isinstance(saved, dict) or saved.get("version") != 1
            or type(saved.get("load_seconds")) is not int
            or type(saved.get("poll_seconds")) is not int
            or not 1800 <= saved["load_seconds"] <= 7200
            or not saved["load_seconds"] + 300 <= saved["poll_seconds"] <= 7500
        ):
            raise GateBlocked("invalid controlled orchestration time budget")
        return saved
    return {"version": 0, "load_seconds": 1800, "poll_seconds": 1800}


def build_time_budget(policy: dict[str, Any]) -> dict[str, Any]:
    stages = {
        "warmup": 60, "stabilization_waits": 9 * 15, "baseline": 90,
        "workflow": int(policy["workflow_timeout_seconds"]),
        "restart_confirmation": CONTAINER_KILL_RESTART_TIMEOUT_SECONDS,
        "active_intervention": max(ACTIVE_SECONDS.values()), "post_observation": 120,
        "experimental_recovery": int(policy["recovery_timeout_seconds"]),
        "operational_recovery": int((policy.get("resilience") or {}).get("operational_timeout_seconds", 0)),
        "cooldown": 30, "collection_and_controller_allowance": 900,
    }
    load_seconds = sum(stages.values())
    return controlled_time_budget({"time_budget": {
        "version": 1, "load_seconds": load_seconds,
        "poll_seconds": load_seconds + 300, "stages": stages,
        "limitation": "Infrastructure overhead is bounded separately; stage gates are unchanged.",
    }})


def stability_metrics(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks = {}
    for metric, floor in (("traffic", .1), ("cpu_cores", .02),
                          ("memory_bytes", 8 * 2**20), ("latency_ms", 20)):
        values = [value for _, value in samples(data[metric])]
        if not values:
            raise GateBlocked(f"no {metric} samples for stabilization")
        variation = statistics.pstdev(values)
        limit = max(abs(statistics.fmean(values)) * .20, floor)
        checks[metric] = {"stddev": variation, "limit": limit, "passed": variation <= limit}
    return checks


def validate_cpu_profile(calibration: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(calibration, dict):
        raise GateBlocked("service calibration must be a mapping")
    configured = calibration.get("cpu") or {}
    if not isinstance(configured, dict):
        raise GateBlocked("CPU profile must be a mapping")
    candidate = configured.get("calibration_status") == "candidate" and bool(configured.get("rationale"))
    if not (candidate or configured.get("evidence")):
        raise GateBlocked("CPU needs explicit measured evidence or a documented candidate profile")
    cores, load = configured.get("cores"), configured.get("load")
    target = configured.get("target_quota_fraction", .8)
    tolerance = configured.get("target_tolerance_fraction", .05)
    if (
        type(cores) is not int or cores < 1
        or type(load) is not int or not 1 <= load <= 100
        or type(target) not in (int, float) or not .70 <= target <= 1.0
        or type(tolerance) not in (int, float) or not 0 < tolerance < .20
    ):
        raise GateBlocked("invalid candidate CPU parameters (cores/load/target/tolerance)")
    return configured


def validate_workflow_template(
    workflow: dict[str, Any], namespace: str, service: str, fault: str,
) -> dict[str, Any]:
    import yaml

    images, definitions, engines = [], [], []
    for template in workflow["spec"]["templates"]:
        if template.get("container", {}).get("image"):
            images.append(template["container"]["image"])
        for artifact in template.get("inputs", {}).get("artifacts", []):
            text = artifact.get("raw", {}).get("data", "")
            if "kind: ChaosExperiment" in text:
                definitions.append(yaml.safe_load(text)["spec"]["definition"]["image"])
            if "kind: ChaosEngine" in text:
                engines.append(yaml.safe_load(text))
    if definitions != [INJECTOR_IMAGE]:
        raise GateBlocked("injector image differs from reviewed Litmus 3.28.0 semantics")
    expected = {"appns": namespace, "applabel": f"name={service}", "appkind": "deployment"}
    if (
        len(engines) != 1 or engines[0]["spec"].get("appinfo") != expected
        or [item.get("name") for item in engines[0]["spec"].get("experiments", [])] != [f"pod-{fault}"]
    ):
        raise GateBlocked("reviewed template does not select exactly the intended service/intervention")
    return {"runner": definitions, "workflow": images}


def parse_cgroup_v2(text: str) -> dict[str, Any]:
    markers = {
        "__cpu_max__": "cpu.max",
        "__cpu_stat__": "cpu.stat",
        "__memory_current__": "memory.current",
        "__memory_max__": "memory.max",
        "__memory_events__": "memory.events",
    }
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        value = line.strip()
        if value in markers:
            current = markers[value]
            sections[current] = []
        elif current and value:
            sections[current].append(value)
    if set(sections) != set(markers.values()) or any(not values for values in sections.values()):
        raise GateBlocked("incomplete cgroup v2 response")

    cpu_max = sections["cpu.max"][0].split()
    if len(cpu_max) != 2 or cpu_max[0] == "max":
        raise GateBlocked("finite cgroup v2 CPU quota required")
    if sections["memory.max"][0] == "max":
        raise GateBlocked("finite cgroup v2 memory limit required")
    try:
        quota, period = int(cpu_max[0]), int(cpu_max[1])
        cpu_stat = {
            key: int(value)
            for line in sections["cpu.stat"]
            for key, value in [line.split()]
        }
        memory_current = int(sections["memory.current"][0])
        memory_max = int(sections["memory.max"][0])
        memory_events = {
            key: int(value)
            for line in sections["memory.events"]
            for key, value in [line.split()]
        }
    except (ValueError, IndexError) as exc:
        raise GateBlocked("invalid cgroup v2 numeric data") from exc
    if quota <= 0 or period <= 0 or memory_current < 0 or memory_max <= 0:
        raise GateBlocked("invalid or unlimited cgroup v2 resource limit")
    required_cpu = {"usage_usec", "nr_throttled", "throttled_usec"}
    if not required_cpu.issubset(cpu_stat) or not {"oom", "oom_kill"}.issubset(memory_events):
        raise GateBlocked("required cgroup v2 counters are missing")
    if any(value < 0 for value in (*cpu_stat.values(), *memory_events.values())):
        raise GateBlocked("negative cgroup v2 counter")
    return {
        "cpu_quota_cores": quota / period,
        "cpu_usage_usec": cpu_stat["usage_usec"],
        "nr_throttled": cpu_stat["nr_throttled"],
        "throttled_usec": cpu_stat["throttled_usec"],
        "memory_usage_bytes": memory_current,
        "memory_limit_bytes": memory_max,
        "oom_events": memory_events["oom"],
        "oom_kill_events": memory_events["oom_kill"],
        "raw": {
            "cpu.max": sections["cpu.max"],
            "cpu.stat": cpu_stat,
            "memory.current": memory_current,
            "memory.max": memory_max,
            "memory.events": memory_events,
        },
    }


def samples(payload: dict[str, Any]) -> list[tuple[float, float]]:
    if payload.get("status") != "success":
        raise GateBlocked(f"metrics backend error: {payload.get('error', payload)}")
    result = (payload.get("data") or {}).get("result")
    if not isinstance(result, list):
        raise GateBlocked("malformed metrics result")
    points: dict[float, float] = {}
    for series in result:
        for timestamp, value in series.get("values", []):
            t, v = float(timestamp), float(value)
            if not math.isfinite(t) or not math.isfinite(v):
                raise GateBlocked("non-finite metric sample")
            if t in points:
                raise GateBlocked("ambiguous multiple time series for target container")
            points[t] = v
    return sorted(points.items())


def stats(points: list[tuple[float, float]]) -> dict[str, Any]:
    values = sorted(value for _, value in points)
    if not values:
        return {"status": "no_data", "samples": 0}
    return {
        "status": "ok", "samples": len(values), "mean": statistics.fmean(values),
        "p95": values[int((len(values) - 1) * .95)], "max": values[-1],
        "start": points[0][0], "end": points[-1][0],
    }


def sustained(
    points: list[tuple[float, float]], threshold: float, seconds: float, max_gap: float,
) -> dict[str, Any]:
    start = previous = None
    longest = 0.0
    for timestamp, value in points:
        if value < threshold:
            start = previous = None
            continue
        if previous is None or timestamp - previous > max_gap:
            start = timestamp
        previous = timestamp
        if start is not None:
            longest = max(longest, timestamp - start)
    return {
        "passed": longest >= seconds, "threshold": threshold,
        "required_seconds": seconds, "observed_seconds": longest,
        "sample_count": len(points), "maximum_sample_gap_seconds": max_gap,
    }


def resource_environment(
    fault: str, limits: dict[str, Any], baseline: dict[str, Any], calibration: dict[str, Any],
) -> dict[str, Any]:
    try:
        memory_limit = float(_parse_quantity(limits.get("memory"), _MEMORY_SUFFIXES, "memory"))
        cpu_quota = float(_parse_quantity(limits.get("cpu"), _CPU_SUFFIXES, "CPU"))
    except ValueError as exc:
        raise GateBlocked(f"known positive application resource limits required: {exc}") from exc
    if fault == "memory-hog":
        # Litmus 3.28.0 passes --vm-bytes <MEMORY_CONSUMPTION>M to stress-ng.
        # Keep one worker so the per-worker/total interpretation is unambiguous.
        additional = max(0.0, .80 * memory_limit - baseline["memory_bytes"]["p95"] - .05 * memory_limit)
        mib = math.floor(additional / 2**20)
        if mib < int(calibration.get("minimum_memory_mib", 16)):
            raise GateBlocked("insufficient memory headroom for non-OOM pressure")
        return {
            "environment": {
                "MEMORY_CONSUMPTION": str(mib), "NUMBER_OF_WORKERS": "1",
                "TOTAL_CHAOS_DURATION": "180",
            },
            "additional_bytes": mib * 2**20, "workers": 1, "unit": "MiB",
            "limit_bytes": memory_limit, "baseline_p95_bytes": baseline["memory_bytes"]["p95"],
                "policy": "max(0, 0.80*limit - baseline_p95_cgroup_memory.current - 0.05*limit)",
                "limitation": "cgroup memory.current includes charged cache; allocation policy cannot guarantee no OOM",
        }
    if fault != "cpu-hog":
        raise GateBlocked(f"not a resource intervention: {fault}")
    configured = validate_cpu_profile(calibration)
    cores, load = int(configured["cores"]), int(configured["load"])
    target = float(configured.get("target_quota_fraction", .8))
    tolerance = float(configured.get("target_tolerance_fraction", .05))
    baseline_fraction = baseline["cpu_cores"]["p95"] / cpu_quota
    if baseline_fraction >= .70 or baseline_fraction >= target - .10:
        raise GateBlocked(
            f"CPU baseline already high: p95={baseline['cpu_cores']['p95']:.4f} cores, "
            f"quota={cpu_quota:.4f}, fraction={baseline_fraction:.1%}; "
            "requires <70% and target headroom; recalibration required"
        )
    return {
        "environment": {"CPU_CORES": str(cores), "CPU_LOAD": str(load), "TOTAL_CHAOS_DURATION": "180"},
        "quota_cores": cpu_quota, "baseline_cores": baseline["cpu_cores"],
        "candidate_target_quota_fraction": target, "target_tolerance_fraction": tolerance,
        "calibration_status": configured.get("calibration_status", "measured"),
        "calibration": configured,
        "semantics": "stress-ng --cpu N --cpu-load P; P is duty cycle per worker, not container quota",
    }


def kubectl(*args: str) -> Any:
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout)


class ControlledRun:
    def __init__(self, engine: OrchestratorEngine, run_id: str, spec: dict[str, Any]):
        self.engine, self.run_id, self.spec = engine, run_id, spec
        self.policy = spec["controlled_campaign"]
        self.service, self.fault = self.policy["service"], self.policy["fault"]
        if self.service not in SERVICES or self.fault not in FAULTS:
            raise GateBlocked("service or intervention outside controlled campaign allowlist")
        self.namespace = spec["scope"]["namespace"]
        if self.namespace != "sock-shop":
            raise GateBlocked("controlled campaign is restricted to sock-shop")
        self.context = {"run_id": run_id, "experiment_id": spec["experiment"]["id"]}
        self.report: dict[str, Any] = {
            "schema_version": 1, "status": "preparing", "gates": {},
            "timestamps": {}, "limitations": [], "observations": [],
            "planned": spec["timeline"], "fault": self.fault, "service": self.service,
        }
        self.paths: dict[str, str] = {}
        self.chaos_handle: dict[str, Any] | None = None
        self.load_handle: dict[str, Any] | None = None
        self.baseline: dict[str, Any] = {}
        self.raw: dict[str, Any] = {}
        self.target: dict[str, Any] = {}
        self.cgroup_samples: list[dict[str, Any]] = []
        self.cgroup_sampling_enabled = False
        self.cgroup_sampling_paused_for_restart = False
        self.container_kill_signal_attempted = False
        self.container_kill_observation_started = False
        self.window: dict[str, Any] = {}
        self.cleaned = False
        self.engine_uids: set[str] = set()
        self.collection_sequence = 0
        self.telemetry_lock = threading.RLock()
        self.report["gates"] = {
            "submitted": False, "intervention_confirmed_on_target": None,
            "mechanism_observed": {"status": "not_checked"},
            "impact_observed": {"status": "not_checked"},
            "telemetry_coverage": {"status": "not_checked"},
            "recovery": {"passed": False, "status": "not_checked"},
            "cleanup": {"passed": True, "status": "not_required"},
        }

    def save(self, name: str, content: dict[str, Any]) -> None:
        self.paths[name] = self.engine.storage.save_artifact(self.run_id, name, content)

    def checkpoint(self) -> None:
        with self.telemetry_lock:
            self.save("controlled_report", self.report)

    def mark(self, name: str, source: str = "controller_clock", **extra: Any) -> str:
        now = utc_now_iso()
        self.report["timestamps"][name] = {"at": now, "source": source, **extra}
        self.engine._event(self.run_id, name, self.report["timestamps"][name])
        self.checkpoint()
        return now

    def wait(self, seconds: int) -> None:
        if self.fault == "container-kill" and self.container_kill_observation_started:
            self.observe_container_kill_state("post_observation_poll")
        if self.cgroup_sampling_enabled:
            self.sample_cgroup()
        remaining = seconds
        while remaining > 0:
            interval = min(5, remaining)
            self.engine._sleep_interruptible(self.engine._stop_flags[self.run_id], interval)
            remaining -= interval
            if self.fault == "container-kill" and self.container_kill_observation_started:
                self.observe_container_kill_state("post_observation_poll")
            if self.cgroup_sampling_enabled:
                self.sample_cgroup()
            if self.load_handle and self.engine.load.status(self.context, self.load_handle).get("state") != "running":
                raise RuntimeError("load stopped before controlled lifecycle finished")

    def start_cgroup_sampling(self) -> None:
        if not all(self.target.get(key) for key in ("pod", "uid", "container")):
            raise GateBlocked("exact application container identity required for cgroup sampling")
        self.cgroup_sampling_enabled = True
        self.sample_cgroup()

    def sample_cgroup(self) -> dict[str, Any]:
        container_id = self.target.get("container_id")
        event = self.report.get("container_kill_event") or {}
        if self.fault == "container-kill" and event.get("restart_observed"):
            container_id = event["container_id_after_restart"]
            status = self.application_container_status()
            if not status["running"] or status["container_id"] != container_id:
                raise GateBlocked("container identity changed before startup cgroup read")
        started = time.time()
        started_at = utc_now_iso()
        command = [
                "kubectl", "-n", self.namespace, "exec", self.target["pod"],
                "-c", self.service, "--", "sh", "-c", CGROUP_READ_COMMAND,
        ]
        result = None
        for attempt in range(2):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=30,
                )
                break
            except subprocess.TimeoutExpired as exc:
                failure = {
                    "at": utc_now_iso(), "attempt": attempt + 1, "timeout_seconds": 30,
                    "error": str(exc),
                }
                with self.telemetry_lock:
                    self.report.setdefault("telemetry", {}).setdefault(
                        "cgroup_v2_read_retries", [],
                    ).append(failure)
                    self.checkpoint()
                if attempt == 1:
                    self.record_cgroup_read_failure(str(exc), container_id)
                    raise GateBlocked(
                        "cgroup v2 sampling timed out twice; explicit collection errors prevent baseline use"
                    ) from exc
        if result is None:
            raise RuntimeError("cgroup v2 sampling ended without a command result")
        completed_at = utc_now_iso()
        finished = time.time()
        if result.returncode:
            error = result.stderr.strip() or result.stdout.strip()
            self.record_cgroup_read_failure(error, container_id)
            raise GateBlocked(
                "cgroup v2 sampling failed for the application container: "
                f"{error}"
            )
        values = parse_cgroup_v2(result.stdout)
        if self.fault == "container-kill" and event.get("restart_observed"):
            status = self.application_container_status()
            if not status["running"] or status["container_id"] != container_id:
                raise GateBlocked("container identity changed during startup cgroup read")
        timestamp = (started + finished) / 2
        sample = {
            **{key: value for key, value in values.items() if key != "raw"},
            "timestamp": timestamp,
            "timestamp_utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "container_id": container_id,
            "read_started_at": started_at,
            "read_completed_at": completed_at,
            "read_duration_seconds": finished - started,
            "raw": values["raw"],
        }
        with self.telemetry_lock:
            self.cgroup_samples.append(sample)
            self.update_cgroup_telemetry()
        return sample

    def record_cgroup_read_failure(self, error: str, container_id: str | None) -> None:
        failure: dict[str, Any] = {
            "at": utc_now_iso(), "error": error,
            "pod": self.target.get("pod"), "pod_uid": self.target.get("uid"),
            "container": self.service, "expected_container_id": container_id,
        }
        try:
            pods = self.pods()
            self.save("cgroup_failure_pods", pods)
            failure["observed_pods"] = [
                {
                    "name": pod.get("metadata", {}).get("name"),
                    "uid": pod.get("metadata", {}).get("uid"),
                    "containers": [
                        status for status in pod.get("status", {}).get("containerStatuses", [])
                        if status.get("name") == self.service
                    ],
                }
                for pod in pods.get("items", [])
            ]
        except (RuntimeError, OSError, ValueError) as exc:
            failure["pods_error"] = str(exc)
        try:
            if not self.target.get("uid"):
                raise GateBlocked("target pod UID unavailable for lifecycle event lookup")
            events = kubectl(
                "-n", self.namespace, "get", "events", "--field-selector",
                f'involvedObject.uid={self.target["uid"]}', "-o", "json",
            )
            self.save("cgroup_failure_events", events)
            failure["events_status"] = "ok" if events.get("items") else "no_data"
        except (RuntimeError, OSError, ValueError) as exc:
            failure["events_error"] = str(exc)
        with self.telemetry_lock:
            self.report.setdefault("telemetry", {}).setdefault(
                "cgroup_v2_read_failures", [],
            ).append(failure)
            self.checkpoint()

    def update_cgroup_telemetry(self) -> None:
        timestamps = [sample["timestamp"] for sample in self.cgroup_samples]
        gaps = [right - left for left, right in zip(timestamps, timestamps[1:])]
        self.report.setdefault("telemetry", {})["cgroup_v2"] = {
            "source": "kubectl_exec_inside_application_container",
            "scope": "exact application container; sidecars excluded",
            "pod": self.target.get("pod"),
            "pod_uid": self.target.get("uid"),
            "container": self.service,
            "container_id_before": self.target.get("container_id"),
            "container_id_after_restart": (
                self.report.get("container_kill_event", {}).get("container_id_after_restart")
                if isinstance(self.report.get("container_kill_event"), dict) else None
            ),
            "requested_sample_interval_seconds": 5,
            "sample_count": len(self.cgroup_samples),
            "sampling_status": (
                (
                    "active_after_container_restart"
                    if self.cgroup_sampling_enabled else "stopped_at_one_shot_container_kill"
                )
                if self.fault == "container-kill" and self.container_kill_signal_attempted
                else "active" if self.cgroup_sampling_enabled else "stopped"
            ),
            **(
                {
                    "post_kill_sampling": (
                        "resumed_after_application_container_running"
                        if self.cgroup_sampling_enabled
                        else "paused_until_application_container_running"
                    ),
                }
                if self.fault == "container-kill" and self.container_kill_signal_attempted else {}
            ),
            "observed_sample_gap_seconds": {
                "minimum": min(gaps) if gaps else None,
                "mean": statistics.fmean(gaps) if gaps else None,
                "maximum": max(gaps) if gaps else None,
            },
            "limitations": [
                "sample intervals include kubectl exec and controller overhead",
                "memory.current includes memory charged to this container, including reclaimable cache",
                "cgroup counters may reset when the container restarts; resets are excluded from CPU rate intervals",
                *(
                    ["cgroup sampling remains paused after the one-shot container restart"]
                    if (
                        self.fault == "container-kill" and self.container_kill_signal_attempted
                        and not self.cgroup_sampling_enabled
                    ) else []
                ),
            ],
        }

    def cgroup_payloads(self, start: str, end: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
        with self.telemetry_lock:
            snapshot = list(self.cgroup_samples)
        in_window = [sample for sample in snapshot if start_ts <= sample["timestamp"] <= end_ts]
        previous = [sample for sample in snapshot if sample["timestamp"] < start_ts]
        selected = ([previous[-1]] if previous else []) + in_window
        values: dict[str, list[tuple[float, float]]] = {
            key: [] for key in CGROUP_RESOURCE_KEYS
        }
        counter_resets = 0
        identity_changes = 0
        long_gaps = 0
        for sample in in_window:
            timestamp = sample["timestamp"]
            values["cpu_quota_cores"].append((timestamp, sample["cpu_quota_cores"]))
            values["memory_bytes"].append((timestamp, sample["memory_usage_bytes"]))
            values["memory_usage_bytes"].append((timestamp, sample["memory_usage_bytes"]))
            values["memory_limit_bytes"].append((timestamp, sample["memory_limit_bytes"]))
            values["oom_events"].append((timestamp, sample["oom_events"]))
            values["oom_kill_events"].append((timestamp, sample["oom_kill_events"]))
        for before, after in zip(selected, selected[1:]):
            timestamp = after["timestamp"]
            elapsed = timestamp - before["timestamp"]
            if timestamp < start_ts or elapsed <= 0:
                continue
            if before.get("container_id") != after.get("container_id"):
                identity_changes += 1
                continue
            if elapsed > 20:
                long_gaps += 1
                continue
            usage_delta = after["cpu_usage_usec"] - before["cpu_usage_usec"]
            throttled_delta = after["throttled_usec"] - before["throttled_usec"]
            periods_delta = after["nr_throttled"] - before["nr_throttled"]
            if min(usage_delta, throttled_delta, periods_delta) < 0:
                counter_resets += 1
                continue
            values["cpu_cores"].append((timestamp, usage_delta / (elapsed * 1_000_000)))
            values["throttled_seconds_per_second"].append(
                (timestamp, throttled_delta / (elapsed * 1_000_000))
            )
            values["throttled_periods_per_second"].append((timestamp, periods_delta / elapsed))
        payloads = {
            key: {
                "status": "success",
                "data": {
                    "result": [{
                        "metric": {
                            "source": "cgroup-v2",
                            "pod": self.target.get("pod", ""),
                            "container": self.service,
                        },
                        "values": [[timestamp, str(value)] for timestamp, value in sorted(points)],
                    }] if points else [],
                },
            }
            for key, points in values.items()
        }
        telemetry = {
            "source": "cgroup-v2",
            "sample_count": len(in_window),
            "samples": in_window,
            "cpu_predecessor_sample": previous[-1] if previous else None,
            "cpu_rate_intervals_skipped_for_long_gap": long_gaps,
            "cpu_rate_intervals_skipped_for_counter_reset": counter_resets,
            "cpu_rate_intervals_skipped_for_identity_change": identity_changes,
        }
        return payloads, telemetry

    def pods(self) -> dict[str, Any]:
        return kubectl("-n", self.namespace, "get", "pods", "-l", f"name={self.service}", "-o", "json")

    def qdisc(self) -> list[dict[str, Any]]:
        interface = (self.policy.get("calibration") or {}).get("interface", "eth0")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
            raise GateBlocked("invalid network interface")
        result = kubectl(
            "-n", self.namespace, "exec", self.target["pod"],
            "-c", self.target.get("network_inspection_container", self.service),
            "--", "tc", "-j", "qdisc", "show", "dev", interface,
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise GateBlocked("malformed tc qdisc JSON response")
        return result

    def health(self) -> dict[str, Any]:
        deployments = kubectl("-n", self.namespace, "get", "deployments", "-o", "json")
        by_name = {item["metadata"]["name"]: item for item in deployments["items"]}
        unhealthy = [
            name for name in SERVICES
            if by_name.get(name, {}).get("status", {}).get("readyReplicas", 0)
            != by_name.get(name, {}).get("spec", {}).get("replicas", 1)
        ]
        nodes = kubectl("get", "nodes", "-o", "json")
        bad_nodes = [
            item["metadata"]["name"] for item in nodes["items"]
            if any(
                (c["type"] == "Ready" and c["status"] != "True")
                or (c["type"] in {"MemoryPressure", "DiskPressure", "PIDPressure"} and c["status"] != "False")
                for c in item.get("status", {}).get("conditions", [])
            )
        ]
        return {"healthy": not unhealthy and not bad_nodes, "unhealthy_services": unhealthy, "unhealthy_nodes": bad_nodes}

    def inspect_target(self) -> None:
        health = self.health()
        self.report["gates"]["application_stability"] = health
        if not health["healthy"]:
            raise GateBlocked(f"application/node unhealthy: {health}")
        pods = self.pods()
        self.save("pods_before", pods)
        if len(pods["items"]) != 1:
            raise GateBlocked("controlled intervention requires exactly one target pod")
        pod = pods["items"][0]
        containers = [c for c in pod["spec"]["containers"] if c["name"] == self.service]
        statuses = [c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == self.service]
        if len(containers) != 1 or len(statuses) != 1 or not statuses[0].get("ready"):
            raise GateBlocked("exact application container not ready; sidecars cannot be targets")
        sidecar = [
            c for c in pod.get("status", {}).get("containerStatuses", [])
            if c.get("name") == "istio-proxy" and c.get("ready")
        ]
        self.target = {
            "pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
            "container": self.service, "node": pod["spec"]["nodeName"],
            "image": containers[0]["image"], "image_id": statuses[0].get("imageID"),
            "container_id": statuses[0].get("containerID"),
            "restart_count": statuses[0]["restartCount"],
            "limits": containers[0].get("resources", {}).get("limits", {}),
            "network_inspection_container": "istio-proxy" if sidecar else self.service,
        }
        self.report["target_before"] = self.target
        self.checkpoint()

    def application_container_status(self) -> dict[str, Any]:
        pods = self.pods()
        if len(pods.get("items", [])) != 1:
            raise GateBlocked("application container identity is not unique")
        pod = pods["items"][0]
        metadata = pod.get("metadata", {})
        if metadata.get("uid") != self.target.get("uid"):
            raise GateBlocked("target pod identity changed during controlled observation")
        statuses = [
            item for item in pod.get("status", {}).get("containerStatuses", [])
            if item.get("name") == self.service
        ]
        if len(statuses) != 1:
            raise GateBlocked("exact application container status is unavailable")
        container = statuses[0]
        restart_count = container.get("restartCount")
        if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count < 0:
            raise GateBlocked("application container restart count is unavailable")
        state = container.get("state") or {}
        terminated = (container.get("lastState") or {}).get("terminated")
        return {
            "pod": metadata.get("name"),
            "pod_uid": metadata.get("uid"),
            "container": self.service,
            "container_id": container.get("containerID"),
            "restart_count": restart_count,
            "ready": container.get("ready") is True,
            "running": isinstance(state.get("running"), dict),
            "state": "running" if "running" in state else "waiting" if "waiting" in state
            else "terminated" if "terminated" in state else "unknown",
            "last_terminated": terminated if isinstance(terminated, dict) else None,
        }

    def inspect_docker_container(self, container_id: str) -> dict[str, Any]:
        runtime, separator, identifier = container_id.partition("://")
        if runtime != "docker" or not separator or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise GateBlocked(
                "one-shot container-kill requires a canonical docker:// container ID; "
                "other runtimes are blocked rather than targeted ambiguously"
            )
        try:
            result = subprocess.run(
                ["docker", "inspect", identifier],
                capture_output=True, text=True, check=False, timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise GateBlocked("Docker runtime identity inspection timed out; no kill submitted") from exc
        except OSError as exc:
            raise GateBlocked(f"Docker runtime inspection unavailable; no kill submitted: {exc}") from exc
        if result.returncode != 0:
            raise GateBlocked(
                "Docker runtime could not inspect the exact application container; "
                f"no kill submitted: {result.stderr.strip()}"
            )
        try:
            inspected = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GateBlocked("Docker runtime returned malformed container identity; no kill submitted") from exc
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            raise GateBlocked("Docker runtime identity response is ambiguous; no kill submitted")
        container = inspected[0]
        labels = (container.get("Config") or {}).get("Labels") or {}
        expected_labels = {
            "io.kubernetes.pod.namespace": self.namespace,
            "io.kubernetes.pod.name": self.target["pod"],
            "io.kubernetes.container.name": self.service,
        }
        observed_labels = {key: labels.get(key) for key in expected_labels}
        if (
            container.get("Id") != identifier
            or (container.get("State") or {}).get("Running") is not True
            or observed_labels != expected_labels
        ):
            raise GateBlocked(
                "Docker runtime identity/state does not match the selected application container; "
                "no kill submitted"
            )
        return {
            "runtime": "docker",
            "id": identifier,
            "running": True,
            "labels": observed_labels,
        }

    def stop_docker_container(self, identifier: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "stop", "--signal=9", "--time=0", identifier],
            capture_output=True, text=True, check=False, timeout=timeout,
        )

    def prepare_container_kill(self) -> None:
        status = self.application_container_status()
        if (
            not status["ready"] or not status["running"]
            or not status["container_id"]
            or status["container_id"] != self.target.get("container_id")
            or status["restart_count"] != self.target.get("restart_count")
        ):
            raise GateBlocked("application container changed or is not stable; no kill submitted")
        retry_guard = self.policy.get("previous_attempt_guard")
        if retry_guard and any(
            retry_guard.get(key) != value
            for key, value in {
                "pod": status["pod"],
                "pod_uid": status["pod_uid"],
                "container": status["container"],
                "container_id": status["container_id"],
                "restart_count": status["restart_count"],
            }.items()
        ):
            raise GateBlocked(
                "container identity changed since the prior unconfirmed attempt; "
                "retry is blocked before injection"
            )
        runtime_identity = self.inspect_docker_container(status["container_id"])
        self.report["container_kill_preflight"] = {
            "passed": True,
            "target": status,
            "runtime_readback": runtime_identity,
            "source": "docker_inspect_with_kubernetes_identity_labels",
            "sidecars_excluded": True,
        }
        self.report["intensity"] = {
            "signal": "SIGKILL",
            "target_container": self.service,
            "event_count": 1,
            "active_seconds": 0,
            "post_event_observation_seconds": 120,
            "method": "single Docker runtime stop with SIGKILL and zero grace against the exact verified application container ID",
        }
        self.checkpoint()

    def observe_container_kill_state(self, phase: str) -> dict[str, Any]:
        event = self.report.get("container_kill_event")
        if not isinstance(event, dict) or not event.get("restart_observed"):
            raise RuntimeError("container-kill restart was not confirmed before observation")
        status = self.application_container_status()
        snapshot = {"phase": phase, "observed_at": utc_now_iso(), **status}
        event.setdefault("observations", []).append(snapshot)
        expected_restart_count = event.get("restart_count_after_signal")
        if isinstance(expected_restart_count, int) and status["restart_count"] > expected_restart_count:
            self.report["scenario_deviation"] = "additional_container_restart_after_single_signal"
            self.report["limitations"].append(
                "additional application-container restart observed after the single SIGKILL; "
                "campaign stopped before another target"
            )
            self.checkpoint()
            raise RuntimeError("additional container restart after one-shot kill; stop campaign")
        if status["running"] and status["container_id"] != event.get("container_id_after_restart"):
            raise RuntimeError("application container identity changed after confirmed restart")
        self.mark_container_ready_after_kill(status)
        self.update_container_kill_availability(status)
        self.checkpoint()
        return status

    def update_container_kill_availability(self, status: dict[str, Any]) -> None:
        event = self.report["container_kill_event"]
        observations = event.get("observations", [])
        self.report["gates"].setdefault("impact_observed", {}).update({
            "availability_impact": {
                "status": "ready" if status["ready"] and status["running"] else "not_ready",
                "ready": status["ready"],
                "running": status["running"],
                "sampled_status_polls": len(observations),
                "not_ready_status_polls": sum(
                    not item.get("ready") or not item.get("running")
                    for item in observations
                ),
                "restart_observed_at": event.get("restart_observed_at"),
                "ready_observed_at": event.get("ready_after_restart_at"),
                "limitation": (
                    "Kubernetes status polling brackets readiness; it is not a continuous "
                    "request-availability measurement"
                ),
            },
        })

    def mark_container_ready_after_kill(self, status: dict[str, Any]) -> None:
        event = self.report["container_kill_event"]
        if self.cgroup_sampling_paused_for_restart and status["running"]:
            if status["container_id"] != event.get("container_id_after_restart"):
                raise RuntimeError("cannot resume cgroup sampling for an unconfirmed container")
            self.cgroup_sampling_enabled = True
            self.cgroup_sampling_paused_for_restart = False
            self.mark("startup_cgroup_sampling_resumed",
                      source="kubernetes_running_container_identity", container_id=status["container_id"])
            self.update_cgroup_telemetry()
        if not status["ready"] or not status["running"]:
            return
        if not event.get("ready_after_restart_at"):
            event["restart_count_at_ready"] = status["restart_count"]
            event["ready_after_restart_at"] = self.mark(
                "container_ready_after_kill",
                source="kubernetes_application_container_status",
                container_id=status["container_id"],
            )

    def queries(self) -> dict[str, str]:
        selector = f'namespace="{self.namespace}",pod="{self.target["pod"]}",container="{self.service}"'
        mesh = f'destination_workload_namespace="{self.namespace}",destination_workload="{self.service}",reporter="destination"'
        traffic_rate = f"sum(rate(istio_requests_total{{{mesh}}}[30s]))"
        server_5xx_rate = f'sum(rate(istio_requests_total{{{mesh},response_code=~"5.."}}[30s]))'
        if self.fault == "http-status-code" and self.service == "front-end":
            target_edge = (
                f'destination_workload_namespace="{self.namespace}",'
                f'destination_workload="{self.service}",reporter="destination"'
            )
        else:
            target_edge = (
                f'destination_workload_namespace="{self.namespace}",'
                f'destination_workload="{self.service}",source_workload!="unknown",'
                "reporter=\"source\""
            )
        target_edge_requests = (
            f"sum(rate(istio_requests_total{{{target_edge}}}[30s]))"
        )
        target_edge_5xx = (
            f'sum(rate(istio_requests_total{{{target_edge},response_code=~"5.."}}[30s]))'
        )
        target_edge_latency = (
            f'histogram_quantile(0.95,sum by(le)(rate(istio_request_duration_milliseconds_bucket'
            f"{{{target_edge}}}[30s])))"
        )
        queries = {
            "cpu_cores": f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[30s]))",
            "memory_bytes": f"sum(container_memory_working_set_bytes{{{selector}}})",
            "throttled_seconds_per_second": f"sum(rate(container_cpu_cfs_throttled_seconds_total{{{selector}}}[30s]))",
            "traffic": traffic_rate,
            "errors_5xx_rate": server_5xx_rate,
            "errors_percent": (
                f"100 * (({server_5xx_rate}) or (({traffic_rate}) * 0)) "
                f"/ clamp_min(({traffic_rate}), 1e-9)"
            ),
            "latency_ms": f"histogram_quantile(0.95,sum by(le)(rate(istio_request_duration_milliseconds_bucket{{{mesh}}}[30s])))",
            "oom_events": f'sum(container_oom_events_total{{{selector}}})',
            "memory_usage_bytes": f"sum(container_memory_usage_bytes{{{selector}}})",
            "cpu_quota_cores": f"sum(container_spec_cpu_quota{{{selector}}} / container_spec_cpu_period{{{selector}}})",
            "restarts": f'sum(kube_pod_container_status_restarts_total{{{selector}}})',
            "ready": f'min(kube_pod_container_status_ready{{{selector}}})',
            "last_termination_timestamp": f'max(kube_pod_container_status_last_terminated_timestamp{{{selector}}})',
            "last_termination_exitcode": f'max(kube_pod_container_status_last_terminated_exitcode{{{selector}}})',
        }
        for service in ("orders", "payment", "shipping"):
            queries[f"checkout_{service}"] = (
                f'sum(rate(istio_requests_total{{destination_workload_namespace="{self.namespace}",'
                f'destination_workload="{service}",reporter="destination"}}[30s]))'
            )
        queries["target_edge_requests"] = target_edge_requests
        queries["target_edge_5xx"] = target_edge_5xx
        queries["target_edge_latency_ms"] = target_edge_latency
        queries["target_edge_error_percent"] = (
            f'100 * (({target_edge_5xx}) or (({target_edge_requests}) * 0)) '
            f'/ clamp_min(({target_edge_requests}), 1e-9)'
        )
        return queries

    def collect(self, name: str, start: str, end: str, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
        cfg = dict(self.spec["observability"]["metrics"])
        cfg["queries"] = [
            {"id": key, "query": query} for key, query in self.queries().items()
            if (keys is None or key in keys)
            and (not self.cgroup_sampling_enabled or key not in CGROUP_RESOURCE_KEYS)
        ]
        raw = (
            self.engine.metrics.collect_window(self.context, cfg, start, end, 5)
            if cfg["queries"] else {"status": "success", "data": {}}
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            raise GateBlocked("malformed metrics window response")
        if self.cgroup_sampling_enabled:
            payloads, resource_telemetry = self.cgroup_payloads(start, end)
            raw["data"].update(payloads)
            raw["resource_telemetry"] = resource_telemetry
        self.raw[name] = raw
        self.collection_sequence += 1
        self.save(f"controlled_metrics_{self.collection_sequence:04d}_{name}", raw)
        summary = {}
        for key, payload in raw.get("data", {}).items():
            try:
                summary[key] = stats(samples(payload))
            except (GateBlocked, ValueError, TypeError, KeyError) as exc:
                summary[key] = {"status": "tool_error", "error": str(exc), "samples": 0}
        error_rate_basis = {}
        for percent_key, numerator_key, denominator_key in (
            ("errors_percent", "errors_5xx_rate", "traffic"),
            ("target_edge_error_percent", "target_edge_5xx", "target_edge_requests"),
        ):
            if (
                summary.get(numerator_key, {}).get("status") == "no_data"
                and summary.get(denominator_key, {}).get("status") == "ok"
                and summary[denominator_key]["mean"] > 0
                and summary.get(percent_key, {}).get("status") == "ok"
                and summary[percent_key]["mean"] == 0
            ):
                error_rate_basis[percent_key] = {
                    "status": "zero_with_positive_request_rate",
                    "numerator_series": "no_data",
                    "denominator_mean": summary[denominator_key]["mean"],
                    "interpretation": "zero is inferred only with positive request volume; raw missing 5xx series remains preserved",
                }
        self.report["observations"].append({
            "phase": name, "start": start, "end": end, "summary": summary,
            "error_rate_basis": error_rate_basis,
        })
        self.checkpoint()
        return summary

    def require_coverage(self, summary: dict[str, Any], seconds: int) -> None:
        required = [
            "cpu_cores", "memory_bytes", "traffic", "errors_percent", "latency_ms",
            "checkout_orders", "checkout_payment", "checkout_shipping",
        ]
        if self.fault == "network-latency":
            required.extend(("target_edge_requests", "target_edge_latency_ms"))
        for key in required:
            item = summary.get(key, {})
            if item.get("status") != "ok" or item["samples"] < 4:
                raise GateBlocked(f"insufficient {key} coverage: {item}")
            if item["end"] - item["start"] < seconds * .8:
                raise GateBlocked(f"insufficient temporal coverage for {key}")
            if (key == "traffic" or key.startswith("checkout_") or key == "target_edge_requests") and item["mean"] <= 0:
                raise GateBlocked(f"journey produces no traffic: {key}")
        if summary["errors_percent"]["mean"] > float(self.policy.get("baseline_max_error_percent", 1)):
            raise GateBlocked("application error rate above baseline stability threshold")

    def metadata(self) -> None:
        config = self.spec["observability"]["metrics"]
        client = MCPToolClient(
            sse_url=config["mcp_sse_url"], timeout_seconds=int(config.get("timeout_seconds", 10)),
        )
        targets = client.call_tool_json("prometheus_scrape_targets", {})
        if not isinstance(targets, dict):
            self.save("scrape_targets", {
                "status": "malformed_response",
                "response_type": type(targets).__name__,
                "raw_response": targets,
            })
            raise GateBlocked(
                "scrape-target metadata tool returned a non-object response "
                f"({type(targets).__name__}); update the observability MCP before retrying"
            )
        self.save("scrape_targets", targets)
        if targets.get("status") != "success":
            raise GateBlocked(
                "cannot record actual scrape intervals: "
                f"{targets.get('error') or targets.get('status')!r}"
            )
        data = targets.get("data")
        if not isinstance(data, dict):
            raise GateBlocked("malformed scrape-target metadata: data must be an object")
        active = data.get("activeTargets")
        if not isinstance(active, list):
            raise GateBlocked("malformed scrape-target metadata: activeTargets must be a list")
        if not active or not any(
            isinstance(target, dict) and target.get("scrapeInterval")
            for target in active
        ):
            raise GateBlocked("no scrape target metadata")
        self.report["telemetry"] = {
            "requested_query_step_seconds": 5, "rate_lookback_seconds": 30,
            "metadata_source": "same_mcp_prometheus_client_as_metric_queries",
            "resource_metrics": {
                "source": "cgroup v2 read inside the selected application container",
                "scope": "container-specific; Prometheus pod-aggregated resource series are not used",
                "requested_sample_interval_seconds": 5,
            },
            "scrapes": [
                {
                    key: target.get(key)
                    for key in ("labels", "scrapeInterval", "lastScrape", "health", "lastError")
                }
                for target in active if isinstance(target, dict)
            ],
            "limitation": "5s query step does not change exporter/scrape frequency; repeated interpolated samples are not independent",
        }
        query = f'time() - timestamp(container_memory_working_set_bytes{{namespace="{self.namespace}",pod="{self.target["pod"]}"}})'
        lag = client.call_tool_json("prometheus_instant_query", {"query": query})
        self.report["telemetry"]["sample_age_raw"] = lag
        result = (lag.get("data") or {}).get("result") or []
        if lag.get("status") != "success" or len(result) != 1:
            raise GateBlocked("cannot measure pod-level Prometheus sample age/ingestion lag")
        age = float(result[0]["value"][1])
        self.report["telemetry"]["sample_age_seconds"] = age
        self.report["telemetry"]["sample_age_scope"] = (
            "pod-level cAdvisor series; freshness only, not an application-container resource measurement"
        )
        self.report["telemetry"]["ingestion_delay_limitation"] = "sample age bounds freshness, not exporter-to-backend transport latency alone"
        if not math.isfinite(age) or age < 0 or age > 45:
            raise GateBlocked("target metric too stale")

    def prepare_intervention(self) -> None:
        cfg = self.spec["chaos_profile"]
        if self.fault == "container-kill":
            self.prepare_container_kill()
            return
        if self.fault in {"cpu-hog", "memory-hog"}:
            try:
                limit = float(_parse_quantity(self.target["limits"].get("cpu"), _CPU_SUFFIXES, "CPU"))
                memory_limit = float(_parse_quantity(self.target["limits"].get("memory"), _MEMORY_SUFFIXES, "memory"))
            except ValueError as exc:
                raise GateBlocked(f"known resource limits required for calibration: {exc}") from exc
            observed_quota = self.baseline.get("cpu_quota_cores") or {}
            if observed_quota.get("status") != "ok" or not math.isclose(observed_quota["mean"], limit, rel_tol=.02):
                raise GateBlocked("actual cgroup CPU quota missing or differs from application container limit")
            observed_memory_limit = self.baseline.get("memory_limit_bytes") or {}
            if (
                observed_memory_limit.get("status") != "ok"
                or not math.isclose(observed_memory_limit["mean"], memory_limit, rel_tol=.02)
            ):
                raise GateBlocked("actual cgroup memory limit missing or differs from application container limit")
            if self.fault == "memory-hog" and self.baseline.get("memory_usage_bytes", {}).get("status") != "ok":
                raise GateBlocked("memory safety requires exact application-container cgroup usage")
            if self.fault == "memory-hog" and self.baseline.get("oom_events", {}).get("status") != "ok":
                raise GateBlocked("memory safety requires the cgroup v2 OOM counter, not only restarts")
            if self.fault == "memory-hog" and self.baseline.get("oom_kill_events", {}).get("status") != "ok":
                raise GateBlocked("memory safety requires the cgroup v2 OOM-kill counter")
            if self.fault == "memory-hog":
                if self.baseline["memory_usage_bytes"]["max"] >= .85 * memory_limit:
                    raise GateBlocked("application-container cgroup memory.current already >=85% of limit")
            profile = resource_environment(
                self.fault, self.target["limits"], self.baseline,
                self.policy.get("calibration") or {},
            )
            env = {**profile["environment"], "TARGET_CONTAINER": self.service, "TARGET_PODS": self.target["pod"]}
            cfg["resource_chaos"] = {
                "experiment_name": f"pod-{self.fault}", "environment": env,
                "target_container": self.service, "container_limits": self.target["limits"],
            }
            self.report["intensity"] = profile
        if self.fault == "network-latency":
            try:
                before = self.qdisc()
            except (RuntimeError, OSError, ValueError) as exc:
                raise GateBlocked(f"cannot inspect tc in target network namespace: {exc}") from exc
            if not isinstance(before, list) or any(item.get("kind") == "netem" for item in before):
                raise GateBlocked("unknown qdisc state or pre-existing netem rule")
            modules = subprocess.run(
                ["kubectl", "-n", self.namespace, "exec", self.target["pod"], "-c", self.service,
                 "--", "cat", "/proc/modules"], capture_output=True, text=True, check=False, timeout=30,
            )
            self.report["netem_preflight"] = {
                "returncode": modules.returncode,
                "sch_netem_loaded": any(line.startswith("sch_netem ") for line in modules.stdout.splitlines()),
                "stderr": modules.stderr,
            }
            if not self.report["netem_preflight"]["sch_netem_loaded"]:
                raise GateBlocked("target node sch_netem module not confirmed; no automatic modprobe")
            self.report["network_before"] = before
            latency = int((self.policy.get("calibration") or {}).get("latency_ms", 200))
            if not 1 <= latency <= 2000:
                raise GateBlocked("invalid candidate network latency")
            cfg["resource_chaos"] = {
                "experiment_name": "pod-network-latency",
                "environment": {
                    "TARGET_CONTAINER": self.service, "NETWORK_LATENCY": str(latency),
                    "TARGET_PODS": self.target["pod"],
                    "DESTINATION_IPS": "", "DESTINATION_HOSTS": "", "DESTINATION_PORTS": "",
                    "NETWORK_INTERFACE": (self.policy.get("calibration") or {}).get("interface", "eth0"),
                    "TOTAL_CHAOS_DURATION": "120", "JITTER": "0",
                },
            }
            self.report["intensity"] = {
                "latency_ms": latency, "interface": cfg["resource_chaos"]["environment"]["NETWORK_INTERFACE"],
                "direction": "egress", "scope": "all traffic on selected pod network interface, including shared sidecar netns",
                "qdisc_inspection_container": self.target["network_inspection_container"],
                "netem_support": "unverified until actual qdisc readback; workflow must fail if unsupported",
            }
        if self.fault == "http-status-code":
            edge = self.baseline.get("target_edge_requests") or {}
            if edge.get("status") != "ok" or edge["mean"] <= 0:
                raise GateBlocked("HTTP 500 requires caller-reported traffic on target edge")
            if self.baseline.get("target_edge_error_percent", {}).get("status") != "ok":
                raise GateBlocked("HTTP intervention requires observed baseline error telemetry on target edge")
            if not cfg.get("manifest_path"):
                raise GateBlocked("HTTP requires an isolated Istio VirtualService, not proxy bypassed by mesh")
            manifest = self.policy["http_manifest"]
            name = manifest["metadata"]["name"]
            existing = kubectl("-n", self.namespace, "get", "virtualservices", "-o", "json")
            aliases = (
                self.service, f"{self.service}.{self.namespace}",
                f"{self.service}.{self.namespace}.svc",
                f"{self.service}.{self.namespace}.svc.cluster.local",
            )
            if any(
                item["metadata"]["name"] == name
                or any(fnmatchcase(alias, host) for alias in aliases for host in item.get("spec", {}).get("hosts", []))
                for item in existing["items"]
            ):
                raise GateBlocked("existing VirtualService for target; refusing to overwrite or compete")
            self.report["intensity"] = {"http_status": 500, "abort_percent": 100}
        self.save("effective_spec", self.spec)
        self.checkpoint()

    def workflow_preflight(self) -> None:
        if self.fault not in {"cpu-hog", "memory-hog", "network-latency"}:
            return
        template = self.spec["chaos_profile"]["chaos_engine"]
        workflows = kubectl("-n", "litmus", "get", "workflows.argoproj.io", "-o", "json")
        matches = [w for w in workflows["items"] if w["metadata"]["name"] == template]
        if len(matches) != 1:
            raise GateBlocked(f"missing unique registered template {template}; no automatic priming")
        workflow = matches[0]
        self.save("injector_template", workflow)
        images = validate_workflow_template(workflow, self.namespace, self.service, self.fault)
        self.spec["chaos_profile"]["template_snapshot"] = workflow
        self.report["injector_images"] = images

    def cleanup(self) -> None:
        if self.chaos_handle is None or self.cleaned:
            return
        result = self.engine.chaos.stop(self.context, self.chaos_handle)
        self.report["gates"]["cleanup"] = result
        self.save("chaos_artifacts", self.engine.chaos.collect_artifacts(self.context, self.chaos_handle))
        if not result.get("deleted"):
            raise RuntimeError(f"intervention cleanup failed: {result}")
        if self.fault == "http-status-code":
            remaining = kubectl("-n", self.namespace, "get", "virtualservices", "-o", "json")
            name = self.policy["http_manifest"]["metadata"]["name"]
            if any(item["metadata"]["name"] == name for item in remaining["items"]):
                raise RuntimeError("HTTP intervention still present after cleanup")
        else:
            final = self.chaos_handle.get("final_status") or {}
            self.report["gates"]["cleanup"].update({
                "final_status": final, "engine_uids": sorted(self.engine_uids),
            })
            helpers = kubectl("-n", "litmus", "get", "pods", "-o", "json")
            self.save("cleanup_helper_pods", helpers)
            active_helpers = [
                p.get("metadata", {}).get("name")
                for p in helpers["items"]
                if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                and p.get("metadata", {}).get("labels", {}).get("chaosUID") in self.engine_uids
            ]
            self.report["gates"]["cleanup"].update({
                "active_helpers": active_helpers,
            })
            if self.fault == "network-latency":
                after = self.qdisc()
                self.report["network_after"] = after
                self.report["gates"]["cleanup"]["netem_remaining"] = any(
                    item.get("kind") == "netem" for item in after
                )
                self.checkpoint()
            if final.get("state") != "completed" or final.get("chaos_verdict") != "Pass":
                raise RuntimeError("Litmus cleanup/revert not confirmed; stop campaign")
            # Workflow success alone is not proof that helpers left the node.
            if active_helpers or not self.engine_uids:
                raise RuntimeError("helper cleanup identity unavailable or helpers remain; stop campaign")
            if self.fault == "network-latency":
                if self.report["gates"]["cleanup"]["netem_remaining"]:
                    raise RuntimeError("netem rule still applied after workflow completion")
        self.cleaned = True
        self.report["gates"]["cleanup"]["passed"] = True
        self.mark("intervention_removed", source="api_delete_verified" if self.fault == "http-status-code" else "litmus_terminal_and_helpers")

    def intervene_container_kill(self) -> tuple[str, str, str]:
        if self.container_kill_signal_attempted:
            raise RuntimeError("one-shot container-kill signal already attempted; refusing a second signal")
        before = self.application_container_status()
        if (
            not before["ready"] or not before["running"]
            or before["container_id"] != self.target.get("container_id")
            or before["restart_count"] != self.target.get("restart_count")
        ):
            raise GateBlocked("application container changed after baseline; no kill submitted")
        runtime_identity = self.inspect_docker_container(before["container_id"])

        sampling_before_kill = self.cgroup_sampling_enabled
        if sampling_before_kill:
            self.cgroup_sampling_enabled = False
            self.cgroup_sampling_paused_for_restart = True
            self.report.setdefault("telemetry", {}).setdefault("cgroup_v2", {}).update({
                "sampling_status": "stopped_before_one_shot_container_restart",
                "post_kill_sampling": "paused_until_application_container_is_ready",
            })
        self.report["gates"]["submission_attempted"] = True
        signal_at = self.mark(
            "intervention_submission",
            source="controller_clock_before_docker_runtime_SIGKILL",
            target_container=self.service,
        )
        self.container_kill_signal_attempted = True
        event: dict[str, Any] = {
            "signal": "SIGKILL",
            "event_count_submitted": 1,
            "target": before,
            "submission_started_at": signal_at,
            "runtime_readback_before": runtime_identity,
            "submission_source": "docker_runtime_stop_with_SIGKILL_on_exact_verified_container_id",
            "sidecars_excluded": True,
            "restart_observed": False,
            "observations": [],
            "limitation": (
                "Docker reports command acceptance but not the exact kernel event timestamp; "
                "the recorded controller time brackets the runtime request"
            ),
        }
        self.report["container_kill_event"] = event
        self.checkpoint()
        try:
            result = self.stop_docker_container(runtime_identity["id"], timeout=15)
            event["command_result"] = {
                "returncode": result.returncode,
                "stdout": result.stdout[-500:],
                "stderr": result.stderr[-500:],
                "timed_out": False,
            }
            self.report["gates"]["submitted"] = True
        except subprocess.TimeoutExpired as exc:
            event["command_result"] = {
                "returncode": None,
                "stdout": str(exc.stdout or "")[-500:],
                "stderr": str(exc.stderr or "")[-500:],
                "timed_out": True,
                "limitation": "exec stream timed out; restart status is used to confirm the effect",
            }
            self.report["gates"]["submitted"] = True
        except OSError as exc:
            event["command_result"] = {
                "returncode": None, "error": f"{type(exc).__name__}: {exc}",
                "timed_out": False,
            }
            self.checkpoint()
            raise RuntimeError(f"could not submit one-shot Docker container stop: {exc}") from exc
        self.checkpoint()

        deadline = time.monotonic() + CONTAINER_KILL_RESTART_TIMEOUT_SECONDS
        while True:
            status = self.application_container_status()
            restart_delta = status["restart_count"] - before["restart_count"]
            if restart_delta > 1:
                event.update({
                    "restart_observed": True,
                    "restart_count_after_signal": status["restart_count"],
                    "restart_count_delta": restart_delta,
                    "container_id_after_restart": status["container_id"],
                    "termination": status["last_terminated"],
                })
                self.report["scenario_deviation"] = "multiple_restarts_after_single_signal"
                self.report["limitations"].append(
                    "more than one container restart followed the single SIGKILL; "
                    "campaign stopped before another target"
                )
                self.checkpoint()
                raise RuntimeError("multiple container restarts after one-shot kill; stop campaign")
            if (
                restart_delta == 1 and status["container_id"]
                and status["container_id"] != before["container_id"]
            ):
                if not event["restart_observed"]:
                    event.update({
                        "restart_observed": True,
                        "restart_count_delta": restart_delta,
                        "restart_count_after_signal": status["restart_count"],
                        "container_id_after_restart": status["container_id"],
                        "termination": status["last_terminated"],
                    })
                    event["restart_observed_at"] = self.mark(
                        "container_restart_observed",
                        source="kubernetes_application_container_status",
                        restart_count_delta=restart_delta,
                        container_id=status["container_id"],
                    )
                    event["observations"].append({
                        "phase": "restart_confirmation",
                        "observed_at": event["restart_observed_at"],
                        **status,
                    })
                    self.report["gates"]["intervention_confirmed_on_target"] = True
                    self.report["gates"]["mechanism_observed"] = {
                        "status": "one_shot_signal_and_restart_observed",
                        "passed": True,
                    }
                    self.report["gates"]["impact_observed"] = {
                        "status": "container_lifecycle_impact_observed",
                        "container_restart_observed": True,
                    }
                self.mark_container_ready_after_kill(status)
                self.update_container_kill_availability(status)
                if sampling_before_kill and status["running"]:
                    self.sample_cgroup()
                self.report["timestamps"]["intervention_estimate"] = {
                    "start": signal_at,
                    "end": event["restart_observed_at"],
                    "source": "controller_clock_and_kubernetes_restart_readback",
                    "duration_verified": False,
                    "effective_start": None,
                    "effective_end": None,
                    "configured_seconds": 0,
                    "limitation": (
                        "the signal is a one-shot event; the measured bracket ends at the first "
                        "Kubernetes readback proving a restart and is not active fault duration"
                    ),
                }
                self.report["gates"]["intervention_confirmation_source"] = (
                    "docker_SIGKILL_and_restart_count_container_id_readback"
                )
                self.checkpoint()
                return signal_at, event["restart_observed_at"], (
                    "single_SIGKILL_to_application_container_restart_observed"
                )
            if time.monotonic() >= deadline:
                event["confirmation_timeout_seconds"] = CONTAINER_KILL_RESTART_TIMEOUT_SECONDS
                event["restart_observed"] = False
                self.report["gates"]["intervention_confirmed_on_target"] = False
                self.report["gates"]["mechanism_observed"] = {
                    "status": "restart_not_confirmed", "passed": False,
                }
                self.checkpoint()
                raise RuntimeError(
                    "one-shot signal was submitted but no application-container restart "
                    "and readiness were confirmed"
                )
            self.wait(CONTAINER_KILL_POLL_SECONDS)

    def intervene(self) -> tuple[str, str, str]:
        if self.fault == "container-kill":
            return self.intervene_container_kill()
        cfg = self.spec["chaos_profile"]
        cfg["fault_duration_seconds"] = ACTIVE_SECONDS[self.fault]
        self.mark("intervention_submission")
        self.report["gates"]["submission_attempted"] = True
        self.checkpoint()
        if cfg.get("manifest_path"):
            self.chaos_handle = {
                "mode": "manifest", "manifest_path": cfg["manifest_path"], "namespace": self.namespace,
            }
        self.chaos_handle = self.engine.chaos.inject(self.context, cfg)
        self.report["gates"]["submitted"] = True
        self.engine._runtime_handles[self.run_id]["chaos"] = self.chaos_handle
        self.save("submission_handle", self.chaos_handle)
        if self.fault == "http-status-code":
            applied = kubectl("-n", self.namespace, "get", "virtualservice",
                              self.policy["http_manifest"]["metadata"]["name"], "-o", "json")
            self.save("http_applied", applied)
            if applied.get("spec") != self.policy["http_manifest"]["spec"]:
                raise RuntimeError("readback differs from submitted HTTP intervention")
            self.report["http_intervention_readback"] = {
                "passed": True,
                "resource_name": self.policy["http_manifest"]["metadata"]["name"],
                "spec_matches_submitted": True,
                "source": "kubernetes_virtualservice_api_readback",
                "limitation": "confirms the API object, not Envoy data-plane propagation",
            }
            self.report["gates"]["intervention_confirmed_on_target"] = True
            self.report["gates"]["intervention_confirmation_source"] = (
                "virtualservice_api_readback"
            )
            active_start = self.mark("intervention_start_estimate", source="virtualservice_readback",
                                     limitation="API readback is not Envoy propagation timestamp")
            self.wait(ACTIVE_SECONDS[self.fault])
            self.cleanup()
            self.report["timestamps"]["intervention_estimate"] = {
                "start": active_start, "end": self.report["timestamps"]["intervention_removed"]["at"],
                "source": "virtualservice_api_bracket", "duration_verified": False,
                "effective_start": None, "effective_end": None,
                "configured_seconds": ACTIVE_SECONDS[self.fault],
                "limitation": "API object lifetime is not measured Envoy fault lifetime",
            }
            return (
                active_start,
                self.report["timestamps"]["intervention_removed"]["at"],
                "virtualservice_api_bracket_estimated",
            )
        deadline = time.monotonic() + int(self.policy.get("workflow_timeout_seconds", 600))
        while time.monotonic() < deadline:
            status = self.engine.chaos.status(self.context, self.chaos_handle)
            workflow = kubectl("-n", "litmus", "get", "workflow.argoproj.io",
                               self.chaos_handle["workflow_name"], "-o", "json")
            self.save("workflow_observed", workflow)
            self.report["workflow_timestamps"] = {
                "start": workflow.get("status", {}).get("startedAt"),
                "end": workflow.get("status", {}).get("finishedAt"),
                "source": "argo_workflow_status",
            }
            engines = kubectl("-n", "litmus", "get", "chaosengines", "-l",
                              f'workflow_run_id={workflow["metadata"]["uid"]}', "-o", "json")
            self.save("chaosengines_observed", engines)
            self.engine_uids.update(item["metadata"]["uid"] for item in engines["items"])
            self.save("injector_pods", kubectl("-n", "litmus", "get", "pods", "-o", "json"))
            if self.fault == "network-latency":
                rules = self.qdisc()
                if any(item.get("kind") == "netem" for item in rules):
                    self.report["network_applied"] = rules
                    netem = [item for item in rules if item.get("kind") == "netem"]
                    delays = [
                        item.get("options", {}).get("delay", {}).get("delay")
                        if isinstance(item.get("options", {}).get("delay"), dict) else None
                        for item in netem
                    ]
                    expected_seconds = self.report["intensity"]["latency_ms"] / 1000
                    valid = len(delays) == 1 and isinstance(delays[0], (int, float)) and math.isclose(
                        delays[0], expected_seconds, rel_tol=.01, abs_tol=.001,
                    )
                    self.report["network_rule_validation"] = {
                        "passed": valid, "expected_seconds": expected_seconds, "observed_seconds": delays,
                        "unit_source": "iproute2 tc JSON options.delay.delay uses seconds",
                    }
                    if not valid:
                        raise RuntimeError("netem applied but configured delay not verified from actual rule")
                    self.report["timestamps"].setdefault("network_rule_first_observed", {
                        "at": utc_now_iso(), "source": "tc_qdisc_readback",
                        "limitation": "polling timestamp brackets application, not exact tc install instant",
                    })
            if self.fault == "memory-hog":
                try:
                    if self.cgroup_sampling_enabled:
                        self.sample_cgroup()
                    now = utc_now_iso()
                    check = self.collect(
                        "memory_safety", (datetime.fromisoformat(now) - timedelta(seconds=15)).isoformat(),
                        now, keys=("memory_usage_bytes", "oom_events"),
                    )
                except Exception as exc:
                    check = {"memory_usage_bytes": {"status": "tool_error", "error": str(exc)}}
                pods = self.pods()
                self.save("pods_during", pods)
                self.check_memory_safety(check, pods, engines)
            self.save("workflow_status", status)
            self.report["workflow"] = status
            self.checkpoint()
            if status.get("state") in {"completed", "failed"}:
                self.chaos_handle["final_status"] = status
                self.chaos_handle["chaos_uid"] = status.get("chaos_uid")
                break
            if self.health()["unhealthy_nodes"]:
                self.report["scenario_deviation"] = "application_or_node_unhealthy"
                raise RuntimeError("application/node became unhealthy; intervention requires cleanup")
            self.wait(5)
        else:
            raise RuntimeError("workflow timeout; no further intervention allowed")
        self.cleanup()
        status = self.report["workflow"]
        anchor = status.get("chaos_result_created_at")
        if not anchor:
            raise RuntimeError("no intervention timing anchor; refusing to present workflow overhead as active duration")
        estimated_end = (datetime.fromisoformat(anchor.replace("Z", "+00:00"))
                         + timedelta(seconds=ACTIVE_SECONDS[self.fault])).isoformat()
        self.report["timestamps"]["intervention_estimate"] = {
            "start": anchor, "end": estimated_end,
            "source": "chaos_result_created_at_plus_configured_duration",
            "effective_start": None, "effective_end": None, "duration_verified": False,
            "configured_seconds": ACTIVE_SECONDS[self.fault],
            "limitation": "ChaosResult creation precedes helper injection; configured duration is not measured active time",
        }
        return anchor, estimated_end, "chaos_result_created_at_plus_configured_duration"

    def check_memory_safety(
        self, summary: dict[str, Any], pods: dict[str, Any], engines: dict[str, Any],
    ) -> None:
        item = summary.get("memory_usage_bytes", {})
        oom = summary.get("oom_events", {})
        baseline_oom = self.baseline.get("oom_events", {})
        oom_kill = summary.get("oom_kill_events", {})
        baseline_oom_kill = self.baseline.get("oom_kill_events", {})
        oom_covered = oom.get("status") == baseline_oom.get("status") == "ok"
        if self.cgroup_sampling_enabled:
            oom_covered = (
                oom_covered and oom_kill.get("status") == baseline_oom_kill.get("status") == "ok"
            )
        oom_increased = oom_covered and (
            oom["max"] > baseline_oom["max"]
            or (
                oom_kill.get("status") == baseline_oom_kill.get("status") == "ok"
                and oom_kill["max"] > baseline_oom_kill["max"]
            )
        )
        limit = self.report["intensity"]["limit_bytes"]
        containers = [
            c for pod in pods["items"] for c in pod.get("status", {}).get("containerStatuses", [])
            if c["name"] == self.service
        ]
        deviation = (
            oom_increased or len(containers) != 1
            or any(pod["metadata"]["uid"] != self.target["uid"] for pod in pods["items"])
            or any(c["restartCount"] != self.target["restart_count"] for c in containers)
            or any((c.get("lastState", {}).get("terminated") or {}).get("reason") == "OOMKilled"
                   and c["restartCount"] > self.target["restart_count"] for c in containers)
        )
        cutoff = item.get("status") != "ok" or not oom_covered or item.get("max", 0) >= limit * .90
        if not deviation and not cutoff:
            return
        self.report["scenario_deviation"] = "unexpected_oom_or_restart" if deviation else "90_percent_memory_cutoff_or_telemetry_loss"
        self.report["memory_safety"] = {
            "threshold_bytes": limit * .90, "observed": item,
            "oom_counter_baseline": baseline_oom, "oom_counter_observed": oom,
            "oom_kill_counter_baseline": baseline_oom_kill,
            "oom_kill_counter_observed": oom_kill,
            "limitation": "5s polling plus query/scrape latency and abort handling cannot guarantee OOM prevention",
        }
        # Litmus engineState=stop requests abort; never kill an application or
        # delete a helper before its revert handler has run.
        for engine in engines["items"]:
            result = subprocess.run(
                ["kubectl", "-n", "litmus", "patch", "chaosengine", engine["metadata"]["name"],
                 "--type=merge", "-p", '{"spec":{"engineState":"stop"}}'],
                check=False, capture_output=True, text=True, timeout=30,
            )
            self.report.setdefault("abort_requests", []).append({
                "engine": engine["metadata"]["name"], "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "at": utc_now_iso(),
            })
        self.checkpoint()
        raise RuntimeError("memory scenario safety abort; preserve data and do not escalate or retry")

    def evaluate_http_effect(self, intervention: dict[str, Any]) -> None:
        readback = self.report.get("http_intervention_readback") or {}
        confirmed = readback.get("passed") is True and readback.get("spec_matches_submitted") is True
        self.report["gates"]["intervention_confirmed_on_target"] = confirmed
        self.report["gates"]["mechanism_observed"] = {
            **readback,
            "status": "configured" if confirmed else "not_confirmed",
            "passed": confirmed,
        }
        baseline_edge_error = self.baseline.get("target_edge_error_percent", {})
        intervention_edge_error = intervention.get("target_edge_error_percent", {})
        edge_error_increased = None
        if baseline_edge_error.get("status") == intervention_edge_error.get("status") == "ok":
            edge_error_increased = (
                intervention_edge_error["mean"] > baseline_edge_error["mean"] + 1
            )
        self.report["gates"]["impact_observed"] = {
            "target_edge_error_increased": edge_error_increased,
            "target_edge_error_percent": {
                "baseline": baseline_edge_error,
                "intervention": intervention_edge_error,
            },
            "target_edge_requests": intervention.get("target_edge_requests", {"status": "no_data"}),
            "application_errors_increased": (
                intervention["errors_percent"]["mean"] > self.baseline["errors_percent"]["mean"] + 1
                if intervention.get("errors_percent", {}).get("status") == "ok" else None
            ),
            "latency_increased": (
                intervention["latency_ms"]["mean"] > self.baseline["latency_ms"]["p95"] * 1.5
                if intervention.get("latency_ms", {}).get("status") == "ok" else None
            ),
        }
        if edge_error_increased is None:
            self.report["limitations"].append(
                "HTTP impact at the target edge is not measurable; configuration confirmation is not evidence of observed 5xx"
            )
        try:
            degraded = [
                timestamp for timestamp, value in samples(
                    self.raw["intervention"]["data"].get("errors_percent", {})
                )
                if value > self.baseline["errors_percent"]["p95"] + 1
            ]
        except (GateBlocked, ValueError, TypeError) as exc:
            degraded = []
            self.report["limitations"].append(f"degradation onset unavailable: {exc}")
        self.report["timestamps"]["degradation_onset"] = {
            "at": datetime.fromtimestamp(min(degraded), timezone.utc).isoformat() if degraded else None,
            "source": "first_error_rate_sample_exceeding_baseline_p95_plus_1pp",
            "limitation": "sampled onset, not exact failure onset; no observed error does not refute a configured intervention",
        }

    def evaluate_effect(self, intervention: dict[str, Any], start: str) -> None:
        if self.fault == "container-kill":
            event = self.report.get("container_kill_event") or {}
            confirmed = (
                event.get("event_count_submitted") == 1
                and event.get("restart_observed") is True
                and event.get("restart_count_delta") == 1
            )
            self.report["gates"]["intervention_confirmed_on_target"] = confirmed
            self.report["gates"]["mechanism_observed"] = {
                "status": "one_shot_signal_and_restart_observed" if confirmed else "not_confirmed",
                "passed": confirmed,
                "signal": event.get("signal"),
                "target": event.get("target"),
                "restart_count_delta": event.get("restart_count_delta"),
                "container_id_after_restart": event.get("container_id_after_restart"),
                "termination": event.get("termination"),
                "limitation": "exit code 137 alone is not evidence of OOM",
            }
            self.report["gates"]["impact_observed"] = {
                "status": "container_lifecycle_impact_observed" if confirmed else "not_confirmed",
                "container_restart_observed": confirmed,
                "restart_count_delta": event.get("restart_count_delta"),
                "availability_impact": {"status": "pending_observation"},
                "limitation": "restart confirmation does not by itself quantify request-level impact",
            }
            termination = event.get("termination") or {}
            finished_at = termination.get("finishedAt")
            self.report["timestamps"]["degradation_onset"] = {
                "at": finished_at or start,
                "source": (
                    "kubernetes_last_terminated_finishedAt"
                    if finished_at else "controller_clock_before_docker_runtime_kill"
                ),
                "limitation": (
                    "Kubernetes termination timestamp is the closest available event time"
                    if finished_at else "signal time is a controller-side bracket, not the exact kernel event time"
                ),
            }
            if not confirmed:
                self.report["limitations"].append(
                    "single-signal restart effect was not confirmed; missing lifecycle data is not absence of impact"
                )
            return
        if self.fault == "http-status-code":
            self.evaluate_http_effect(intervention)
            return
        if self.fault == "network-latency":
            self.report["gates"]["intervention_confirmed_on_target"] = (
                self.report.get("network_rule_validation", {}).get("passed")
            )
        key = {"cpu-hog": "cpu_cores", "memory-hog": "memory_bytes",
               "network-latency": "target_edge_latency_ms"}.get(self.fault)
        if not key:
            return
        baseline = self.baseline.get(key) or {}
        observed = intervention.get(key) or {}
        if observed.get("status") != "ok":
            self.report["gates"]["mechanism_observed"] = {"status": "no_data", "metric": key}
            return
        if baseline.get("status") != "ok":
            self.report["gates"]["mechanism_observed"] = {
                "status": "missing_baseline", "metric": key, "intervention": observed,
                "limitation": "missing series is not assumed to be a zero baseline",
            }
            return
        delta = observed["mean"] - baseline.get("mean", 0)
        threshold = max(
            baseline.get("mean", 0) * float(self.policy.get("sustained_ratio", 1.25)),
            baseline.get("mean", 0) + float(self.policy.get("minimum_cpu_delta_cores", .05))
            if key == "cpu_cores" else baseline.get("mean", 0) + 1,
        )
        points = samples(self.raw["intervention"]["data"][key])
        # Allow activation uncertainty and RED rate-window convergence before the sustained gate.
        lo = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() + 30
        result = sustained(
            [(t, v) for t, v in points if t >= lo], threshold,
            float(self.policy.get("sustained_seconds", 60)), 20,
        )
        result.update({
            "metric": key, "unit": {
                "cpu_cores": "cores", "memory_bytes": "bytes",
                "target_edge_error_percent": "percent",
                "latency_ms": "milliseconds", "target_edge_latency_ms": "milliseconds",
            }[key],
            "baseline": baseline, "intervention": observed, "mean_delta": delta,
            "timing_source": self.window["phase_sources"]["fault"],
        })
        if self.fault == "cpu-hog":
            quota = self.report["intensity"]["quota_cores"]
            result["baseline_mean_quota_fraction"] = baseline["mean"] / quota
            result["intervention_mean_quota_fraction"] = observed["mean"] / quota
            calibration = self.report["intensity"]["calibration"]
            target = self.report["intensity"]["candidate_target_quota_fraction"]
            tolerance = self.report["intensity"]["target_tolerance_fraction"]
            target_floor = target * (1 - tolerance)
            target_ceiling = target * (1 + tolerance)
            target_sustained = sustained(
                [(timestamp, value / quota) for timestamp, value in points if timestamp >= lo],
                target_floor,
                float(self.policy.get("sustained_seconds", 60)),
                20,
            )
            result["target_attainment"] = {
                **target_sustained,
                "passed": target_sustained["passed"] and target_floor <= observed["mean"] / quota <= target_ceiling,
                "target_quota_fraction": target,
                "accepted_mean_fraction": [target_floor, target_ceiling],
                "observed_mean_fraction": observed["mean"] / quota,
                "calibration_status": self.report["intensity"]["calibration_status"],
                "limitation": "candidate parameters are not prior proof; this run measures whether the configured target was reached",
            }
            result["passed"] = result["passed"] and result["target_attainment"]["passed"]
            result["throttling_separately"] = {
                "throttled_seconds_per_second": {
                    "unit": "seconds_per_second",
                    "baseline": self.baseline.get("throttled_seconds_per_second", {"status": "no_data"}),
                    "intervention": intervention.get("throttled_seconds_per_second", {"status": "no_data"}),
                },
                "throttled_periods_per_second": {
                    "unit": "periods_per_second",
                    "baseline": self.baseline.get("throttled_periods_per_second", {"status": "no_data"}),
                    "intervention": intervention.get("throttled_periods_per_second", {"status": "no_data"}),
                },
            }
        self.report["gates"]["mechanism_observed"] = result
        self.report["gates"]["intervention_confirmed_on_target"] = (
            self.report.get("network_rule_validation", {}).get("passed", False)
            if self.fault == "network-latency" else result["passed"]
        )
        latency_key = "target_edge_latency_ms" if self.fault == "network-latency" else "latency_ms"
        latency_baseline = self.baseline.get(latency_key, {})
        latency_intervention = intervention.get(latency_key, {})
        self.report["gates"]["impact_observed"] = {
            "errors_increased": (
                intervention["errors_percent"]["mean"] > self.baseline["errors_percent"]["mean"] + 1
                if intervention.get("errors_percent", {}).get("status") == "ok" else None
            ),
            "latency_increased": (
                latency_intervention["mean"] > latency_baseline["p95"] * 1.5
                if latency_intervention.get("status") == "ok" and latency_baseline.get("status") == "ok"
                else None
            ),
        }
        self.report["gates"]["latency_comparison"] = {
            "latency_metric": latency_key,
            "latency_baseline": latency_baseline,
            "latency_intervention": latency_intervention,
        }
        degraded = []
        if any(value is None for value in self.report["gates"]["impact_observed"].values()):
            self.report["limitations"].append("impact metrics missing; no-data is not absence of impact")
        degradation_key = "target_edge_latency_ms" if self.fault == "network-latency" else "errors_percent"
        if self.fault == "network-latency":
            baseline_latency = self.baseline.get(degradation_key, {})
            degradation_threshold = max(
                baseline_latency.get("mean", 0) * 1.25,
                baseline_latency.get("mean", 0) + 1,
            )
            degradation_source = "first_source_reporter_edge_latency_sample_above_baseline_threshold"
        else:
            degradation_threshold = self.baseline["errors_percent"]["p95"] + 1
            degradation_source = "first_error_rate_sample_exceeding_baseline_p95_plus_1pp"
        try:
            degraded = [
                t for t, value in samples(
                    self.raw["intervention"]["data"].get(degradation_key, {})
                )
                if value > degradation_threshold
            ]
        except (GateBlocked, ValueError, TypeError) as exc:
            self.report["limitations"].append(f"degradation onset unavailable: {exc}")
        self.report["timestamps"]["degradation_onset"] = {
            "at": datetime.fromtimestamp(min(degraded), timezone.utc).isoformat() if degraded else None,
            "source": degradation_source,
            "metric": degradation_key,
            "threshold": degradation_threshold,
            "limitation": "sampled onset, not exact failure onset; missing samples do not establish absence of impact",
        }
        if not result["passed"]:
            self.report["limitations"].append("mechanism not sustained/detectable: calibration required; no automatic escalation")

    def recovery_check(self) -> dict[str, Any]:
        if self.fault == "container-kill":
            self.observe_container_kill_state("recovery_check")
        now = utc_now_iso()
        check_start = (datetime.fromisoformat(now) - timedelta(seconds=60)).isoformat()
        summary = self.collect("recovery_check", check_start, now)
        healthy = self.health()
        recovered = healthy["healthy"]
        latency_key = "target_edge_latency_ms" if self.fault == "network-latency" else "latency_ms"
        for key in ("traffic", "cpu_cores", "memory_bytes", "errors_percent", latency_key):
            observed = summary.get(key, {})
            if observed.get("status") != "ok" or observed["samples"] < 4:
                recovered = False
                continue
            base = self.baseline[key]
            if key == "traffic":
                recovered &= observed["mean"] >= base["mean"] * .8
            else:
                margin = {
                    "cpu_cores": .05, "memory_bytes": 16 * 2**20,
                    "errors_percent": 1, "latency_ms": 50,
                    "target_edge_latency_ms": 50,
                }[key]
                recovered &= observed["p95"] <= max(base["p95"] * 1.2, base["p95"] + margin)
        return {"passed": bool(recovered), "health": healthy, "summary": summary}

    def operational_recovery(self) -> None:
        policy = self.policy.get("resilience")
        if not isinstance(policy, dict) or policy.get("version") != 1:
            return
        timeout = policy.get("operational_timeout_seconds")
        stable_seconds = policy.get("stable_seconds")
        if (
            type(timeout) is not int or type(stable_seconds) is not int
            or not 120 <= stable_seconds < timeout <= 600
        ):
            raise RuntimeError("invalid operational recovery budget")
        self.mark("operational_recovery_started")
        deadline = time.monotonic() + timeout
        stable_since: float | None = None
        last_check: float | None = None
        while time.monotonic() < deadline:
            status = self.application_container_status()
            event = self.report.get("container_kill_event") or {}
            expected_id = (
                event.get("container_id_after_restart")
                if self.fault == "container-kill" else self.target.get("container_id")
            )
            expected_restarts = (
                event.get("restart_count_after_signal")
                if self.fault == "container-kill" else self.target.get("restart_count")
            )
            if (
                not expected_id or status["container_id"] != expected_id
                or status["restart_count"] != expected_restarts
            ):
                raise RuntimeError("application container changed during operational recovery")
            check = self.recovery_check()
            if self.fault == "memory-hog":
                self.check_memory_safety(check["summary"], self.pods(), {"items": []})
            if not status["ready"] or not status["running"]:
                check["passed"] = False
            now = time.monotonic()
            if last_check is not None and now - last_check > 60:
                stable_since = None
            last_check = now
            if check["passed"]:
                if stable_since is None:
                    stable_since = now
            else:
                stable_since = None
            stable_for = now - stable_since if stable_since is not None else 0
            passed = check["passed"] and stable_for >= stable_seconds and now < deadline
            self.report["gates"]["operational_recovery"] = {
                **check, "passed": bool(passed), "policy_version": 1,
                "target": status,
                "stable_seconds": stable_for, "required_stable_seconds": stable_seconds,
                "source": "same_load_same_baseline_unchanged_recovery_thresholds",
            }
            self.checkpoint()
            if passed:
                self.mark("operational_recovery_observed")
                return
            self.wait(15)
        self.mark("operational_recovery_exhausted")

    def recover(self) -> str:
        self.mark("post_observation_started")
        if self.fault == "container-kill":
            self.container_kill_observation_started = True
        post_start = utc_now_iso()
        self.wait(120)
        deadline = time.monotonic() + int(self.policy.get("recovery_timeout_seconds", 300))
        while True:
            check = self.recovery_check()
            self.report["gates"]["recovery"] = check
            self.checkpoint()
            if check["passed"]:
                now = utc_now_iso()
                self.mark("recovery_observed", source="60s_metrics_window_and_kubernetes_health")
                self.collect("post", post_start, now)
                return now
            if time.monotonic() >= deadline:
                self.mark("experimental_recovery_expired")
                self.operational_recovery()
                raise RuntimeError("recovery not confirmed; campaign must stop before next service")
            self.wait(15)

    def run(self) -> None:
        self.save("effective_spec", self.spec)
        self.checkpoint()
        self.engine._runtime_handles.setdefault(self.run_id, {})
        failure: Exception | None = None
        try:
            self.engine.metrics.validate(self.spec["observability"]["metrics"])
            self.engine.traces.validate(self.spec["observability"]["traces"])
            if self.fault == "container-kill":
                self.report["injection_strategy"] = "single_Docker_runtime_SIGKILL_to_exact_application_container"
            else:
                self.engine.chaos.validate(self.spec["chaos_profile"])
            self.inspect_target()
            if self.fault == "container-kill":
                self.prepare_intervention()
            self.workflow_preflight()
            self.metadata()
            self.start_cgroup_sampling()
            budget = controlled_time_budget(self.policy)
            self.report["time_budget"] = budget
            load = dict(self.spec["load_profile"], run_time_seconds=budget["load_seconds"], controlled_remote=True)
            self.engine.load.validate(load)
            self.engine.load.prepare(self.context, load)
            self.engine._event(self.run_id, "run_started", {"at": utc_now_iso(), "controlled": True})
            self.load_handle = self.engine.load.start_load(self.context, load)
            self.engine._runtime_handles[self.run_id]["load"] = self.load_handle
            warm_start = self.mark("warmup_started")
            origin = datetime.fromisoformat(warm_start)
            nominal_submission = 60 + 90
            nominal_removal = nominal_submission + ACTIVE_SECONDS[self.fault]
            self.report["planned_timestamps"] = {
                "source": "nominal_minimum_schedule_from_load_start",
                "limitation": "stabilization, workflow overhead and recovery gates may extend the schedule; not observed timestamps",
                **{
                    name: (origin + timedelta(seconds=seconds)).isoformat()
                    for name, seconds in {
                        "baseline_start": 60, "submission": nominal_submission,
                        "intervention_end_excluding_overhead": nominal_removal,
                        "post_observation_end": nominal_removal + 120,
                        "cooldown_end_after_nominal_recovery": nominal_removal + 150,
                    }.items()
                },
            }
            self.checkpoint()
            self.wait(60)
            stable = False
            for attempt in range(9):
                end = utc_now_iso()
                start = (datetime.fromisoformat(end) - timedelta(seconds=30)).isoformat()
                summary = self.collect(f"stabilization_{attempt}", start, end)
                try:
                    self.require_coverage(summary, 30)
                    checks = stability_metrics(self.raw[f"stabilization_{attempt}"]["data"])
                    stable_metrics = all(check["passed"] for check in checks.values())
                    health = self.health()
                    self.report.setdefault("stabilization_checks", []).append({
                        "attempt": attempt, "start": start, "end": end,
                        "metrics": checks, "health": health,
                        "passed": bool(stable_metrics and health["healthy"]),
                    })
                    self.checkpoint()
                    if stable_metrics and health["healthy"]:
                        stable = True
                        break
                except GateBlocked as exc:
                    self.report["limitations"].append(str(exc))
                self.wait(15)
            if not stable:
                raise GateBlocked("load did not stabilize after minimum 60s warmup")
            self.mark("warmup_completed")
            baseline_start = self.mark("baseline_started")
            self.wait(90)
            baseline_end = self.mark("baseline_completed")
            self.baseline = self.collect("baseline", baseline_start, baseline_end)
            self.require_coverage(self.baseline, 90)
            self.report["gates"]["telemetry_coverage"] = self.baseline
            self.prepare_intervention()
            current = self.pods()
            if self.target.get("uid") and (
                len(current["items"]) != 1 or current["items"][0]["metadata"]["uid"] != self.target["uid"]
            ):
                raise GateBlocked("target pod changed during warmup/baseline; recalibrate without reinjecting")
            start, end, source = self.intervene()
            self.window = {
                "schema_version": "1", "run_id": self.run_id, "experiment_id": self.context["experiment_id"],
                "status": "running", "timezone": "UTC", "sampling_interval_seconds": 5,
                "phases": {
                    "warmup": {"start": warm_start, "end": baseline_start},
                    "baseline": {"start": baseline_start, "end": baseline_end},
                    "fault": {"start": start, "end": end},
                },
                "phase_sources": {"baseline": "after_load_stabilization", "fault": source},
            }
            intervention = self.collect("intervention", start, end)
            self.evaluate_effect(intervention, start)
            recovery = self.recover()
            post_start = (
                self.report["timestamps"]["post_observation_started"]["at"]
                if self.fault == "container-kill"
                else self.report["timestamps"]["intervention_removed"]["at"]
            )
            self.window["phases"]["post"] = {
                "start": post_start, "end": recovery,
            }
            self.window["window"] = {"start": warm_start, "end": recovery}
            self.window["status"] = "completed"
            self.save("observation_window", self.window)
            self.capture_telemetry(warm_start, recovery)
            self.mark("cooldown_started")
            self.wait(30)
            self.mark("cooldown_completed")
            if self.fault == "container-kill":
                self.observe_container_kill_state("cooldown_end")
            if self.fault == "memory-hog":
                now = utc_now_iso()
                final_safety = self.collect(
                    "memory_final_safety",
                    (datetime.fromisoformat(now) - timedelta(seconds=15)).isoformat(), now,
                    keys=("memory_usage_bytes", "oom_events"),
                )
                self.check_memory_safety(final_safety, self.pods(), {"items": []})
            if not self.health()["healthy"]:
                raise RuntimeError("application/node unhealthy after cooldown; stop campaign")
            self.report["status"] = "completed"
        except GateBlocked as exc:
            self.report["status"] = "blocked"
            self.report["reason"] = str(exc)
            if self.report["gates"].get("submission_attempted"):
                self.report["status"] = "aborted"
                failure = exc
        except Exception as exc:
            self.report["status"] = "aborted"
            self.report["reason"] = str(exc)
            failure = exc
        finally:
            if self.cgroup_samples:
                try:
                    self.update_cgroup_telemetry()
                    self.paths["cgroup_v2_samples"] = self.engine.storage.save_artifact(
                        self.run_id,
                        "cgroup_v2_samples",
                        {
                            "source": "cgroup v2 read inside the selected application container",
                            "pod": self.target.get("pod"),
                            "pod_uid": self.target.get("uid"),
                            "container": self.service,
                            "samples": self.cgroup_samples,
                        },
                    )
                except Exception as exc:
                    self.report["gates"]["cgroup_telemetry"] = {
                        "passed": False, "error": f"{type(exc).__name__}: {exc}",
                    }
                    self.report["status"] = "aborted"
                    failure = exc
            try:
                self.cleanup()
            except Exception as exc:
                self.report["gates"]["cleanup"].update(passed=False, error=str(exc))
                self.report["status"] = "aborted"
                failure = exc
            if self.load_handle:
                try:
                    result = self.engine.load.stop_load(self.context, self.load_handle)
                    self.report["gates"]["load_stopped"] = result
                    if not result.get("stopped"):
                        raise RuntimeError(f"load cleanup failed: {result}")
                    self.save("load_summary", self.engine.load.collect_summary(self.context, self.load_handle))
                except Exception as exc:
                    self.report["gates"]["load_stopped"] = {"stopped": False, "error": str(exc)}
                    self.report["status"] = "aborted"
                    failure = exc
            if self.report["status"] != "completed" and self.load_handle:
                self.capture_partial()
            if self.report["status"] == "blocked" and self.load_handle:
                try:
                    healthy = self.health()["healthy"]
                except (RuntimeError, OSError, ValueError, KeyError) as exc:
                    healthy = False
                    self.report["limitations"].append(f"final health check unavailable: {exc}")
                if not healthy:
                    self.report["status"] = "aborted"
                    failure = RuntimeError("application unhealthy after blocked attempt; stop campaign")
                    self.report["reason"] = str(failure)
            try:
                self.save("pods_after", self.pods())
                self.save("kubernetes_events", kubectl("-n", self.namespace, "get", "events", "-o", "json"))
            except (RuntimeError, OSError, ValueError) as exc:
                self.report["limitations"].append(f"historical lifecycle capture failed: {exc}")
            if failure and not self.report.get("reason"):
                self.report["reason"] = str(failure)
            self.checkpoint()
            self.mark("run_finished")
            run = self.engine.storage.get_run(self.run_id)
            run.status = RunStatus.COMPLETED if self.report["status"] == "completed" else RunStatus.FAILED
            run.ended_at = utc_now_iso()
            run.timings = dict(self.report["timestamps"])
            if self.window:
                run.timings["controlled_observation_window"] = self.window
                run.timings["sampling_interval_seconds"] = self.window.get(
                    "sampling_interval_seconds", 5,
                )
            run.summary.update({
                "controlled_status": self.report["status"],
                "controlled_gates": self.report["gates"],
                "reason": self.report.get("reason"), "artifact_paths": self.paths,
            })
            self.engine.storage.update_run(run)
            self.engine._event(self.run_id, "controlled_run_finished", {"status": self.report["status"], "reason": self.report.get("reason")})
        if failure:
            raise RuntimeError(f"controlled campaign abort: {failure}") from failure

    def capture_telemetry(self, start: str, end: str) -> None:
        for name in ("metrics_raw", "traces_raw"):
            try:
                if name == "metrics_raw":
                    raw = self.engine.metrics.collect_window(
                        self.context, self.spec["observability"]["metrics"], start, end, 5,
                    )
                else:
                    raw = self.engine.traces.collect_window(
                        self.context, self.spec["observability"]["traces"], start, end,
                    )
            except Exception as exc:
                raw = {"status": "tool_error", "error": f"{type(exc).__name__}: {exc}"}
                self.report["limitations"].append(f"{name} capture failed: {raw['error']}")
                self.engine._event(self.run_id, "telemetry_capture_failed", {"source": name, **raw})
            self.save(name, raw)
        try:
            snapshot = self.engine._persist_rca_evidence_snapshot(
                run_id=self.run_id, context=self.context,
                observability_config=self.spec["observability"], namespace=self.namespace,
                observation_window=self.window, capture_config={"enabled": True},
                artifact_paths=self.paths,
            )
            if snapshot is None:
                raise RuntimeError("log snapshot was not persisted; provider returned no result")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.report["limitations"].append(f"log snapshot capture failed: {error}")
            self.save("rca_evidence_snapshot", {"status": "tool_error", "error": error})

    def capture_partial(self) -> None:
        timestamps = self.report["timestamps"]
        end = timestamps.get("experimental_recovery_expired", {}).get("at") or utc_now_iso()
        start = timestamps.get("warmup_started", {}).get("at")
        if not start:
            return
        try:
            self.window.setdefault("phases", {})
            if "baseline_started" in timestamps and "baseline_completed" in timestamps:
                self.window["phases"]["baseline"] = {
                    "start": timestamps["baseline_started"]["at"], "end": timestamps["baseline_completed"]["at"],
                }
            if "intervention_submission" in timestamps:
                self.window["phases"].setdefault("fault", {
                    "start": timestamps["intervention_submission"]["at"], "end": end,
                })
            phase_sources = {
                "baseline": "after_load_stabilization",
                "fault": "partial_observation_not_effective_intervention",
            }
            post_marker = timestamps.get("post_observation_started")
            post_start = post_marker.get("at") if isinstance(post_marker, dict) else None
            if isinstance(post_start, str) and "post" not in self.window["phases"]:
                self.window["phases"]["post"] = {"start": post_start, "end": end}
                phase_sources["post"] = "recovery_observation"
            self.window.update({
                "schema_version": "1", "run_id": self.run_id, "partial": True,
                "status": "failed", "window": {"start": start, "end": end},
                "phase_sources": phase_sources,
            })
            self.save("observation_window", self.window)
            self.capture_telemetry(start, end)
        except Exception as exc:
            self.report["limitations"].append(f"partial telemetry capture failed: {type(exc).__name__}: {exc}")
            self.report["gates"]["partial_telemetry"] = {
                "passed": False, "error": f"{type(exc).__name__}: {exc}",
            }
