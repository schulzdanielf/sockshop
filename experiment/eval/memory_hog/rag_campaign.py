"""Online A/B campaign hooks and offline, ground-truth-only evaluation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from experiment.platform.backend.rca_knowledge import (
    OPENSEARCH_RETRIEVER_VERSION,
    VERSION,
    IncidentA,
    digest,
    write_once,
)
from .controlled_campaign import (
    campaign_cells, read_ledger, validate_rag_reviewed_retry_protocol,
)

METHODS = ("original", "control", "rag")
EXPLORATORY_METHODS = (
    "original", "exploratory_control", "exploratory_rag"
)
EXPLORATORY_MAX_TOTAL_TOKENS = 300_000
MAX_DIAGNOSTIC_RETRIES = 2
TERMINAL = {"completed", "blocked", "aborted", "failed", "stopped"}


def reconcile_exploratory_test_runs(
    directory: Path,
    config: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> dict[str, int] | None:
    knowledge = config.get("knowledge_campaign") or {}
    if not knowledge.get("exploratory"):
        return None

    path = directory / "exploratory_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("mode") != "exploratory"
        or manifest.get("corpus_id") != knowledge.get("corpus_id")
    ):
        raise ValueError("exploratory manifest does not match the saved campaign")

    test_cells = {cell["cell_id"] for cell in config["test_experiments"]}
    started_attempts = {
        (row["cell_id"], int(row.get("attempt", 1)), row["run_id"])
        for row in ledger
        if row.get("cell_id") in test_cells and row.get("run_id")
    }
    count = len(started_attempts)
    previous = manifest.get("test_runs_started")
    if type(previous) is not int or previous < 0:
        raise ValueError("exploratory manifest has an invalid test run counter")
    if previous > count:
        raise ValueError("exploratory manifest counts test runs absent from the ledger")
    if previous == count:
        return None

    manifest["test_runs_started"] = count
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return {"previous": previous, "reconciled": count}


def options_for(config: dict[str, Any], mode: str) -> dict[str, Any]:
    knowledge = config["knowledge_campaign"]
    opensearch = (
        mode == "exploratory_rag"
        and knowledge.get("retrieval_backend") == "opensearch"
    )
    options = {
        key: value
        for key, value in {
            **knowledge,
            "mode": mode,
            "retrieval_backend": "opensearch" if opensearch else "local",
            "opensearch_base_sha256": (
                knowledge.get("opensearch_base_sha256") if opensearch else None
            ),
            "opensearch_source_skill_sha256": (
                knowledge.get("opensearch_source_skill_sha256")
                if opensearch
                else None
            ),
            "allow_legacy_skill_hash": (
                bool(knowledge.get("allow_legacy_skill_hash")) if opensearch else False
            ),
            "opensearch_source_model": (
                knowledge.get("opensearch_source_model") if opensearch else None
            ),
            "opensearch_current_model": (
                knowledge.get("opensearch_current_model") if opensearch else None
            ),
            "allow_legacy_model_mismatch": (
                bool(knowledge.get("allow_legacy_model_mismatch"))
                if opensearch
                else False
            ),
            "opensearch_a_text_version": (
                knowledge.get("opensearch_a_text_version", "incident-a-v2")
            ),
            "retriever_version": (
                knowledge.get("retriever_version", OPENSEARCH_RETRIEVER_VERSION)
                if opensearch
                else "hybrid-rrf-bm25-v1"
            ),
            "require_best_guess": bool(
                knowledge.get("exploratory", False)
            ),
            "include_unvalidated_experimental": bool(
                knowledge.get("include_unvalidated_experimental", False)
                and mode == "exploratory_rag"
            ),
            "possible_fault_types": sorted(
                {cell["chaos_type"] for cell in campaign_cells(config)}
            ),
        }.items()
        if key
        not in {
            "version",
            "exploratory",
            "parent_campaign_id",
            "opensearch_current_skill_sha256",
        }
    }
    if mode in {"exploratory_control", "exploratory_rag"}:
        max_total_tokens = knowledge.get(
            "max_total_tokens", EXPLORATORY_MAX_TOTAL_TOKENS
        )
        if (
            type(max_total_tokens) is not int
            or not 8_000 <= max_total_tokens <= 500_000
        ):
            raise ValueError("max_total_tokens must be an integer from 8000 to 500000")
        options["max_total_tokens"] = max_total_tokens
    return options


def result_path(directory: Path, cell: dict[str, Any], method: str) -> Path:
    base = directory / "rag_results" / cell["cell_id"] / f"{method}.json"
    retries = sorted(
        base.parent.glob(f"{method}.retry-*.json"),
        key=lambda path: int(path.stem.rsplit(".retry-", 1)[1]),
    )
    return retries[-1] if retries else base


def _diagnostic_record_is_valid(record: dict[str, Any]) -> bool:
    response = record.get("response")
    return (
        isinstance(response, dict)
        and response.get("status") != "error"
        and not record.get("validation_error")
    )


def wait_for_history_archive(
    config: dict[str, Any], run_id: str, api: Any
) -> dict[str, Any] | None:
    history = config.get("historical_observability") or {}
    if not history.get("required"):
        return None
    if history.get("mode") == "dual_write":
        timeout = history.get("visibility_timeout_seconds", 600)
        if type(timeout) is not int or timeout < 1:
            raise ValueError("visibility_timeout_seconds must be a positive integer")
        deadline = time.monotonic() + timeout
        next_notice = time.monotonic() + 60
        last_error = None
        while time.monotonic() < deadline:
            try:
                validation = api.get(
                    f"/api/runs/{quote(run_id, safe='')}/history/validation"
                )
                last_error = None
            except (OSError, RuntimeError, TimeoutError) as exc:
                validation = {"status": "pending"}
                last_error = f"{type(exc).__name__}: {exc}"
            if not isinstance(validation, dict):
                raise RuntimeError(
                    f"historical validation returned a non-object for {run_id}"
                )
            if validation.get("status") == "complete":
                print(
                    f"[history] {run_id}: dual-write read-back verified "
                    f"({validation.get('source_counts')})",
                    flush=True,
                )
                return validation
            if validation.get("status") == "failed":
                raise RuntimeError(
                    f"dual-write validation failed for {run_id}: {validation!r}"
                )
            if validation.get("status") != "pending":
                raise RuntimeError(
                    f"dual-write validation returned an unknown status for "
                    f"{run_id}: {validation!r}"
                )
            if validation.get("error"):
                last_error = str(validation["error"])
            now = time.monotonic()
            if now >= next_notice:
                print(
                    f"[history] waiting for dual-write visibility for {run_id}; "
                    f"{validation.get('telemetry') or last_error or 'pending'}, "
                    f"{max(0, int(deadline - now))}s remain",
                    flush=True,
                )
                next_notice = now + 60
            time.sleep(min(15, max(0, deadline - now)))
        raise TimeoutError(
            f"dual-write history for {run_id} was not visible within {timeout}s"
            + (f": {last_error}" if last_error else "")
        )

    run_record = api.get(f"/api/runs/{quote(run_id, safe='')}")
    summary = run_record.get("summary") if isinstance(run_record, dict) else None
    migration = summary.get("history_migration") if isinstance(summary, dict) else None
    if not isinstance(migration, dict) or migration.get("status") != "queued":
        raise RuntimeError(
            f"history migration was not queued for {run_id}: {migration!r}"
        )
    job_name = migration.get("job_name")
    namespace = migration.get("namespace")
    if (
        not isinstance(job_name, str)
        or not re.fullmatch(r"history-migrate-[a-z0-9-]{1,63}", job_name)
        or not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9-]{1,63}", namespace)
    ):
        raise RuntimeError(f"history migration job identity is invalid for {run_id}")
    timeout = history.get("migration_timeout_seconds", 1800)
    if type(timeout) is not int or timeout < 1:
        raise ValueError("migration_timeout_seconds must be a positive integer")

    deadline = time.monotonic() + timeout
    next_notice = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            process = subprocess.run(
                ["kubectl", "-n", namespace, "get", "job", job_name, "-o", "json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"could not inspect history migration job {job_name}: {exc}"
            ) from exc
        if process.returncode:
            if "not found" not in process.stderr.casefold():
                raise RuntimeError(
                    f"could not inspect history migration job {job_name}: "
                    f"{process.stderr.strip() or process.stdout.strip()}"
                )
        else:
            try:
                job = json.loads(process.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"history migration job {job_name} returned invalid JSON"
                ) from exc
            job_status = job.get("status") or {}
            conditions = job_status.get("conditions") or []
            if any(
                item.get("type") == "Failed" and item.get("status") == "True"
                for item in conditions
                if isinstance(item, dict)
            ):
                raise RuntimeError(
                    f"history migration job {job_name} failed: {conditions}"
                )
            if job_status.get("succeeded", 0) >= 1:
                validation = api.get(
                    f"/api/runs/{quote(run_id, safe='')}/history/validation"
                )
                if not isinstance(validation, dict) or validation.get("status") != "complete":
                    raise RuntimeError(
                        f"history validation failed for {run_id}: {validation!r}"
                    )
                print(
                    f"[history] {run_id}: metrics/logs/traces archived and verified "
                    f"({validation.get('source_counts')})",
                    flush=True,
                )
                return validation
        now = time.monotonic()
        if now >= next_notice:
            print(
                f"[history] waiting for {job_name}; "
                f"{max(0, int(deadline - now))}s remain",
                flush=True,
            )
            next_notice = now + 60
        time.sleep(15)
    raise TimeoutError(
        f"history migration job {job_name} did not complete within {timeout}s"
    )


def validate_historical_diagnostic(
    config: dict[str, Any], method: str, response: dict[str, Any]
) -> None:
    if method != "annotate" and response.get("status") == "error":
        raise ValueError(
            f"{method} diagnostic returned an error: "
            f"{str(response.get('error') or 'unspecified error')[:300]}"
        )
    history = config.get("historical_observability") or {}
    if not history.get("required"):
        return
    if response.get("observability_source") != "history":
        raise ValueError(f"{method} diagnostic did not use historical observability")
    if method == "original" and history.get("mode") != "dual_write":
        archive = response.get("history_archive") or {}
        if archive.get("status") != "ok":
            raise ValueError(
                f"Skill RCA did not validate archived traces: {archive!r}"
            )
    families = {
        item.get("family")
        for item in response.get("evidence") or []
        if isinstance(item, dict)
        and item.get("status") in {"ok", "partial"}
        and item.get("observation_count", 0) > 0
    }
    if not {"metrics", "logs", "traces"}.issubset(families):
        raise ValueError(
            f"{method} diagnostic lacks usable historical telemetry families: "
            f"{families}"
        )
    if method != "exploratory_rag":
        return

    try:
        incident = IncidentA.model_validate(response.get("a"))
    except (TypeError, ValueError) as exc:
        raise ValueError("RAG diagnostic did not create a valid incident A") from exc
    if incident.text_version != "incident-a-v2" or not incident.metric_facts:
        raise ValueError("RAG incident A is missing versioned metric facts")
    knowledge = config["knowledge_campaign"]
    retrieval = response.get("retrieval") or {}
    if (
        retrieval.get("backend") != "opensearch"
        or retrieval.get("retriever_version") != OPENSEARCH_RETRIEVER_VERSION
        or response.get("base_sha256")
        != knowledge.get("opensearch_base_sha256")
    ):
        raise ValueError("RAG diagnostic did not use the pinned OpenSearch corpus")
    compatibility = retrieval.get("skill_compatibility") or {}
    if (
        compatibility.get("source_skill_sha256")
        != knowledge.get("opensearch_source_skill_sha256")
        or compatibility.get("current_skill_sha256")
        != knowledge.get("opensearch_current_skill_sha256")
        or compatibility.get("legacy_hash_allowed")
        is not knowledge.get("allow_legacy_skill_hash", False)
    ):
        raise ValueError("RAG Skill-version compatibility differs from the campaign")
    model_compatibility = retrieval.get("model_compatibility") or {}
    if (
        model_compatibility.get("source_model")
        != knowledge.get("opensearch_source_model")
        or model_compatibility.get("current_model")
        != knowledge.get("opensearch_current_model")
        or model_compatibility.get("models_match")
        is not (
            knowledge.get("opensearch_source_model")
            == knowledge.get("opensearch_current_model")
        )
        or model_compatibility.get("legacy_model_mismatch_allowed")
        is not knowledge.get("allow_legacy_model_mismatch", False)
    ):
        raise ValueError("RAG model compatibility differs from the campaign")


def process_cell(
    directory: Path,
    config: dict[str, Any],
    cell: dict[str, Any],
    entry: dict[str, Any],
    api: Any,
) -> None:
    """Run diagnostics immediately after a safe terminal run, before retention."""
    write_once(
        directory / "rag_protocol.json",
        {
            "version": VERSION,
            "config_sha256": digest(config),
        },
    )
    run_id = entry["run_id"]
    methods = ("annotate",) if cell["phase"] == "train" else METHODS
    exploratory = bool(config["knowledge_campaign"].get("exploratory"))
    history_validation = None
    history_path = (
        directory / "history_validation" / f"{cell['cell_id']}.json"
    )
    if cell["phase"] == "test":
        if history_path.exists():
            saved_history = json.loads(history_path.read_text(encoding="utf-8"))
            history_validation = saved_history.get("validation")
            if (
                saved_history.get("run_id") != run_id
                or not isinstance(history_validation, dict)
                or history_validation.get("status") != "complete"
            ):
                raise ValueError(
                    "persisted historical validation is missing, incomplete, "
                    "or belongs to another run"
                )
        else:
            history_validation = wait_for_history_archive(config, run_id, api)
    # Rotate query order, not the injection order or the train/test assignment.
    if cell["phase"] == "test":
        method_order = EXPLORATORY_METHODS if exploratory else METHODS
        index = config["test_experiments"].index(cell) % len(method_order)
        methods = method_order[index:] + method_order[:index]
    for method in methods:
        path = result_path(directory, cell, method)
        retry_metadata = None
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["run_id"] != run_id:
                raise ValueError("persisted diagnostic belongs to a different run")
            valid = _diagnostic_record_is_valid(saved)
            if valid:
                continue
            retry_match = re.search(r"\.retry-(\d+)$", path.stem)
            retry_attempt = int(retry_match.group(1)) if retry_match else 0
            if retry_attempt >= MAX_DIAGNOSTIC_RETRIES:
                raise RuntimeError(
                    f"{method} diagnostic failed after {MAX_DIAGNOSTIC_RETRIES} "
                    "retries; no further retry is allowed"
                )
            retry_attempt += 1
            path = path.with_name(f"{method}.retry-{retry_attempt}.json")
            retry_metadata = {
                "attempt": retry_attempt,
                "previous_error": str(
                    saved.get("validation_error")
                    or (saved.get("response") or {}).get("error")
                    or "diagnostic validation failed"
                )[:300],
            }
        escaped = quote(run_id, safe="")
        body = options_for(config, method)
        endpoint = "skill-rag"
        if method == "original":
            endpoint = "skill-rca"
            body = {
                "possible_fault_types": body["possible_fault_types"],
                "require_best_guess": body["require_best_guess"],
            }
        started = time.monotonic()
        try:
            response = api.post(
                f"/api/runs/{escaped}/{endpoint}",
                body=body,
                request_timeout=config["knowledge_campaign"]["timeout_seconds"] + 60,
            )
            if not isinstance(response, dict):
                raise ValueError("diagnostic endpoint returned a non-object")
            if method != "original" and response.get("version") != VERSION:
                raise ValueError("loaded knowledge strategy version mismatch")
        except (RuntimeError, OSError, ValueError) as exc:
            response = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        response["request_duration_ms"] = round((time.monotonic() - started) * 1000)
        record = {"run_id": run_id, "method": method, "response": response}
        if retry_metadata is not None:
            record["retry"] = {
                **retry_metadata,
                "max_total_tokens": body.get("max_total_tokens"),
            }
        try:
            validate_historical_diagnostic(config, method, response)
        except ValueError as exc:
            record["validation_error"] = str(exc)[:500]
            write_once(path, record)
            raise RuntimeError(
                f"{method} diagnostic failed for {run_id}: {exc}"
            ) from exc
        if cell["phase"] == "train" and response.get("status") == "error":
            failures = path.parent / f"annotation_error_{digest(record)}.json"
            write_once(failures, record)
            raise RuntimeError(
                f"training annotation failed for {run_id}; no new injection authorized: "
                f"{response.get('error')}"
            )
        write_once(path, record)
    if history_validation is not None:
        if not history_path.exists():
            write_once(
                history_path,
                {
                    "run_id": run_id,
                    "mode": history_validation.get("mode", "archive"),
                    "validation": history_validation,
                },
            )


def prepare_test(
    directory: Path,
    config: dict[str, Any],
    latest: dict[str, Any],
    api: Any,
) -> None:
    write_once(
        directory / "rag_protocol.json",
        {
            "version": VERSION,
            "config_sha256": digest(config),
        },
    )
    training_ids = []
    for cell in config["train_experiments"]:
        entry = latest.get(cell["cell_id"], {})
        if entry.get("status") not in TERMINAL:
            raise ValueError(
                "training phase has unfinished cells; testing is forbidden"
            )
        if entry["status"] == "completed":
            path = result_path(directory, cell, "annotate")
            if not path.is_file():
                raise ValueError("training annotation must finish before testing")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record["run_id"] != entry["run_id"]
                or record["response"]["status"] == "error"
            ):
                raise ValueError("missing or inconsistent training annotation")
            training_ids.append(entry["run_id"])
    corpus = quote(config["knowledge_campaign"]["corpus_id"], safe="")
    frozen_path = directory / "rag_base.json"
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        expected_ids = (
            frozen.get("parent_training_run_ids")
            if config["knowledge_campaign"].get("exploratory")
            else frozen.get("training_run_ids")
        )
        if expected_ids != sorted(training_ids):
            raise ValueError("training cohort changed after base freeze")
        return
    if config["knowledge_campaign"].get("exploratory"):
        knowledge = config["knowledge_campaign"]
        query = {}
        if knowledge.get("retrieval_backend") == "opensearch":
            query = {
                "expected_skill_sha256": knowledge.get(
                    "opensearch_source_skill_sha256"
                ),
                "allow_legacy_skill_hash": bool(
                    knowledge.get("allow_legacy_skill_hash")
                ),
                "expected_model": knowledge.get("opensearch_source_model"),
                "allow_legacy_model_mismatch": bool(
                    knowledge.get("allow_legacy_model_mismatch")
                ),
            }
        frozen = api.get(
            f"/api/skill-rag/{corpus}/exploratory-base", **query
        )
        if frozen.get("mode") != "exploratory":
            raise ValueError("the installed corpus is not an exploratory knowledge base")
        if knowledge.get("retrieval_backend") == "opensearch" and (
            frozen.get("source_model")
            != knowledge.get("opensearch_source_model")
            or frozen.get("current_model")
            != knowledge.get("opensearch_current_model")
            or frozen.get("models_match")
            is not (
                knowledge.get("opensearch_source_model")
                == knowledge.get("opensearch_current_model")
            )
            or frozen.get("legacy_model_mismatch_allowed")
            is not knowledge.get("allow_legacy_model_mismatch", False)
        ):
            raise ValueError(
                "frozen corpus model compatibility differs from the campaign"
            )
    else:
        frozen = api.post(
            f"/api/skill-rag/{corpus}/freeze",
            body={"training_run_ids": training_ids},
        )
    if not isinstance(frozen, dict) or frozen.get("version") != VERSION:
        raise ValueError("invalid frozen base acknowledgement")
    if config["knowledge_campaign"].get("exploratory"):
        frozen = {
            **frozen,
            "training_run_ids": sorted(frozen["source_run_ids"]),
            "parent_training_run_ids": sorted(training_ids),
            "skill_compatibility": {
                "source_skill_sha256": frozen["source_skill_sha256"],
                "current_skill_sha256": frozen["current_skill_sha256"],
                "hashes_match": frozen["skill_hashes_match"],
                "legacy_hash_allowed": frozen["legacy_skill_hash_allowed"],
            },
            "model_compatibility": {
                "source_model": frozen["source_model"],
                "current_model": frozen["current_model"],
                "models_match": frozen["models_match"],
                "legacy_model_mismatch_allowed": (
                    frozen["legacy_model_mismatch_allowed"]
                ),
            },
        }
    else:
        frozen = {**frozen, "training_run_ids": sorted(training_ids)}
    write_once(frozen_path, frozen)


def _score(response: dict[str, Any], service: str, fault: str) -> dict[str, Any]:
    diagnosis = response.get("diagnosis") or {}
    valid = response.get("status") in {"confirmed", "inconclusive"}
    localized = valid and diagnosis.get("primary_service") == service
    classified = valid and diagnosis.get("fault_type") == fault
    ids = set(diagnosis.get("supporting_evidence") or [])
    usable = [
        item
        for item in response.get("evidence") or []
        if item.get("id") in ids
        and item.get("status") == "ok"
        and item.get("fault_observation_count", 0) > 0
    ]
    families = {item["family"] for item in usable}
    confirmed = valid and diagnosis.get("status") == "confirmed"
    referenced = {item["id"] for item in usable}
    return {
        "status": response.get("status", "missing"),
        "service_selected": bool(
            valid and isinstance(diagnosis.get("primary_service"), str)
        ),
        "fault_type_selected": bool(
            valid and isinstance(diagnosis.get("fault_type"), str)
        ),
        "classification_complete": bool(
            valid
            and isinstance(diagnosis.get("primary_service"), str)
            and isinstance(diagnosis.get("fault_type"), str)
        ),
        "service_correct": bool(localized),
        "fault_type_correct": bool(classified),
        "joint_correct": bool(localized and classified),
        "confirmed": confirmed,
        "inconclusive": valid and not confirmed,
        "unsupported_confirmation": confirmed
        and (len(families) < 2 or not ids or not ids.issubset(referenced)),
        "incorrect_confirmation": confirmed and not (localized and classified),
        "supporting_families": sorted(families),
        "all_support_refs_usable": bool(ids) and ids.issubset(referenced),
        "duration_ms": response.get("duration_ms", response.get("request_duration_ms")),
        "total_tokens": response.get("total_tokens"),
        "model_calls": response.get(
            "model_calls", len(response.get("model_attempts") or [])
        ),
        "usage": [
            attempt.get("usage", {}) for attempt in response.get("model_attempts") or []
        ],
        "additional_checks": response.get("additional_checks", []),
        "error": response.get("error"),
    }


def build_evaluation(directory: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Only this evaluator matches persisted diagnoses against current labels."""
    if config.get("knowledge_campaign", {}).get("exploratory"):
        return build_exploratory_evaluation(directory, config)
    ledger = read_ledger(directory / "ledger.jsonl")
    latest = {row["cell_id"]: row for row in ledger}
    reviewed_retry_protocol = validate_rag_reviewed_retry_protocol(
        directory, config, campaign_cells(config), ledger,
    )
    attempts = {}
    for row in ledger:
        key = (row["cell_id"], int(row.get("attempt", 1)))
        attempts[key] = row
    submitted_attempts = [
        row for row in attempts.values()
        if row.get("run_id")
    ]
    frozen_path = directory / "rag_base.json"
    frozen = (
        json.loads(frozen_path.read_text(encoding="utf-8"))
        if frozen_path.exists()
        else None
    )
    all_cells = campaign_cells(config)
    if (
        len(all_cells) != 70
        or len(config["train_experiments"]) != 35
        or len(config["test_experiments"]) != 35
    ):
        raise ValueError("RAG evaluation requires the 70-run, 35/35 plan")
    train_by_combination = {
        (cell["service"], cell["chaos_type"]): latest.get(cell["cell_id"], {}).get(
            "run_id"
        )
        for cell in config["train_experiments"]
    }
    per_run = []
    for cell in config["test_experiments"]:
        entry = latest.get(cell["cell_id"], {})
        run_id = entry.get("run_id")
        responses = {}
        for method in METHODS:
            path = result_path(directory, cell, method)
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["run_id"] != run_id:
                    raise ValueError("diagnostic run ID differs from campaign ledger")
                responses[method] = record["response"]
        row: dict[str, Any] = {
            "cell_id": cell["cell_id"],
            "run_id": run_id,
            "run_status": entry.get("status", "not_run"),
            "ground_truth_service": cell["service"],
            "chaos_type": cell["chaos_type"],
            "effect_validated": (
                (entry.get("gates") or {}).get("intervention_confirmed_on_target")
                is True
                and ((entry.get("gates") or {}).get("mechanism_observed") or {}).get(
                    "passed"
                )
                is True
            ),
            "methods": {
                method: _score(
                    responses.get(method, {}), cell["service"], cell["chaos_type"]
                )
                for method in METHODS
            },
        }
        for method in ("control", "rag"):
            response = responses.get(method)
            if response and response.get("status") != "error":
                if not frozen or response.get("base_sha256") != frozen["sha256"]:
                    raise ValueError("test response used a different knowledge base")
        rag = responses.get("rag") or {}
        neighbors = rag.get("retrieved") or []
        expected_train_id = train_by_combination[(cell["service"], cell["chaos_type"])]
        row["retrieval"] = {
            "pair_ids": [item["pair_id"] for item in neighbors],
            "scores": [item["score"] for item in neighbors],
            "same_combination_recalled": bool(expected_train_id)
            and any(item["source_run_id"] == expected_train_id for item in neighbors),
            "retrieval_config": rag.get("retrieval"),
            "initial_diagnosis": rag.get("initial_diagnosis"),
        }
        per_run.append(row)

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            method: {
                "denominator": len(rows),
                **{
                    field: sum(row["methods"][method][field] for row in rows)
                    for field in (
                        "service_correct",
                        "fault_type_correct",
                        "joint_correct",
                        "confirmed",
                        "inconclusive",
                        "unsupported_confirmation",
                        "incorrect_confirmation",
                    )
                },
                "diagnoses_available": sum(
                    row["methods"][method]["status"] in {"confirmed", "inconclusive"}
                    for row in rows
                ),
                "joint_accuracy": (
                    sum(row["methods"][method]["joint_correct"] for row in rows)
                    / len(rows)
                    if rows
                    else None
                ),
                "total_duration_ms": sum(
                    row["methods"][method]["duration_ms"] or 0 for row in rows
                ),
                "total_model_calls": sum(
                    row["methods"][method]["model_calls"] for row in rows
                ),
                "total_tokens_reported": sum(
                    row["methods"][method]["total_tokens"] or 0 for row in rows
                ),
                "token_coverage": sum(
                    row["methods"][method]["total_tokens"] is not None for row in rows
                ),
            }
            for method in METHODS
        }

    comparisons = {}
    for baseline in ("original", "control"):
        gains = sum(
            row["methods"]["rag"]["joint_correct"]
            and not row["methods"][baseline]["joint_correct"]
            for row in per_run
        )
        losses = sum(
            row["methods"][baseline]["joint_correct"]
            and not row["methods"]["rag"]["joint_correct"]
            for row in per_run
        )
        comparisons[f"rag_vs_{baseline}"] = {
            "denominator": 35,
            "gains": gains,
            "losses": losses,
            "net": gains - losses,
        }
    return {
        "version": VERSION,
        "campaign_id": config["experiment_campaign_id"],
        "protocol": "new incidents of known scenarios; no mandatory human guide approval",
        "planned": {"total": 70, "train": 35, "test": 35},
        "execution_attempts": {
            "planned_cells": len(all_cells),
            "submitted_attempts": len(submitted_attempts),
            "additional_attempts": sum(
                int(row.get("attempt", 1)) > 1 for row in submitted_attempts
            ),
            "outcomes_by_attempt": dict(
                Counter(row.get("status", "unknown") for row in submitted_attempts)
            ),
            "reviewed_retry_protocol": reviewed_retry_protocol,
        },
        "outcomes": {
            phase: dict(
                Counter(
                    latest.get(cell["cell_id"], {}).get("status", "not_run")
                    for cell in config[f"{phase}_experiments"]
                )
            )
            for phase in ("train", "test")
        },
        "base": frozen,
        "per_run": per_run,
        "summary": summarize(per_run),
        "completed_summary": summarize(
            [row for row in per_run if row["run_status"] == "completed"]
        ),
        "by_effect": {
            str(effect).lower(): summarize(
                [row for row in per_run if row["effect_validated"] == effect]
            )
            for effect in (True, False)
        },
        "by_fault": {
            fault: summarize([row for row in per_run if row["chaos_type"] == fault])
            for fault in sorted(
                {cell["chaos_type"] for cell in config["test_experiments"]}
            )
        },
        "by_service": {
            service: summarize(
                [row for row in per_run if row["ground_truth_service"] == service]
            )
            for service in sorted(
                {cell["service"] for cell in config["test_experiments"]}
            )
        },
        "paired": comparisons,
        "recall_at_k": sum(
            row["retrieval"]["same_combination_recalled"] for row in per_run
        )
        / 35,
    }


