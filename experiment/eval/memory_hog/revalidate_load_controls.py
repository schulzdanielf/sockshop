"""Requery historical no-fault controls without loading or injecting anything."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment.platform.backend.adapters.noop_notifier import NoopNotificationAdapter
from experiment.platform.backend.controlled_experiment import (
    CGROUP_RESOURCE_KEYS, CONTROLLED_MEASUREMENT_POLICY,
    DEFAULT_STABILITY_RELATIVE_LIMIT, ControlledRun, GateBlocked,
    resource_environment, samples, stability_metrics, stats, validate_cgroup_timing,
)
from experiment.platform.backend.engine import OrchestratorEngine
from experiment.platform.backend.plugins.litmus_plugin import LitmusChaosPlugin
from experiment.platform.backend.plugins.locust_plugin import LocustLoadPlugin
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.plugins.mcp_metrics_plugin import MCPPrometheusMetricsPlugin
from experiment.platform.backend.plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage
from experiment.eval.memory_hog.supplemental_campaign import (
    SUPPORTED_SUPPLEMENTAL_STABILITY_POLICIES,
)
from experiment.eval.memory_hog.load_check import control_acceptance_policy


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read(path: Path, sources: dict[str, str]) -> dict[str, Any]:
    content = path.read_bytes()
    sources[str(path)] = hashlib.sha256(content).hexdigest()
    return json.loads(content)


def query_window(
    client: MCPToolClient, queries: dict[str, str], start: str, end: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # One request keeps all metrics on exactly the same evaluation grid.
    combined = " or ".join(
        f'label_replace(({query}),"recheck_metric","{key}","","^$")'
        for key, query in queries.items()
    )
    arguments = {"query": combined, "start": start, "end": end, "step": "5s"}
    response = client.call_tool_json("prometheus_range_query", arguments)
    if response.get("status") != "success":
        raise GateBlocked(f"historical metric requery failed: {response}")
    result = response.get("data", {})
    if result.get("resultType") != "matrix" or not isinstance(result.get("result"), list):
        raise GateBlocked("historical metric requery did not return a matrix")
    data: dict[str, Any] = {
        key: {"status": "success", "data": {"resultType": "matrix", "result": []}}
        for key in queries
    }
    for series in result["result"]:
        entry = copy.deepcopy(series)
        key = entry["metric"].pop("recheck_metric")
        if key not in data:
            raise GateBlocked(f"unexpected historical metric: {key}")
        data[key]["data"]["result"].append(entry)
    return data, {"tool": "prometheus_range_query", "arguments": arguments, "response": response}


def evaluate_window(
    run: ControlledRun, raw: dict[str, Any], seconds: int,
    *, check_stability: bool,
    default_relative_limit: float = DEFAULT_STABILITY_RELATIVE_LIMIT,
    relative_limit_overrides: dict[str, float] | None = None,
    stability_blocking: bool = True,
) -> dict[str, Any]:
    summary = {key: stats(samples(payload)) for key, payload in raw["data"].items()}
    errors = []
    telemetry = raw["resource_telemetry"]
    readings = [
        *([telemetry["cpu_predecessor_sample"]] if telemetry.get("cpu_predecessor_sample") else []),
        *telemetry["samples"],
    ]
    try:
        validate_cgroup_timing(readings)
    except GateBlocked as exc:
        errors.append(str(exc))
        # Legacy reads have no monotonic timestamps: never manufacture CPU deltas.
        summary["cpu_cores"] = {"status": "invalid", "samples": 0, "error": str(exc)}
    try:
        run.require_coverage(summary, seconds)
    except GateBlocked as exc:
        errors.append(str(exc))
    checks = {}
    if check_stability:
        try:
            checks = stability_metrics(
                raw["data"],
                include_target_edge=True,
                default_relative_limit=default_relative_limit,
                relative_limit_overrides=relative_limit_overrides,
            )
        except GateBlocked as exc:
            errors.append(str(exc))
        for metric in checks:
            if summary[metric]["status"] != "ok":
                checks[metric] = {"passed": False, "status": "invalid",
                                  "error": summary[metric].get("error", "missing samples")}
    return {
        "summary": summary, "metrics": checks,
        "stability_passed": all(check["passed"] for check in checks.values()) and not errors,
        "passed": not errors and (
            not stability_blocking or all(check["passed"] for check in checks.values())
        ),
        "errors": errors,
    }


def revalidate_control(
    root: Path, output: Path, index: int, manifest: dict[str, Any],
    engine: OrchestratorEngine, client: MCPToolClient, sources: dict[str, str],
) -> dict[str, Any]:
    directory = root / "calibrations" / f"no-fault-{index:02d}"
    original = _read(directory / "report.json", sources)
    if (
        original.get("kind") != "load_only_rehearsal"
        or original.get("injections_submitted") != 0
        or not original.get("ended_at")
        or original.get("load_stopped", {}).get("stopped") is not True
        or original.get("no_fault_window_seconds") != 600
        or original.get("measurement_fault") != "network-latency"
    ):
        raise ValueError(f"control {index} is not a completed, cleaned no-fault control")
    stability_policy = manifest.get("stability_policy")
    if stability_policy is not None and (
        not isinstance(stability_policy, dict)
        or SUPPORTED_SUPPLEMENTAL_STABILITY_POLICIES.get(
            stability_policy.get("version"),
        ) != stability_policy
    ):
        raise ValueError("unsupported supplemental stability policy")
    default_relative_limit = (
        stability_policy["default_relative_limit"]
        if stability_policy else DEFAULT_STABILITY_RELATIVE_LIMIT
    )
    relative_limit_overrides = (
        stability_policy["relative_limit_overrides"] if stability_policy else {}
    )
    expected_report_policy = {
        "default_relative_limit": default_relative_limit,
        "relative_limit_overrides": relative_limit_overrides,
    }
    if stability_policy is not None and original.get("stability_policy") != expected_report_policy:
        raise ValueError("control stability policy does not match campaign manifest")
    acceptance_policy = manifest.get("control_acceptance_policy")
    if acceptance_policy is not None and (
        not isinstance(acceptance_policy, dict)
        or acceptance_policy != control_acceptance_policy(
            acceptance_policy.get("long_window_stability"),
        )
    ):
        raise ValueError("unsupported control acceptance policy")
    original_acceptance = original.get("control_acceptance_policy")
    if (
        acceptance_policy is not None and original_acceptance != acceptance_policy
        or acceptance_policy is None
        and original_acceptance not in (None, control_acceptance_policy())
    ):
        raise ValueError("control acceptance policy does not match campaign manifest")
    diagnostic_control = (
        acceptance_policy is not None
        and acceptance_policy["long_window_stability"] == "diagnostic"
    )
    report = copy.deepcopy(original)
    report["warnings"] = []
    report["measurement_policy"] = dict(CONTROLLED_MEASUREMENT_POLICY)
    report["measurement_policy"]["cpu_elapsed_clock"] = "legacy_wall_clock_not_reconstructable"
    report["limitations"].append(
        "Retrospective historical requery, not a new control; legacy CPU clock anomalies "
        "invalidate affected windows. Original reports and raw evidence are unchanged."
    )
    report["revalidation"] = {
        "source_report": str(directory / "report.json"),
        "source_report_sha256": sources[str(directory / "report.json")],
        "requeried_at": datetime.now(timezone.utc).isoformat(),
        "mcp_sse_url": client.sse_url,
        "thresholds_unchanged": True, "new_loads": 0, "new_injections": 0,
    }
    service_results: dict[str, dict[str, Any]] = {}
    for service in original["services"]:
        artifact_dir = directory / "artifacts" / f"{directory.name}-{service}"
        controlled = _read(artifact_dir / "controlled_report.json", sources)
        spec = {
            "controlled_campaign": {**manifest["policy"], "service": service,
                                    "fault": original["measurement_fault"]},
            "scope": {"namespace": "sock-shop"},
            "experiment": {"id": "historical-control-revalidation"},
            "timeline": controlled["planned"],
        }
        run = ControlledRun(engine, f"revalidation-{index}-{service}", spec)
        run.target = controlled["target_before"]
        queries = {key: value for key, value in run.queries().items()
                   if key not in CGROUP_RESOURCE_KEYS}
        service_results[service] = {}
        destination = output / f"no-fault-{index:02d}" / service
        destination.mkdir(parents=True, exist_ok=False)
        for path in sorted(artifact_dir.glob("controlled_metrics_*.json")):
            raw = _read(path, sources)
            phase = path.stem.split("_", 3)[3]
            start, end = raw["window_start"], raw["window_end"]
            network, query = query_window(client, queries, start, end)
            corrected = {**raw, "data": {**raw["data"], **network}}
            _write(destination / path.name, {"corrected": corrected, "query": query,
                                             "source_sha256": sources[str(path)]})
            seconds = (
                original["preparation_policy"]["stabilization_window_seconds"]
                if phase.startswith("stabilization_")
                else original["baseline"]["duration_seconds"]
                if phase == "baseline" else 600
            )
            evaluated = evaluate_window(
                run, corrected, seconds, check_stability=phase != "baseline",
                default_relative_limit=default_relative_limit,
                relative_limit_overrides=relative_limit_overrides.get(service),
                stability_blocking=not (diagnostic_control and phase == "no_fault_control"),
            )
            if phase == "baseline":
                try:
                    resource_environment("memory-hog", run.target["limits"], evaluated["summary"], {})
                    baseline = evaluated["summary"]
                    if baseline["memory_usage_bytes"]["max"] >= .85 * baseline["memory_limit_bytes"]["mean"]:
                        raise GateBlocked("baseline memory already >=85% of application limit")
                except GateBlocked as exc:
                    evaluated["passed"] = False
                    evaluated["errors"].append(str(exc))
                evaluated["passed"] &= original["services"][service].get("passed") is True
            service_results[service][phase] = evaluated
        expected = {
            *(f"stabilization_{w['attempt']}" for w in report["preparation"]["stabilization_windows"]),
            "baseline", "no_fault_control",
        }
        if set(service_results[service]) != expected:
            raise ValueError(f"missing or unexpected metric windows for control {index}/{service}")
    consecutive = 0
    reached = False
    for window in report["preparation"]["stabilization_windows"]:
        phase = f"stabilization_{window['attempt']}"
        window["services"] = {s: service_results[s][phase] for s in service_results}
        window["passed"] = all(result["passed"] for result in window["services"].values())
        consecutive = consecutive + 1 if window["passed"] else 0
        window["consecutive_stable_windows"] = consecutive
        reached |= consecutive >= report["preparation_policy"]["stable_windows_required"]
    report["stabilization_passed"] = reached
    report["preparation"].update(stabilization_passed=reached,
                                  consecutive_stable_windows=consecutive, timed_out=not reached)
    report["baseline"]["eligible"] = reached
    for service, phases in service_results.items():
        report["services"][service].update({
            "stabilization": [
                {"attempt": w["attempt"], **phases[f"stabilization_{w['attempt']}"]}
                for w in report["preparation"]["stabilization_windows"]
            ],
            "baseline": phases["baseline"]["summary"],
            "passed": phases["baseline"]["passed"],
            "baseline_errors": phases["baseline"]["errors"],
            "control_window_passed": phases["no_fault_control"]["passed"],
        })
        control = phases["no_fault_control"]
        report["no_fault_window"]["services"][service] = {
            "summary": control["summary"], "stability": control["metrics"],
            "stability_passed": control["stability_passed"],
            "passed": control["passed"], "errors": control["errors"],
        }
        if diagnostic_control and not control["stability_passed"] and control["passed"]:
            report["warnings"].append({
                "service": service,
                "phase": "no_fault_control",
                "reason": "long-window variability exceeded the diagnostic limits",
                "metrics": [key for key, check in control["metrics"].items() if not check["passed"]],
            })
    report["no_fault_window"]["stability_passed"] = all(
        phase["no_fault_control"]["stability_passed"] for phase in service_results.values()
    )
    health = [
        *report["preparation"]["health_checks"],
        *report["no_fault_window"]["health_checks"],
    ]
    report["passed"] = bool(
        reached and health and all(item["healthy"] for item in health)
        and report["no_fault_window"]["observed_seconds"] >= 600
        and all(phases["baseline"]["passed"] and phases["no_fault_control"]["passed"]
                for phases in service_results.values())
        and not report.get("error")
    )
    invalid_timing = any(
        phase["summary"]["cpu_cores"]["status"] == "invalid"
        for phases in service_results.values() for phase in phases.values()
    )
    report["verification_status"] = (
        "invalid_telemetry" if invalid_timing else "passed" if report["passed"] else "failed"
    )
    path = output / f"no-fault-{index:02d}" / "report.json"
    _write(path, report)
    return {"index": index, "passed": report["passed"], "report": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "stabilization_passed": reached, "verification_status": report["verification_status"]}


def revalidate(root: Path, mcp_sse_url: str) -> Path:
    sources: dict[str, str] = {}
    state = _read(root / "campaign_state.json", sources)
    if state.get("status") != "blocked":
        raise ValueError("revalidation requires a blocked, inactive campaign")
    manifest = _read(root / "campaign_manifest.json", sources)
    output = root / "revalidations" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "original_campaign_state.json", state)
    client = MCPToolClient(mcp_sse_url, timeout_seconds=60)
    engine = OrchestratorEngine(
        storage=SqliteStorage(output / "observations.db", output / "artifacts"),
        chaos=LitmusChaosPlugin(), load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(), traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=Path(__file__).resolve().parents[2] / "platform/data/KILL_SWITCH",
    )
    results = [
        revalidate_control(root, output, index, manifest, engine, client, sources)
        for index in (1, 2)
    ]
    for filename, digest in sources.items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"source changed during revalidation: {filename}")
    summary = {
        "measurement_policy": CONTROLLED_MEASUREMENT_POLICY,
        "controls": results, "sources_sha256": sources,
        "passed": all(item["passed"] for item in results),
        "new_loads": 0, "new_injections": 0,
        "status": "review_required" if all(item["passed"] for item in results) else "blocked",
    }
    _write(output / "report.json", summary)
    _write(root / "campaign_state.json", {
        **state, "status": "blocked",
        "reason": (
            "retrospective controls require review and fresh preflight"
            if summary["passed"] else "revalidated no-fault control failed or has invalid telemetry"
        ),
        "verification_revalidation": {
            "report": str(output / "report.json"),
            "sha256": hashlib.sha256((output / "report.json").read_bytes()).hexdigest(),
            "passed": summary["passed"], "controls": results,
        },
    })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--mcp-sse-url", default="http://127.0.0.1:18082/sse")
    args = parser.parse_args()
    root = args.campaign.resolve()
    with (root.parent / ".supplemental.lock").open("a") as parent_lock, (
        root / ".supplemental.lock"
    ).open("a") as campaign_lock:
        for lock in (parent_lock, campaign_lock):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(revalidate(root, args.mcp_sse_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
