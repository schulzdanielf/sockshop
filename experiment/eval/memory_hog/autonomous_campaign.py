"""Fail-closed, resumable orchestration for the long Sock Shop fault campaign."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Generator

import yaml

from experiment.eval.memory_hog import run_forty_faults as forty_faults
from experiment.eval.memory_hog import run_targeted_training as effect_validator
from experiment.eval.memory_hog import runner
from experiment.platform.backend.plugins.mcp_client import MCPToolClient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CAMPAIGN_ROOT = HERE / "out" / "autonomous_campaigns"
PROTOCOL_VERSION = 1
DEFAULT_RUN_TIMEOUT_SECONDS = 1800
DEFAULT_RECOVERY_TIMEOUT_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_MAX_INFRA_RETRIES = 2
DEFAULT_PORT_FORWARD_RESTARTS = 2
MCP_SSE_URL = "http://127.0.0.1:18080/sse"
PROMETHEUS_READY_URL = "http://127.0.0.1:9091/-/ready"
TERMINAL_STATUSES = {"completed", "failed", "stopped", "aborted"}
ACTIVE_STATUSES = {"pending", "running", "pending_approval", "queued", "starting"}
RUN_CSV_FIELDS = [
    "service",
    "replica_idx",
    "phase",
    "chaos_target",
    "experiment_id",
    "run_id",
    "status",
    "started_at",
    "ended_at",
    "notes",
    "chaos_type",
    "attempt",
]


class CampaignError(RuntimeError):
    """An error that must stop the campaign and be written to its report."""

    failure_domain = "unclassified"


class InfrastructureError(CampaignError):
    failure_domain = "experimental_infrastructure"


class ScenarioError(CampaignError):
    failure_domain = "injected_fault_or_invalid_result"


class PortForwardConflict(InfrastructureError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {message}\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def _exclusive_lock(path: Path, message: str) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CampaignError(message) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _scenario_dir(campaign_dir: Path, scenario: dict[str, Any]) -> Path:
    return campaign_dir / "scenarios" / scenario["scenario_id"]


def _summarize_scenarios(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in scenarios if item.get("state") == "completed"]
    failed = [item for item in scenarios if item.get("state") == "failed"]
    incomplete = [
        item
        for item in scenarios
        if item.get("state")
        in {
            "planned",
            "prepared",
            "submitting",
            "running",
            "collecting_evidence",
            "interrupted",
        }
    ]

    def public(item: dict[str, Any]) -> dict[str, Any]:
        result = item.get("result") or {}
        return {
            "order": item["order"],
            "scenario_id": item["scenario_id"],
            "service": item["cell"]["service"],
            "chaos_type": item["cell"]["chaos_type"],
            "state": item.get("state"),
            "run_id": item.get("run_id"),
            "failure_domain": item.get("failure_domain"),
            "failure": item.get("failure"),
            "timestamps": item.get("timestamps", {}),
            "result_status": result.get("result_status"),
            "logs": item.get("logs"),
        }

    return {
        "total": len(scenarios),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "incomplete_count": len(incomplete),
        "completed": [public(item) for item in completed],
        "failed": [public(item) for item in failed],
        "incomplete": [public(item) for item in incomplete],
    }


class CampaignRepository:
    def __init__(
        self,
        directory: Path,
        campaign: dict[str, Any],
        scenarios: list[dict[str, Any]],
    ) -> None:
        self.directory = directory
        self.campaign = campaign
        self.scenarios = scenarios

    def save_scenario(
        self,
        scenario: dict[str, Any],
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        timestamp = utc_now()
        scenario["timestamps"]["updated_at"] = timestamp
        _atomic_json(
            _scenario_dir(self.directory, scenario) / "scenario.json", scenario
        )
        _append_jsonl(
            self.directory / "events.jsonl",
            {
                "at": timestamp,
                "event": event,
                "scenario_id": scenario["scenario_id"],
                "state": scenario.get("state"),
                "run_id": scenario.get("run_id"),
                "details": details or {},
            },
        )
        self.save_checkpoint()
        self.save_report()

    def save_campaign(self) -> None:
        _atomic_json(self.directory / "campaign.json", self.campaign)

    def save_checkpoint(self) -> None:
        next_index = next(
            (
                item["order"]
                for item in self.scenarios
                if item.get("state") != "completed"
            ),
            len(self.scenarios),
        )
        _atomic_json(
            self.directory / "checkpoint.json",
            {
                "protocol_version": PROTOCOL_VERSION,
                "campaign_id": self.campaign["campaign_id"],
                "plan_sha256": self.campaign["plan_sha256"],
                "updated_at": utc_now(),
                "next_order": next_index,
                "completed_scenario_ids": [
                    item["scenario_id"]
                    for item in self.scenarios
                    if item.get("state") == "completed"
                ],
            },
        )

    def save_report(self) -> dict[str, Any]:
        summary = _summarize_scenarios(self.scenarios)
        report = {
            "protocol_version": PROTOCOL_VERSION,
            "campaign_id": self.campaign["campaign_id"],
            "mode": self.campaign["mode"],
            "status": self.campaign.get("status", "running"),
            "created_at": self.campaign["created_at"],
            "updated_at": utc_now(),
            "plan_sha256": self.campaign["plan_sha256"],
            "pilot_report": self.campaign.get("pilot_report"),
            "interruption_cause": self.campaign.get("interruption_cause"),
            "summary": summary,
            "passed": (
                self.campaign.get("status") == "completed"
                and summary["completed_count"] == summary["total"]
            ),
            "plan": [item["cell"] for item in self.scenarios],
        }
        _atomic_json(self.directory / "report.json", report)
        return report


@dataclass(frozen=True)
class PortForwardSpec:
    name: str
    namespace: str
    service: str
    local_port: int
    remote_port: int

    @property
    def mapping(self) -> str:
        return f"{self.local_port}:{self.remote_port}"


DEFAULT_PORT_FORWARDS = (
    PortForwardSpec("mcp", "mcp-server", "mcp-observability", 18080, 8000),
    PortForwardSpec("prometheus", "monitoring", "prometheus-v3", 9091, 9090),
)


class PortForwardSupervisor:
    """Own or adopt only matching tunnels, with bounded health-based restarts."""

    def __init__(
        self,
        log_directory: Path,
        *,
        specs: tuple[PortForwardSpec, ...] = DEFAULT_PORT_FORWARDS,
        max_restarts: int = DEFAULT_PORT_FORWARD_RESTARTS,
        startup_timeout_seconds: float = 15,
        probe_interval_seconds: float = 0.5,
        logger: Any = None,
    ) -> None:
        self.log_directory = log_directory
        self.specs = specs
        self.max_restarts = max_restarts
        self.startup_timeout_seconds = startup_timeout_seconds
        self.probe_interval_seconds = probe_interval_seconds
        self.logger = logger or (lambda _message: None)
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.restart_counts = {spec.name: 0 for spec in specs}
        self.last_probe_errors: dict[str, str] = {}

    def _probe(self, spec: PortForwardSpec) -> bool:
        try:
            if spec.name == "mcp":
                MCPToolClient(MCP_SSE_URL, timeout_seconds=5).assert_sse_connectivity()
                return True
            if spec.name == "prometheus":
                request = urllib.request.Request(PROMETHEUS_READY_URL, method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read(256).decode("utf-8", errors="replace")
                    if response.status != 200 or "ready" not in body.casefold():
                        self.last_probe_errors[spec.name] = (
                            f"Prometheus readiness response was HTTP {response.status}: "
                            f"{body[:120]}"
                        )
                        return False
                return True
            raise InfrastructureError(f"no health probe is defined for {spec.name!r}")
        except Exception as exc:
            self.last_probe_errors[spec.name] = f"{type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            return connection.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _process_arguments(pid: int) -> list[str] | None:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return [
            item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item
        ]

    @classmethod
    def _matches_process(cls, pid: int, spec: PortForwardSpec) -> bool:
        args = cls._process_arguments(pid)
        if not args:
            return False
        required = {
            "port-forward",
            "-n",
            spec.namespace,
            f"svc/{spec.service}",
            spec.mapping,
        }
        return required.issubset(set(args))

    @classmethod
    def _matching_pids(cls, spec: PortForwardSpec) -> list[int]:
        matches = []
        for item in Path("/proc").iterdir():
            if item.name.isdigit() and cls._matches_process(int(item.name), spec):
                matches.append(int(item.name))
        return matches

    def _wait_healthy(self, spec: PortForwardSpec) -> bool:
        deadline = time.monotonic() + self.startup_timeout_seconds
        while True:
            if self._probe(spec):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.probe_interval_seconds)

    def _wait_port_closed(self, spec: PortForwardSpec) -> None:
        deadline = time.monotonic() + 5
        while self._port_open(spec.local_port):
            if time.monotonic() >= deadline:
                raise PortForwardConflict(
                    f"port {spec.local_port} remains occupied after stopping the "
                    f"matching {spec.name} tunnel"
                )
            time.sleep(0.1)

    def _stop_owned(self, spec: PortForwardSpec) -> None:
        process = self.children.pop(spec.name, None)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.logger(f"stopped owned {spec.name} port-forward pid={process.pid}")

    def _stop_matching_pid(self, spec: PortForwardSpec, pid: int) -> None:
        if not self._matches_process(pid, spec):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while self._matches_process(pid, spec):
            if time.monotonic() >= deadline:
                raise InfrastructureError(
                    f"matching {spec.name} port-forward pid={pid} did not stop"
                )
            time.sleep(0.1)

    def _start(self, spec: PortForwardSpec) -> subprocess.Popen[bytes]:
        self.log_directory.mkdir(parents=True, exist_ok=True)
        log_path = self.log_directory / f"{spec.name}.log"
        command = [
            "kubectl",
            "port-forward",
            "--address",
            "127.0.0.1",
            "-n",
            spec.namespace,
            f"svc/{spec.service}",
            spec.mapping,
        ]
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.children[spec.name] = process
        self.logger(
            f"started {spec.name} port-forward pid={process.pid} "
            f"local=127.0.0.1:{spec.local_port}"
        )
        return process

    def ensure(self, spec: PortForwardSpec) -> None:
        if self._probe(spec):
            if len(self._matching_pids(spec)) > 1:
                raise PortForwardConflict(
                    f"multiple matching {spec.name} processes exist even though "
                    f"the health check passed on port {spec.local_port}"
                )
            return
        starts_remaining = self.max_restarts + 1 - self.restart_counts[spec.name]
        if starts_remaining <= 0:
            detail = self.last_probe_errors.get(spec.name, "health check failed")
            raise InfrastructureError(
                f"{spec.name} port-forward exhausted its restart budget "
                f"({self.max_restarts} restarts): {detail}"
            )
        matching = self._matching_pids(spec)
        if len(matching) > 1:
            raise PortForwardConflict(
                f"found {len(matching)} matching {spec.name} tunnels on "
                f"port {spec.local_port}; refusing to add another"
            )
        if matching:
            pid = matching[0]
            owned = self.children.get(spec.name)
            if owned is not None and owned.pid == pid:
                self._stop_owned(spec)
            else:
                self._stop_matching_pid(spec, pid)
            self._wait_port_closed(spec)
        elif self._port_open(spec.local_port):
            raise PortForwardConflict(
                f"port {spec.local_port} is occupied by an unhealthy or unrelated "
                f"listener; refusing to create a duplicate {spec.name} tunnel; "
                f"health check: {self.last_probe_errors.get(spec.name, 'failed')}"
            )

        for attempt in range(starts_remaining):
            if attempt:
                self.logger(
                    f"retrying {spec.name} port-forward ({attempt}/{self.max_restarts})"
                )
            self.restart_counts[spec.name] += 1
            try:
                process = self._start(spec)
            except (OSError, subprocess.SubprocessError) as exc:
                error = f"cannot start {spec.name} port-forward: {exc}"
                if attempt == starts_remaining - 1:
                    raise InfrastructureError(error) from exc
                self.logger(error)
                continue
            if self._wait_healthy(spec):
                return
            self._stop_owned(spec)
            detail = self.last_probe_errors.get(spec.name, "health check failed")
            self.logger(
                f"{spec.name} port-forward pid={process.pid} did not become healthy: "
                f"{detail}"
            )
            if attempt == starts_remaining - 1:
                raise InfrastructureError(
                    f"{spec.name} port-forward failed health checks after "
                    f"{self.restart_counts[spec.name]} total start attempt(s): {detail}"
                )

    def ensure_all(self) -> None:
        for spec in self.specs:
            self.ensure(spec)

    def close(self) -> None:
        for spec in self.specs:
            self._stop_owned(spec)


def _new_scenario(order: int, cell: dict[str, str]) -> dict[str, Any]:
    scenario_id = str(uuid.uuid4())
    return {
        "scenario_id": scenario_id,
        "order": order,
        "cell": dict(cell),
        "state": "planned",
        "spec": None,
        "run_id": None,
        "experiment_version": None,
        "submission_attempts": 0,
        "timestamps": {"created_at": utc_now()},
        "result": {},
        "failure_domain": None,
        "failure": None,
        "logs": f"scenarios/{scenario_id}/scenario.log",
    }


def _default_campaign_directory(mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return CAMPAIGN_ROOT / f"{mode}-{stamp}"


def _load_pilot_report(path: Path, first_cell: dict[str, str]) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot read pilot report {path}: {exc}") from exc
    if (
        not isinstance(report, dict)
        or report.get("protocol_version") != PROTOCOL_VERSION
        or report.get("mode") != "pilot"
        or report.get("status") != "completed"
        or report.get("passed") is not True
        or report.get("plan") != [first_cell]
        or (report.get("summary") or {}).get("completed_count") != 1
        or (report.get("summary") or {}).get("failed_count") != 0
    ):
        raise CampaignError(
            "full execution requires a successful pilot report from this protocol "
            "for the first scenario in the saved matrix"
        )
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "campaign_id": report["campaign_id"],
        "completed_at": report.get("updated_at"),
    }


def _create_campaign(
    mode: str,
    directory: Path,
    max_infra_retries: int,
    run_timeout_seconds: int,
    recovery_timeout_seconds: int,
    pilot_report: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], CampaignRepository]:
    source = yaml.safe_load(forty_faults.SOURCE_CONFIG.read_text(encoding="utf-8"))
    campaign_id = directory.name
    config = forty_faults.build_config(source, campaign_id, directory.resolve())
    cells = [dict(cell) for cell in forty_faults.CELLS]
    if mode == "pilot":
        cells = cells[:1]
    config["test_experiments"] = cells
    config["platform"]["initiated_by"] = "autonomous-forty-faults"
    config["platform"]["poll_interval_seconds"] = DEFAULT_POLL_INTERVAL_SECONDS
    config["platform"]["run_timeout_seconds"] = run_timeout_seconds
    config["autonomous_campaign"] = {
        "protocol_version": PROTOCOL_VERSION,
        "max_infra_retries": max_infra_retries,
        "run_timeout_seconds": run_timeout_seconds,
        "recovery_timeout_seconds": recovery_timeout_seconds,
        "failure_policy": "fail-fast; no automatic scenario skip or reinjection",
    }
    if directory.exists():
        raise CampaignError(
            f"campaign directory already exists; refusing to overwrite: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=False)
    scenarios = [_new_scenario(index, cell) for index, cell in enumerate(cells)]
    created_at = utc_now()
    campaign = {
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": campaign_id,
        "mode": mode,
        "status": "created",
        "created_at": created_at,
        "config_sha256": _sha256(config),
        "plan_sha256": _sha256(cells),
        "pilot_report": pilot_report,
        "interruption_cause": None,
        "preflight": [],
    }
    (directory / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _atomic_json(
        directory / "plan.json",
        {
            "protocol_version": PROTOCOL_VERSION,
            "campaign_id": campaign_id,
            "mode": mode,
            "plan_sha256": campaign["plan_sha256"],
            "scenarios": [
                {
                    "scenario_id": item["scenario_id"],
                    "order": item["order"],
                    "cell": item["cell"],
                }
                for item in scenarios
            ],
        },
    )
    repository = CampaignRepository(directory, campaign, scenarios)
    repository.save_campaign()
    for scenario in scenarios:
        repository.save_scenario(scenario, "scenario_planned")
    return campaign, scenarios, repository


def _load_campaign(
    directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], CampaignRepository]:
    try:
        campaign = json.loads((directory / "campaign.json").read_text(encoding="utf-8"))
        plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CampaignError(
            f"cannot load campaign checkpoint from {directory}: {exc}"
        ) from exc
    if (
        not isinstance(campaign, dict)
        or not isinstance(plan, dict)
        or not isinstance(config, dict)
        or not isinstance(plan.get("scenarios"), list)
    ):
        raise CampaignError("campaign checkpoint has an invalid top-level structure")
    if any(
        not isinstance(entry, dict)
        or not isinstance(entry.get("scenario_id"), str)
        or not isinstance(entry.get("order"), int)
        or not isinstance(entry.get("cell"), dict)
        for entry in plan["scenarios"]
    ):
        raise CampaignError("campaign plan contains a malformed scenario")
    cells = [entry["cell"] for entry in plan.get("scenarios", [])]
    if (
        campaign.get("protocol_version") != PROTOCOL_VERSION
        or plan.get("protocol_version") != PROTOCOL_VERSION
        or plan.get("campaign_id") != campaign.get("campaign_id")
        or plan.get("plan_sha256") != _sha256(cells)
        or campaign.get("plan_sha256") != _sha256(cells)
        or config.get("test_experiments") != cells
        or campaign.get("config_sha256") != _sha256(config)
    ):
        raise CampaignError(
            "saved campaign plan/configuration is inconsistent; refusing resume"
        )

    scenarios = []
    for planned in plan["scenarios"]:
        scenario_path = (
            directory / "scenarios" / planned["scenario_id"] / "scenario.json"
        )
        if scenario_path.is_file():
            try:
                scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CampaignError(
                    f"cannot load scenario checkpoint {scenario_path}: {exc}"
                ) from exc
            if not isinstance(scenario, dict):
                raise CampaignError(
                    f"scenario checkpoint is not an object: {scenario_path}"
                )
            if (
                scenario.get("scenario_id") != planned["scenario_id"]
                or scenario.get("cell") != planned["cell"]
                or scenario.get("order") != planned["order"]
            ):
                raise CampaignError(
                    f"scenario checkpoint differs from immutable plan: {scenario_path}"
                )
        else:
            scenario = _new_scenario(planned["order"], planned["cell"])
            scenario["scenario_id"] = planned["scenario_id"]
            scenario["logs"] = f"scenarios/{planned['scenario_id']}/scenario.log"
            scenario["timestamps"]["created_at"] = campaign["created_at"]
        scenarios.append(scenario)
    repository = CampaignRepository(directory, campaign, scenarios)
    return campaign, config, scenarios, repository


def _transition(
    repository: CampaignRepository,
    scenario: dict[str, Any],
    state: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    scenario["state"] = state
    if state == "running" and not scenario["timestamps"].get("started_at"):
        scenario["timestamps"]["started_at"] = utc_now()
    if state in {"completed", "failed"}:
        scenario["timestamps"]["ended_at"] = utc_now()
    _log_line(
        repository.directory / scenario["logs"],
        f"state={state} event={event} details={json.dumps(details or {}, ensure_ascii=False)}",
    )
    repository.save_scenario(scenario, event, details)


def _make_specs(
    repository: CampaignRepository,
    config: dict[str, Any],
) -> None:
    for scenario in repository.scenarios:
        scenario_path = _scenario_dir(repository.directory, scenario)
        scenario_path.mkdir(parents=True, exist_ok=True)
        spec_path = scenario_path / "spec.json"
        if spec_path.is_file():
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        else:
            cell = scenario["cell"]
            spec = runner.build_spec(config, cell["service"], 0, cell["chaos_type"])
            experiment_id = (
                f"{repository.campaign['campaign_id']}-{scenario['scenario_id']}"
            )
            spec["experiment"]["id"] = experiment_id
            spec["experiment"]["name"] = (
                f"Autonomous {cell['chaos_type']} on {cell['service']} "
                f"({scenario['scenario_id']})"
            )
            spec["experiment"]["tags"] = [
                *spec["experiment"].get("tags", []),
                f"campaign:{repository.campaign['campaign_id']}",
                f"scenario:{scenario['scenario_id']}",
            ]
            scenario["spec"] = spec
            _atomic_json(spec_path, spec)
            _transition(repository, scenario, "prepared", "scenario_prepared")
        if spec.get("experiment", {}).get("id") != (
            f"{repository.campaign['campaign_id']}-{scenario['scenario_id']}"
        ):
            raise CampaignError(f"experiment ID mismatch in {spec_path}")
        scenario["spec"] = spec
        repository.save_scenario(scenario, "scenario_spec_loaded")


def _port_forward_logger(directory: Path) -> Any:
    log_path = directory / "port-forwards" / "supervisor.log"

    def write(message: str) -> None:
        _log_line(log_path, message)

    return write


def _check_mcp_contract() -> None:
    try:
        tools = MCPToolClient(MCP_SSE_URL, timeout_seconds=10).list_tools()
    except Exception as exc:
        raise InfrastructureError(
            f"observability MCP is unreachable at {MCP_SSE_URL}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not any(tool.get("name") == "prometheus_scrape_targets" for tool in tools):
        raise InfrastructureError(
            "observability MCP is missing prometheus_scrape_targets; "
            "no experiment was submitted"
        )


def _check_platform(api: runner.ApiClient) -> None:
    try:
        health = api.get("/api/health")
    except Exception as exc:
        raise InfrastructureError(
            f"experiment platform is unreachable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise InfrastructureError(
            "experiment platform health endpoint returned invalid data"
        )


def _active_foreign_runs(
    api: runner.ApiClient, owned_run_ids: set[str]
) -> list[dict[str, Any]]:
    try:
        runs = api.get("/api/runs")
    except Exception as exc:
        raise InfrastructureError(
            f"cannot inspect active platform runs: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(runs, list):
        raise InfrastructureError("platform returned a malformed run inventory")
    return [
        item
        for item in runs
        if item.get("status") in ACTIVE_STATUSES
        and item.get("run_id") not in owned_run_ids
    ]


def _preflight(
    campaign: dict[str, Any],
    config: dict[str, Any],
    scenarios: list[dict[str, Any]],
    api: runner.ApiClient,
    supervisor: PortForwardSupervisor,
    *,
    is_resume: bool,
) -> list[str]:
    supervisor.ensure_all()
    _check_platform(api)
    _check_mcp_contract()
    owned = {
        item["run_id"] for item in scenarios if isinstance(item.get("run_id"), str)
    }
    active_foreign = _active_foreign_runs(api, owned)
    if active_foreign:
        identifiers = ", ".join(str(item.get("run_id")) for item in active_foreign)
        raise InfrastructureError(
            f"unrelated platform runs are active ({identifiers}); refusing concurrent experiments"
        )
    owned_incomplete = any(
        scenario.get("run_id") and scenario.get("state") not in {"completed", "failed"}
        for scenario in scenarios
    )
    checks: list[str] = [
        "platform API healthy",
        "MCP and Prometheus port-forwards healthy",
        "no unrelated platform runs active",
    ]
    if not is_resume:
        try:
            forty_faults.diverse.wait_for_idle(
                config["platform"]["api_base"], wait=False
            )
        except Exception as exc:
            raise InfrastructureError(
                f"preflight found active/unknown experiment work: {exc}"
            ) from exc
    if owned_incomplete:
        checks.append(
            "resuming a scenario with a persisted run; cluster preflight deferred until recovery"
        )
    else:
        try:
            checks.extend(forty_faults.preflight(config))
        except Exception as exc:
            raise InfrastructureError(
                f"Kubernetes/Sock Shop preflight failed: {type(exc).__name__}: {exc}"
            ) from exc
    campaign["preflight"] = checks
    campaign["status"] = "running"
    campaign["interruption_cause"] = None
    return checks


def _get_existing_run(
    api: runner.ApiClient, experiment_id: str
) -> dict[str, Any] | None:
    runs = api.get("/api/runs", experiment_id=experiment_id)
    if not isinstance(runs, list):
        raise InfrastructureError("platform returned a malformed experiment run list")
    if len(runs) > 1:
        raise CampaignError(
            f"multiple runs found for deterministic experiment {experiment_id}; "
            "refusing to choose or inject again"
        )
    return runs[0] if runs else None


def _is_retryable_submission_error(exc: Exception) -> bool:
    message = str(exc)
    if "HTTP 4" in message and not any(
        code in message for code in ("HTTP 408", "HTTP 429")
    ):
        return False
    return isinstance(exc, (OSError, TimeoutError, RuntimeError, urllib.error.URLError))


def _find_or_submit_run(
    scenario: dict[str, Any],
    config: dict[str, Any],
    api: runner.ApiClient,
    repository: CampaignRepository,
    max_infra_retries: int,
) -> str:
    if scenario.get("run_id"):
        return str(scenario["run_id"])

    spec = scenario.get("spec")
    if not isinstance(spec, dict):
        raise CampaignError("scenario has no persisted experiment spec")
    experiment_id = spec["experiment"]["id"]
    idempotency_key = f"autonomous:{experiment_id}"
    last_error: Exception | None = None
    for attempt in range(max_infra_retries + 1):
        scenario["submission_attempts"] = (
            int(scenario.get("submission_attempts", 0)) + 1
        )
        _transition(
            repository,
            scenario,
            "submitting",
            "submission_attempt",
            {"attempt": attempt + 1, "idempotency_key": idempotency_key},
        )
        try:
            existing = _get_existing_run(api, experiment_id)
            if existing is not None:
                scenario["run_id"] = existing["run_id"]
                scenario["timestamps"]["submitted_at"] = (
                    existing.get("started_at") or utc_now()
                )
                scenario["result"]["initial_run_status"] = existing.get("status")
                _transition(
                    repository,
                    scenario,
                    "running",
                    "existing_run_attached",
                    {"run_id": scenario["run_id"], "status": existing.get("status")},
                )
                return str(scenario["run_id"])

            try:
                saved_experiment = api.get(f"/api/experiments/{experiment_id}")
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                saved_experiment = None
            if saved_experiment is None:
                created = api.post(
                    "/api/experiments",
                    body={
                        "initiated_by": config["platform"]["initiated_by"],
                        "spec": spec,
                    },
                )
                version = created.get("version")
            else:
                if saved_experiment.get("spec") != spec:
                    raise CampaignError(
                        f"persisted platform experiment {experiment_id} differs from "
                        "the campaign spec; refusing submission"
                    )
                version = saved_experiment.get("version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise InfrastructureError(
                    f"platform returned invalid experiment version for {experiment_id}"
                )
            scenario["experiment_version"] = version
            repository.save_scenario(
                scenario,
                "experiment_version_persisted",
                {"experiment_id": experiment_id, "version": version},
            )
            started = api.post(
                "/api/runs/start",
                body={
                    "experiment_id": experiment_id,
                    "version": version,
                    "initiated_by": config["platform"]["initiated_by"],
                    "idempotency_key": idempotency_key,
                    "is_training": False,
                },
                request_timeout=60,
            )
            run_id = started.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise InfrastructureError(
                    "platform returned no run_id after submission"
                )
            scenario["run_id"] = run_id
            scenario["timestamps"]["submitted_at"] = utc_now()
            scenario["result"]["initial_run_status"] = started.get("status")
            _transition(
                repository,
                scenario,
                "running",
                "run_submitted",
                {"run_id": run_id, "status": started.get("status")},
            )
            return run_id
        except Exception as exc:
            last_error = exc
            try:
                existing = _get_existing_run(api, experiment_id)
            except Exception as lookup_error:
                last_error = InfrastructureError(
                    f"submission outcome is uncertain ({exc}); run lookup also failed "
                    f"({lookup_error}); resume will reconcile by idempotency key"
                )
                if attempt < max_infra_retries:
                    time.sleep(min(attempt + 1, 3))
                    continue
                raise last_error from lookup_error
            if existing is not None:
                scenario["run_id"] = existing["run_id"]
                scenario["timestamps"]["submitted_at"] = utc_now()
                scenario["result"]["initial_run_status"] = existing.get("status")
                _transition(
                    repository,
                    scenario,
                    "running",
                    "submission_reconciled",
                    {"run_id": scenario["run_id"], "status": existing.get("status")},
                )
                return str(scenario["run_id"])
            if attempt >= max_infra_retries or not _is_retryable_submission_error(exc):
                raise InfrastructureError(
                    f"could not submit {experiment_id} after {attempt + 1} "
                    f"attempt(s): {type(exc).__name__}: {exc}"
                ) from exc
            _log_line(
                repository.directory / scenario["logs"],
                f"bounded infrastructure retry {attempt + 1}/{max_infra_retries}: "
                f"{type(exc).__name__}: {exc}",
            )
            time.sleep(min(attempt + 1, 3))
    if last_error is not None:
        raise InfrastructureError(f"submission failed: {last_error}") from last_error
    raise InfrastructureError(f"submission failed for {experiment_id}")


def _stop_timed_out_run(
    api: runner.ApiClient, run_id: str, repository: CampaignRepository
) -> str:
    try:
        return api.post(
            f"/api/runs/{run_id}/stop",
            body={
                "requested_by": "autonomous-campaign",
                "reason": "autonomous campaign run timeout",
            },
            request_timeout=30,
        ).get("status", "unknown")
    except Exception as exc:
        _log_line(
            repository.directory / "campaign.log",
            f"stop request failed for timed-out run {run_id}: "
            f"{type(exc).__name__}: {exc}",
        )
        return "stop_request_failed"


def _wait_for_run(
    run_id: str,
    api: runner.ApiClient,
    supervisor: PortForwardSupervisor,
    repository: CampaignRepository,
    scenario: dict[str, Any],
    *,
    timeout_seconds: int,
    poll_seconds: int,
    max_infra_retries: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    consecutive_errors = 0
    while True:
        supervisor.ensure_all()
        try:
            result = api.get(f"/api/runs/{run_id}")
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            if consecutive_errors > max_infra_retries:
                raise InfrastructureError(
                    f"run status unavailable after {consecutive_errors} consecutive "
                    f"poll failures: {type(exc).__name__}: {exc}"
                ) from exc
            _log_line(
                repository.directory / scenario["logs"],
                f"poll retry {consecutive_errors}/{max_infra_retries}: "
                f"{type(exc).__name__}: {exc}",
            )
            time.sleep(min(poll_seconds, 2**consecutive_errors))
            continue
        if not isinstance(result, dict):
            raise InfrastructureError(f"platform returned malformed run {run_id}")
        status = result.get("status")
        if status in TERMINAL_STATUSES:
            return result
        if time.monotonic() >= deadline:
            stop_status = _stop_timed_out_run(api, run_id, repository)
            scenario["result"]["timeout_stop_status"] = stop_status
            raise InfrastructureError(
                f"run {run_id} exceeded {timeout_seconds}s; stop status={stop_status}; "
                "no reinjection will be attempted"
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _write_runs_csv(directory: Path, scenarios: list[dict[str, Any]]) -> None:
    path = directory / "runs.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".runs.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RUN_CSV_FIELDS)
            writer.writeheader()
            for item in scenarios:
                if not item.get("run_id"):
                    continue
                cell = item["cell"]
                writer.writerow(
                    {
                        "service": cell["service"],
                        "replica_idx": 0,
                        "phase": "test",
                        "chaos_target": cell["service"],
                        "experiment_id": item["spec"]["experiment"]["id"],
                        "run_id": item["run_id"],
                        "status": item.get("result", {}).get(
                            "run_status",
                            item.get("result", {}).get("initial_run_status", "running"),
                        ),
                        "started_at": item.get("timestamps", {}).get("started_at", ""),
                        "ended_at": item.get("timestamps", {}).get("ended_at", ""),
                        "notes": item.get("failure") or "",
                        "chaos_type": cell["chaos_type"],
                        "attempt": 1,
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _collect_run_evidence(
    api: runner.ApiClient,
    scenario: dict[str, Any],
    repository: CampaignRepository,
    final_run: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    scenario_directory = _scenario_dir(repository.directory, scenario)
    _atomic_json(scenario_directory / "run.json", final_run)
    run_id = str(scenario["run_id"])
    try:
        events = api.get(f"/api/runs/{run_id}/events")
        features = api.get(f"/api/runs/{run_id}/features")
    except Exception as exc:
        raise InfrastructureError(
            f"could not persist run events/features for {run_id}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(events, list) or not isinstance(features, dict):
        raise InfrastructureError(
            f"platform returned malformed events/features for run {run_id}"
        )
    _atomic_json(scenario_directory / "events.json", events)
    _atomic_json(scenario_directory / "run_features.json", features)
    try:
        observation_window = api.get(f"/api/runs/{run_id}/observation-window")
        if not isinstance(observation_window, dict):
            raise InfrastructureError(
                f"platform returned malformed observation window for {run_id}"
            )
        _atomic_json(scenario_directory / "observation_window.json", observation_window)
        if final_run.get("status") == "completed":
            phases = observation_window.get("phases")
            required_phases = {"baseline", "fault", "post"}
            if (
                observation_window.get("partial") is True
                or not isinstance(phases, dict)
                or not required_phases.issubset(phases)
                or any(
                    not isinstance(phases[phase], dict)
                    or not all(
                        isinstance(phases[phase].get(bound), str)
                        and phases[phase][bound]
                        for bound in ("start", "end")
                    )
                    for phase in required_phases
                )
            ):
                raise InfrastructureError(
                    f"completed run {run_id} has an incomplete or partial "
                    "baseline/fault/post observation window"
                )
    except Exception as exc:
        _atomic_json(
            scenario_directory / "observation_window_error.json",
            {
                "run_id": run_id,
                "at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        if final_run.get("status") == "completed":
            raise InfrastructureError(
                f"could not persist a valid observation window for {run_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return final_run, events, features


def _validate_single_run(
    scenario: dict[str, Any],
    repository: CampaignRepository,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    _write_runs_csv(repository.directory, scenarios)
    scenario_directory = _scenario_dir(repository.directory, scenario)
    input_csv = scenario_directory / "validation_input.csv"
    with input_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "service": scenario["cell"]["service"],
                "replica_idx": 0,
                "phase": "test",
                "chaos_target": scenario["cell"]["service"],
                "experiment_id": scenario["spec"]["experiment"]["id"],
                "run_id": scenario["run_id"],
                "status": scenario["result"]["run_status"],
                "started_at": scenario["timestamps"].get("started_at", ""),
                "ended_at": scenario["timestamps"].get("ended_at", ""),
                "notes": "",
                "chaos_type": scenario["cell"]["chaos_type"],
                "attempt": 1,
            }
        )
        handle.flush()
        os.fsync(handle.fileno())
    reports = effect_validator.validate_runs(
        input_csv,
        object_store=effect_validator.OBJECT_STORE,
        cells=[scenario["cell"]],
        replicas=1,
        phase="test",
    )
    if len(reports) != 1:
        raise InfrastructureError(
            f"expected one effect-validation report for {scenario['scenario_id']}; "
            f"received {len(reports)}"
        )
    report = reports[0]
    _atomic_json(scenario_directory / "effect_validation.json", report)
    return report


def _wait_for_recovery(
    namespace: str,
    supervisor: PortForwardSupervisor,
    *,
    timeout_seconds: int,
    stable_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_unready: list[str] = []
    while True:
        supervisor.ensure_all()
        try:
            proc = runner._kubectl(
                "get", "deployments", "-n", namespace, "-o", "json", timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            proc = None
            last_unready = [f"kubectl deployment readiness query timed out: {exc}"]
        except OSError as exc:
            raise InfrastructureError(
                f"cannot execute kubectl recovery health check: {exc}"
            ) from exc
        if proc is None:
            pass
        elif proc.returncode != 0:
            last_unready = [proc.stderr.strip() or "kubectl deployment query failed"]
        else:
            try:
                deployments = json.loads(proc.stdout).get("items", [])
            except json.JSONDecodeError as exc:
                raise InfrastructureError(
                    f"kubectl returned malformed deployment JSON: {exc}"
                ) from exc
            by_name = {
                item.get("metadata", {}).get("name"): item for item in deployments
            }
            last_unready = []
            for service in forty_faults.diverse.SERVICES:
                deployment = by_name.get(service)
                if deployment is None:
                    last_unready.append(f"{service}: deployment missing")
                    continue
                metadata = deployment.get("metadata") or {}
                spec = deployment.get("spec") or {}
                status = deployment.get("status") or {}
                desired = int(spec.get("replicas", 1))
                available = int(status.get("availableReplicas", 0))
                observed = int(status.get("observedGeneration", 0))
                generation = int(metadata.get("generation", 0))
                if available < desired or observed < generation:
                    last_unready.append(
                        f"{service}: available={available}/{desired}, "
                        f"observed_generation={observed}/{generation}"
                    )
        now = time.monotonic()
        if not last_unready:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= stable_seconds:
                return {
                    "passed": True,
                    "stable_seconds": stable_seconds,
                    "checked_at": utc_now(),
                }
        else:
            stable_since = None
        if now >= deadline:
            raise InfrastructureError(
                f"Sock Shop did not remain healthy for {stable_seconds}s within "
                f"{timeout_seconds}s: {'; '.join(last_unready)}"
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - now)))


def _classify_effect_failure(report: dict[str, Any]) -> str:
    checks = report.get("checks") or {}
    injection = checks.get("injection") or {}
    metrics = checks.get("metrics") or {}
    reason = str(report.get("reason", "")).casefold()
    if any(
        marker in reason
        for marker in ("cannot read run artifacts", "run file not found")
    ):
        return "experimental_infrastructure"
    if injection.get("passed") is not True:
        return "fault_injection_failed_or_unverified"
    metric_rows = (metrics.get("metrics") or {}).values()
    sample_fields = {"baseline_mean", "fault_mean", "fault_p95", "delta_abs"}
    if not any(
        isinstance(row, dict)
        and any(row.get(field) is not None for field in sample_fields)
        for row in metric_rows
    ):
        return "telemetry_or_target_coverage_gap"
    if metrics.get("signal_observed") is False:
        return "injected_fault_effect_not_observed"
    return "invalid_or_incomplete_effect_evidence"


def _run_rca(
    scenario: dict[str, Any],
    config: dict[str, Any],
    repository: CampaignRepository,
) -> dict[str, Any]:
    report_path = _scenario_dir(repository.directory, scenario) / "rca_evaluation.json"
    if report_path.is_file():
        return json.loads(report_path.read_text(encoding="utf-8"))
    categories = {
        entry["name"]: entry.get("fault_category", "")
        for entry in config["chaos_types"]
    }
    rca = forty_faults.evaluate_rca_campaign(
        config["platform"]["api_base"],
        repository.directory / config["output"]["runs_csv"],
        [scenario["cell"]],
        categories,
        repository.campaign["campaign_id"],
    )
    _atomic_json(report_path, rca)
    if (rca.get("summary") or {}).get("rca_completed") != 1:
        raise InfrastructureError(
            f"RCA did not complete for {scenario['run_id']}; report persisted at "
            f"{report_path}"
        )
    return rca


def _process_scenario(
    scenario: dict[str, Any],
    config: dict[str, Any],
    api: runner.ApiClient,
    supervisor: PortForwardSupervisor,
    repository: CampaignRepository,
    *,
    run_timeout_seconds: int,
    recovery_timeout_seconds: int,
    max_infra_retries: int,
) -> None:
    if scenario.get("state") == "completed":
        scenario_directory = _scenario_dir(repository.directory, scenario)
        required = [
            scenario_directory / "run.json",
            scenario_directory / "events.json",
            scenario_directory / "run_features.json",
            scenario_directory / "observation_window.json",
            scenario_directory / "effect_validation.json",
            scenario_directory / "rca_evaluation.json",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise CampaignError(
                "completed scenario has missing persisted evidence; refusing to rerun: "
                + ", ".join(missing)
            )
        return
    if scenario.get("state") == "failed":
        raise ScenarioError(
            f"scenario {scenario['scenario_id']} previously failed: "
            f"{scenario.get('failure') or 'manual review required'}"
        )
    _transition(repository, scenario, "prepared", "scenario_resuming")
    supervisor.ensure_all()
    owned_run_ids = {
        item["run_id"]
        for item in repository.scenarios
        if isinstance(item.get("run_id"), str)
    }
    active_foreign = _active_foreign_runs(api, owned_run_ids)
    if active_foreign:
        identifiers = ", ".join(str(item.get("run_id")) for item in active_foreign)
        raise InfrastructureError(
            f"unrelated platform runs became active during the campaign "
            f"({identifiers}); stopping before another submission"
        )
    run_id = _find_or_submit_run(scenario, config, api, repository, max_infra_retries)
    final = _wait_for_run(
        run_id,
        api,
        supervisor,
        repository,
        scenario,
        timeout_seconds=run_timeout_seconds,
        poll_seconds=int(config["platform"]["poll_interval_seconds"]),
        max_infra_retries=max_infra_retries,
    )
    scenario["result"]["run_status"] = final.get("status")
    scenario["timestamps"]["started_at"] = (
        scenario["timestamps"].get("started_at") or final.get("started_at") or utc_now()
    )
    scenario["timestamps"]["run_ended_at"] = final.get("ended_at") or utc_now()
    _transition(
        repository,
        scenario,
        "collecting_evidence",
        "run_terminal",
        {
            "run_id": run_id,
            "run_status": final.get("status"),
        },
    )
    try:
        _collect_run_evidence(api, scenario, repository, final)
    except Exception as exc:
        scenario["failure_domain"] = getattr(
            exc, "failure_domain", "experimental_infrastructure"
        )
        scenario["failure"] = f"{type(exc).__name__}: {exc}"
        scenario["state"] = "failed"
        _transition(repository, scenario, "failed", "evidence_collection_failed")
        raise
    _write_runs_csv(repository.directory, repository.scenarios)

    recovery = _wait_for_recovery(
        config["cluster"]["app_namespace"],
        supervisor,
        timeout_seconds=recovery_timeout_seconds,
        stable_seconds=int(config["platform"].get("inter_run_cooldown_seconds", 0)),
        poll_seconds=int(config["platform"]["poll_interval_seconds"]),
    )
    scenario["result"]["recovery"] = recovery
    repository.save_scenario(scenario, "service_recovery_confirmed", recovery)

    if final.get("status") != "completed":
        scenario["failure_domain"] = "run_execution_unclassified"
        scenario["failure"] = (
            f"platform run ended with status {final.get('status')}; "
            "evidence preserved; injection-vs-infrastructure cause requires review"
        )
        _transition(repository, scenario, "failed", "run_ended_unsuccessfully")
        raise ScenarioError(scenario["failure"])

    try:
        effect = _validate_single_run(scenario, repository, repository.scenarios)
    except Exception as exc:
        scenario["failure_domain"] = getattr(
            exc, "failure_domain", "experimental_infrastructure"
        )
        scenario["failure"] = f"{type(exc).__name__}: {exc}"
        _transition(repository, scenario, "failed", "effect_validation_failed")
        raise
    scenario["result"]["effect_validation"] = effect
    scenario["result"]["result_status"] = effect.get("result_status")
    if effect.get("passed") is not True:
        scenario["failure_domain"] = _classify_effect_failure(effect)
        scenario["failure"] = (
            effect.get("reason")
            or effect.get("result_status")
            or "effect validation returned no valid result"
        )
        _transition(
            repository,
            scenario,
            "failed",
            "invalid_or_unconfirmed_fault_evidence",
            {"failure_domain": scenario["failure_domain"]},
        )
        raise ScenarioError(
            f"{scenario['failure_domain']}: {scenario['failure']}; stopping before "
            "the next scenario"
        )

    _write_runs_csv(repository.directory, repository.scenarios)
    try:
        rca = _run_rca(scenario, config, repository)
    except Exception as exc:
        scenario["failure_domain"] = getattr(
            exc, "failure_domain", "analysis_infrastructure"
        )
        scenario["failure"] = f"{type(exc).__name__}: {exc}"
        _transition(repository, scenario, "failed", "rca_failed")
        raise
    scenario["result"]["rca"] = rca
    scenario["failure_domain"] = None
    scenario["failure"] = None
    _transition(
        repository,
        scenario,
        "completed",
        "scenario_completed",
        {
            "run_id": run_id,
            "fault_effect_confirmed": True,
            "rca_completed": True,
        },
    )


def _verify_pilot_reference(campaign: dict[str, Any]) -> None:
    reference = campaign.get("pilot_report")
    if not isinstance(reference, dict):
        raise CampaignError(
            "full campaign checkpoint has no successful pilot reference"
        )
    path = Path(reference["path"])
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CampaignError(f"saved pilot report is unavailable: {path}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != reference.get("sha256"):
        raise CampaignError("pilot report changed after full campaign initialization")
    report = json.loads(payload.decode("utf-8"))
    if report.get("status") != "completed" or report.get("passed") is not True:
        raise CampaignError("referenced pilot is no longer a successful completed run")


def _execute_campaign(
    directory: Path,
    *,
    is_resume: bool,
    max_infra_retries: int,
    run_timeout_seconds: int,
    recovery_timeout_seconds: int,
) -> int:
    campaign, config, scenarios, repository = _load_campaign(directory)
    if campaign.get("mode") == "full":
        _verify_pilot_reference(campaign)
    campaign_lock = directory / ".campaign.lock"
    global_forward_lock = CAMPAIGN_ROOT / ".port-forwards.lock"
    with _exclusive_lock(campaign_lock, f"campaign is already running: {directory}"):
        with _exclusive_lock(
            global_forward_lock,
            "another autonomous campaign owns the experiment port-forwards",
        ):
            supervisor = PortForwardSupervisor(
                directory / "port-forwards",
                max_restarts=DEFAULT_PORT_FORWARD_RESTARTS,
                logger=_port_forward_logger(directory),
            )
            api = runner.ApiClient(
                config["platform"]["api_base"],
                role=config["platform"]["user_role"],
            )
            active_scenario: dict[str, Any] | None = None
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def handle_sigterm(_signum: int, _frame: Any) -> None:
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, handle_sigterm)
            try:
                checks = _preflight(
                    campaign,
                    config,
                    scenarios,
                    api,
                    supervisor,
                    is_resume=is_resume,
                )
                campaign["preflight"] = checks
                campaign["status"] = "running"
                repository.save_campaign()
                repository.save_report()
                manifest_dir = directory / config["output"]["manifests_dir"]
                if is_resume:
                    hashes_path = directory / "manifest_hashes.json"
                    if manifest_dir.is_dir() and hashes_path.is_file():
                        _verify_manifest_hashes(directory, manifest_dir)
                    elif not any(item.get("run_id") for item in scenarios):
                        forty_faults.generate_chaos_manifests.generate(
                            config, manifest_dir
                        )
                        _atomic_json(
                            hashes_path,
                            {
                                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted(manifest_dir.glob("*.yaml"))
                            },
                        )
                    else:
                        raise CampaignError(
                            "campaign manifests are missing after a run was submitted; "
                            "refusing to regenerate evidence-bearing configuration"
                        )
                else:
                    forty_faults.generate_chaos_manifests.generate(config, manifest_dir)
                    _atomic_json(
                        directory / "manifest_hashes.json",
                        {
                            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(manifest_dir.glob("*.yaml"))
                        },
                    )
                _make_specs(repository, config)
                for scenario in scenarios:
                    active_scenario = scenario
                    if scenario.get("state") == "completed":
                        _log_line(
                            directory / scenario["logs"],
                            "resume skipped completed scenario; no reinjection",
                        )
                        continue
                    _process_scenario(
                        scenario,
                        config,
                        api,
                        supervisor,
                        repository,
                        run_timeout_seconds=run_timeout_seconds,
                        recovery_timeout_seconds=recovery_timeout_seconds,
                        max_infra_retries=max_infra_retries,
                    )
                campaign["status"] = "completed"
                campaign["interruption_cause"] = None
                repository.save_campaign()
                report = repository.save_report()
                if report["summary"]["completed_count"] != report["summary"]["total"]:
                    raise CampaignError(
                        "campaign ended without every planned scenario completed"
                    )
                print(
                    f"Campaign complete: {directory}\n"
                    f"Report: {directory / 'report.json'}"
                )
                return 0
            except KeyboardInterrupt:
                cause = (
                    "interrupted by operator; resume from the saved campaign directory"
                )
                campaign["status"] = "interrupted"
                campaign["interruption_cause"] = cause
                if active_scenario and active_scenario.get("state") != "completed":
                    if active_scenario.get("run_id"):
                        active_scenario["result"]["interrupt_stop_status"] = (
                            _stop_timed_out_run(
                                api, str(active_scenario["run_id"]), repository
                            )
                        )
                    active_scenario["state"] = "interrupted"
                    active_scenario["failure"] = cause
                    repository.save_scenario(active_scenario, "operator_interruption")
                repository.save_campaign()
                repository.save_report()
                print(
                    f"Campaign interrupted. Resume with --resume {directory}",
                    file=sys.stderr,
                )
                return 130
            except Exception as exc:
                cause = f"{type(exc).__name__}: {exc}"
                campaign["status"] = "interrupted"
                campaign["interruption_cause"] = cause
                if active_scenario and active_scenario.get("state") not in {
                    "completed",
                    "failed",
                }:
                    active_scenario["state"] = "interrupted"
                    active_scenario["failure_domain"] = getattr(
                        exc, "failure_domain", "unclassified"
                    )
                    active_scenario["failure"] = cause
                    repository.save_scenario(
                        active_scenario,
                        "scenario_interrupted",
                        {"failure_domain": active_scenario["failure_domain"]},
                    )
                repository.save_campaign()
                repository.save_report()
                _log_line(directory / "campaign.log", f"INTERRUPTED: {cause}")
                print(
                    f"Campaign stopped: {cause}\n"
                    f"Evidence/report: {directory / 'report.json'}\n"
                    f"Resume only after reviewing the cause: "
                    f"{sys.executable} -m experiment.eval.memory_hog.autonomous_campaign "
                    f"--resume {directory}",
                    file=sys.stderr,
                )
                return 1
            finally:
                try:
                    supervisor.close()
                finally:
                    signal.signal(signal.SIGTERM, previous_sigterm)


def _verify_manifest_hashes(directory: Path, manifest_dir: Path) -> None:
    hashes_path = directory / "manifest_hashes.json"
    try:
        expected = json.loads(hashes_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot verify persisted manifests: {exc}") from exc
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(manifest_dir.glob("*.yaml"))
    }
    if actual != expected:
        raise CampaignError(
            "campaign manifests changed after initialization; refusing resume"
        )


def _simulate() -> int:
    cells = forty_faults.CELLS
    lower, _ = forty_faults.diverse.duration_estimate(
        len(cells),
        timeline_seconds=60 + 30 + 270 + 120,
        non_phase_overhead_seconds=0,
        cooldown_seconds=60,
        jitter_max_seconds=0,
        setup_seconds=0,
    )
    print(
        f"Simulation only: {len(cells)} unique sequential scenarios; "
        f"{forty_faults.FAULT_SECONDS}s injection each."
    )
    for order, cell in enumerate(cells, start=1):
        print(f"  {order:02d}. {cell['chaos_type']:<18} -> {cell['service']}")
    print(
        f"Estimated timeline plus cooldown: {(lower + 59) // 60} min minimum; "
        f"cluster/collection/LLM overhead is additional."
    )
    print(
        "No cluster/API calls, manifests, campaign folders, or fault injections were made."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--simulate", action="store_true")
    modes.add_argument("--pilot", action="store_true")
    modes.add_argument(
        "--run",
        action="store_true",
        help="Run all 40 scenarios; requires a successful pilot.",
    )
    modes.add_argument(
        "--resume",
        type=Path,
        help="Resume an existing campaign without repeating completed scenarios.",
    )
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--pilot-report", type=Path)
    parser.add_argument(
        "--max-infra-retries", type=int, default=DEFAULT_MAX_INFRA_RETRIES
    )
    parser.add_argument("--run-timeout", type=int, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    parser.add_argument(
        "--recovery-timeout", type=int, default=DEFAULT_RECOVERY_TIMEOUT_SECONDS
    )
    args = parser.parse_args(argv)
    if args.max_infra_retries < 0 or args.max_infra_retries > 5:
        parser.error("--max-infra-retries must be between 0 and 5")
    if args.run_timeout < 1 or args.recovery_timeout < 1:
        parser.error("--run-timeout and --recovery-timeout must be positive")
    if args.simulate:
        if args.campaign_dir or args.pilot_report:
            parser.error("--simulate does not accept campaign paths")
        return _simulate()
    if args.resume:
        if args.campaign_dir or args.pilot_report:
            parser.error("--resume does not accept --campaign-dir or --pilot-report")
        directory = args.resume.expanduser().resolve()
        if not directory.is_dir():
            parser.error(f"campaign directory does not exist: {directory}")
        try:
            _, saved_config, _, _ = _load_campaign(directory)
        except CampaignError as exc:
            parser.error(str(exc))
        return _execute_campaign(
            directory,
            is_resume=True,
            max_infra_retries=int(
                saved_config["autonomous_campaign"]["max_infra_retries"]
            ),
            run_timeout_seconds=int(
                saved_config["autonomous_campaign"]["run_timeout_seconds"]
            ),
            recovery_timeout_seconds=int(
                saved_config["autonomous_campaign"]["recovery_timeout_seconds"]
            ),
        )

    if args.run and not args.pilot_report:
        parser.error(
            "--run requires --pilot-report from a completed successful --pilot"
        )
    if args.pilot and args.pilot_report:
        parser.error("--pilot does not accept --pilot-report")
    mode = "pilot" if args.pilot else "full"
    directory = (
        args.campaign_dir.expanduser().resolve()
        if args.campaign_dir
        else _default_campaign_directory(mode)
    )
    first_cell = dict(forty_faults.CELLS[0])
    pilot = _load_pilot_report(args.pilot_report, first_cell) if args.run else None
    try:
        _ = _create_campaign(
            mode,
            directory,
            args.max_infra_retries,
            args.run_timeout,
            args.recovery_timeout,
            pilot,
        )
    except (CampaignError, OSError, yaml.YAMLError) as exc:
        print(f"Cannot initialize campaign: {exc}", file=sys.stderr)
        return 2
    print(f"Campaign initialized: {directory}")
    if mode == "pilot":
        print(f"Pilot report will be written to: {directory / 'report.json'}")
    return _execute_campaign(
        directory,
        is_resume=False,
        max_infra_retries=args.max_infra_retries,
        run_timeout_seconds=args.run_timeout,
        recovery_timeout_seconds=args.recovery_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
