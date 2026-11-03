"""Isolated, retrospective A/B/C evaluation; never writes operational storage."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

from experiment.platform.backend.analysis.llm_client import (
    LLMClientError,
    call_llm_tool_messages,
)
from experiment.platform.backend.history_config import (
    DEFAULT_HISTORICAL_MCP_SSE_URL,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.rca_skill import (
    TOOL_ERRORS,
    Diagnosis,
    validate_diagnosis,
)

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / ".github/skills/rca-evaluation/SKILL.md"
DEFAULT_CAMPAIGN = (
    Path(__file__).resolve().parent
    / "out/controlled_campaign/controlled-20261021T091929101290Z"
)
SERVICES = ("front-end", "catalogue", "carts", "orders", "payment", "shipping", "user")
FAULTS = (
    "cpu-hog",
    "memory-hog",
    "network-latency",
    "container-kill",
    "http-status-code",
)
CATEGORIES = (
    "cpu-exhaustion",
    "memory-exhaustion",
    "network-latency",
    "pod-failure",
    "http-error",
)
TERMINAL = {"completed", "blocked", "failed", "stopped", "aborted"}
CONDITIONS = ("skill", "skill_card", "skill_card_history")
ARTIFACTS = (
    "observation_window.json",
    "metrics_raw.json",
    "traces_raw.json",
    "rca_evidence_snapshot.json",
    "controlled_report.json",
    "effective_spec.json",
)
LABEL_KEYS = {
    "destination_workload",
    "source_workload",
    "service",
    "service_name",
    "pod",
    "container",
    "namespace",
    "reason",
    "code",
    "status_code",
    "http_response_status_code",
    "operation",
    "instance",
    "__name__",
}
ModelCall = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]


class EvaluationError(ValueError):
    pass


class ModelOutputError(EvaluationError):
    def __init__(self, message: str, response: dict[str, Any]):
        super().__init__(message)
        self.response = response


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Finding(StrictModel):
    observation: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class Observations(StrictModel):
    findings: list[Finding] = Field(max_length=20)
    alternatives: list[str] = Field(min_length=2, max_length=5)
    limitations: list[str] = Field(max_length=20)


class Experience(StrictModel):
    mechanism_supported: bool
    explanation: str = Field(min_length=1, max_length=1800)
    discriminators: list[Finding] = Field(max_length=10)
    contradictions: list[Finding] = Field(max_length=10)
    limitations: list[str] = Field(max_length=20)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path: Path, value: Any) -> None:
    text = canonical(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def write_or_verify(path: Path, value: Any) -> None:
    if path.exists():
        if read_json(path) != value:
            raise EvaluationError(f"Existing artifact differs: {path}")
    else:
        write_new(path, value)


def seal(value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "sha256": digest(value)}


def load_sealed(path: Path) -> dict[str, Any]:
    value = read_json(path)
    expected = value.get("sha256")
    content = {key: item for key, item in value.items() if key != "sha256"}
    if expected != digest(content):
        raise EvaluationError(f"Integrity check failed: {path}")
    return value


def timestamp(value: str) -> float:
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise EvaluationError("Observation timestamps must have a timezone")
    return date.timestamp()


def safe_text(value: str, limit: int = 1200) -> str:
    text = re.sub(
        r"(?i)(authorization|password|secret|token|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        value,
    )
    return text[:limit]


def partition(plan: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    pairs = {(cell["service"], cell["chaos_type"]) for cell in plan}
    if len(plan) != 35 or pairs != {(s, f) for s in SERVICES for f in FAULTS}:
        raise EvaluationError(
            "Expected exactly one run for each of the 35 medium cells"
        )
    services, faults = list(SERVICES), list(FAULTS)
    rng = random.Random(seed)
    rng.shuffle(services)
    rng.shuffle(faults)
    return [
        {
            "case_id": f"case-{index + 1:03d}",
            "service": service,
            "chaos_type": fault,
            "split": "support" if (i + j) % 2 else "test",
        }
        for index, (i, j, service, fault) in enumerate(
            (i, j, service, fault)
            for i, service in enumerate(services)
            for j, fault in enumerate(faults)
        )
    ]


def initialize(
    campaign: Path,
    output: Path,
    seed: int = 20261021,
    evidence_mode: str = "live_shared",
    trace_limit: int = 5,
    continuation: Path | None = None,
    max_repairs: int = 0,
    allow_missing_trace_details: bool = False,
    history_policy: str = "validated_only",
) -> None:
    if evidence_mode not in {"live_per_condition", "live_shared"}:
        raise EvaluationError(
            "RCA evaluations require fresh historical telemetry collected via MCP; "
            "offline evidence is prohibited"
        )
    if history_policy not in {"validated_only", "qualified_examples"}:
        raise EvaluationError("Unknown historical knowledge policy")
    if max_repairs not in (0, 1):
        raise EvaluationError("At most one audited contract repair is allowed")
    if not 1 <= trace_limit <= 50:
        raise EvaluationError("Trace query limit must be between 1 and 50 per phase")
    campaign, output = campaign.resolve(), output.resolve()
    if output.exists():
        raise EvaluationError(
            "Use a new output directory; existing data is never reset"
        )
    if output == campaign or campaign in output.parents:
        raise EvaluationError("Evaluation must live outside the running campaign")
    if continuation is not None:
        continuation = continuation.resolve()
        if output == continuation or continuation in output.parents:
            raise EvaluationError("Evaluation must live outside the continuation")
    plan = read_json(campaign / "plan.json")
    cases = partition(plan, seed)
    card_path = ROOT / "experiment/platform/data/system_cards/sock-shop.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    known = sorted(card["services"])
    if not set(SERVICES).issubset(known):
        raise EvaluationError("System card is missing medium campaign services")
    skill = SKILL.read_text(encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign.name,
        "campaign_dir": str(campaign),
        "object_store": str(ROOT / "experiment/platform/data/object_store"),
        "seed": seed,
        "cases": cases,
        "contract": {
            "known_services": known,
            "possible_fault_types": list(FAULTS),
            "allowed_categories": sorted(
                set(CATEGORIES) | set(card.get("allowed_fault_categories", []))
            ),
        },
        "context": {
            "system_id": card.get("system_id"),
            "version": card.get("version"),
            "platform": card.get("platform"),
            "services": card["services"],
        },
        "skill": skill,
        "source_plan_sha256": digest(plan),
        "source_card_sha256": digest(card),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "design": "retrospective-fixed-evidence-checkerboard-v1",
        "evidence_mode": evidence_mode,
        "history_policy": history_policy,
        "trace_limit_per_phase": trace_limit,
        "max_repairs": max_repairs,
        "allow_missing_trace_details": allow_missing_trace_details,
        "limitations": [
            "One intervention per cell; no sham controls.",
            "Not a replication of the live operational skill.",
            "Context and experiences are varied only for final analysis.",
        ],
    }
    if history_policy == "qualified_examples":
        manifest["limitations"].append(
            "Historical cases preserve experimental assignments and gate/model "
            "assessments as qualified context, not causal ground truth."
        )
    if evidence_mode == "live_per_condition":
        manifest["design"] = "online-exploratory-historical-windows-v1"
        manifest["limitations"].append(
            "Backends are queried separately per condition/repetition; evidence is not identical across A/B/C."
        )
    elif evidence_mode == "live_shared":
        manifest["design"] = "paired-shared-historical-evidence-v1"
        manifest["limitations"].append(
            "Historical telemetry is collected once per case before inference and "
            "reused unchanged across conditions and repetitions; retention gaps remain."
        )
    if allow_missing_trace_details:
        if evidence_mode != "live_shared":
            raise EvaluationError(
                "Missing trace detail policy requires live_shared mode"
            )
        manifest["limitations"].append(
            "Explicitly accepted trace-detail HTTP 404 gaps remain unavailable in "
            "the same packet for every condition; they cannot corroborate a cause."
        )
    if continuation is not None:
        manifest["continuation_dir"] = str(continuation)
        with campaign_idle(manifest):
            _, segments = linked_rows(manifest)
            manifest["source_segments"] = segments
        manifest["limitations"].append(
            "Linked environment segments are heterogeneous; aggregate results are exploratory. "
            "Report segment-specific scores; support retrieval may cross linked segments."
        )
    output.mkdir(parents=True, mode=0o700)
    write_new(output / "manifest.json", seal(manifest))


def load_manifest(output: Path) -> dict[str, Any]:
    manifest = load_sealed(output / "manifest.json")
    if manifest.get("evidence_mode") not in {"live_shared", "live_per_condition"}:
        raise EvaluationError(
            "RCA evaluations require fresh historical telemetry collected via MCP; "
            "offline evidence is prohibited"
        )
    if (
        manifest["evaluator_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise EvaluationError(
            "Evaluator code changed after initialization; use a new directory"
        )
    return manifest


@contextmanager
def campaign_idle(manifest: dict[str, Any]) -> Iterator[None]:
    if manifest.get("continuation_dir"):
        directories = sorted(
            {Path(manifest["campaign_dir"]), Path(manifest["continuation_dir"])}
        )
        with ExitStack() as stack:
            for directory in directories:
                lock_path = directory / ".campaign.lock"
                if not lock_path.is_file():
                    raise EvaluationError("Campaign lock is missing")
                handle = stack.enter_context(lock_path.open("r"))
                try:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise EvaluationError(
                        "Campaign is running; no evaluation work started"
                    ) from exc
            linked_rows(manifest)
            yield
        return
    campaign = Path(manifest["campaign_dir"])
    lock_path = campaign / ".campaign.lock"
    if not lock_path.exists():
        raise EvaluationError("Campaign lock is missing; cannot prove campaign is idle")
    with lock_path.open("r") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvaluationError(
                "Campaign is running; no evaluation work started"
            ) from exc
        try:
            with (campaign / "runs.csv").open(
                newline="", encoding="utf-8"
            ) as rows_file:
                rows = list(csv.DictReader(rows_file))
            if len(rows) != 35 or any(
                row.get("status") not in TERMINAL for row in rows
            ):
                raise EvaluationError(
                    "Campaign has unfinished cells; wait for completion"
                )
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def linked_rows(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parent = Path(manifest["campaign_dir"]).resolve()
    child = Path(manifest["continuation_dir"]).resolve()
    if parent == child:
        raise EvaluationError("Continuation must be a distinct campaign")
    config = yaml.safe_load((child / "config.yaml").read_text(encoding="utf-8"))
    link = config.get("continuation", {})
    if (
        link.get("parent_campaign_id") != parent.name
        or Path(link.get("parent_directory", "")).resolve() != parent
        or config.get("experiment_campaign_id") != child.name
    ):
        raise EvaluationError("Continuation does not belong to the parent workflow")
    for name in ("config.yaml", "plan.json", "ledger.jsonl"):
        if hashlib.sha256((parent / name).read_bytes()).hexdigest() != link.get(
            "source_sha256", {}
        ).get(name):
            raise EvaluationError("Continuation parent source hashes disagree")
    parent_plan = read_json(parent / "plan.json")
    partition(parent_plan, manifest["seed"])
    if digest(parent_plan) != manifest["source_plan_sha256"]:
        raise EvaluationError("Campaign plan changed after initialization")
    parent_config = yaml.safe_load((parent / "config.yaml").read_text(encoding="utf-8"))
    if (
        parent_config.get("experiment_campaign_id") != parent.name
        or parent_config.get("test_experiments") != parent_plan
    ):
        raise EvaluationError("Parent campaign config disagrees with its plan")
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    segments = []
    expected_pending = set()
    for directory in (parent, child):
        plan = read_json(directory / "plan.json")
        if directory == child and config.get("test_experiments") != plan:
            raise EvaluationError("Continuation config disagrees with its plan")
        pairs = {(cell["service"], cell["chaos_type"]) for cell in plan}
        if (
            len(pairs) != len(plan)
            or any(cell.get("replica_idx") != 0 for cell in plan)
            or (directory == child and pairs != expected_pending)
        ):
            raise EvaluationError(
                "Continuation must cover exactly the pending parent cells"
            )
        with (directory / "runs.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if (
            len(rows) != len(plan)
            or {(r["service"], r["chaos_type"]) for r in rows} != pairs
        ):
            raise EvaluationError("Campaign contains duplicate/missing cells")
        ledger = {
            entry["cell_id"]: entry
            for entry in (
                json.loads(line)
                for line in (directory / "ledger.jsonl").read_text().splitlines()
                if line.strip()
            )
        }
        plan_ids = {f"{cell['service']}-{cell['chaos_type']}-r0" for cell in plan}
        if set(ledger) - plan_ids or any(
            entry.get("status") not in TERMINAL for entry in ledger.values()
        ):
            raise EvaluationError("Campaign has unresolved or foreign ledger cells")
        for row in rows:
            pair = (row["service"], row["chaos_type"])
            cell_id = f"{row['service']}-{row['chaos_type']}-r0"
            entry = ledger.get(cell_id)
            if (
                row.get("replica_idx") != "0"
                or (
                    row["status"] == "not_run"
                    and (entry is not None or row.get("run_id") or directory == child)
                )
                or (
                    row["status"] != "not_run"
                    and (
                        row["status"] not in TERMINAL
                        or entry is None
                        or entry.get("status") != row["status"]
                        or entry.get("run_id") != row.get("run_id")
                    )
                )
            ):
                raise EvaluationError("Campaign CSV disagrees with terminal ledger")
            selected[pair] = {
                **row,
                "source_campaign_id": directory.name,
                "source_campaign_dir": str(directory),
            }
            if directory == parent and row["status"] != "completed":
                expected_pending.add(pair)
        segments.append(
            {
                "campaign_id": directory.name,
                "campaign_dir": str(directory),
                "source_sha256": {
                    name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                    for name in ("config.yaml", "plan.json", "ledger.jsonl", "runs.csv")
                },
                "planned": len(plan),
                "completed": sum(row["status"] == "completed" for row in rows),
            }
        )
    if manifest.get("source_segments") not in (None, segments):
        raise EvaluationError("Linked campaign sources changed after initialization")
    return list(selected.values()), segments


@contextmanager
def evaluation_lock(output: Path) -> Iterator[None]:
    with (output / ".evaluation.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvaluationError("Another evaluation stage is running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def phase_windows(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
    windows = {
        phase: {"start": value["start"], "end": value["end"]}
        for phase, value in raw["phases"].items()
        if phase in {"baseline", "fault", "post"}
    }
    if not {"baseline", "fault"}.issubset(windows):
        raise EvaluationError("Missing baseline/fault windows")
    for window in windows.values():
        if timestamp(window["start"]) >= timestamp(window["end"]):
            raise EvaluationError("Invalid observation window")
    return windows


def metric_evidence(
    raw: dict[str, Any], windows: dict[str, Any]
) -> list[dict[str, Any]]:
    evidence = []
    for metric, payload in sorted(raw.get("data", {}).items()):
        series = payload.get("data", {}).get("result", [])
        if payload.get("status") != "success" or not series:
            evidence.append(
                {
                    "family": "metrics",
                    "metric": metric,
                    "status": (
                        "tool_error"
                        if payload.get("status") != "success"
                        else "no_data"
                    ),
                    "phases": {},
                    "observation_count": 0,
                    "fault_observation_count": 0,
                }
            )
            continue
        for item in series:
            labels = {
                key: safe_text(str(value), 200)
                for key, value in item.get("metric", {}).items()
                if key in LABEL_KEYS
            }
            if any(
                "litmus" in value or "go-runner" in value for value in labels.values()
            ):
                continue
            phases = {}
            invalid = 0
            points = []
            for pair in item.get("values", []):
                t, value = float(pair[0]), float(pair[1])
                if math.isfinite(value) and math.isfinite(t):
                    points.append((t, value))
                else:
                    invalid += 1
            for phase, window in windows.items():
                values = [
                    v
                    for t, v in points
                    if timestamp(window["start"]) <= t <= timestamp(window["end"])
                ]
                phases[phase] = {
                    "sample_count": len(values),
                    "mean": statistics.fmean(values) if values else None,
                    "min": min(values) if values else None,
                    "max": max(values) if values else None,
                    "first": values[0] if values else None,
                    "last": values[-1] if values else None,
                }
            fault_count = phases["fault"]["sample_count"]
            evidence.append(
                {
                    "family": "metrics",
                    "metric": metric,
                    "labels": labels,
                    "phases": phases,
                    "invalid_samples": invalid,
                    "status": "ok" if fault_count else "no_data",
                    "observation_count": sum(
                        p["sample_count"] for p in phases.values()
                    ),
                    "fault_observation_count": fault_count,
                    "aggregation": "phase summary; raw data remains in sealed sources",
                }
            )
    return evidence


def log_evidence(
    raw: dict[str, Any] | None,
    windows: dict[str, Any],
    known_services: list[str] | None = None,
) -> list[dict[str, Any]]:
    evidence = []
    catalog = raw.get("services", {}) if raw is not None else {}
    services = sorted(set(known_services or []) | set(catalog)) or [None]
    for service in services:
        payload = catalog.get(service, {})
        for phase in windows:
            value = payload.get("phases", {}).get(phase, {})
            if raw is None:
                status = "missing_artifact"
            elif not value:
                status = (
                    raw.get("status")
                    if raw.get("status")
                    in {
                        "tool_error",
                        "tool_unavailable",
                        "invalid_config",
                    }
                    else "missing_coverage"
                )
            else:
                status = value.get("status", "missing_coverage")
            lines = []
            rejected = 0
            for line in value.get("sample_lines", []):
                at = line.get("timestamp")
                if not at or not (
                    timestamp(windows[phase]["start"])
                    <= timestamp(at)
                    <= timestamp(windows[phase]["end"])
                ):
                    rejected += 1
                    continue
                if any(
                    marker in str(line.get("container", "")) + str(line.get("pod", ""))
                    for marker in ("litmus", "go-runner", "chaos")
                ):
                    rejected += 1
                    continue
                message = str(line.get("message", ""))
                lines.append(
                    {
                        "timestamp": at,
                        "container": safe_text(str(line.get("container", "")), 200),
                        "message": safe_text(message),
                        "message_truncated": len(message) > 1200,
                    }
                )
            evidence.append(
                {
                    "family": "logs",
                    "service": service,
                    "phase": phase,
                    "status": status,
                    "coverage": value.get("coverage", {}),
                    "sample_lines": lines,
                    "rejected_lines": rejected,
                    "observation_count": len(lines),
                    "fault_observation_count": len(lines) if phase == "fault" else 0,
                }
            )
    return evidence


def trace_evidence(
    raw: dict[str, Any] | None, windows: dict[str, Any]
) -> list[dict[str, Any]]:
    if not raw or not raw.get("traces"):
        return [
            {
                "family": "traces",
                "status": (
                    "missing_artifact"
                    if raw is None
                    else (
                        "tool_error"
                        if raw.get("collection", {}).get("search_error")
                        else "unsupported_schema" if raw.get("count") else "no_data"
                    )
                ),
                "observation_count": 0,
                "fault_observation_count": 0,
            }
        ]
    evidence = []
    for trace in raw["traces"]:
        item: dict[str, Any] = {
            "family": "traces",
            "trace_id": trace.get("trace_id"),
            "status": "unsupported_schema",
            "observation_count": 0,
            "fault_observation_count": 0,
        }
        start_ns = trace.get("start_time_unix_nano")
        duration_ms = trace.get("duration_ms")
        features = trace.get("features", {})
        if start_ns is None or duration_ms is None or not isinstance(features, dict):
            evidence.append(item)
            continue
        start = float(start_ns) / 1e9
        duration = float(duration_ms) / 1000
        if not math.isfinite(start) or not math.isfinite(duration) or duration < 0:
            raise EvaluationError("Invalid trace timing")
        phase = next(
            (
                name
                for name, window in windows.items()
                if timestamp(window["start"]) <= start
                and start + duration <= timestamp(window["end"])
            ),
            None,
        )
        item["phase"] = phase
        if (
            features.get("status") == "unavailable"
            and features.get("http_status") == 404
        ):
            item["status"] = "unavailable"
            item["reason"] = "trace_detail_http_404"
        elif phase is None:
            item["status"] = "outside_or_crosses_phase"
        elif features.get("error"):
            item["status"] = "tool_error"
        else:
            span_count = int(features.get("span_count", 0))
            item.update(
                {
                    "status": "ok" if span_count else "no_data",
                    "start_time_unix_nano": str(start_ns),
                    "duration_ms": duration_ms,
                    "root_service": safe_text(str(trace.get("root_service", "")), 200),
                    "observation_count": span_count,
                    "fault_observation_count": span_count if phase == "fault" else 0,
                }
            )
            for field in ("critical_path_raw", "hot_spans", "error_spans"):
                values = features.get(field, [])
                item[field] = [
                    {
                        key: safe_text(str(value)) if isinstance(value, str) else value
                        for key, value in row.items()
                        if key
                        in {
                            "service",
                            "name",
                            "duration_ms",
                            "exclusive_duration_ms",
                            "inclusive_duration_ms",
                            "message",
                        }
                        and isinstance(value, (str, int, float))
                    }
                    for row in values[:20]
                ]
                item[f"{field}_omitted"] = max(0, len(values) - 20)
        evidence.append(item)
    return evidence


def make_bundle(
    case_id: str,
    sources: dict[str, Any],
    known_services: list[str] | None = None,
) -> dict[str, Any]:
    windows = phase_windows(sources["observation_window.json"])
    evidence = metric_evidence(sources["metrics_raw.json"], windows)
    evidence.extend(
        log_evidence(sources.get("rca_evidence_snapshot.json"), windows, known_services)
    )
    evidence.extend(trace_evidence(sources.get("traces_raw.json"), windows))
    for index, item in enumerate(evidence, 1):
        item["id"] = f"E{index:04d}"
    return {
        "case_id": case_id,
        "windows": windows,
        "evidence": evidence,
        "phase_sources": {
            key: value
            for key, value in sources["observation_window.json"]
            .get("phase_sources", {})
            .items()
            if key in windows
        },
        "limitations": [
            "Phase aggregates are not event ordering or mechanism proof.",
            "Logs are bounded snapshots; application/database streams may be mixed.",
            "No live telemetry or intervention artifacts are available to the model.",
            "LLM trace summaries are excluded; traces crossing phase boundaries are not attributed.",
        ],
    }


def validate_source_identity(
    manifest: dict[str, Any],
    row: dict[str, Any],
    sources: dict[str, Any],
) -> None:
    expected_experiment = f"{manifest['campaign_id']}-{row['service']}-{row['chaos_type']}-r{row['replica_idx']}"
    window = sources["observation_window.json"]
    spec = sources["effective_spec.json"]
    if window.get("run_id") != row["run_id"]:
        raise EvaluationError(
            "Observation window run_id disagrees with campaign ledger"
        )
    actual_experiment = window.get("experiment_id")
    if actual_experiment != expected_experiment:
        ledger_path = Path(manifest["campaign_dir"]) / "ledger.jsonl"
        if not ledger_path.is_file():
            raise EvaluationError(
                "Retried experiment identity requires campaign ledger provenance"
            )
        cell_id = f"{row['service']}-{row['chaos_type']}-r{row['replica_idx']}"
        entries = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        latest = next(
            (entry for entry in reversed(entries) if entry.get("cell_id") == cell_id),
            None,
        )
        attempt = latest.get("attempt") if latest else None
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 2
            or latest.get("run_id") != row["run_id"]
            or latest.get("status") != "completed"
            or actual_experiment != f"{expected_experiment}-attempt-{attempt}"
        ):
            raise EvaluationError(
                "Artifact experiment identity disagrees with campaign ledger"
            )
        expected_experiment = actual_experiment
    if (
        window.get("experiment_id") != expected_experiment
        or spec.get("experiment", {}).get("id") != expected_experiment
        or (row.get("experiment_id") and row["experiment_id"] != expected_experiment)
    ):
        raise EvaluationError(
            "Artifact experiment identity disagrees with campaign cell"
        )
    logs = sources.get("rca_evidence_snapshot.json")
    if logs is not None and logs.get("run_id") != row["run_id"]:
        raise EvaluationError("Log snapshot run_id disagrees with campaign ledger")


def freeze(output: Path) -> dict[str, Any]:
    manifest = load_manifest(output)
    with evaluation_lock(output), campaign_idle(manifest):
        if (output / "snapshot.json").exists():
            raise EvaluationError(
                "Snapshot already exists; use a new evaluation directory"
            )
        campaign = Path(manifest["campaign_dir"])
        if digest(read_json(campaign / "plan.json")) != manifest["source_plan_sha256"]:
            raise EvaluationError("Campaign plan changed after initialization")
        if manifest.get("continuation_dir"):
            rows, _ = linked_rows(manifest)
        else:
            with (campaign / "runs.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        by_pair = {(row["service"], row["chaos_type"]): row for row in rows}
        if len(by_pair) != 35:
            raise EvaluationError("Campaign contains duplicate/missing cells")
        bundles, labels, exclusions, provenance = {}, {}, [], {}
        seen_runs: set[str] = set()
        for case in manifest["cases"]:
            row = by_pair[(case["service"], case["chaos_type"])]
            case_id = case["case_id"]
            if row["status"] != "completed":
                exclusions.append(
                    {
                        "case_id": case_id,
                        "split": case["split"],
                        "status": row["status"],
                    }
                )
                continue
            run_id = row["run_id"]
            if not re.fullmatch(r"run-[a-zA-Z0-9-]+", run_id) or run_id in seen_runs:
                raise EvaluationError("Invalid or duplicate run identity")
            seen_runs.add(run_id)
            root = Path(manifest["object_store"]) / run_id
            sources = {}
            for name in ARTIFACTS:
                path = root / name
                if path.exists():
                    sources[name] = read_json(path)
            required = {
                "observation_window.json",
                "metrics_raw.json",
                "controlled_report.json",
                "effective_spec.json",
            }
            if not required.issubset(sources):
                raise EvaluationError(f"Missing required artifacts for {case_id}")
            source_manifest = {
                **manifest,
                "campaign_id": row.get("source_campaign_id", manifest["campaign_id"]),
                "campaign_dir": row.get(
                    "source_campaign_dir", manifest["campaign_dir"]
                ),
            }
            validate_source_identity(source_manifest, row, sources)
            report = sources["controlled_report.json"]
            if (
                report.get("service") != row["service"]
                or report.get("fault") != row["chaos_type"]
            ):
                raise EvaluationError(f"Intervention labels disagree for {case_id}")
            bundles[case_id] = make_bundle(
                case_id,
                sources,
                manifest["contract"]["known_services"],
            )
            mechanism = report.get("gates", {}).get("mechanism_observed", {})
            passed = mechanism is True or (
                isinstance(mechanism, dict) and mechanism.get("passed") is True
            )
            labels[case_id] = {
                "service": row["service"],
                "fault_type": row["chaos_type"],
                "mechanism_validated": passed,
            }
            provenance[case_id] = {
                "run_id": run_id,
                "source_campaign_id": source_manifest["campaign_id"],
                "sources": {name: digest(value) for name, value in sources.items()},
            }
            write_or_verify(
                output / "private/sources" / f"{case_id}.json", seal(sources)
            )
        snapshot = seal(
            {
                "manifest_sha256": manifest["sha256"],
                "campaign_ledger_sha256": (
                    hashlib.sha256((campaign / "ledger.jsonl").read_bytes()).hexdigest()
                    if (campaign / "ledger.jsonl").is_file()
                    else None
                ),
                "bundles": bundles,
                "exclusions": exclusions,
                "case_sources": {
                    case_id: item["source_campaign_id"]
                    for case_id, item in provenance.items()
                },
                "source_segments": manifest.get("source_segments", []),
            }
        )
        write_or_verify(
            output / "private/labels.json",
            seal(
                {
                    "manifest_sha256": manifest["sha256"],
                    "snapshot_sha256": snapshot["sha256"],
                    "labels": labels,
                    "provenance": provenance,
                }
            ),
        )
        write_new(output / "snapshot.json", snapshot)
        return snapshot


def load_snapshot(output: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    snapshot = load_sealed(output / "snapshot.json")
    if snapshot["manifest_sha256"] != manifest["sha256"]:
        raise EvaluationError("Snapshot belongs to a different manifest")
    return snapshot


def load_labels(
    output: Path, manifest: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    private = load_sealed(output / "private/labels.json")
    if (
        private["manifest_sha256"] != manifest["sha256"]
        or private["snapshot_sha256"] != snapshot["sha256"]
    ):
        raise EvaluationError("Private labels belong to a different workflow snapshot")
    return private["labels"]


def collect_live_sources(
    manifest: dict[str, Any],
    bundle: dict[str, Any],
    observability: dict[str, Any],
    client_factory: Callable[..., Any] = MCPToolClient,
    historical_observability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query historical windows only; failures never fall back to stored telemetry."""
    windows = bundle["windows"]
    metrics_config = observability.get("metrics", {})
    traces_config = observability.get("traces", {})
    history = historical_observability or {}
    metric_url = trace_url = (
        history.get("mcp_sse_url") or DEFAULT_HISTORICAL_MCP_SSE_URL
    )
    timeout_seconds = history.get("timeout_seconds", 90)
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise EvaluationError(
            "Historical MCP timeout must be a positive integer"
        )
    for url in (metric_url, trace_url):
        if not isinstance(url, str) or urlparse(url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise EvaluationError(
                "Live telemetry requires explicit local MCP endpoints"
            )
    if not metrics_config.get("queries"):
        raise EvaluationError("Live telemetry configuration has no metric queries")
    calls: list[dict[str, Any]] = []

    def query(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = {
            key: value for key, value in arguments.items() if value is not None
        }
        record: dict[str, Any] = {
            "endpoint": url,
            "timeout_seconds": timeout_seconds,
            "tool": name,
            "arguments": arguments,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        started = time.monotonic()
        try:
            result = client_factory(
                sse_url=url, timeout_seconds=timeout_seconds
            ).call_tool_json(
                name,
                arguments,
            )
            if not isinstance(result, dict):
                raise EvaluationError("MCP returned a non-object response")
            if (
                manifest.get("allow_missing_trace_details") is True
                and name == "tempo_analyze_trace"
                and result.get("status") == "error"
                and str(result.get("error", "")).startswith(
                    "Client error '404 Not Found' for url "
                )
            ):
                result = {
                    "status": "unavailable",
                    "http_status": 404,
                    "reason": "trace_detail_http_404",
                    "backend_response": result,
                }
                print(
                    f"Explicit missing trace detail: {arguments['trace_id']} (HTTP 404)",
                    file=sys.stderr,
                    flush=True,
                )
            if (
                result.get("isError")
                or result.get("error")
                or result.get("status")
                in {
                    "error",
                    "tool_error",
                    "tool_unavailable",
                    "invalid_config",
                }
            ):
                raise EvaluationError(
                    f"MCP returned an error: {safe_text(canonical(result))}"
                )
        except TOOL_ERRORS as exc:
            result = {"status": "tool_error", "error": f"{type(exc).__name__}: {exc}"}
            print(f"Live telemetry {name}: {result['error']}", file=sys.stderr)
        record["response"] = result
        record["elapsed_seconds"] = time.monotonic() - started
        calls.append(record)
        return result

    start = min((window["start"] for window in windows.values()), key=timestamp)
    end = max((window["end"] for window in windows.values()), key=timestamp)
    metric_data = {}
    for spec in metrics_config["queries"]:
        metric_id = spec.get("id") or spec.get("name")
        promql = spec.get("query") or spec.get("query_ref")
        if not metric_id or not promql or metric_id in metric_data:
            raise EvaluationError("Invalid or duplicate live metric query")
        metric_data[metric_id] = query(
            metric_url,
            "prometheus_range_query",
            {
                "query": promql,
                "start": start,
                "end": end,
                "step": "5s",
            },
        )
    arguments: dict[str, Any] = {
        "services": manifest["contract"]["known_services"],
        "namespace": manifest["context"]["platform"]["namespace"],
        "max_total_lines": 1200,
    }
    for phase, window in windows.items():
        arguments[f"{phase}_start"] = window["start"]
        arguments[f"{phase}_end"] = window["end"]
    logs = query(metric_url, "capture_run_log_snapshot", arguments)
    traces = []
    trace_errors = []
    seen: set[str] = set()
    for window in windows.values():
        search = query(
            trace_url,
            "tempo_search_traces",
            {
                "query": traces_config.get("query"),
                "service_name": traces_config.get("service_name"),
                "start": window["start"],
                "end": window["end"],
                "limit": min(
                    int(traces_config.get("limit", 50)),
                    manifest["trace_limit_per_phase"],
                ),
            },
        )
        if search.get("status") in {"tool_error", "tool_unavailable"}:
            trace_errors.append(search.get("error", search["status"]))
        for trace in search.get("traces", []):
            trace_id = trace.get("traceID") or trace.get("traceId")
            if not isinstance(trace_id, str) or trace_id in seen:
                continue
            seen.add(trace_id)
            features = query(trace_url, "tempo_analyze_trace", {"trace_id": trace_id})
            if features.get("status") in {"tool_error", "tool_unavailable"}:
                features["error"] = features.get("error", features["status"])
            traces.append(
                {
                    "trace_id": trace_id,
                    "root_service": trace.get("rootServiceName"),
                    "start_time_unix_nano": trace.get("startTimeUnixNano"),
                    "duration_ms": trace.get("durationMs"),
                    "features": features,
                }
            )
    return {
        "observation_window.json": {
            "phases": windows,
            "phase_sources": bundle.get("phase_sources", {}),
        },
        "metrics_raw.json": {"data": metric_data},
        "rca_evidence_snapshot.json": logs,
        "traces_raw.json": {
            "count": len(traces),
            "traces": traces,
            "collection": {"search_error": "; ".join(trace_errors) or None},
        },
        "live_collection": {
            "started_at": calls[0]["started_at"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "historical_windows": windows,
            "calls": calls,
            "fallback_to_stored_telemetry": False,
            "trace_limit_per_phase": manifest["trace_limit_per_phase"],
        },
    }


def stage_bundle(
    output: Path,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    case_id: str,
    collection_key: str,
) -> dict[str, Any]:
    if manifest["evidence_mode"] == "live_shared":
        index = load_shared_evidence(output, manifest, snapshot)
        record = load_sealed(
            output / "private/live_evidence" / f"shared-{case_id}.json"
        )
        if record["sha256"] != index["records"][case_id]:
            raise EvaluationError("Shared evidence changed after collection")
        return record["bundle"]
    if manifest["evidence_mode"] == "live_per_condition":
        return live_bundle(output, manifest, snapshot, case_id, collection_key)
    raise EvaluationError("RCA evaluation has no valid online evidence mode")


def live_bundle(
    output: Path,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    case_id: str,
    collection_key: str,
) -> dict[str, Any]:
    original = snapshot["bundles"][case_id]
    path = output / "private/live_evidence" / f"{collection_key}.json"
    if path.exists():
        record = load_sealed(path)
        if (
            record["manifest_sha256"] != manifest["sha256"]
            or record["snapshot_sha256"] != snapshot["sha256"]
            or record["case_id"] != case_id
            or record["collection_key"] != collection_key
        ):
            raise EvaluationError("Live evidence belongs to another attempt/workflow")
        return record["bundle"]
    private_sources = load_sealed(output / "private/sources" / f"{case_id}.json")
    effective_spec = private_sources["effective_spec.json"]
    config = effective_spec["observability"]
    sources = collect_live_sources(
        manifest,
        original,
        config,
        historical_observability=effective_spec.get("historical_observability"),
    )
    current = make_bundle(case_id, sources, manifest["contract"]["known_services"])
    current["evidence_origin"] = "live_historical_query"
    current["collection_started_at"] = sources["live_collection"]["started_at"]
    current["collection_completed_at"] = sources["live_collection"]["completed_at"]
    current["limitations"] = [
        limit
        for limit in current["limitations"]
        if not limit.startswith("No live telemetry")
    ] + [
        (
            "Historical windows queried once; this sealed bundle is shared across all conditions."
            if manifest["evidence_mode"] == "live_shared"
            else "Historical windows re-queried now; retention/indexing may differ between conditions."
        ),
        "Unavailable live evidence is not replaced with stored telemetry.",
    ]
    write_new(
        path,
        seal(
            {
                "manifest_sha256": manifest["sha256"],
                "snapshot_sha256": snapshot["sha256"],
                "case_id": case_id,
                "collection_key": collection_key,
                "bundle": current,
                "sources": sources,
            }
        ),
    )
    return current


def load_shared_evidence(
    output: Path, manifest: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    path = output / "shared_evidence.json"
    if not path.exists():
        raise EvaluationError("Run collect before inference in live_shared mode")
    index = load_sealed(path)
    if (
        index["manifest_sha256"] != manifest["sha256"]
        or index["snapshot_sha256"] != snapshot["sha256"]
        or set(index["records"]) != set(snapshot["bundles"])
    ):
        raise EvaluationError("Shared evidence index belongs to different inputs")
    if index["collection_error_cases"]:
        raise EvaluationError(
            "Shared collection has backend errors; inspect shared_evidence.json. "
            "Do not replace failed queries with stored telemetry."
        )
    initialized_at = timestamp(manifest["created_at"])
    if timestamp(index["completed_at"]) < initialized_at:
        raise EvaluationError(
            "Shared evidence predates this evaluation; collect historical telemetry "
            "again through MCP"
        )
    for case_id, expected_sha256 in index["records"].items():
        record = load_sealed(
            output / "private/live_evidence" / f"shared-{case_id}.json"
        )
        if record["sha256"] != expected_sha256:
            raise EvaluationError(
                f"{case_id}: shared evidence changed after collection"
            )
        bundle = record.get("bundle") or {}
        collection = (record.get("sources") or {}).get("live_collection") or {}
        if (
            bundle.get("evidence_origin") != "live_historical_query"
            or collection.get("fallback_to_stored_telemetry") is not False
            or timestamp(bundle["collection_started_at"]) < initialized_at
        ):
            raise EvaluationError(
                f"{case_id}: evidence was not freshly collected via MCP for this evaluation"
            )
    return index


def collect_shared(output: Path) -> dict[str, Any]:
    manifest = load_manifest(output)
    if manifest["evidence_mode"] != "live_shared":
        raise EvaluationError("collect requires live_shared mode")
    with evaluation_lock(output), campaign_idle(manifest):
        snapshot = load_snapshot(output, manifest)
        if (output / "shared_evidence.json").exists():
            return load_shared_evidence(output, manifest, snapshot)
        records, coverage, errors = {}, {}, []
        for case_id in sorted(snapshot["bundles"]):
            bundle = live_bundle(
                output, manifest, snapshot, case_id, f"shared-{case_id}"
            )
            record = load_sealed(
                output / "private/live_evidence" / f"shared-{case_id}.json"
            )
            records[case_id] = record["sha256"]
            coverage[case_id] = {
                family: {
                    status: sum(
                        e["family"] == family and e["status"] == status
                        for e in bundle["evidence"]
                    )
                    for status in sorted(
                        {
                            e["status"]
                            for e in bundle["evidence"]
                            if e["family"] == family
                        }
                    )
                }
                for family in ("metrics", "logs", "traces")
            }
            if any(
                call["response"].get("status") == "tool_error"
                for call in record["sources"]["live_collection"]["calls"]
            ):
                errors.append(case_id)
            print(f"Collected shared evidence: {case_id}", flush=True)
        index = seal(
            {
                "manifest_sha256": manifest["sha256"],
                "snapshot_sha256": snapshot["sha256"],
                "records": records,
                "coverage": coverage,
                "collection_error_cases": errors,
                "unavailable_trace_details": {
                    case_id: coverage[case_id]["traces"].get("unavailable", 0)
                    for case_id in coverage
                    if coverage[case_id]["traces"].get("unavailable", 0)
                },
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_new(output / "shared_evidence.json", index)
        return load_shared_evidence(output, manifest, snapshot)


def import_shared_evidence(output: Path, source_output: Path) -> dict[str, Any]:
    raise EvaluationError(
        "Evidence from another evaluation cannot be reused; collect historical "
        "telemetry again through MCP"
    )


def audited_model(output: Path, call: ModelCall, settings: dict[str, Any]) -> ModelCall:
    config_path = output / "model_settings.json"
    if config_path.exists():
        if load_sealed(config_path)["settings"] != settings:
            raise EvaluationError(
                "Model settings changed; use a new evaluation directory"
            )
    else:
        write_new(config_path, seal({"settings": settings}))

    def audited(
        messages: list[dict[str, Any]], schema: dict[str, Any]
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "settings": settings,
            "messages": messages,
            "schema": schema,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        started = time.monotonic()
        try:
            response = call(messages, schema)
            record["response"] = response
            return response
        except (ValueError, LLMClientError) as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            write_new(
                output / "private/model_calls" / f"{uuid.uuid4().hex}.json",
                seal(record),
            )

    return audited


def model_json(
    call: ModelCall,
    messages: list[dict[str, Any]],
    schema: type[BaseModel],
    *,
    evidence: list[dict[str, Any]] | None = None,
    contract: dict[str, Any] | None = None,
    max_repairs: int = 0,
    audit_prefix: Path | None = None,
) -> tuple[BaseModel, dict[str, Any]]:
    output_schema = schema.model_json_schema()
    if evidence is not None:
        ids = sorted(item["id"] for item in evidence)

        def constrain(node: Any) -> None:
            if isinstance(node, dict):
                for name, field in node.get("properties", {}).items():
                    if name in {
                        "evidence_ids",
                        "supporting_evidence",
                        "contradicting_evidence",
                    }:
                        field["items"] = {"type": "string", "enum": ids}
                for value in node.values():
                    constrain(value)
            elif isinstance(node, list):
                for value in node:
                    constrain(value)

        constrain(output_schema)
    if schema is Diagnosis and contract is not None:
        output_schema["properties"]["primary_service"] = {
            "anyOf": [
                {"type": "string", "enum": contract["known_services"]},
                {"type": "null"},
            ]
        }
        for field, values in (
            ("fault_type", contract["possible_fault_types"]),
            ("category", contract["allowed_categories"]),
        ):
            output_schema["properties"][field] = {"type": "string", "enum": values}
        output_schema["required"] = sorted(
            set(output_schema["required"]) | {"fault_type"}
        )
    current_messages = list(messages)
    attempts = []
    for attempt in range(max_repairs + 1):
        fingerprint = digest({"messages": current_messages, "schema": output_schema})
        path = (
            audit_prefix.with_name(f"{audit_prefix.name}-{attempt}.json")
            if audit_prefix is not None
            else None
        )
        if path is not None and path.exists():
            record = load_sealed(path)
            if record["input_sha256"] != fingerprint:
                raise EvaluationError("Cached contract attempt has different inputs")
        else:
            record = {"input_sha256": fingerprint}
            try:
                record["response"] = call(current_messages, output_schema)
            except (ValueError, LLMClientError, TimeoutError) as exc:
                record["error"] = f"{type(exc).__name__}: {safe_text(str(exc), 4000)}"
            if path is not None:
                write_new(path, seal(record))
        response = record.get("response", {})
        error = record.get("error")
        parsed = None
        if error is None:
            try:
                content = response.get("content")
                if response.get("tool_calls") or not isinstance(content, str):
                    raise EvaluationError("Model must return JSON, not tool calls")
                parsed = schema.model_validate(json.loads(content))
                value = parsed.model_dump()
                if evidence is not None:
                    check_finding_refs(value, evidence)
                if schema is Diagnosis and contract is not None:
                    if (
                        value["fault_type"] not in contract["possible_fault_types"]
                        or value["category"] not in contract["allowed_categories"]
                        or (
                            value["primary_service"] is not None
                            and value["primary_service"]
                            not in contract["known_services"]
                        )
                    ):
                        raise EvaluationError(
                            "Diagnosis selected an invalid contract option"
                        )
            except ValueError as exc:
                error = f"{type(exc).__name__}: {safe_text(str(exc), 4000)}"
        attempts.append(
            {"attempt": attempt, "error": error, "usage": response.get("usage", {})}
        )
        combined = {
            **response,
            "usage": {
                key: sum(item["usage"].get(key, 0) or 0 for item in attempts)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            },
            "contract_validation": {
                "first_pass_valid": attempts[0]["error"] is None,
                "repairs_used": attempt,
                "attempts": attempts,
            },
        }
        if error is None and parsed is not None:
            return parsed, combined
        if attempt == max_repairs:
            raise ModelOutputError(f"Contract validation exhausted: {error}", combined)
        print(f"Contract repair {attempt + 1}: {error}", file=sys.stderr, flush=True)
        current_messages = (
            messages
            + (
                [{"role": "assistant", "content": response["content"]}]
                if isinstance(response.get("content"), str)
                else []
            )
            + [
                {
                    "role": "user",
                    "content": canonical(
                        {
                            "task": "Corrija apenas a resposta invalida. Retorne JSON em portugues. "
                            "Use IDs exatos do pacote; nao invente nem abrevie referencias. "
                            "Dados ausentes nao sustentam ausencia de falha.",
                            "validation_error": error,
                        }
                    ),
                }
            ]
        )
    raise EvaluationError("Invalid repair budget")


def check_finding_refs(value: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    ids = {item["id"] for item in evidence}

    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {
                    "evidence_ids",
                    "supporting_evidence",
                    "contradicting_evidence",
                }:
                    invalid = sorted(set(item) - ids)
                    if invalid:
                        raise EvaluationError(
                            f"Knowledge cites nonexistent evidence: {', '.join(invalid)}"
                        )
                else:
                    check(item)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(value)


def support_messages(
    manifest: dict[str, Any], bundle: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": manifest["skill"]},
        {
            "role": "user",
            "content": canonical(
                {
                    "task": "Extraia observacoes em portugues, sem presumir causa; compare ao menos duas alternativas. "
                    "Confira servico, unidade e fase de cada referencia. Ausencia de OOM "
                    "nao exclui consumo de memoria; ausencia de drops nao exclui atraso de rede. "
                    "Um restart nao explica o proprio motivo.",
                    "contract": manifest["contract"],
                    "incident": bundle,
                }
            ),
        },
    ]


def train_case(
    output: Path,
    call: ModelCall,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    case_id: str,
    label: dict[str, Any],
) -> None:
    bundle = stage_bundle(output, manifest, snapshot, case_id, f"support-{case_id}")
    messages = support_messages(manifest, bundle)
    blind_path = output / "observations" / f"{case_id}.json"
    if blind_path.exists():
        blind = load_sealed(blind_path)
        if blind["snapshot_sha256"] != snapshot["sha256"] or blind[
            "bundle_sha256"
        ] != digest(bundle):
            raise EvaluationError("Observations belong to different evidence")
        observed = Observations.model_validate(blind["observations"]).model_dump()
        blind_response = blind["model_response"]
    else:
        observations, blind_response = model_json(
            call,
            messages,
            Observations,
            evidence=bundle["evidence"],
            max_repairs=manifest["max_repairs"],
            audit_prefix=output
            / "private/contract_attempts"
            / f"{case_id}-observations",
        )
        observed = observations.model_dump()
        write_new(
            blind_path,
            seal(
                {
                    "snapshot_sha256": snapshot["sha256"],
                    "bundle_sha256": digest(bundle),
                    "observations": observed,
                    "model_response": blind_response,
                }
            ),
        )
    check_finding_refs(observed, bundle["evidence"])
    labeled_messages = messages + [
        {"role": "assistant", "content": canonical(observed)},
        {
            "role": "user",
            "content": canonical(
                {
                    "task": "Confronte as observacoes com o rotulo experimental, em portugues. "
                    "Nao racionalize o gabarito. Declare suporte insuficiente quando necessario. "
                    "O gate experimental nao substitui evidencia independente; associe cada "
                    "afirmacao ao servico e fase corretos e nao confunda sintoma com mecanismo.",
                    "experimental_label": label,
                }
            ),
        },
    ]
    experience, labeled_response = model_json(
        call,
        labeled_messages,
        Experience,
        evidence=bundle["evidence"],
        max_repairs=manifest["max_repairs"],
        audit_prefix=output / "private/contract_attempts" / f"{case_id}-experience",
    )
    write_new(
        output / "drafts" / f"{case_id}.json",
        seal(
            {
                "case_id": case_id,
                "campaign_id": manifest["campaign_id"],
                "snapshot_sha256": snapshot["sha256"],
                "bundle": bundle,
                "bundle_sha256": digest(bundle),
                "label": label,
                "observations": observed,
                "experience": experience.model_dump(),
                "review_status": "pending",
                "model_responses": [blind_response, labeled_response],
            }
        ),
    )


def train(output: Path, call: ModelCall, *, settings: dict[str, Any]) -> dict[str, Any]:
    manifest = load_manifest(output)
    with evaluation_lock(output), campaign_idle(manifest):
        snapshot = load_snapshot(output, manifest)
        labels = load_labels(output, manifest, snapshot)
        call = audited_model(output, call, settings)
        eligible = [
            case
            for case in manifest["cases"]
            if case["split"] == "support" and case["case_id"] in snapshot["bundles"]
        ]
        if not eligible:
            raise EvaluationError("No completed support cases")
        if manifest["evidence_mode"] == "live_shared":
            load_shared_evidence(output, manifest, snapshot)
        report_path = output / "training_report.json"
        if report_path.exists():
            return load_sealed(report_path)
        results = []
        for case in eligible:
            case_id = case["case_id"]
            path = output / "drafts" / f"{case_id}.json"
            error_path = output / "private/training_errors" / f"{case_id}.json"
            if path.exists():
                previous = load_sealed(path)
                if previous["snapshot_sha256"] != snapshot["sha256"]:
                    raise EvaluationError("Draft belongs to another snapshot")
            elif error_path.exists():
                previous = load_sealed(error_path)
                if previous["snapshot_sha256"] != snapshot["sha256"]:
                    raise EvaluationError(
                        "Training failure belongs to another snapshot"
                    )
            else:
                try:
                    train_case(
                        output, call, manifest, snapshot, case_id, labels[case_id]
                    )
                except ModelOutputError as exc:
                    write_new(
                        error_path,
                        seal(
                            {
                                "case_id": case_id,
                                "snapshot_sha256": snapshot["sha256"],
                                "error": str(exc),
                                "response": exc.response,
                            }
                        ),
                    )
                    print(f"{case_id}: {exc}", file=sys.stderr, flush=True)
            failed = error_path.exists()
            results.append(
                {
                    "case_id": case_id,
                    "source_campaign_id": snapshot["case_sources"][case_id],
                    "status": "error" if failed else "pending_review",
                    "mechanism_validated": labels[case_id]["mechanism_validated"],
                    "error": load_sealed(error_path)["error"] if failed else None,
                }
            )
            print(f"Support {case_id}: {results[-1]['status']}", flush=True)
        template_path = output / "review_template.json"
        if not template_path.exists():
            write_new(
                template_path,
                {
                    case["case_id"]: {"decision": "pending", "reviewer": "", "note": ""}
                    for case in eligible
                    if (output / "drafts" / f"{case['case_id']}.json").exists()
                },
            )
        report = seal(
            {
                "manifest_sha256": manifest["sha256"],
                "snapshot_sha256": snapshot["sha256"],
                "support_cases": len(eligible),
                "drafts": sum(row["status"] == "pending_review" for row in results),
                "errors": sum(row["status"] == "error" for row in results),
                "results": results,
                "human_review_required": True,
                "review_checklist": [
                    "Conferir IDs, servico, unidade e fase nas fontes citadas.",
                    "Exigir sinais discriminativos de duas familias independentes em fault.",
                    "Explicar o motivo de cada restart; nao inferir a causa pelo restart.",
                    "Nao exigir OOM para memory-hog, nem drops para atraso de rede.",
                    "Nao aprovar apenas pelo gate ou pela declaracao do modelo.",
                    "Rejeitar semantica incorreta; registrar revisor e justificativa.",
                ],
            }
        )
        write_new(report_path, report)
        return report


def historical_item(
    draft: dict[str, Any],
    bundle: dict[str, Any],
    review: dict[str, Any],
    history_policy: str = "validated_only",
) -> dict[str, Any]:
    case_id = draft["case_id"]
    observations = json.loads(canonical(draft["observations"]))
    experience = json.loads(canonical(draft["experience"]))
    cited = {
        ref
        for finding in (
            observations["findings"]
            + experience["discriminators"]
            + experience["contradictions"]
        )
        for ref in finding["evidence_ids"]
    }
    for data, fields in (
        (observations, ("findings",)),
        (experience, ("discriminators", "contradictions")),
    ):
        for field in fields:
            for finding in data[field]:
                finding["evidence_ids"] = [
                    f"K-{case_id}:{ref}" for ref in finding["evidence_ids"]
                ]
    item = {
        "knowledge_id": f"K-{case_id}",
        "support_case_id": case_id,
        "observations": observations,
        "experience": experience,
        "support_evidence": [
            {**evidence, "id": f"K-{case_id}:{evidence['id']}"}
            for evidence in bundle["evidence"]
            if evidence["id"] in cited
        ],
        "retrieval_signature": sorted(signature(bundle)),
        "review": review,
    }
    if history_policy == "qualified_examples":
        label = draft["label"]
        gate_passed = bool(label["mechanism_validated"])
        model_supported = bool(experience["mechanism_supported"])
        if gate_passed and model_supported:
            qualification = "gate_passed_model_supported"
        elif gate_passed:
            qualification = "gate_passed_model_not_supported"
        elif model_supported:
            qualification = "gate_failed_model_supported"
        else:
            qualification = "gate_failed_model_not_supported"
        item["experimental_assignment"] = {
            "target_service": label["service"],
            "injected_fault_type": label["fault_type"],
            "mechanism_gate_passed": gate_passed,
        }
        item["qualification"] = {
            "classification": qualification,
            "gate_status": "passed" if gate_passed else "failed",
            "model_assessment": "supported" if model_supported else "not_supported",
            "causal_confirmation": False,
        }
    else:
        item["known_label"] = draft["label"]
    return item


def publish(output: Path, decisions_path: Path) -> dict[str, Any]:
    manifest = load_manifest(output)
    history_policy = manifest.get("history_policy", "validated_only")
    if history_policy not in {"validated_only", "qualified_examples"}:
        raise EvaluationError("Unknown historical knowledge policy")
    with evaluation_lock(output):
        snapshot = load_snapshot(output, manifest)
        decisions = read_json(decisions_path)
        support = {
            case["case_id"] for case in manifest["cases"] if case["split"] == "support"
        }
        items = []
        if not isinstance(decisions, dict) or not decisions:
            raise EvaluationError(
                "Review decisions must be a nonempty object keyed by support case ID"
            )
        for case_id, decision in sorted(decisions.items()):
            if case_id not in support:
                raise EvaluationError("Cannot publish test/foreign cases")
            if (
                not decision.get("reviewer", "").strip()
                or not decision.get("note", "").strip()
            ):
                raise EvaluationError(
                    "Every decision requires reviewer and review note"
                )
            if decision.get("decision") not in {"approve", "reject"}:
                raise EvaluationError("Review decision must be approve or reject")
            draft = load_sealed(output / "drafts" / f"{case_id}.json")
            if (
                draft["campaign_id"] != manifest["campaign_id"]
                or draft["snapshot_sha256"] != snapshot["sha256"]
            ):
                raise EvaluationError("Draft does not belong to this workflow snapshot")
            if decision["decision"] == "reject":
                continue
            if history_policy == "validated_only":
                if (
                    not draft["label"]["mechanism_validated"]
                    or not draft["experience"]["mechanism_supported"]
                ):
                    raise EvaluationError(
                        "Cannot approve a mechanism without experimental and evidence support"
                    )
                if not draft["experience"]["discriminators"]:
                    raise EvaluationError(
                        "Approved experience needs a discriminator with evidence"
                    )
            bundle = draft["bundle"]
            if draft["bundle_sha256"] != digest(bundle):
                raise EvaluationError("Draft evidence changed before review")
            check_finding_refs(draft["observations"], bundle["evidence"])
            check_finding_refs(draft["experience"], bundle["evidence"])
            item = historical_item(draft, bundle, decision, history_policy)
            if history_policy == "qualified_examples" and not item["support_evidence"]:
                raise EvaluationError(
                    "Qualified historical examples need cited evidence"
                )
            items.append(item)
        if not items:
            raise EvaluationError("No reviewed knowledge approved; nothing published")
        knowledge = seal(
            {
                "campaign_id": manifest["campaign_id"],
                "snapshot_sha256": snapshot["sha256"],
                "manifest_sha256": manifest["sha256"],
                "history_policy": history_policy,
                "decisions": decisions,
                "items": items,
            }
        )
        write_new(output / "knowledge.json", knowledge)
        return knowledge


def signature(bundle: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for item in bundle["evidence"]:
        if item["family"] == "metrics":
            phases = item.get("phases", {})
            base = phases.get("baseline", {}).get("mean")
            fault = phases.get("fault", {}).get("mean")
            if (
                base is not None
                and fault is not None
                and (
                    (base != 0 and (fault > abs(base) * 1.25 or fault < base * 0.8))
                    or (base == 0 and fault != 0)
                )
            ):
                terms.add(item["metric"])
                terms.update(item.get("labels", {}).values())
        elif item["family"] == "logs" and item.get("phase") == "fault":
            for line in item.get("sample_lines", []):
                if re.search(
                    r"error|timeout|exception|refused|oom|killed", line["message"], re.I
                ):
                    terms.add(item["service"])
                    terms.update(
                        re.findall(r"[a-z][a-z0-9_-]{3,}", line["message"].lower())
                    )
    return terms


def retrieve(
    bundle: dict[str, Any],
    knowledge: dict[str, Any],
    snapshot: dict[str, Any],
    limit: int = 3,
) -> list[dict[str, Any]]:
    query = signature(bundle)
    ranked = []
    for item in knowledge["items"]:
        other = set(item["retrieval_signature"])
        score = len(query & other) / len(query | other) if query | other else 0
        if score > 0:
            ranked.append({"score": score, **item})
    return sorted(ranked, key=lambda item: (-item["score"], item["knowledge_id"]))[
        :limit
    ]


def build_messages(
    manifest: dict[str, Any],
    bundle: dict[str, Any],
    condition: str,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if condition not in CONDITIONS:
        raise EvaluationError("Unknown ablation condition")
    payload: dict[str, Any] = {
        "task": "Diagnose this incident retrospectively using only current evidence for confirmation.",
        "contract": manifest["contract"],
        "incident": bundle,
    }
    if condition != "skill":
        payload["application_context"] = manifest["context"]
    if condition == "skill_card_history":
        if manifest.get("history_policy") == "qualified_examples":
            payload["past_experiences_note"] = (
                "Historical entries include experimental assignments and gate/model "
                "assessments. These are qualified prior context, not causal ground "
                "truth or evidence for the current incident; confirm conclusions "
                "only from current incident evidence."
            )
        payload["past_experiences"] = history
    return [
        {"role": "system", "content": manifest["skill"]},
        {"role": "user", "content": canonical(payload)},
    ]


def load_knowledge(
    output: Path, manifest: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    knowledge = load_sealed(output / "knowledge.json")
    if (
        knowledge["campaign_id"] != manifest["campaign_id"]
        or knowledge["manifest_sha256"] != manifest["sha256"]
        or knowledge["snapshot_sha256"] != snapshot["sha256"]
    ):
        raise EvaluationError("Knowledge belongs to another workflow or snapshot")
    support = {
        case["case_id"] for case in manifest["cases"] if case["split"] == "support"
    }
    if not knowledge["items"] or any(
        item["support_case_id"] not in support for item in knowledge["items"]
    ):
        raise EvaluationError("Knowledge contains test/foreign cases or is empty")
    return knowledge


def restarted_services(bundle: dict[str, Any], known: set[str]) -> list[str]:
    restarted = set()
    for item in bundle["evidence"]:
        metric = item.get("metric")
        if metric not in {"pod_restarts", "pod_restarts_total"}:
            continue
        phases = item.get("phases", {})
        fault = phases.get("fault", {})
        previous = phases.get("baseline", {}).get("last")
        last = fault.get("last")
        observed = (metric == "pod_restarts" and (fault.get("max") or 0) > 0) or (
            metric == "pod_restarts_total"
            and previous is not None
            and last is not None
            and last > previous
        )
        if observed:
            labels = item.get("labels", {})
            direct = labels.get("destination_workload") or labels.get("service")
            if direct in known:
                restarted.add(direct)
            else:
                pod = labels.get("pod", "")
                matches = [
                    service
                    for service in known
                    if pod == service or pod.startswith(service + "-")
                ]
                if matches:
                    restarted.add(max(matches, key=len))
    return sorted(restarted)


def evaluate(
    output: Path, call: ModelCall, repetitions: int = 3, *, settings: dict[str, Any]
) -> dict[str, Any]:
    if repetitions < 1:
        raise EvaluationError("Repetitions must be positive")
    manifest = load_manifest(output)
    with evaluation_lock(output), campaign_idle(manifest):
        snapshot = load_snapshot(output, manifest)
        shared = (
            load_shared_evidence(output, manifest, snapshot)
            if manifest["evidence_mode"] == "live_shared"
            else None
        )
        knowledge = load_knowledge(output, manifest, snapshot)
        labels = load_labels(output, manifest, snapshot)
        call = audited_model(output, call, settings)
        report_path = output / f"report-r{repetitions}.json"
        if report_path.exists():
            report = load_sealed(report_path)
            if (
                report["knowledge_sha256"] != knowledge["sha256"]
                or report["snapshot_sha256"] != snapshot["sha256"]
            ):
                raise EvaluationError("Existing report has different inputs")
            return report
        test_ids = [
            case["case_id"]
            for case in manifest["cases"]
            if case["split"] == "test" and case["case_id"] in snapshot["bundles"]
        ]
        if not test_ids:
            raise EvaluationError("No completed test cases")
        jobs = [
            (case_id, condition, repeat)
            for case_id in test_ids
            for condition in CONDITIONS
            for repeat in range(repetitions)
        ]
        random.Random(manifest["seed"]).shuffle(jobs)
        results = []
        for case_id, condition, repeat in jobs:
            path = output / "attempts" / f"{case_id}-{condition}-{repeat}.json"
            collection_key = f"{case_id}-{condition}-{repeat}"
            bundle = stage_bundle(output, manifest, snapshot, case_id, collection_key)
            history = (
                retrieve(bundle, knowledge, snapshot)
                if condition == "skill_card_history"
                else []
            )
            messages = build_messages(manifest, bundle, condition, history)
            fingerprint = digest(
                {
                    "messages": messages,
                    "knowledge_sha256": knowledge["sha256"],
                    "snapshot_sha256": snapshot["sha256"],
                }
            )
            if path.exists():
                result = load_sealed(path)
                if result["input_sha256"] != fingerprint:
                    raise EvaluationError("Existing attempt has different inputs")
                results.append(result)
                continue
            result = {
                "case_id": case_id,
                "source_campaign_id": snapshot["case_sources"][case_id],
                "condition": condition,
                "repeat": repeat,
                "evidence_mode": manifest["evidence_mode"],
                "bundle_sha256": digest(bundle),
                "input_sha256": fingerprint,
                "snapshot_sha256": snapshot["sha256"],
                "knowledge_sha256": knowledge["sha256"],
                "messages": messages,
                "retrieved_ids": [item["knowledge_id"] for item in history],
                "retrieval_query": (
                    sorted(signature(bundle))
                    if condition == "skill_card_history"
                    else []
                ),
            }
            started = time.monotonic()
            try:
                parsed, response = model_json(
                    call,
                    messages,
                    Diagnosis,
                    evidence=bundle["evidence"],
                    contract=manifest["contract"],
                    max_repairs=manifest["max_repairs"],
                    audit_prefix=output / "private/contract_attempts" / collection_key,
                )
                diagnosis = Diagnosis.model_validate(parsed.model_dump())
                known = set(manifest["contract"]["known_services"])
                diagnosis = validate_diagnosis(
                    diagnosis,
                    bundle["evidence"],
                    known,
                    set(manifest["contract"]["allowed_categories"]),
                    restarted_services(bundle, known),
                    list(FAULTS),
                )
                if (
                    bundle.get("phase_sources", {}).get("fault")
                    == "planned_phase_fallback"
                ):
                    diagnosis.status = "inconclusive"
                    diagnosis.limitations.append(
                        "Fault window is planned, not observed"
                    )
                label = labels[case_id]
                service_correct = diagnosis.primary_service == label["service"]
                fault_correct = diagnosis.fault_type == label["fault_type"]
                result.update(
                    {
                        "status": "completed",
                        "diagnosis": diagnosis.model_dump(),
                        "response": response,
                        "service_correct": service_correct,
                        "fault_type_correct": fault_correct,
                        "joint_correct": service_correct and fault_correct,
                    }
                )
            except (ValueError, LLMClientError) as exc:
                if isinstance(exc, ModelOutputError):
                    result["response"] = exc.response
                result.update(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "service_correct": False,
                        "fault_type_correct": False,
                        "joint_correct": False,
                    }
                )
                print(
                    f"{case_id}/{condition}/{repeat}: {result['error']}",
                    file=sys.stderr,
                )
            result["elapsed_seconds"] = time.monotonic() - started
            result["mechanism_validated"] = labels[case_id]["mechanism_validated"]
            sealed = seal(result)
            write_new(path, sealed)
            results.append(sealed)
        evidence_by_case = {
            case_id: sorted(
                {row["bundle_sha256"] for row in results if row["case_id"] == case_id}
            )
            for case_id in test_ids
        }
        if manifest["evidence_mode"] != "live_per_condition" and any(
            len(hashes) != 1 for hashes in evidence_by_case.values()
        ):
            raise EvaluationError(
                "Paired evaluation received different evidence bundles"
            )
        report = {
            "manifest_sha256": manifest["sha256"],
            "snapshot_sha256": snapshot["sha256"],
            "knowledge_sha256": knowledge["sha256"],
            "history_policy": manifest.get("history_policy", "validated_only"),
            "repetitions": repetitions,
            "evidence_mode": manifest["evidence_mode"],
            "design": manifest["design"],
            "identical_evidence_across_conditions": manifest["evidence_mode"]
            != "live_per_condition",
            "test_evidence_sha256": evidence_by_case,
            "shared_evidence_sha256": shared["sha256"] if shared else None,
            "unavailable_trace_details": (
                shared.get("unavailable_trace_details", {}) if shared else {}
            ),
            "collection_count": (
                len(jobs)
                if manifest["evidence_mode"] == "live_per_condition"
                else len(shared["records"]) if shared else 0
            ),
            "settings": settings,
            "test_cases": len(test_ids),
            "dataset": {
                "planned": len(manifest["cases"]),
                "included_completed": len(snapshot["bundles"]),
                "support_cases": sum(
                    case["split"] == "support"
                    and case["case_id"] in snapshot["bundles"]
                    for case in manifest["cases"]
                ),
                "excluded": snapshot["exclusions"],
                "source_segments": snapshot["source_segments"],
            },
            "summary": summarize(results),
            "summary_by_segment": {
                segment: summarize(
                    [row for row in results if row["source_campaign_id"] == segment]
                )
                for segment in sorted(set(snapshot["case_sources"].values()))
            },
            "limitations": manifest["limitations"]
            + [
                "Accuracy is label matching; semantic explanation review remains necessary.",
                "No calibrated confidence or causal superiority claim.",
                "Offline skill variant, not the historical live skill.",
            ],
        }
        write_new(report_path, seal(report))
        return report


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for condition in CONDITIONS:
        rows = [row for row in results if row["condition"] == condition]
        by_case: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_case.setdefault(row["case_id"], []).append(row)
        case_scores = {
            case_id: {
                metric: statistics.fmean(float(row[metric]) for row in attempts)
                for metric in ("service_correct", "fault_type_correct", "joint_correct")
            }
            for case_id, attempts in by_case.items()
        }
        confirmed = [
            row for row in rows if row.get("diagnosis", {}).get("status") == "confirmed"
        ]
        summary[condition] = {
            "cases": len(by_case),
            "attempts": len(rows),
            "errors": sum(row["status"] == "error" for row in rows),
            "per_case": case_scores,
            "accuracy": {
                metric: (
                    statistics.fmean(score[metric] for score in case_scores.values())
                    if case_scores
                    else None
                )
                for metric in ("service_correct", "fault_type_correct", "joint_correct")
            },
            "confirmation_count": len(confirmed),
            "confirmation_coverage": len(confirmed) / len(rows) if rows else None,
            "confirmation_label_precision": (
                sum(row["joint_correct"] for row in confirmed) / len(confirmed)
                if confirmed
                else None
            ),
            "mechanism_validated_cases": sum(
                attempts[0].get("mechanism_validated") is True
                for attempts in by_case.values()
            ),
            "validated_joint_accuracy": (
                statistics.fmean(
                    case_scores[case_id]["joint_correct"]
                    for case_id, attempts in by_case.items()
                    if attempts[0].get("mechanism_validated") is True
                )
                if any(
                    attempts[0].get("mechanism_validated") is True
                    for attempts in by_case.values()
                )
                else None
            ),
            "mean_elapsed_seconds": (
                statistics.fmean(row.get("elapsed_seconds", 0) for row in rows)
                if rows
                else None
            ),
            "reported_total_tokens": sum(
                row.get("response", {}).get("usage", {}).get("total_tokens", 0)
                for row in rows
            ),
            "contract_first_pass_failures": sum(
                row.get("response", {})
                .get("contract_validation", {})
                .get("first_pass_valid")
                is False
                for row in rows
            ),
            "contract_repairs_used": sum(
                row.get("response", {})
                .get("contract_validation", {})
                .get("repairs_used", 0)
                for row in rows
            ),
            "attempts_with_retrieved_history": sum(
                bool(row.get("retrieved_ids")) for row in rows
            ),
        }
    summary["paired_joint_deltas"] = {
        f"{right}-{left}": {
            case_id: summary[right]["per_case"][case_id]["joint_correct"]
            - score["joint_correct"]
            for case_id, score in summary[left]["per_case"].items()
        }
        for left, right in zip(CONDITIONS, CONDITIONS[1:])
    }
    return summary


def local_model(
    url: str, model: str, max_tokens: int, max_input_chars: int
) -> ModelCall:
    if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise EvaluationError(
            "Only a local model endpoint is allowed for private experiment data"
        )
    if not model.strip() or max_tokens < 1 or max_input_chars < 1:
        raise EvaluationError(
            "Model name and positive input/output budgets are required"
        )

    def call(messages: list[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
        if len(canonical(messages)) > max_input_chars:
            raise EvaluationError(
                "Input budget exceeded; no truncation or inference performed"
            )
        usage: dict[str, int] = {}
        started = time.monotonic()
        response = call_llm_tool_messages(
            messages,
            [],
            tool_choice="none",
            max_new_tokens=max_tokens,
            url=url,
            model=model,
            timeout=180,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "evaluation",
                    "strict": False,
                    "schema": schema,
                },
            },
            usage=usage,
        )
        return {
            **response,
            "usage": usage,
            "configured_model": model,
            "url": url,
            "max_new_tokens": max_tokens,
            "elapsed_seconds": time.monotonic() - started,
        }

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=(
            "init",
            "freeze",
            "collect",
            "train",
            "publish",
            "evaluate",
        ),
    )
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument(
        "--continuation-dir",
        type=Path,
        help="At init, include a completed, provenance-linked continuation",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument(
        "--trace-limit",
        type=int,
        default=5,
        help="Frozen at init; maximum traces searched per historical phase in live mode",
    )
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=0,
        help="Frozen at init; 0 or 1 audited contract repair per output",
    )
    parser.add_argument(
        "--llm-url", default="http://127.0.0.1:8001/v1/chat/completions"
    )
    parser.add_argument("--model")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-input-chars", type=int, default=220000)
    parser.add_argument("--allow-inference", action="store_true")
    parser.add_argument(
        "--evidence-mode",
        choices=("live_per_condition", "live_shared"),
        default="live_shared",
        help="Historical telemetry must be collected online through MCP",
    )
    parser.add_argument("--allow-telemetry", action="store_true")
    parser.add_argument(
        "--allow-missing-trace-details",
        action="store_true",
        help="At init, explicitly retain trace-detail HTTP 404 as identical missing evidence",
    )
    parser.add_argument(
        "--history-policy",
        choices=("validated_only", "qualified_examples"),
        default="validated_only",
        help="At init, freeze whether history contains only supported cases or qualified cases",
    )
    args = parser.parse_args(argv)
    try:
        if args.continuation_dir is not None and args.stage != "init":
            raise EvaluationError("--continuation-dir is only accepted at init")
        if args.history_policy != "validated_only" and args.stage != "init":
            raise EvaluationError("--history-policy is only accepted at init")
        if args.max_repairs and args.stage != "init":
            raise EvaluationError("--max-repairs is only accepted at init")
        if args.allow_missing_trace_details and args.stage != "init":
            raise EvaluationError(
                "--allow-missing-trace-details is only accepted at init"
            )
        if args.stage == "init":
            initialize(
                args.campaign_dir,
                args.output_dir,
                args.seed,
                args.evidence_mode,
                args.trace_limit,
                args.continuation_dir,
                args.max_repairs,
                args.allow_missing_trace_details,
                args.history_policy,
            )
        elif args.stage == "freeze":
            freeze(args.output_dir)
        elif args.stage == "collect":
            if not args.allow_telemetry:
                raise EvaluationError("collect requires explicit --allow-telemetry")
            collect_shared(args.output_dir)
        elif args.stage == "publish":
            if args.decisions is None:
                raise EvaluationError("publish requires --decisions")
            publish(args.output_dir, args.decisions)
        else:
            if not args.allow_inference or not args.model:
                raise EvaluationError(
                    "Inference requires explicit --allow-inference and --model"
                )
            manifest = load_manifest(args.output_dir)
            if (
                manifest["evidence_mode"] == "live_per_condition"
                and not args.allow_telemetry
            ):
                raise EvaluationError("Live mode requires explicit --allow-telemetry")
            call = local_model(
                args.llm_url, args.model, args.max_output_tokens, args.max_input_chars
            )
            settings = {
                "model": args.model,
                "url": args.llm_url,
                "max_output_tokens": args.max_output_tokens,
                "max_input_chars": args.max_input_chars,
                "sampling": "server defaults; no temperature/seed override in shared client",
            }
            if args.stage == "train":
                training = train(args.output_dir, call, settings=settings)
                if training["errors"]:
                    print(
                        f"Training processed all support cases with {training['errors']} "
                        "failures; inspect training_report.json before human review",
                        file=sys.stderr,
                    )
                    return 1
            else:
                report = evaluate(
                    args.output_dir, call, args.repetitions, settings=settings
                )
                errors = sum(
                    report["summary"][condition]["errors"] for condition in CONDITIONS
                )
                if errors:
                    print(
                        f"Evaluation preserved with {errors} failed attempts; inspect report",
                        file=sys.stderr,
                    )
                    return 1
    except (EvaluationError, OSError, ValueError, LLMClientError) as exc:
        print(f"campaign_knowledge: {exc}", file=sys.stderr)
        return 1
    print(f"{args.stage}: completed in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
