"""Collect a second, frozen 35-cell test cohort after a successful evaluation."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import runner
from experiment.eval.memory_hog import supplemental_campaign as supplemental
from experiment.platform.backend.controlled_experiment import (
    FAULTS,
    SERVICES,
    SUPPLEMENTAL_PREPARATION_POLICY,
    build_time_budget,
)
from experiment.platform.backend.rca_knowledge import digest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
VERSION = "test-replication-35-v1"
SEED = 20261110
ACTIVE = {"pending", "running", "pending_approval", "queued", "starting"}
FAILURES = (RuntimeError, ValueError, OSError, KeyError, subprocess.SubprocessError)
write_json = supplemental._write_json


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def event(root: Path, name: str, **fields: Any) -> None:
    campaign.append_ledger(root / "campaign_events.jsonl", {
        "event": name, "at": datetime.now(timezone.utc).isoformat(), **fields,
    })


def state(root: Path, **fields: Any) -> dict[str, Any]:
    path = root / "campaign_state.json"
    value = read_json(path) if path.exists() else {}
    value.update(fields)
    write_json(path, value)
    print(json.dumps(fields, ensure_ascii=False), flush=True)
    return value


def process_identity(pid: int) -> dict[str, Any] | None:
    directory = Path("/proc") / str(pid)
    try:
        command = (directory / "cmdline").read_bytes().decode().split("\0")
        stat = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": stat[19], "command": command[:-1]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def evaluation_processes() -> list[int]:
    matches = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            if path.stat().st_uid != os.getuid():
                continue
            identity = process_identity(int(path.name))
            if (
                identity
                and "experiment.eval.memory_hog.test_base_rag_evaluation"
                in identity["command"]
                and (path / "cwd").resolve() == ROOT
            ):
                matches.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return matches


def validate_matrix(cohort: dict[str, Any]) -> None:
    runs = cohort["runs"]
    expected = {(service, fault) for service in SERVICES for fault in FAULTS}
    actual = {(row["cell"]["service"], row["cell"]["chaos_type"]) for row in runs}
    ids = {row["run_id"] for row in runs}
    if (
        len(runs) != 35 or len(ids) != 35 or actual != expected
        or any(row["status"] != "completed" for row in runs)
        or ids.intersection(cohort["training_run_ids"])
    ):
        raise ValueError("reference must contain 35 unique, completed, held-out 7x5 tests")


def collection_config(
    source: dict[str, Any], directory: Path, history_id: str,
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    config = campaign.build_config(
        source, directory, "medium", SEED,
        source["controlled_campaign"]["calibrations"],
    )
    config.pop("knowledge_campaign", None)
    config["train_experiments"] = []
    config["test_experiments"] = cells
    config["historical_observability"]["campaign_id"] = history_id
    policy = config["controlled_campaign"]
    policy["level"] = VERSION
    policy["preparation_policy"] = dict(SUPPLEMENTAL_PREPARATION_POLICY)
    policy["recovery_timeout_by_service"] = {"orders": 600}
    policy["resilience"].pop("pre_intervention_retry", None)
    policy["time_budget"] = build_time_budget(policy)
    config["platform"]["run_timeout_seconds"] = policy["time_budget"]["poll_seconds"]
    config["timeline"].update({
        "warmup_seconds": 180, "baseline_seconds": 180,
    })
    return config


def prepare(
    evaluation: Path, root: Path, *, supersedes_campaign: Path | None = None,
) -> None:
    evaluation = evaluation.resolve()
    cohort = read_json(evaluation / "test_cohort.json")
    validate_matrix(cohort)
    source = yaml.safe_load((evaluation / "config.yaml").read_text())
    cells = [
        {**cell, "phase": "test", "replica_idx": 2,
         "cell_id": f"{cell['service']}-{cell['chaos_type']}-r2"}
        for cell in campaign.build_plan("medium", SEED)
    ]
    config = collection_config(source, root, root.name, cells)
    pins = {
        str(evaluation / filename): supplemental._sha256(evaluation / filename)
        for filename in (
            "config.yaml", "test_cohort.json", "rag_base.json",
            "evaluation_manifest.json", "precollection_manifest.json",
        )
    }
    for module in (
        sys.modules[__name__], campaign, supplemental,
        campaign.manifests, supplemental.load_check,
    ):
        path = Path(module.__file__).resolve()
        pins[str(path)] = supplemental._sha256(path)
    memory_template = campaign.manifests.CHAOS_MANIFEST_DIR / "catalogue-memory-hog.yaml"
    pins[str(memory_template)] = supplemental._sha256(memory_template)
    api_processes = []
    for path in Path("/proc").glob("[0-9]*"):
        try:
            if path.stat().st_uid != os.getuid():
                continue
            identity = process_identity(int(path.name))
            if (
                identity
                and "experiment.platform.backend.main:app" in identity["command"]
                and (path / "cwd").resolve() == ROOT
            ):
                api_processes.append(identity)
        except (FileNotFoundError, ProcessLookupError):
            continue
    if len(api_processes) != 1:
        raise RuntimeError("exactly one identifiable local platform API is required")
    supersession = None
    if supersedes_campaign is not None:
        previous = supersedes_campaign.resolve()
        previous_state_path = previous / "campaign_state.json"
        previous_report_path = previous / "collection_report.json"
        if not previous_state_path.is_file() or not previous_report_path.is_file():
            raise RuntimeError("superseded campaign lacks its terminal audit records")
        previous_state = read_json(previous_state_path)
        previous_report = read_json(previous_report_path)
        if (
            previous_state.get("status") != "blocked"
            or previous_state.get("new_count") != 0
            or previous_report.get("attempt_count") != 0
            or previous_report.get("new_count") != 0
            or previous_report.get("combined_count") != 35
        ):
            raise RuntimeError(
                "only a blocked, zero-attempt campaign may be superseded automatically"
            )
        if evaluation_processes():
            raise RuntimeError("evaluation remains active; cannot create the successor campaign")
        active = [row for row in api_for(config).get("/api/runs") if row["status"] in ACTIVE]
        if active:
            raise RuntimeError(
                f"active platform runs prevent successor campaign preparation: {active}"
            )
        supersession = {
            "previous_campaign": str(previous),
            "previous_state_sha256": supplemental._sha256(previous_state_path),
            "previous_report_sha256": supplemental._sha256(previous_report_path),
            "reason": "previous read-only clock readiness failed; no cases or controls started",
            "authorization_source": (
                "explicit user authorization to create a new campaign after a new "
                "600-second read-only clock preflight"
            ),
            "retry_gate": (
                "new 600-second clock preflight must pass; failure stops before controls"
            ),
        }
    manifest = {
        "version": VERSION, "campaign_id": root.name,
        "evaluation": str(evaluation), "pins": pins,
        "backend": campaign.controlled_backend_capabilities(),
        "api_process": api_processes[0],
        "seed": SEED, "max_attempts_per_cell": 2,
        "max_hours_after_release": 24, "max_service_restarts": 3,
        "control_acceptance_policy": supplemental.CONTROL_ACCEPTANCE_POLICY,
        "stability_policy": supplemental.SUPPLEMENTAL_STABILITY_POLICY,
        "authorizations": {
            "user_authorized_supersession": supersession,
            "automatic_control_review": True,
            "technical_reinjection_after_cleanup": True,
            "idle_api_restart": True,
            "target_service_restart_after_cleanup": True,
            "repair_registered_memory_cleanup_from_reviewed_source": True,
            "automatic_diagnostics": False,
        },
        "protocol_note": (
            "Prospective replication block, not identical-protocol replication: "
            "measurement-v2, 180s warmup/baseline, two stable 60s windows, "
            "orders recovery 600s; controls 25% diagnostic, cases 20% blocking."
        ),
    }
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "plan.json", cells)
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    write_json(root / "original_cohort.json", cohort)
    manifest["local_pins"] = {
        name: supplemental._sha256(root / name)
        for name in ("config.yaml", "plan.json", "original_cohort.json")
    }
    manifest["sha256"] = digest(manifest)
    write_json(root / "manifest.json", manifest)
    state(root, status="prepared", planned_cells=35, diagnostics_enabled=False)


def verify_pins(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "manifest.json")
    payload = {key: value for key, value in manifest.items() if key != "sha256"}
    if manifest["sha256"] != digest(payload) or manifest["version"] != VERSION:
        raise RuntimeError("campaign manifest integrity/version mismatch")
    for name, expected in manifest["pins"].items():
        if supplemental._sha256(Path(name)) != expected:
            raise RuntimeError(f"pinned input/code changed: {name}")
    for name, expected in manifest["local_pins"].items():
        if supplemental._sha256(root / name) != expected:
            raise RuntimeError(f"local campaign input changed: {name}")
    if manifest["backend"] != campaign.controlled_backend_capabilities():
        raise RuntimeError("backend implementation changed since campaign preparation")
    return manifest


def evaluation_succeeded(directory: Path) -> bool:
    if evaluation_processes():
        return False
    required = (
        "diagnostics_complete.json", "test_base_rag_evaluation.json",
    )
    if not all((directory / name).is_file() for name in required):
        raise RuntimeError("evaluation exited without a complete persisted report")
    from experiment.eval.memory_hog.test_base_rag_evaluation import build_report

    report = read_json(directory / "test_base_rag_evaluation.json")
    rebuilt = build_report(directory)
    if report != rebuilt:
        raise RuntimeError("persisted evaluation report differs from validated diagnostics")
    if (
        report["evaluation_id"] != directory.name
        or report["provenance"]["test_run_count"] != 35
        or not report["diagnosis_summary"]
        or any(value["errors"] != 0 for value in report["diagnosis_summary"].values())
    ):
        raise RuntimeError("evaluation report contains errors or unexpected cohort")
    return True


def api_for(config: dict[str, Any]) -> runner.ApiClient:
    return runner.ApiClient(
        config["platform"]["api_base"], role=config["platform"]["user_role"],
    )


def require_idle(config: dict[str, Any]) -> None:
    if evaluation_processes():
        raise RuntimeError("diagnostic evaluation still active; no operational changes allowed")
    active = [row for row in api_for(config).get("/api/runs") if row["status"] in ACTIVE]
    if active:
        raise RuntimeError(f"active platform runs prevent operation: {[r['run_id'] for r in active]}")


def remaining(root: Path) -> float:
    value = read_json(root / "campaign_state.json")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if value["boot_id"] != boot:
        raise RuntimeError("host rebooted after release; manual review required")
    return value["deadline_monotonic"] - time.monotonic()


def budget(root: Path, seconds: float) -> None:
    if remaining(root) < seconds:
        raise TimeoutError("24-hour budget cannot safely cover the next complete stage")


def ensure_connections(root: Path, children: list[subprocess.Popen]) -> None:
    for port, namespace, service, remote in (
        (18080, "mcp-server", "mcp-observability", 8000),
        (18082, "monitoring-history", "history-observability-mcp", 8000),
        (9091, "monitoring", "prometheus-v3", 9090),
    ):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                continue
        except OSError as exc:
            event(root, "connection_unavailable", port=port, error=str(exc))
        with (root / f"port-forward-{port}.log").open("a") as log:
            child = subprocess.Popen(
                ["kubectl", "-n", namespace, "port-forward", "--address", "127.0.0.1",
                 f"svc/{service}", f"{port}:{remote}"],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        children.append(child)
        deadline = time.monotonic() + 30
        while True:
            if child.poll() is not None:
                raise RuntimeError(f"owned port-forward for {port} exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"owned port-forward for {port} did not become ready")
                time.sleep(1)
        event(root, "owned_port_forward_ready", port=port, pid=child.pid)


def ensure_backend(root: Path, config: dict[str, Any], manifest: dict[str, Any]) -> None:
    require_idle(config)
    try:
        campaign.require_controlled_backend(api_for(config))
        return
    except RuntimeError as exc:
        event(root, "backend_restart_required", error=str(exc))
    old = manifest["api_process"]
    if process_identity(old["pid"]) != old:
        raise RuntimeError("API process identity changed; refusing to kill an unowned process")
    command = old["command"]
    if not (
        command[1:4] == ["-m", "uvicorn", "experiment.platform.backend.main:app"]
        and "--port" in command and command[command.index("--port") + 1] == "8010"
    ):
        raise RuntimeError("API command is not the expected local uvicorn instance")
    os.kill(old["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 30
    while process_identity(old["pid"]) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError("idle API did not stop gracefully; no forced kill")
        time.sleep(1)
    with (root / "api-restart.log").open("a") as log:
        child = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    event(root, "idle_api_restarted", old_pid=old["pid"], pid=child.pid)
    deadline = time.monotonic() + 60
    while True:
        if child.poll() is not None:
            raise RuntimeError("replacement API exited; inspect api-restart.log")
        try:
            campaign.require_controlled_backend(api_for(config))
            return
        except (RuntimeError, OSError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("replacement API did not become ready") from exc
            time.sleep(2)


def clean_for_continuation(entry: dict[str, Any]) -> bool:
    gates = entry.get("gates") or {}
    return bool(
        entry.get("status") in {"completed", "blocked", "failed", "aborted"}
        and type(gates.get("submitted")) is bool
        and (gates.get("cleanup") or {}).get("passed") is True
        and (gates.get("load_stopped") or {}).get("stopped") is True
    )


def memory_cleanup_patch(
    workflow: dict[str, Any], service: str,
) -> list[dict[str, Any]]:
    if workflow.get("status", {}).get("phase") not in {"Succeeded", "Failed", "Error"}:
        raise RuntimeError("registered memory workflow is not terminal; do not mutate it")
    if workflow["metadata"]["name"] != f"{service}-memory-hog":
        raise RuntimeError("registered memory workflow identity mismatch")
    expected = yaml.safe_load(campaign.manifests._render_memory_hog(service, {}))

    def engine_artifact(document: dict[str, Any]) -> tuple[int, int, str, dict[str, Any]]:
        found = []
        for i, template in enumerate(document["spec"]["templates"]):
            for j, artifact in enumerate(template.get("inputs", {}).get("artifacts", [])):
                raw = artifact.get("raw", {}).get("data", "")
                if "kind: ChaosEngine" in raw:
                    found.append((i, j, raw, yaml.safe_load(raw)))
        if len(found) != 1:
            raise RuntimeError("memory template must contain exactly one ChaosEngine artifact")
        return found[0]

    def cleanup_env(engine: dict[str, Any]) -> dict[str, Any]:
        values = [
            item for experiment in engine["spec"]["experiments"]
            for item in experiment["spec"]["components"]["env"]
            if item["name"] == "CHAOS_KILL_COMMAND"
        ]
        if len(values) != 1:
            raise RuntimeError("memory template must contain exactly one cleanup command")
        return values[0]

    _, _, _, expected_engine = engine_artifact(expected)
    i, j, raw, engine = engine_artifact(workflow)
    command = cleanup_env(expected_engine)["value"]
    if cleanup_env(engine)["value"] == command:
        campaign.validate_workflow_template(workflow, "sock-shop", service, "memory-hog")
        return []
    cleanup_env(engine)["value"] = command
    replacement = yaml.safe_dump(engine, sort_keys=False)
    updated = copy.deepcopy(workflow)
    updated["spec"]["templates"][i]["inputs"]["artifacts"][j]["raw"]["data"] = replacement
    campaign.validate_workflow_template(updated, "sock-shop", service, "memory-hog")
    path = f"/spec/templates/{i}/inputs/artifacts/{j}/raw/data"
    return [
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": workflow["metadata"]["resourceVersion"]},
        {"op": "test", "path": path, "value": raw},
        {"op": "replace", "path": path, "value": replacement},
    ]


def repair_memory_templates(root: Path, config: dict[str, Any]) -> None:
    require_idle(config)
    workflows = campaign.kubectl(
        "-n", "litmus", "get", "workflows.argoproj.io", "-o", "json",
    )["items"]
    if any(w.get("status", {}).get("phase") in {"Pending", "Running"} for w in workflows):
        raise RuntimeError("active Argo workflows prevent registered-template repair")
    by_name = {w["metadata"]["name"]: w for w in workflows}
    for service in SERVICES:
        name = f"{service}-memory-hog"
        if name not in by_name:
            raise RuntimeError(f"registered workflow missing: {name}; no automatic priming")
        workflow = by_name[name]
        patch = memory_cleanup_patch(workflow, service)
        if not patch:
            continue
        directory = root / "template_repairs" / name
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / f"before-{time.time_ns()}.json", workflow)
        write_json(directory / f"patch-{time.time_ns()}.json", patch)
        require_idle(config)
        event(root, "memory_cleanup_template_repair_requested", workflow=name,
              resource_version=workflow["metadata"]["resourceVersion"])
        updated = campaign.kubectl(
            "-n", "litmus", "patch", "workflows.argoproj.io", name,
            "--type=json", "-p", json.dumps(patch), "-o", "json",
        )
        campaign.validate_workflow_template(updated, "sock-shop", service, "memory-hog")
        if updated.get("status", {}).get("phase") != workflow["status"]["phase"]:
            raise RuntimeError("registered workflow phase changed during cleanup-only repair")
        write_json(directory / f"after-{time.time_ns()}.json", updated)
        event(root, "memory_cleanup_template_repaired", workflow=name,
              new_workflow_submitted=False)


def technical_retry(entry: dict[str, Any]) -> bool:
    gates = entry.get("gates") or {}
    return bool(
        clean_for_continuation(entry)
        and (
            gates["submitted"] is False
            or (gates.get("cgroup_telemetry") or {}).get("passed") is False
            or (gates.get("partial_telemetry") or {}).get("passed") is False
            or (
                (gates.get("mechanism_observed") or {}).get("status") == "not_confirmed"
                and (gates.get("mechanism_observed") or {}).get("spec_matches_submitted")
                is False
            )
        )
    )


def recover_service(
    root: Path, config: dict[str, Any], cell: dict[str, Any], entry: dict[str, Any],
) -> None:
    if not clean_for_continuation(entry):
        raise RuntimeError("cleanup/load-stop unproven; no restart or next injection")
    require_idle(config)
    gates = entry["gates"]
    recovered = (
        (gates.get("recovery") or {}).get("passed") is True
        or campaign.operationally_recovered(config, entry)
        or gates["submitted"] is False
    )
    if not recovered:
        events = campaign.read_ledger(root / "campaign_events.jsonl")
        restarts = [row for row in events if row["event"] == "service_restart_requested"]
        service = cell["service"]
        if len(restarts) >= 3 or any(row["service"] == service for row in restarts):
            raise RuntimeError("service restart budget exhausted")
        budget(root, 900)
        event(root, "service_restart_requested", service=service, run_id=entry["run_id"],
              original_status_preserved=True)
        namespace = config["cluster"]["app_namespace"]
        for args in (
            ("rollout", "restart", f"deployment/{service}"),
            ("rollout", "status", f"deployment/{service}", "--timeout=300s"),
        ):
            result = runner._kubectl("-n", namespace, *args, timeout=330)
            if result.returncode:
                raise RuntimeError(f"target service recovery failed: {result.stderr}")
        event(root, "service_restart_completed", service=service)
    report = campaign.environment_preflight(config)
    write_json(root / "latest_recovery_environment.json", report)
    if report["passed"] is not True:
        raise RuntimeError("environment not healthy/clean after terminal attempt")
    time.sleep(120)
    require_idle(config)
    report = campaign.environment_preflight(config)
    write_json(root / "latest_recovery_environment_stable.json", report)
    if report["passed"] is not True:
        raise RuntimeError("environment failed the second idle recovery check")


def attempt_config(root: Path, cell: dict[str, Any], number: int) -> Path:
    directory = root / "cases" / cell["cell_id"] / (
        f"{root.name}-{cell['cell_id']}-a{number}"
    )
    source = yaml.safe_load((root / "config.yaml").read_text())
    config = collection_config(source, directory, root.name, [cell])
    if directory.exists():
        if (
            yaml.safe_load((directory / "config.yaml").read_text()) != config
            or read_json(directory / "plan.json") != [cell]
        ):
            raise RuntimeError("saved attempt configuration changed")
    else:
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        write_json(directory / "plan.json", [cell])
    return directory


def collect_cell(root: Path, cell: dict[str, Any]) -> None:
    results = campaign.read_ledger(root / "attempts.jsonl")
    prior = [row for row in results if row["cell"]["cell_id"] == cell["cell_id"]]
    if (
        any(row["accepted"] for row in prior)
        or (prior and not prior[-1]["retry_eligible"])
        or len(prior) >= 2
    ):
        return
    for number in range(len(prior) + 1, 3):
        verify_pins(root)
        source = yaml.safe_load((root / "config.yaml").read_text())
        supplemental._wait_for_history_visibility(source, root, (cell["service"],))
        directory = attempt_config(root, cell, number)
        config = yaml.safe_load((directory / "config.yaml").read_text())
        budget(root, config["platform"]["run_timeout_seconds"] + 1200)
        state(root, status="collecting", cell_id=cell["cell_id"], attempt=number)
        execution_error = None
        try:
            campaign.execute(directory, resume=(directory / "ledger.jsonl").exists())
        except FAILURES as exc:
            execution_error = f"{type(exc).__name__}: {exc}"
            event(root, "attempt_executor_error", cell_id=cell["cell_id"],
                  attempt=number, error=execution_error)
        entry = supplemental._latest_entry(directory, cell["cell_id"])
        if not entry or entry["status"] not in {"completed", "blocked", "failed", "aborted"}:
            raise RuntimeError("attempt outcome ambiguous; resume same attempt, never reinject")
        if entry["status"] == "completed":
            try:
                entry = campaign.ensure_train_history_validation(
                    directory, config, cell, entry, api_for(config),
                )
            except FAILURES as exc:
                event(root, "historical_validation_failed", cell_id=cell["cell_id"],
                      attempt=number, error=f"{type(exc).__name__}: {exc}")
                entry = supplemental._latest_entry(directory, cell["cell_id"])
        accepted = campaign.train_cell_gate_passed(entry)
        recover_service(root, config, cell, entry)
        campaign.append_ledger(root / "attempts.jsonl", {
            "cell": cell, "attempt": number, "directory": str(directory),
            "entry": entry, "accepted": accepted, "execution_error": execution_error,
            "retry_eligible": technical_retry(entry),
        })
        publish(root)
        if accepted or number == 2 or not technical_retry(entry):
            return
        event(root, "technical_retry_authorized", cell_id=cell["cell_id"],
              previous_run_id=entry.get("run_id"), next_attempt=number + 1)
        time.sleep(60)


def publish(root: Path) -> dict[str, Any]:
    original = read_json(root / "original_cohort.json")
    validate_matrix(original)
    attempts = campaign.read_ledger(root / "attempts.jsonl")
    accepted = [row for row in attempts if row["accepted"]]
    if any(not campaign.train_cell_gate_passed(row["entry"]) for row in accepted):
        raise RuntimeError("an accepted attempt no longer satisfies evidence/effect/recovery gates")
    cells = [row["cell"]["cell_id"] for row in accepted]
    new = [
        {"cell": row["cell"], "run_id": row["entry"]["run_id"], "status": "completed",
         "attempt": row["attempt"], "attempt_directory": row["directory"]}
        for row in accepted
    ]
    old_ids = {row["run_id"] for row in original["runs"]}
    new_ids = {row["run_id"] for row in new}
    training_ids = set(original["training_run_ids"])
    if (
        len(cells) != len(set(cells)) or len(new_ids) != len(new)
        or old_ids & new_ids or (old_ids | new_ids) & training_ids
    ):
        raise RuntimeError("cohort overlap/duplicate detected; publication refused")
    pairs = {
        (row["cell"]["service"], row["cell"]["chaos_type"]): row
        for row in original["runs"]
    }
    metadata = {
        "version": VERSION, "campaign_id": root.name,
        "manifest_sha256": read_json(root / "manifest.json")["sha256"],
        "training_run_ids": sorted(training_ids),
        "diagnostics_executed": False, "complete": len(new) == 35,
    }
    for name, runs in (
        ("cohort_original_35.json", original["runs"]),
        ("cohort_new_35.json", new),
        ("cohort_combined_70.json", original["runs"] + new),
    ):
        payload = {**metadata, "runs": runs, "run_count": len(runs)}
        payload["sha256"] = digest(payload)
        write_json(root / name, payload)
    summary = {
        **metadata, "original_count": 35, "new_count": len(new),
        "combined_count": 35 + len(new), "attempt_count": len(attempts),
        "invalid_attempt_count": sum(not row["accepted"] for row in attempts),
        "pairs": [
            {"service": row["cell"]["service"], "fault": row["cell"]["chaos_type"],
             "original_run_id": pairs[(row["cell"]["service"], row["cell"]["chaos_type"])]["run_id"],
             "new_run_id": row["run_id"]}
            for row in new
        ],
        "limitations": [
            "Two temporal blocks, not 70 IID experiments or a blind benchmark.",
            "Original cohort mixes collection protocols; compare paired cells and report block effects.",
            "All attempts remain in attempts.jsonl; report failures/retries alongside accepted-only analyses.",
            "No new test run may enter training or change the frozen RAG index.",
        ],
    }
    write_json(root / "collection_report.json", summary)
    return summary


def stop_owned_active(root: Path, config: dict[str, Any]) -> None:
    api = api_for(config)
    owned = [
        row for row in api.get("/api/runs")
        if row["experiment_id"].startswith(root.name + "-")
    ]
    for row in owned:
        run_id = row["run_id"]
        if row["status"] in ACTIVE:
            event(root, "owned_run_stop_requested", run_id=run_id)
            api.post(f"/api/runs/{run_id}/stop", body={
                "requested_by": VERSION, "reason": "campaign supervisor stopping safely",
            })
        deadline = time.monotonic() + 600
        while True:
            result = api.get(f"/api/runs/{run_id}")
            gates = (result.get("summary") or {}).get("controlled_gates") or {}
            if (
                result["status"] not in ACTIVE
                and (gates.get("cleanup") or {}).get("passed") is True
                and (gates.get("load_stopped") or {}).get("stopped") is True
            ):
                event(root, "owned_run_stopped_and_cleaned", run_id=run_id)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("owned run cleanup unproven; retain historical dual-write")
            time.sleep(5)


def run(root: Path) -> int:
    manifest = verify_pins(root)
    config = yaml.safe_load((root / "config.yaml").read_text())
    value = read_json(root / "campaign_state.json")
    if value["status"] in {"completed", "partial", "blocked"}:
        raise RuntimeError("terminal campaign: inspect artifacts; no automatic restart")
    children: list[subprocess.Popen] = []
    enabled = False
    try:
        if "deadline_monotonic" not in value:
            state(root, status="waiting_for_evaluation", pid=os.getpid())
            while not evaluation_succeeded(Path(manifest["evaluation"])):
                verify_pins(root)
                time.sleep(30)
            verify_pins(root)
            state(root, status="released", released_at=datetime.now(timezone.utc).isoformat(),
                  boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  deadline_monotonic=time.monotonic() + 24 * 3600)
            event(root, "evaluation_success_verified", evaluation=manifest["evaluation"])
        budget(root, 3600)
        ensure_connections(root, children)
        ensure_backend(root, config, manifest)
        repair_memory_templates(root, config)
        readiness = supplemental.readiness_preflight(config)
        write_json(root / f"readiness-{time.time_ns()}.json", readiness)
        if readiness["passed"] is not True:
            raise RuntimeError("readiness failed before load or injection")
        # Set the flag first: a partial enable must also be cleaned up on error.
        enabled = True
        supplemental._set_campaign_dual_write(root, True)
        visibility = supplemental._wait_for_history_visibility(config, root, SERVICES)
        write_json(root / f"history-visibility-{time.time_ns()}.json", visibility)
        state(root, status="controls")
        budget(root, 6000)
        if not supplemental._run_load_controls(root, config, config):
            raise RuntimeError("fresh no-fault controls did not pass the authorized policy")
        event(root, "controls_automatically_reviewed",
              policy=manifest["control_acceptance_policy"], warnings_preserved=True)
        for cell in read_json(root / "plan.json"):
            ensure_connections(root, children)
            require_idle(config)
            collect_cell(root, cell)
        report = publish(root)
        state(root, status="completed" if report["complete"] else "partial",
              new_count=report["new_count"], combined_count=report["combined_count"])
        return 0 if report["complete"] else 2
    except FAILURES as exc:
        event(root, "campaign_blocked", error=f"{type(exc).__name__}: {exc}")
        report = publish(root)
        state(root, status="partial" if isinstance(exc, TimeoutError) else "blocked",
              reason=f"{type(exc).__name__}: {exc}", new_count=report["new_count"],
              combined_count=report["combined_count"])
        return 2
    finally:
        try:
            if enabled:
                try:
                    stop_owned_active(root, config)
                    supplemental._set_campaign_dual_write(root, False)
                except FAILURES as exc:
                    event(root, "dual_write_cleanup_failed", error=f"{type(exc).__name__}: {exc}")
                    state(root, status="blocked", reason="dual-write cleanup failed; manual review")
                    raise
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--prepare", type=Path, metavar="EVALUATION_DIRECTORY")
    operation.add_argument("--run", type=Path, metavar="CAMPAIGN_DIRECTORY")
    operation.add_argument("--check", type=Path, metavar="CAMPAIGN_DIRECTORY")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--supersedes", type=Path, metavar="BLOCKED_CAMPAIGN_DIRECTORY")
    args = parser.parse_args(argv)
    if args.prepare:
        if args.output is None:
            parser.error("--prepare requires --output")
        prepare(
            args.prepare, args.output.resolve(),
            supersedes_campaign=args.supersedes,
        )
        print(args.output.resolve())
        return 0
    if args.supersedes is not None:
        parser.error("--supersedes is valid only with --prepare")
    root = (args.run or args.check).resolve()
    if args.check:
        print(json.dumps(verify_pins(root), indent=2))
        return 0
    with (root / ".replication.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root.parent / ".test-replication.lock").open("a") as global_lock:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, interrupt)
            return run(root)


def interrupt(signum: int, frame: Any) -> None:
    raise RuntimeError(f"supervisor interrupted by signal {signum}")


if __name__ == "__main__":
    raise SystemExit(main())
