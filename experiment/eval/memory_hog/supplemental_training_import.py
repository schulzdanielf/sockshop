"""Build a provenance-pinned 35-pair training base from a supplemental campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from experiment.eval.memory_hog import controlled_campaign, supplemental_campaign
from experiment.eval.memory_hog import training_b_rebuild as training_b
from experiment.eval.memory_hog.opensearch_experiments import (
    DEFAULT_OPENSEARCH_URL,
    OPENSEARCH_RETRIEVER_V6,
    OpenSearchExperimentIndex,
)
from experiment.platform.backend.analysis.embeddings import get_embedding_provider
from experiment.platform.backend.history_config import DEFAULT_HISTORICAL_MCP_SSE_URL
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.rca_knowledge import (
    A_TEXT_VERSION,
    BLIND_LIVE_REVIEW_BYPASSED_STATUS,
    BLIND_LIVE_REVIEW_STATUS,
    IncidentA,
    KnowledgeStore,
    QWEN35_MODEL_ID,
    VERSION as KNOWLEDGE_VERSION,
    digest,
    load_skill,
    validated_blind_review_bypass_source_run_ids,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
OBJECT_STORE = REPO_ROOT / "experiment" / "platform" / "data" / "object_store"
DEFAULT_CAMPAIGN = (
    HERE / "out" / "supplemental_campaign"
    / "supplemental-20261109T223633107132Z"
)
PROTOCOL_VERSION = "supplemental-training-import-v2"
BLIND_REVIEW_PROTOCOL_VERSION = "blind-live-review-packet-v2"
EXPECTED_SOURCE_PAIR_COUNT = 32
EXPECTED_PAIR_COUNT = 35
REQUIRED_TRAINING_GATES = (
    "submitted",
    "intervention_confirmed_on_target",
    "mechanism_observed",
    "recovery",
    "cleanup",
    "load_stopped",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _atomic_json(
    path: Path, payload: dict[str, Any], *, permissions: int | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if permissions is not None:
        os.chmod(temporary, permissions)
    temporary.replace(path)


def _aware_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is missing a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp is not timezone-aware")
    return parsed


def phase_windows(report: dict[str, Any]) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    observations = report.get("observations")
    if not isinstance(observations, list):
        raise ValueError("controlled report has no observations")
    source_phases = {
        "baseline": "baseline",
        "fault": "intervention",
        "post": "post",
    }
    windows: dict[str, dict[str, str]] = {}
    provenance: dict[str, Any] = {}
    for phase, observed_phase in source_phases.items():
        matching = [
            item for item in observations
            if isinstance(item, dict) and item.get("phase") == observed_phase
        ]
        if len(matching) != 1:
            raise ValueError(
                f"controlled report must contain one {observed_phase} observation"
            )
        start, end = matching[0].get("start"), matching[0].get("end")
        start_at = _aware_timestamp(start, label=f"{phase}.start")
        end_at = _aware_timestamp(end, label=f"{phase}.end")
        if end_at <= start_at:
            raise ValueError(f"{phase} observation window is not increasing")
        windows[phase] = {"start": start, "end": end}
        provenance[phase] = {
            "source_observation_phase": observed_phase,
            "start": start,
            "end": end,
        }

    ordered = ("baseline", "fault", "post")
    for previous, following in zip(ordered, ordered[1:]):
        if _aware_timestamp(
            windows[previous]["end"], label=f"{previous}.end",
        ) > _aware_timestamp(windows[following]["start"], label=f"{following}.start"):
            raise ValueError(f"{previous} and {following} windows overlap")

    estimate = (report.get("timestamps") or {}).get("intervention_estimate")
    if not isinstance(estimate, dict):
        raise ValueError("controlled report has no intervention timing estimate")
    if (
        estimate.get("start") != windows["fault"]["start"]
        or estimate.get("end") != windows["fault"]["end"]
    ):
        raise ValueError("fault observation differs from its recorded timing estimate")
    provenance["fault"].update({
        "timing_source": estimate.get("source"),
        "duration_verified": estimate.get("duration_verified"),
        "limitation": estimate.get("limitation"),
    })
    return windows, provenance


def _case_run_data(
    campaign_root: Path,
    case: dict[str, Any],
    api: Any,
) -> dict[str, Any]:
    case_dir = supplemental_campaign._case_directory(campaign_root, case)
    latest = supplemental_campaign._latest_entry(case_dir, case["cell_id"])
    if not controlled_campaign.train_cell_gate_passed(latest):
        raise ValueError(f"training gates or history validation failed: {case['cell_id']}")
    run_id = latest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"completed training cell has no run ID: {case['cell_id']}")

    object_dir = OBJECT_STORE / run_id
    report_path = object_dir / "controlled_report.json"
    spec_path = object_dir / "effective_spec.json"
    report = _read_json(report_path)
    effective_spec = _read_json(spec_path)
    if report.get("status") != "completed":
        raise ValueError(f"controlled report is not completed: {run_id}")
    if not isinstance(effective_spec, dict):
        raise ValueError(f"effective spec is malformed: {run_id}")
    policy = effective_spec.get("controlled_campaign") or {}
    if (
        policy.get("level") != "supplemental-6"
        or policy.get("service") != case["service"]
        or policy.get("fault") != case["chaos_type"]
    ):
        raise ValueError(f"effective spec does not match the frozen cell: {case['cell_id']}")
    history = effective_spec.get("historical_observability") or {}
    if history.get("campaign_id") != campaign_root.name:
        raise ValueError(f"run has a different historical campaign ID: {run_id}")

    run = api.get(f"/api/runs/{run_id}")
    if (
        run.get("status") != "completed"
        or (run.get("summary") or {}).get("controlled_status") != "completed"
    ):
        raise ValueError(f"platform run is not completed: {run_id}")
    experiment_id = run.get("experiment_id")
    experiment_version = run.get("experiment_version")
    if not isinstance(experiment_id, str) or type(experiment_version) is not int:
        raise ValueError(f"platform run has invalid experiment provenance: {run_id}")
    saved_experiment = api.get(
        f"/api/experiments/{experiment_id}?version={experiment_version}"
    )
    saved_spec = saved_experiment.get("spec")
    saved_policy = (
        (saved_spec or {}).get("controlled_campaign")
        if isinstance(saved_spec, dict) else None
    )
    if (
        not isinstance(saved_policy, dict)
        or saved_policy.get("service") != case["service"]
        or saved_policy.get("fault") != case["chaos_type"]
        or saved_policy.get("level") != "supplemental-6"
    ):
        raise ValueError(f"platform experiment does not match the frozen cell: {case['cell_id']}")

    api_gates = (run.get("summary") or {}).get("controlled_gates") or {}
    report_gates = report.get("gates") or {}
    for name in REQUIRED_TRAINING_GATES:
        if latest["gates"].get(name) != report_gates.get(name):
            raise ValueError(f"ledger and controlled report disagree on {name}: {run_id}")
        if latest["gates"].get(name) != api_gates.get(name):
            raise ValueError(f"ledger and platform disagree on {name}: {run_id}")

    windows, phase_provenance = phase_windows(report)
    return {
        "case": case,
        "entry": latest,
        "run_id": run_id,
        "report": report,
        "effective_spec": effective_spec,
        "saved_spec": saved_spec,
        "windows": windows,
        "phase_provenance": phase_provenance,
        "report_sha256": _sha256(report_path),
        "effective_spec_sha256": _sha256(spec_path),
        "platform_experiment_sha256": digest(saved_spec),
        "history_validation": latest["historical_validation"],
        "source_pair_sha256": digest({
            "run_id": run_id,
            "case": case,
            "report_sha256": _sha256(report_path),
            "effective_spec_sha256": _sha256(spec_path),
            "platform_experiment_sha256": digest(saved_spec),
            "history_validation": latest["historical_validation"],
        }),
    }


def validate_campaign_inputs(
    campaign_root: Path,
    *,
    api: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    campaign_root = campaign_root.resolve()
    references = supplemental_campaign.validate_reference_inputs()
    parent = _read_json(supplemental_campaign.REFERENCE_TRAINING_BASE)
    if (
        len(parent.get("pairs") or []) != EXPECTED_SOURCE_PAIR_COUNT
        or parent.get("sha256")
        != digest({key: value for key, value in parent.items() if key != "sha256"})
    ):
        raise ValueError("frozen parent training base is not the expected intact 32-pair base")
    parent_run_ids = set(parent.get("training_run_ids") or [])
    parent_pair_run_ids = {
        pair.get("source_run_id") for pair in parent["pairs"]
    }
    if (
        len(parent_run_ids) != EXPECTED_SOURCE_PAIR_COUNT
        or parent_pair_run_ids != parent_run_ids
    ):
        raise ValueError("frozen parent training base has an unexpected run count")

    state = _read_json(campaign_root / "campaign_state.json")
    manifest = _read_json(campaign_root / "campaign_manifest.json")
    addendum_path = campaign_root / "training_source_addendum.json"
    addendum = _read_json(addendum_path)
    if (
        state.get("status") != "awaiting_training_base_review"
        or state.get("training_source_addendum_sha256") != _sha256(addendum_path)
    ):
        raise ValueError("supplemental campaign is not ready for a frozen training-base import")
    if (
        manifest.get("campaign_id") != campaign_root.name
        or manifest.get("protocol_version") != supplemental_campaign.PROTOCOL_VERSION
        or addendum.get("status") != "requires_new_35_pair_base_before_test_phase"
        or addendum.get("parent_training_base_sha256")
        != references["training_base_sha256"]
        or addendum.get("parent_training_run_ids") != parent["training_run_ids"]
    ):
        raise ValueError("supplemental addendum does not match the frozen parent base")

    train_cases = [
        case for case in supplemental_campaign.CASES if case["phase"] == "train"
    ]
    if addendum.get("supplemental_cells") != [
        case["cell_id"] for case in train_cases
    ]:
        raise ValueError("supplemental addendum has an unexpected training-cell matrix")
    expected_run_ids = addendum.get("supplemental_training_run_ids")
    if (
        not isinstance(expected_run_ids, list)
        or len(expected_run_ids) != len(train_cases)
        or len(set(expected_run_ids)) != len(train_cases)
    ):
        raise ValueError("supplemental addendum must list three unique training runs")

    source = supplemental_campaign._source_config(HERE)
    if api is None:
        api = supplemental_campaign.runner.ApiClient(
            source["platform"]["api_base"],
            role=source["platform"]["user_role"],
        )
    runs = [
        _case_run_data(campaign_root, case, api)
        for case in train_cases
    ]
    if [item["run_id"] for item in runs] != expected_run_ids:
        raise ValueError("supplemental addendum run IDs differ from the validated case ledgers")

    parent_run_ids = set(parent["training_run_ids"])
    test_cohort = _read_json(supplemental_campaign.REFERENCE_TEST_COHORT)
    test_run_ids = set(test_cohort.get("test_run_ids") or [])
    if (
        parent_run_ids & test_run_ids
        or set(test_cohort.get("training_run_ids") or []) != parent_run_ids
    ):
        raise ValueError("frozen parent train and test-cohort provenance disagree")
    imported_run_ids = {item["run_id"] for item in runs}
    old_attempt_ids = {
        run_id
        for case in train_cases
        for run_id in case["previous_run_ids"]
    }
    if (
        imported_run_ids & parent_run_ids
        or imported_run_ids & test_run_ids
        or imported_run_ids & old_attempt_ids
    ):
        raise ValueError("supplemental runs overlap a frozen, test, or excluded prior run")

    cells = {
        ((pair.get("labels") or {}).get("service"),
         (pair.get("labels") or {}).get("fault_type"))
        for pair in parent["pairs"]
    }
    cells.update(
        (item["case"]["service"], item["case"]["chaos_type"])
        for item in runs
    )
    expected_cells = {
        (service, fault)
        for service in controlled_campaign.SERVICES
        for fault in controlled_campaign.FAULTS
    }
    if len(cells) != EXPECTED_PAIR_COUNT or cells != expected_cells:
        raise ValueError("32 parent plus 3 supplemental runs do not form the exact 35-cell matrix")
    return {
        "campaign_root": campaign_root,
        "parent": parent,
        "state": state,
        "manifest": manifest,
        "addendum": addendum,
        "references": references,
        "runs": runs,
        "api_base": source["platform"]["api_base"],
        "user_role": source["platform"]["user_role"],
    }, runs


def _pair_source(
    item: dict[str, Any],
    *,
    corpus_id: str,
    client: MCPToolClient,
    output_dir: Path,
    skill_sha256: str,
) -> dict[str, Any]:
    case = item["case"]
    run_id = item["run_id"]
    case_id = f"{item['case']['cell_id']}-{run_id}"
    source_path = output_dir / "source_pairs" / f"{case_id}.json"
    source_manifest = {
        "source_run_id": run_id,
        "cell_id": case["cell_id"],
        "split": "train",
        "campaign_id": item["effective_spec"]["historical_observability"]["campaign_id"],
        "prior_run_ids": list(case["previous_run_ids"]),
        "report_sha256": item["report_sha256"],
        "effective_spec_sha256": item["effective_spec_sha256"],
        "platform_experiment_sha256": item["platform_experiment_sha256"],
        "history_validation": item["history_validation"],
        "phase_provenance": item["phase_provenance"],
    }
    source_manifest_sha256 = digest(source_manifest)
    if source_path.is_file():
        source_pair = _read_json(source_path)
        if (
            source_pair.get("import_provenance", {}).get("source_manifest_sha256")
            != source_manifest_sha256
            or source_pair.get("source_run_id") != run_id
            or source_pair.get("sha256")
            != digest({key: value for key, value in source_pair.items() if key != "sha256"})
        ):
            raise ValueError(f"saved source pair provenance differs: {run_id}")
        return source_pair

    a_dir = output_dir / "initial_a" / case_id
    session = training_b.HistoricalQueries(client, item["windows"], a_dir)
    session.triage()
    incident_a = training_b._live_training_a(session)
    if incident_a.text_version != A_TEXT_VERSION or not incident_a.metric_facts:
        raise ValueError(f"new training A is not a factual metric-backed incident: {run_id}")
    category = training_b.FAULT_FAMILY.get(case["chaos_type"])
    if category is None:
        raise ValueError(f"unsupported training fault type: {case['chaos_type']}")
    source_pair = {
        "version": KNOWLEDGE_VERSION,
        "pair_id": digest([corpus_id, run_id]),
        "source_run_id": run_id,
        "split": "train",
        "labels": {
            "service": case["service"],
            "fault_type": case["chaos_type"],
            "causal_category": category,
        },
        "skill_hash": skill_sha256,
        "a": incident_a.model_dump(),
        "evidence": session.evidence,
        "phases": item["windows"],
        "effect_validated": True,
        "phase_provenance": item["phase_provenance"],
        "import_provenance": {
            **source_manifest,
            "source_manifest_sha256": source_manifest_sha256,
            "initial_a_evidence_sha256": digest(session.evidence),
            "a_text_version": A_TEXT_VERSION,
        },
    }
    source_pair["sha256"] = digest(source_pair)
    _atomic_json(source_path, source_pair)
    _atomic_json(
        a_dir / "incident_a.json",
        {
            "source_run_id": run_id,
            "incident_a": incident_a.model_dump(),
            "evidence": session.evidence,
            "calls": session.calls,
            "phase_windows": item["windows"],
            "phase_provenance": item["phase_provenance"],
            "sha256": source_pair["import_provenance"]["initial_a_evidence_sha256"],
        },
    )
    return source_pair


def _case_result(
    item: dict[str, Any],
    source_pair: dict[str, Any],
    *,
    client: MCPToolClient,
    output_dir: Path,
) -> dict[str, Any]:
    case = item["case"]
    run_id = item["run_id"]
    case_id = f"{case['cell_id']}-{run_id}"
    result_path = output_dir / "case_results" / f"{case_id}.json"
    if result_path.is_file():
        saved = _read_json(result_path)
        if (
            saved.get("case_id") != case_id
            or saved.get("source_run_id") != run_id
            or saved.get("source_pair_sha256") != source_pair.get("sha256")
            or saved.get("model") != QWEN35_MODEL_ID
            or saved.get("rebuild_version") != training_b.REBUILD_VERSION
            or saved.get("historical_mcp_url") != DEFAULT_HISTORICAL_MCP_SSE_URL
        ):
            raise ValueError(f"saved training-B provenance differs: {run_id}")
        return saved

    attempt = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    attempt_dir = output_dir / "investigations" / case_id / attempt
    result = training_b._case_result(
        source_pair,
        case_id,
        client,
        attempt_dir,
    )
    result["source_run_id"] = run_id
    if (
        result.get("status") not in {"procedurally_validated", "procedural_pending"}
        or result.get("source_pair_sha256") != source_pair.get("sha256")
        or result.get("model") != QWEN35_MODEL_ID
    ):
        raise ValueError(f"training-B result has invalid status or provenance: {run_id}")
    _atomic_json(result_path, result)
    return result


def merged_base(
    parent: dict[str, Any],
    imported_pairs: list[dict[str, Any]],
    imported_vectors: dict[str, list[float]],
    *,
    skill_sha256: str,
    import_metadata: dict[str, Any],
) -> dict[str, Any]:
    if len(parent.get("pairs") or []) != EXPECTED_SOURCE_PAIR_COUNT:
        raise ValueError("parent base must contain exactly 32 pairs")
    if len(imported_pairs) != 3:
        raise ValueError("supplemental import must contain exactly three new pairs")
    old_runs = {pair.get("source_run_id") for pair in parent["pairs"]}
    old_pair_ids = {pair.get("pair_id") for pair in parent["pairs"]}
    if (
        len(old_runs) != EXPECTED_SOURCE_PAIR_COUNT
        or None in old_runs
        or len(old_pair_ids) != EXPECTED_SOURCE_PAIR_COUNT
        or None in old_pair_ids
    ):
        raise ValueError("parent base has duplicate or missing run/pair IDs")
    new_runs = [pair.get("source_run_id") for pair in imported_pairs]
    new_pair_ids = [pair.get("pair_id") for pair in imported_pairs]
    if (
        len(set(new_runs)) != len(new_runs)
        or len(set(new_pair_ids)) != len(new_pair_ids)
        or old_runs & set(new_runs)
        or old_pair_ids & set(new_pair_ids)
    ):
        raise ValueError("supplemental pairs have duplicate IDs or overlap the parent")
    if set(imported_vectors) != set(new_pair_ids):
        raise ValueError("supplemental embeddings do not exactly match the new pair IDs")

    expected_supplemental = {
        (case["service"], case["chaos_type"])
        for case in supplemental_campaign.CASES if case["phase"] == "train"
    }
    observed_supplemental = {
        (
            (pair.get("labels") or {}).get("service"),
            (pair.get("labels") or {}).get("fault_type"),
        )
        for pair in imported_pairs
    }
    if observed_supplemental != expected_supplemental:
        raise ValueError("new pair labels do not match the frozen three training cells")
    for pair in imported_pairs:
        if (
            pair.get("split") != "train"
            or pair.get("effect_validated") is not True
            or pair.get("b_rebuild_version") != training_b.TRAINING_B_SCHEMA
            or pair.get("sha256")
            != digest({key: value for key, value in pair.items() if key != "sha256"})
        ):
            raise ValueError(f"new pair failed integrity or training eligibility checks: {pair.get('source_run_id')}")

    parent_index = parent.get("index")
    if not isinstance(parent_index, dict) or set(parent_index) != old_pair_ids:
        raise ValueError("parent embedding index does not match the frozen 32 pairs")
    combined_index = {**parent_index, **imported_vectors}
    payload = {
        **{
            key: value for key, value in parent.items()
            if key not in {
                "sha256", "pairs", "index", "training_run_ids", "skill_sha256",
                "parent_base_sha256", "parent_base_file_sha256",
                "supplemental_training_import",
            }
        },
        "pairs": [*parent["pairs"], *imported_pairs],
        "index": combined_index,
        "training_run_ids": sorted(old_runs | set(new_runs)),
        "skill_sha256": skill_sha256,
        "parent_base_sha256": parent["sha256"],
        "supplemental_training_import": {
            **import_metadata,
            "pair_count": EXPECTED_PAIR_COUNT,
            "parent_pair_count": EXPECTED_SOURCE_PAIR_COUNT,
            "parent_training_run_ids": parent["training_run_ids"],
            "supplemental_pair_count": len(imported_pairs),
            "parent_skill_sha256": parent.get("skill_sha256"),
            "active_skill_sha256": skill_sha256,
            "skill_compatibility": (
                "parent pairs are preserved with historical per-pair hashes; "
                "the base-level hash pins the active query Skill"
            ),
        },
    }
    if len(payload["pairs"]) != EXPECTED_PAIR_COUNT:
        raise ValueError("merged base does not contain exactly 35 pairs")
    if len(combined_index) != EXPECTED_PAIR_COUNT:
        raise ValueError("merged embedding index does not contain exactly 35 vectors")
    payload["sha256"] = digest(payload)
    return payload


def build_training_base(campaign_root: Path) -> dict[str, Any]:
    inputs, runs = validate_campaign_inputs(campaign_root)
    parent = inputs["parent"]
    corpus_id = f"supplemental-training-35-{inputs['campaign_root'].name}"
    output_dir = HERE / "out" / "knowledge_rebuild" / corpus_id
    output_dir.mkdir(parents=True, exist_ok=True)

    models = training_b._served_models()
    if QWEN35_MODEL_ID not in models:
        raise RuntimeError(f"required model {QWEN35_MODEL_ID!r} is not served")
    client = MCPToolClient(DEFAULT_HISTORICAL_MCP_SSE_URL, timeout_seconds=120)
    client.list_tools()
    _, _, skill_sha256 = load_skill()
    source_hashes = {
        "parent_base_file_sha256": inputs["references"]["training_base_sha256"],
        "parent_base_content_sha256": parent["sha256"],
        "supplemental_campaign_manifest_sha256": _sha256(
            inputs["campaign_root"] / "campaign_manifest.json"
        ),
        "supplemental_addendum_sha256": _sha256(
            inputs["campaign_root"] / "training_source_addendum.json"
        ),
        "supplemental_state_sha256": _sha256(
            inputs["campaign_root"] / "campaign_state.json"
        ),
        "training_b_producer_sha256": training_b._producer_sha256(),
        "importer_sha256": _sha256(Path(__file__)),
        "active_skill_sha256": skill_sha256,
        "historical_mcp_url": DEFAULT_HISTORICAL_MCP_SSE_URL,
    }

    imported_pairs = []
    imported_vectors: dict[str, list[float]] = {}
    case_statuses: dict[str, str] = {}
    provider = get_embedding_provider()
    if (
        provider.name != parent.get("embedding_provider")
        or provider.dim != parent.get("embedding_dim")
    ):
        raise ValueError("active embedding provider differs from the frozen parent base")

    for item in runs:
        source_pair = _pair_source(
            item,
            corpus_id=corpus_id,
            client=client,
            output_dir=output_dir,
            skill_sha256=skill_sha256,
        )
        result = _case_result(
            item,
            source_pair,
            client=client,
            output_dir=output_dir,
        )
        pair = training_b._updated_pair(source_pair, result)
        pair["source_pair_sha256"] = result["source_pair_sha256"]
        evidence_ids = {
            evidence.get("id")
            for evidence in pair.get("evidence") or []
            if isinstance(evidence, dict)
        }
        referenced_ids = {
            evidence_id
            for item_a in (
                *(pair.get("a") or {}).get("symptoms", []),
                *(pair.get("a") or {}).get("metric_changes", []),
                *(pair.get("a") or {}).get("service_relations", []),
                *(pair.get("a") or {}).get("log_patterns", []),
                *(pair.get("a") or {}).get("trace_patterns", []),
                *(pair.get("a") or {}).get("temporal_sequence", []),
            )
            for evidence_id in item_a.get("evidence_ids", [])
        }
        referenced_ids.update(
            fact.get("evidence_id")
            for fact in (pair.get("a") or {}).get("metric_facts", [])
        )
        if not referenced_ids or not referenced_ids <= evidence_ids:
            raise ValueError(f"new incident A references missing stored evidence: {item['run_id']}")
        pair["sha256"] = digest(
            {key: value for key, value in pair.items() if key != "sha256"}
        )
        imported_pairs.append(pair)
        vector = provider.embed(
            IncidentA.model_validate(pair["a"]).retrieval_text(
                version=A_TEXT_VERSION
            )
        )
        values = vector.tolist()
        if (
            len(values) != provider.dim
            or any(not math.isfinite(float(value)) for value in values)
        ):
            raise ValueError(f"invalid embedding for new pair {pair['pair_id']}")
        imported_vectors[pair["pair_id"]] = [float(value) for value in values]
        case_statuses[item["run_id"]] = pair["guide_status"]

    import_metadata = {
        "protocol_version": PROTOCOL_VERSION,
        "campaign_id": inputs["campaign_root"].name,
        "parent_base_sha256": parent["sha256"],
        "parent_training_run_ids": parent["training_run_ids"],
        "source_hashes": source_hashes,
        "training_runs": [
            {
                "run_id": item["run_id"],
                "cell_id": item["case"]["cell_id"],
                "service": item["case"]["service"],
                "fault_type": item["case"]["chaos_type"],
                "split": "train",
                "prior_run_ids": item["case"]["previous_run_ids"],
                "effect_validated": True,
                "history_validation": item["history_validation"],
                "report_sha256": item["report_sha256"],
                "effective_spec_sha256": item["effective_spec_sha256"],
                "pair_status": case_statuses[item["run_id"]],
            }
            for item in runs
        ],
        "status_counts": dict(Counter(case_statuses.values())),
    }
    base = merged_base(
        parent,
        imported_pairs,
        imported_vectors,
        skill_sha256=skill_sha256,
        import_metadata=import_metadata,
    )
    base_path = output_dir / "rag_base.json"
    existing_base = None
    if base_path.exists():
        existing_base = _read_json(base_path)
        existing_by_run = {
            pair.get("source_run_id"): pair
            for pair in existing_base.get("pairs") or []
        }
        expected_by_run = {
            pair.get("source_run_id"): pair for pair in base["pairs"]
        }
        parent_by_run = {
            pair.get("source_run_id"): pair for pair in parent["pairs"]
        }
        if (
            existing_base.get("parent_base_sha256") != parent["sha256"]
            or any(
                existing_by_run.get(run_id) != pair
                for run_id, pair in parent_by_run.items()
            )
            or any(
                existing_by_run.get(run_id) != expected_by_run.get(run_id)
                for run_id in case_statuses
            )
        ):
            raise ValueError(
                "existing derived base differs from the verified parent or saved case results"
            )
    _atomic_json(base_path, base)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "version": PROTOCOL_VERSION,
        "corpus_id": corpus_id,
        "base_path": str(base_path.resolve()),
        "base_sha256": base["sha256"],
        "source_pair_count": len(base["pairs"]),
        "parent_pair_count": EXPECTED_SOURCE_PAIR_COUNT,
        "supplemental_pair_count": len(imported_pairs),
        "parent_base_sha256": parent["sha256"],
        "index": None,
        "pair_status_counts": dict(Counter(
            pair["guide_status"] for pair in base["pairs"]
        )),
        "source_hashes": source_hashes,
        "completed_at": datetime.now().astimezone().isoformat(),
    }
    if manifest_path.exists():
        previous = _read_json(manifest_path)
        if previous.get("parent_base_sha256") != parent["sha256"]:
            raise ValueError("derived training-base manifest has a different parent")
        if previous.get("base_sha256") == base["sha256"]:
            manifest["index"] = previous.get("index")
    _atomic_json(manifest_path, manifest)
    return manifest


def index_training_base(
    campaign_root: Path,
    *,
    opensearch_url: str = DEFAULT_OPENSEARCH_URL,
    bypass_blind_review: bool = False,
) -> dict[str, Any]:
    inputs, _ = validate_campaign_inputs(campaign_root)
    corpus_id = f"supplemental-training-35-{inputs['campaign_root'].name}"
    output_dir = HERE / "out" / "knowledge_rebuild" / corpus_id
    base_path = output_dir / "rag_base.json"
    manifest_path = output_dir / "manifest.json"
    base = _read_json(base_path)
    manifest = _read_json(manifest_path)
    if (
        manifest.get("corpus_id") != corpus_id
        or manifest.get("source_pair_count") != EXPECTED_PAIR_COUNT
        or manifest.get("base_sha256") != base.get("sha256")
        or base.get("supplemental_training_import", {}).get("pair_count")
        != EXPECTED_PAIR_COUNT
        or base.get("sha256")
        != digest({key: value for key, value in base.items() if key != "sha256"})
    ):
        raise ValueError("derived training base or manifest is not ready for indexing")
    unreviewed = [
        pair.get("source_run_id")
        for pair in base["pairs"]
        if (
            pair.get("review_status") != BLIND_LIVE_REVIEW_STATUS
            or not re.fullmatch(
                r"[a-f0-9]{64}",
                str(pair.get("review_manifest_sha256") or ""),
            )
        )
    ]
    if unreviewed and not bypass_blind_review:
        raise ValueError(
            "independent blind-live adjudication is required before indexing; "
            f"missing review provenance for {len(unreviewed)} pair(s)"
        )
    index_base_path = base_path
    index_base = base
    if unreviewed:
        index_base_path = output_dir / "rag_base_indexed.json"
        if index_base_path.is_file():
            index_base = _read_json(index_base_path)
            bypass = index_base.get("blind_live_review_bypass")
            if not isinstance(bypass, dict):
                raise ValueError("existing indexed base has no review-bypass record")
            expected_index_base = _prepare_index_base(
                base,
                bypass_blind_review=True,
                authorized_at=bypass.get("authorized_at"),
            )
            if (
                index_base != expected_index_base
                or index_base.get("sha256")
                != digest({
                    key: value
                    for key, value in index_base.items()
                    if key != "sha256"
                })
                or validated_blind_review_bypass_source_run_ids(index_base)
                != set(unreviewed)
            ):
                raise ValueError(
                    "existing indexed base does not match the authorized review bypass"
                )
        else:
            index_base = _prepare_index_base(
                base,
                bypass_blind_review=True,
            )
            _atomic_json(index_base_path, index_base)

    index = OpenSearchExperimentIndex(
        opensearch_url,
        index_base["sha256"],
        retriever_version=OPENSEARCH_RETRIEVER_V6,
    )
    index_record = index.index_base(index_base, a_text_version=A_TEXT_VERSION)
    if index_record.get("document_count") != EXPECTED_PAIR_COUNT:
        raise RuntimeError("OpenSearch index did not confirm all 35 training pairs")

    store = KnowledgeStore(OBJECT_STORE / ".rca_knowledge", corpus_id)
    store_path = store.directory / "exploratory_base.json"
    if store_path.exists():
        installed = _read_json(store_path)
        if installed.get("sha256") != index_base["sha256"]:
            raise ValueError("corpus ID is already installed with a different base")
    else:
        _atomic_json(store_path, index_base)
    _, _, skill_sha256 = load_skill()
    loaded = store.load_exploratory_base(
        expected_skill_sha256=skill_sha256,
        expected_model=QWEN35_MODEL_ID,
    )
    if loaded.get("sha256") != index_base["sha256"]:
        raise RuntimeError("installed corpus differs from the indexed base")
    manifest["index"] = index_record
    manifest["indexed_base_path"] = str(index_base_path.resolve())
    manifest["indexed_base_sha256"] = index_base["sha256"]
    if "blind_live_review_bypass" in index_base:
        manifest["blind_live_review_bypass"] = index_base[
            "blind_live_review_bypass"
        ]
    manifest["indexer_sha256"] = _sha256(Path(__file__))
    manifest["indexed_at"] = datetime.now().astimezone().isoformat()
    _atomic_json(manifest_path, manifest)
    return manifest


def _prepare_index_base(
    candidate_base: dict[str, Any],
    *,
    bypass_blind_review: bool,
    authorized_at: str | None = None,
) -> dict[str, Any]:
    base = json.loads(json.dumps(candidate_base))
    unreviewed = [
        pair
        for pair in base.get("pairs", [])
        if (
            pair.get("review_status") != BLIND_LIVE_REVIEW_STATUS
            or not re.fullmatch(
                r"[a-f0-9]{64}",
                str(pair.get("review_manifest_sha256") or ""),
            )
        )
    ]
    if not unreviewed:
        return base
    if not bypass_blind_review:
        raise ValueError(
            "independent blind-live adjudication is required before indexing"
        )
    import_metadata = base.get("supplemental_training_import")
    training_runs = (
        import_metadata.get("training_runs")
        if isinstance(import_metadata, dict) else None
    )
    supplemental_run_ids = {
        item.get("run_id")
        for item in training_runs or []
        if isinstance(item, dict) and isinstance(item.get("run_id"), str)
    }
    unreviewed_run_ids = {pair.get("source_run_id") for pair in unreviewed}
    if (
        len(unreviewed) != 3
        or len(supplemental_run_ids) != 3
        or unreviewed_run_ids != supplemental_run_ids
        or any(
            "review_status" in pair
            or "review_manifest_sha256" in pair
            or pair.get("guide_status")
            not in {"procedurally_validated", "procedural_pending"}
            or pair.get("b_procedural_status") not in {"passed", "pending"}
            for pair in unreviewed
        )
    ):
        raise ValueError(
            "review bypass is restricted to the three unreviewed supplemental pairs"
        )
    candidate_sha256 = base["sha256"]
    bypass = {
        "policy": "user_authorized_procedural_status",
        "candidate_base_sha256": candidate_sha256,
        "authorized_at": (
            authorized_at or datetime.now().astimezone().isoformat()
        ),
        "unreviewed_source_run_ids": sorted(unreviewed_run_ids),
        "notice": (
            "Independent blind-live adjudication was not completed; no review "
            "manifest or adjudication is claimed."
        ),
    }
    base["blind_live_review_bypass"] = bypass
    for pair in unreviewed:
        pair["review_status"] = BLIND_LIVE_REVIEW_BYPASSED_STATUS
        pair["sha256"] = digest(
            {key: value for key, value in pair.items() if key != "sha256"}
        )
    base["sha256"] = digest(
        {key: value for key, value in base.items() if key != "sha256"}
    )
    if (
        validated_blind_review_bypass_source_run_ids(base)
        != unreviewed_run_ids
    ):
        raise ValueError("authorized review bypass failed its provenance check")
    return base


def _strip_review_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        private_keys = {
            "labels",
            "source_run_id",
            "run_id",
            "cell_id",
            "pair_id",
            "review_status",
            "review_manifest_sha256",
            "b_rebuild_gate_reasons",
            "target_result_private",
            "private_training_target_for_validation_only",
            "import_provenance",
        }
        return {
            key: _strip_review_private_fields(item)
            for key, item in value.items()
            if key not in private_keys
        }
    if isinstance(value, list):
        return [_strip_review_private_fields(item) for item in value]
    return value


def _blind_evidence(items: Any, *, collection: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    allowed = (
        "id",
        "tool",
        "family",
        "kind",
        "source",
        "status",
        "observation_count",
        "fault_observation_count",
        "service",
        "phase",
        "arguments",
        "digest",
        "source_provenance",
        "result_sha256",
    )
    return [
        {
            "collection": collection,
            **{
                key: _strip_review_private_fields(item[key])
                for key in allowed if key in item
            },
        }
        for item in items if isinstance(item, dict)
    ]


def _blind_review_case(pair: dict[str, Any], blind_id: str) -> dict[str, Any]:
    return {
        "blind_id": blind_id,
        "phase_windows": pair.get("phases"),
        "phase_provenance": _strip_review_private_fields(
            pair.get("phase_provenance") or {}
        ),
        "incident_a": _strip_review_private_fields(pair.get("a") or {}),
        "incident_a_evidence": _blind_evidence(
            pair.get("evidence"), collection="incident_a"
        ),
        "response_template": {
            "decision": "adjudicated | insufficient",
            "rationale": "",
            "independent_diagnosis": {
                "status": "confirmed | inconclusive",
                "service": "",
                "fault_type": "",
                "causal_category": "",
                "mechanism": "",
                "supporting_evidence": [],
                "contradicting_evidence": [],
            },
            "reproducible_steps": [],
            "unsupported_claims": [],
            "unsupported_evidence_references": [],
            "scope_violations": [],
            "additional_queries": [],
            "reviewed_at": "",
        },
    }


def prepare_blind_review_packet(
    campaign_root: Path,
    *,
    validated_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inputs = validated_inputs
    if inputs is None:
        inputs, _ = validate_campaign_inputs(campaign_root)
    campaign_root = inputs["campaign_root"]
    corpus_id = f"supplemental-training-35-{campaign_root.name}"
    output_dir = HERE / "out" / "knowledge_rebuild" / corpus_id
    base_path = output_dir / "rag_base.json"
    manifest_path = output_dir / "manifest.json"
    base = _read_json(base_path)
    manifest = _read_json(manifest_path)
    if (
        base.get("sha256")
        != digest({key: value for key, value in base.items() if key != "sha256"})
        or manifest.get("base_sha256") != base.get("sha256")
        or manifest.get("source_pair_count") != EXPECTED_PAIR_COUNT
        or manifest.get("index") is not None
    ):
        raise ValueError("the candidate base is not intact or is already indexed")

    parent_run_ids = set(base["supplemental_training_import"]["parent_training_run_ids"])
    new_pairs = [
        pair for pair in base["pairs"]
        if pair.get("source_run_id") not in parent_run_ids
    ]
    if len(new_pairs) != 3:
        raise ValueError("blind review packet requires exactly three supplemental pairs")
    prior_packet = manifest.get("blind_review_packet")
    packet_path = output_dir / "blind_live_review_packet.json"
    custodian_path = output_dir / "custodian" / "blind_live_review_map.json"
    if packet_path.exists() or custodian_path.exists():
        if packet_path.is_file() and custodian_path.is_file() and (
            isinstance(prior_packet, dict)
            and prior_packet.get("candidate_base_sha256") == base["sha256"]
            and prior_packet.get("packet_sha256") == _sha256(packet_path)
            and prior_packet.get("custodian_map_sha256") == _sha256(custodian_path)
        ):
            return {
                "packet_path": str(packet_path.resolve()),
                "packet_sha256": prior_packet["packet_sha256"],
                "custodian_map_path": str(custodian_path.resolve()),
                "custodian_map_sha256": prior_packet["custodian_map_sha256"],
                "case_count": 3,
            }
        if not packet_path.is_file() or not custodian_path.is_file():
            raise ValueError("an incomplete blind-review packet already exists")
        previous_packet = _read_json(packet_path)
        previous_map = _read_json(custodian_path)
        previous_cases = previous_packet.get("cases")
        legacy_responses_untouched = (
            isinstance(previous_cases, list)
            and len(previous_cases) == 3
            and all(
                isinstance(case, dict)
                and "candidate_guide" in case
                and case.get("response_template")
                == {
                    "decision": "pass | fail | insufficient",
                    "rationale": "",
                    "unsupported_claims": [],
                    "unsupported_evidence_references": [],
                    "scope_violations": [],
                    "additional_queries": [],
                    "reviewed_at": "",
                }
                for case in previous_cases
            )
        )
        if not (
            previous_packet.get("protocol_version")
            == "blind-live-review-packet-v1"
            and previous_map.get("protocol_version")
            == "blind-live-review-packet-v1"
            and previous_map.get("packet_id") == previous_packet.get("packet_id")
            and legacy_responses_untouched
        ):
            raise ValueError("an incomplete or stale blind-review packet already exists")
        packet_path.unlink()
        custodian_path.unlink()

    packet_id = f"blind-live-{uuid.uuid4().hex}"
    cases = []
    custodian_cases = []
    for pair in new_pairs:
        blind_id = f"case-{uuid.uuid4().hex[:12]}"
        cases.append(_blind_review_case(pair, blind_id))
        custodian_cases.append({
            "blind_id": blind_id,
            "source_run_id": pair["source_run_id"],
            "source_pair_id": pair["pair_id"],
            "service": pair["labels"]["service"],
            "fault_type": pair["labels"]["fault_type"],
            "causal_category": pair["labels"]["causal_category"],
            "guide_status": pair["guide_status"],
            "pair_sha256": pair["sha256"],
        })
    packet = {
        "protocol_version": BLIND_REVIEW_PROTOCOL_VERSION,
        "packet_id": packet_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "evidence_note": (
            "Included snapshots came from fresh calls to the historical MCP "
            "during reconstruction. The reviewer must independently query the "
            "same historical windows and attach those new results; the included "
            "snapshots alone are not independent review evidence."
        ),
        "instructions": [
            "Diagnose each case independently without accessing campaign specs, run IDs, training labels, generated B guides, or the custodian map.",
            "Use only incident A and observed evidence as initial hypotheses; do not treat any hidden training result as ground truth.",
            "Verify evidence IDs, values, units, services, phases, and timing limitations.",
            "Use the historical MCP to collect fresh evidence for the supplied windows; record each new query and result under additional_queries.",
            "Record a reproducible evidence-backed diagnosis and steps, plus unsupported claims, references, scope violations, and limitations.",
            "Do not infer or fill in hidden labels; the custodian compares the independent result only after adjudication is complete.",
        ],
        "cases": sorted(cases, key=lambda case: case["blind_id"]),
    }
    serialized = json.dumps(packet, ensure_ascii=False, allow_nan=False)
    private_values = [
        value
        for pair in new_pairs
        for value in (
            pair["source_run_id"],
            pair["pair_id"],
            pair["labels"]["fault_type"],
            pair["labels"]["causal_category"],
        )
    ]
    if any(value in serialized for value in private_values):
        raise ValueError(
            "review packet unexpectedly contains a private source ID or fault label"
        )
    packet["sha256"] = digest(packet)
    custodian_map = {
        "protocol_version": BLIND_REVIEW_PROTOCOL_VERSION,
        "packet_id": packet_id,
        "candidate_base_sha256": base["sha256"],
        "parent_base_sha256": base["parent_base_sha256"],
        "cases": custodian_cases,
        "notice": "Do not share this file with reviewers; it contains the blinded labels and source IDs.",
    }
    _atomic_json(custodian_path, custodian_map, permissions=0o600)
    _atomic_json(packet_path, packet)
    packet_record = {
        "packet_id": packet_id,
        "candidate_base_sha256": base["sha256"],
        "packet_sha256": _sha256(packet_path),
        "custodian_map_sha256": _sha256(custodian_path),
        "packet_path": str(packet_path.resolve()),
        "custodian_map_path": str(custodian_path.resolve()),
        "created_at": packet["created_at"],
    }
    manifest["blind_review_packet"] = packet_record
    _atomic_json(manifest_path, manifest)
    return {
        "packet_path": packet_record["packet_path"],
        "packet_sha256": packet_record["packet_sha256"],
        "custodian_map_path": packet_record["custodian_map_path"],
        "custodian_map_sha256": packet_record["custodian_map_sha256"],
        "case_count": len(cases),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--build", action="store_true")
    mode.add_argument("--index", action="store_true")
    mode.add_argument("--prepare-review", action="store_true")
    parser.add_argument("--campaign", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--bypass-blind-review", action="store_true")
    args = parser.parse_args(argv)
    inputs, runs = validate_campaign_inputs(args.campaign)
    if args.bypass_blind_review and not args.index:
        parser.error("--bypass-blind-review requires --index")
    if args.build:
        result = build_training_base(args.campaign)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.index:
        result = index_training_base(
            args.campaign,
            bypass_blind_review=args.bypass_blind_review,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.prepare_review:
        result = prepare_blind_review_packet(
            args.campaign,
            validated_inputs=inputs,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "parent_base_sha256": inputs["parent"]["sha256"],
        "parent_run_count": len(inputs["parent"]["training_run_ids"]),
        "supplemental_run_count": len(runs),
        "expected_pair_count": EXPECTED_PAIR_COUNT,
        "corpus_id": f"supplemental-training-35-{inputs['campaign_root'].name}",
        "runs": [
            {
                "run_id": item["run_id"],
                "cell_id": item["case"]["cell_id"],
                "service": item["case"]["service"],
                "fault_type": item["case"]["chaos_type"],
                "status": item["entry"]["status"],
                "history_validation": (
                    item["history_validation"].get("status")
                    if isinstance(item["history_validation"], dict) else None
                ),
            }
            for item in runs
        ],
        "indexing_requires_independent_blind_live_adjudication_by_default": True,
        "explicit_user_authorized_review_bypass_available": True,
        "indexing_never_switches_an_existing_alias": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