def write_evaluation(directory: Path, config: dict[str, Any]) -> dict[str, Any]:
    report = build_evaluation(directory, config)
    path = directory / (
        "exploratory_rag_evaluation.json"
        if config.get("knowledge_campaign", {}).get("exploratory")
        else "rag_evaluation.json"
    )
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return report


def build_exploratory_evaluation(
    directory: Path, config: dict[str, Any]
) -> dict[str, Any]:
    cells = config.get("test_experiments") or []
    if len(cells) not in {1, 35}:
        raise ValueError("exploratory evaluation requires one pilot or all 35 test cells")
    latest = {
        row["cell_id"]: row for row in read_ledger(directory / "ledger.jsonl")
    }
    frozen_path = directory / "rag_base.json"
    base = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else None
    audit_path = directory / "label_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    source_by_run = {
        row["run_id"]: row.get("target_labels")
        for row in audit.get("records", [])
        if row.get("run_id") and row.get("labels_match") is True
    }
    source_pair_by_run = {
        row["run_id"]: row.get("pair_id")
        for row in audit.get("records", [])
        if row.get("run_id") and row.get("labels_match") is True
    }
    rows = []
    for cell in cells:
        entry = latest.get(cell["cell_id"], {})
        run_id = entry.get("run_id")
        responses = {}
        for method in EXPLORATORY_METHODS:
            path = result_path(directory, cell, method)
            if not path.exists():
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["run_id"] != run_id:
                raise ValueError("diagnostic run ID differs from exploratory campaign")
            responses[method] = record["response"]
        if entry.get("status") == "completed":
            if set(responses) != set(EXPLORATORY_METHODS):
                raise ValueError(
                    f"completed run lacks persisted diagnostics: {cell['cell_id']}"
                )
        for method in ("exploratory_rag",):
            response = responses.get(method)
            if response and response.get("status") != "error":
                expected_base_sha256 = (
                    config["knowledge_campaign"].get("opensearch_base_sha256")
                    or (base or {}).get("sha256")
                )
                if (
                    not base
                    or response.get("base_sha256") != expected_base_sha256
                ):
                    raise ValueError("exploratory diagnosis used a different knowledge base")
        control_response = responses.get("exploratory_control")
        if control_response and control_response.get("status") != "error":
            if control_response.get("base_sha256") is not None:
                raise ValueError(
                    "exploratory control unexpectedly loaded a knowledge base"
                )
        neighbors = (responses.get("exploratory_rag") or {}).get("retrieved", [])
        same_combination_recalled = any(
            source_by_run.get(item.get("source_run_id"), {}).get("service")
            == cell["service"]
            and source_by_run.get(item.get("source_run_id"), {}).get("fault_type")
            == cell["chaos_type"]
            for item in neighbors
        )
        rows.append(
            {
                "cell_id": cell["cell_id"],
                "run_id": run_id,
                "run_status": entry.get("status", "not_run"),
                "ground_truth_service": cell["service"],
                "chaos_type": cell["chaos_type"],
                "effect_validated": (
                    (entry.get("gates") or {}).get(
                        "intervention_confirmed_on_target"
                    ) is True
                    and (
                        (entry.get("gates") or {}).get("mechanism_observed") or {}
                    ).get("passed") is True
                ),
                "methods": {
                    method: _score(
                        responses.get(method, {}),
                        cell["service"],
                        cell["chaos_type"],
                    )
                    for method in EXPLORATORY_METHODS
                },
                "retrieval": {
                    "pair_ids": [item["pair_id"] for item in neighbors],
                    "scores": [item["score"] for item in neighbors],
                    "same_combination_recalled": same_combination_recalled,
                    "source_pair_ids": [
                        source_pair_by_run.get(item.get("source_run_id"))
                        for item in neighbors
                    ],
                    "retrieval_config": (responses.get("exploratory_rag") or {}).get(
                        "retrieval"
                    ),
                },
            }
        )

    def summarize(selected: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            method: {
                "denominator": len(selected),
                "classification_complete": sum(
                    row["methods"][method]["classification_complete"]
                    for row in selected
                ),
                "service_selected": sum(
                    row["methods"][method]["service_selected"] for row in selected
                ),
                "fault_type_selected": sum(
                    row["methods"][method]["fault_type_selected"]
                    for row in selected
                ),
                "service_correct": sum(
                    row["methods"][method]["service_correct"] for row in selected
                ),
                "fault_type_correct": sum(
                    row["methods"][method]["fault_type_correct"]
                    for row in selected
                ),
                "joint_correct": sum(
                    row["methods"][method]["joint_correct"] for row in selected
                ),
                "confirmed": sum(
                    row["methods"][method]["confirmed"] for row in selected
                ),
                "inconclusive": sum(
                    row["methods"][method]["inconclusive"] for row in selected
                ),
                "errors": sum(
                    row["methods"][method]["status"] == "error" for row in selected
                ),
                "total_duration_ms": sum(
                    row["methods"][method]["duration_ms"] or 0 for row in selected
                ),
                "model_calls": sum(
                    row["methods"][method]["model_calls"] for row in selected
                ),
                "total_tokens": sum(
                    row["methods"][method]["total_tokens"] or 0 for row in selected
                ),
            }
            for method in EXPLORATORY_METHODS
        }

    outcomes = Counter(
        latest.get(cell["cell_id"], {}).get("status", "not_run") for cell in cells
    )
    run_ids = [row["run_id"] for row in rows if row["run_id"]]
    completed_run_ids = [
        row["run_id"] for row in rows if row["run_status"] == "completed"
    ]
    return {
        "version": VERSION,
        "campaign_id": config["experiment_campaign_id"],
        "protocol": (
            "single-run exploratory pilot using unvalidated inconclusive guidance; "
            "not official RAG validation"
            if len(cells) == 1
            else (
                "exploratory classification comparison using historical pending/"
                "inconclusive guidance; always require a best-guess service and fault; "
                "not official causal validation"
            )
        ),
        "planned": {"test": len(cells)},
        "outcomes": dict(outcomes),
        "submitted_run_count": len(run_ids),
        "base": base,
        "label_audit": {
            "sha256": audit.get("sha256"),
            "b_records_audited": (audit.get("counts") or {}).get("b_records_audited"),
            "label_matches": (audit.get("counts") or {}).get("label_matches"),
            "label_mismatches": (audit.get("counts") or {}).get("label_mismatches"),
            "effect_validated_pairs": (audit.get("counts") or {}).get(
                "effect_validated_pairs"
            ),
            "effect_unvalidated_pairs": (audit.get("counts") or {}).get(
                "effect_unvalidated_pairs"
            ),
        },
        "per_run": rows,
        "summary": summarize(rows),
        "completed_summary": summarize(
            [row for row in rows if row["run_status"] == "completed"]
        ),
        "by_effect": {
            str(effect).lower(): summarize(
                [row for row in rows if row["effect_validated"] == effect]
            )
            for effect in (True, False)
        },
        "by_fault": {
            fault: summarize([row for row in rows if row["chaos_type"] == fault])
            for fault in sorted({cell["chaos_type"] for cell in cells})
        },
        "by_service": {
            service: summarize(
                [row for row in rows if row["ground_truth_service"] == service]
            )
            for service in sorted({cell["service"] for cell in cells})
        },
        "recall_at_k": (
            sum(row["retrieval"]["same_combination_recalled"] for row in rows)
            / len(cells)
        ),
        "ground_truth_accessed_after_diagnostics_persisted": bool(completed_run_ids),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    args = parser.parse_args(argv)
    config = yaml.safe_load((args.campaign / "config.yaml").read_text(encoding="utf-8"))
    write_evaluation(args.campaign, config)
    print(args.campaign / "rag_evaluation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
