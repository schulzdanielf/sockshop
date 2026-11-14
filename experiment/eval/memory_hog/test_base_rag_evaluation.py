"""Compare the original Skill with the latest RAG on a completed test base."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from experiment.eval.memory_hog import rag_campaign
from experiment.eval.memory_hog import supplemental_campaign
from experiment.eval.memory_hog.controlled_campaign import read_ledger
from experiment.eval.memory_hog.evaluation_telemetry import (
    EvaluationTelemetry,
    create_evaluation_telemetry,
)
from experiment.eval.memory_hog.opensearch_experiments import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchExperimentIndex,
)
from experiment.eval.memory_hog.runner import ApiClient
from experiment.platform.backend.evidence_projection import (
    EVIDENCE_SUMMARY_MAX_NEW_TOKENS,
    MODEL_EVIDENCE_CONTEXT_CHARS,
    MODEL_EVIDENCE_SUMMARY_THRESHOLD_CHARS,
    SPLIT_PROJECTION_VERSION,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.rca_knowledge import (
    OPENSEARCH_RETRIEVER_V4,
    OPENSEARCH_RETRIEVER_V5,
    OPENSEARCH_RETRIEVER_V6,
    QWEN35_MODEL_ID,
    KnowledgeStore,
    digest,
    load_skill,
    write_once,
)
from experiment.platform.backend.resource_signature import resource_signature_metadata

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
OBJECT_STORE = REPO_ROOT / "experiment" / "platform" / "data" / "object_store"
DEFAULT_SOURCE_CAMPAIGN = (
    HERE / "out" / "exploratory_campaigns" / "exploratory-rag-20261101T214710257439Z"
)
DEFAULT_CORPUS_ID = "rag-rebuild-qwen35-procedural-metrics-20261106"
DEFAULT_OUTPUT_ROOT = HERE / "out" / "knowledge_evaluation"
PHASES = ("baseline", "fault", "post")
SOURCES = ("metrics", "logs", "traces")
QUERY_COUNT_PER_CASE = len(PHASES) * len(SOURCES)
METHODS = ("original", "exploratory_control", "exploratory_rag")
RAG_ONLY_METHODS = ("exploratory_rag",)
SKILL_AND_RAG_METHODS = ("original", "exploratory_rag")
METHOD_LABELS = {
    "original": "Skill original",
    "exploratory_control": "Controle exploratório",
    "exploratory_rag": "RAG elegível",
}
HISTORICAL_EVALUATION_ID = "test-base-v4-20261107T092616241528Z"
EVALUATION_TEMPERATURE = 0.1
EVALUATION_SEED = 20261107
FACTORIAL_POLICIES = ("none", "cpu-memory-v1")


def _evidence_context_projection_metadata() -> dict[str, Any]:
    return {
        "version": SPLIT_PROJECTION_VERSION,
        "budget_characters": MODEL_EVIDENCE_CONTEXT_CHARS,
        "summary_threshold_characters": MODEL_EVIDENCE_SUMMARY_THRESHOLD_CHARS,
        "partition_unit": "evidence_id",
        "summarization": "neutral_per_partition",
        "summary_item_cache": "source_hash_per_evidence_id",
        "columnar_encoding": "lossless_nested_json_pointer",
        "summary_max_new_tokens": EVIDENCE_SUMMARY_MAX_NEW_TOKENS,
        "numeric_claim_validation": "source_values_only_with_one_repair_then_redact",
    }
FACTORIAL_SCHEDULE_SEED = 20261108
FACTORIAL_BOOTSTRAP_SEED = 20261109
FACTORIAL_SAMPLE_SEED = 20261110
FACTORIAL_BOOTSTRAP_REPLICATES = 10_000


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_ledger(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_ledger(path):
        latest[row["cell_id"]] = row
    return latest


def _pending_v4_pair(pair: dict[str, Any]) -> bool:
    return (
        pair.get("guide_status") == "procedural_pending"
        and pair.get("b_procedural_status") == "pending"
    )


def _eligible_v4_pair(
    pair: dict[str, Any], *, include_unvalidated_procedural: bool = False
) -> bool:
    return (
        pair.get("guide_status") == "procedurally_validated"
        and pair.get("b_procedural_status") == "passed"
    ) or (include_unvalidated_procedural and _pending_v4_pair(pair))


def _validate_v4_pair_count(
    base: dict[str, Any], manifest: dict[str, Any]
) -> None:
    expected_pair_count = manifest.get("source_pair_count", 32)
    if (
        type(expected_pair_count) is not int
        or expected_pair_count < 1
        or base.get("b_rebuild_version") is None
        or len(base.get("pairs") or []) != expected_pair_count
    ):
        raise ValueError(
            "the selected v4 corpus pair count differs from its rebuild manifest"
        )


def _load_supplemental_test_cohort(path: Path) -> dict[str, Any]:
    path = path.resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("protocol_version") != (
        supplemental_campaign.PROTOCOL_VERSION
    ):
        raise ValueError("unsupported supplemental test cohort protocol")

    parent_path = supplemental_campaign.REFERENCE_TEST_COHORT
    parent_sha256 = _file_sha256(parent_path)
    if parent_sha256 != supplemental_campaign.REFERENCE_TEST_SHA256:
        raise ValueError("the pinned parent test cohort has changed")
    if data.get("parent_test_cohort_sha256") != parent_sha256:
        raise ValueError("supplemental test cohort parent hash does not match")
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    parent_runs = parent.get("runs") if isinstance(parent, dict) else None
    runs = data.get("runs")
    if (
        not isinstance(parent_runs, list)
        or len(parent_runs) != 32
        or not isinstance(runs, list)
        or len(runs) != 35
        or runs[:32] != parent_runs
    ):
        raise ValueError(
            "supplemental test cohort must preserve all 32 parent runs and contain 35 runs"
        )

    expected_cells = {
        case["cell_id"]: {
            key: value for key, value in case.items() if key != "previous_run_ids"
        }
        for case in supplemental_campaign.CASES
        if case["phase"] == "test"
    }
    added_runs = runs[32:]
    if any(not isinstance(run, dict) for run in added_runs):
        raise ValueError("supplemental test cohort contains an invalid run")
    if any(not isinstance(run.get("cell"), dict) for run in added_runs):
        raise ValueError("supplemental test cohort contains an invalid test cell")
    if (
        {run.get("cell", {}).get("cell_id") for run in added_runs}
        != set(expected_cells)
        or any(
            run.get("cell") != expected_cells.get(run.get("cell", {}).get("cell_id"))
            or run.get("status") != "completed"
            for run in added_runs
        )
        or any(run.get("status") != "completed" for run in runs)
    ):
        raise ValueError(
            "supplemental test cohort does not contain the three completed test cells"
        )

    run_ids = [run.get("run_id") if isinstance(run, dict) else None for run in runs]
    training_run_ids = data.get("training_run_ids")
    training_base_sha256 = data.get("training_base_sha256")
    if (
        any(not isinstance(run_id, str) or not run_id for run_id in run_ids)
        or len(set(run_ids)) != 35
        or data.get("test_run_ids") != run_ids
        or not isinstance(training_run_ids, list)
        or len(training_run_ids) != 35
        or any(not isinstance(run_id, str) or not run_id for run_id in training_run_ids)
        or len(set(training_run_ids)) != 35
        or set(run_ids) & set(training_run_ids)
    ):
        raise ValueError(
            "supplemental test cohort must contain 35 unique runs disjoint from training"
        )
    if (
        not isinstance(training_base_sha256, str)
        or len(training_base_sha256) != 64
        or any(char not in "0123456789abcdef" for char in training_base_sha256)
    ):
        raise ValueError("supplemental test cohort has an invalid training-base hash")

    return {
        "runs": runs,
        "test_run_ids": run_ids,
        "training_run_ids": training_run_ids,
        "training_base_sha256": training_base_sha256,
        "source_campaign_id": path.parent.name,
        "source_campaign_path": str(path.parent),
        "source_cohort_path": str(path),
        "source_cohort_sha256": _file_sha256(path),
        "parent_test_cohort_sha256": parent_sha256,
        "supplemental_test_run_ids": [run["run_id"] for run in added_runs],
    }


def _load_v4_corpus(
    corpus_id: str, *, retriever_version: str = OPENSEARCH_RETRIEVER_V6
) -> tuple[dict[str, Any], dict[str, Any]]:
    store = KnowledgeStore(OBJECT_STORE / ".rca_knowledge", corpus_id)
    source_path = store.directory / "exploratory_base.json"
    base_file = json.loads(source_path.read_text(encoding="utf-8"))
    source_skill_sha256 = base_file.get("skill_sha256")
    _, _, current_skill_sha256 = load_skill()
    if source_skill_sha256 != current_skill_sha256:
        raise ValueError("v4 corpus Skill hash differs from the active Skill")
    base = store.load_exploratory_base(
        expected_skill_sha256=source_skill_sha256,
        expected_model=QWEN35_MODEL_ID,
    )
    manifest_path = HERE / "out" / "knowledge_rebuild" / corpus_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_v4_pair_count(base, manifest)
    index_record = manifest.get("index") or {}
    indexed_base_sha256 = manifest.get(
        "indexed_base_sha256", manifest.get("base_sha256")
    )
    if (
        manifest.get("corpus_id") != corpus_id
        or indexed_base_sha256 != base.get("sha256")
        or index_record.get("base_sha256") != base.get("sha256")
        or index_record.get("document_count") != len(base["pairs"])
    ):
        raise ValueError("the v4 index manifest does not match the frozen base")
    index = OpenSearchExperimentIndex(
        DEFAULT_OPENSEARCH_URL,
        index_record["base_sha256"],
        retriever_version=retriever_version,
    )
    live_index = index.connect_existing(
        expected_pair_ids={pair["pair_id"] for pair in base["pairs"]},
        expected_a_text_version="incident-a-v2",
    )
    if live_index.get("document_count") != len(base["pairs"]):
        raise ValueError("the live OpenSearch index has an unexpected document count")
    return base, {
        **index_record,
        "retriever_version": retriever_version,
        "live_index": live_index,
        "eligible_pair_count": sum(_eligible_v4_pair(pair) for pair in base["pairs"]),
        "procedural_pending_pair_count": sum(
            _pending_v4_pair(pair) for pair in base["pairs"]
        ),
    }


def prepare_evaluation(
    *,
    source_campaign: Path = DEFAULT_SOURCE_CAMPAIGN,
    corpus_id: str = DEFAULT_CORPUS_ID,
    api_base: str = "http://127.0.0.1:8012",
    mcp_sse_url: str = "http://127.0.0.1:18082/sse",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    include_unvalidated_procedural_experimental: bool = False,
    retriever_version: str = OPENSEARCH_RETRIEVER_V6,
    resource_signature_policy: str = "none",
    selected_test_run_ids: set[str] | None = None,
    test_cohort_path: Path | None = None,
) -> Path:
    if resource_signature_policy not in FACTORIAL_POLICIES:
        raise ValueError("unsupported resource signature policy")
    if retriever_version not in {
        OPENSEARCH_RETRIEVER_V4,
        OPENSEARCH_RETRIEVER_V5,
        OPENSEARCH_RETRIEVER_V6,
    }:
        raise ValueError(
            "test-base evaluation requires a procedural OpenSearch retriever"
        )
    source_campaign = source_campaign.resolve()
    source_config = yaml.safe_load(
        (source_campaign / "config.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(source_config, dict):
        raise ValueError("the source campaign configuration must be a mapping")
    selected_run_ids = (
        set(selected_test_run_ids) if selected_test_run_ids is not None else None
    )
    if selected_run_ids is not None and not selected_run_ids:
        raise ValueError("selected_test_run_ids must not be empty")
    supplemental_cohort = None
    if test_cohort_path is not None:
        if selected_run_ids is not None:
            raise ValueError(
                "selected_test_run_ids cannot be combined with a supplemental test cohort"
            )
        supplemental_cohort = _load_supplemental_test_cohort(test_cohort_path)
        cohort = supplemental_cohort["runs"]
        test_cells = [item["cell"] for item in cohort]
    else:
        latest = _latest_ledger(source_campaign / "ledger.jsonl")
        test_cells = []
        cohort = []
        for cell in source_config.get("test_experiments") or []:
            entry = latest.get(cell["cell_id"], {})
            if entry.get("status") != "completed" or not entry.get("run_id"):
                continue
            if selected_run_ids is not None and entry["run_id"] not in selected_run_ids:
                continue
            test_cells.append(cell)
            cohort.append(
                {
                    "cell": cell,
                    "run_id": entry["run_id"],
                    "status": entry["status"],
                }
            )
    cohort_run_ids = {item["run_id"] for item in cohort}
    if (
        len(cohort) != len(test_cells)
        or len(cohort_run_ids) != len(cohort)
        or (
            supplemental_cohort is None
            and selected_run_ids is None
            and len(cohort) != 32
        )
        or (selected_run_ids is not None and cohort_run_ids != selected_run_ids)
    ):
        raise ValueError("the selected test base must contain unique completed runs")

    base, index_metadata = _load_v4_corpus(
        corpus_id, retriever_version=retriever_version
    )
    test_run_ids = {item["run_id"] for item in cohort}
    training_run_ids = set(base.get("training_run_ids") or [])
    if test_run_ids & training_run_ids:
        raise ValueError("test run IDs overlap the frozen v4 training runs")
    if supplemental_cohort is not None:
        if (
            len(base.get("pairs") or []) != 35
            or len(training_run_ids) != 35
            or training_run_ids != set(supplemental_cohort["training_run_ids"])
        ):
            raise ValueError(
                "supplemental test cohort does not match the 35-run training base"
            )
        installed_base_path = (
            OBJECT_STORE
            / ".rca_knowledge"
            / corpus_id
            / "exploratory_base.json"
        )
        if (
            not installed_base_path.is_file()
            or _file_sha256(installed_base_path)
            != supplemental_cohort["training_base_sha256"]
        ):
            raise ValueError(
                "supplemental test cohort training-base hash does not match the installed corpus"
            )
    validated_pairs = [pair for pair in base["pairs"] if _eligible_v4_pair(pair)]
    pending_pairs = [pair for pair in base["pairs"] if _pending_v4_pair(pair)]
    if len(validated_pairs) != index_metadata["eligible_pair_count"]:
        raise ValueError("v4 retrieval eligibility does not match its stored pair set")
    if len(validated_pairs) + len(pending_pairs) != len(base["pairs"]):
        raise ValueError(
            "v4 corpus contains pairs outside its procedural status policy"
        )
    candidate_pairs = [
        pair
        for pair in base["pairs"]
        if _eligible_v4_pair(
            pair,
            include_unvalidated_procedural=include_unvalidated_procedural_experimental,
        )
    ]

    evaluation_id = f"test-base-{retriever_version.rsplit('-', 1)[-1]}-" + datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%S%fZ")
    output_root.mkdir(parents=True, exist_ok=True)
    directory = output_root / evaluation_id
    directory.mkdir(parents=True, exist_ok=False)

    config = json.loads(json.dumps(source_config))
    config["experiment_campaign_id"] = evaluation_id
    config["train_experiments"] = []
    config["test_experiments"] = test_cells
    config["platform"]["api_base"] = api_base
    config["knowledge_campaign"].update(
        corpus_id=corpus_id,
        external_test_evaluation_id=evaluation_id,
        external_test_base_sha256=base["sha256"],
        exploratory=True,
        retrieval_backend="opensearch",
        opensearch_base_sha256=base["sha256"],
        opensearch_source_skill_sha256=base["skill_sha256"],
        opensearch_current_skill_sha256=base["skill_sha256"],
        allow_legacy_skill_hash=False,
        opensearch_source_model=base["model"],
        opensearch_current_model=QWEN35_MODEL_ID,
        allow_legacy_model_mismatch=False,
        opensearch_a_text_version="incident-a-v2",
        retriever_version=retriever_version,
        include_unvalidated_experimental=False,
        include_unvalidated_procedural_experimental=(
            include_unvalidated_procedural_experimental
        ),
        top_k=3,
        min_score=0 if retriever_version == OPENSEARCH_RETRIEVER_V6 else 0.05,
        max_rounds=4,
        max_model_calls=30,
        max_total_tokens=rag_campaign.EXPLORATORY_MAX_TOTAL_TOKENS,
        timeout_seconds=600,
        model=QWEN35_MODEL_ID,
        temperature=EVALUATION_TEMPERATURE,
        seed=EVALUATION_SEED,
        diagnostic_methods=list(SKILL_AND_RAG_METHODS),
        diagnostic_scope="skill_and_rag",
        resource_signature_policy=resource_signature_policy,
    )
    config["historical_observability"] = {
        "mcp_sse_url": mcp_sse_url,
        "required": True,
        "mode": "dual_write",
        "campaign_id": evaluation_id,
        "timeout_seconds": 180,
        "visibility_timeout_seconds": 600,
    }
    config["output"]["results_dir"] = str(directory)
    (directory / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (directory / "plan.json").write_text(
        json.dumps(test_cells, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_once(directory / "rag_base.json", base)
    cohort_source_campaign_id = (
        supplemental_cohort["source_campaign_id"]
        if supplemental_cohort is not None
        else source_campaign.name
    )
    cohort_source_campaign_path = (
        supplemental_cohort["source_campaign_path"]
        if supplemental_cohort is not None
        else str(source_campaign)
    )
    cohort_record = {
        "source_campaign_id": cohort_source_campaign_id,
        "source_campaign_path": cohort_source_campaign_path,
        "test_run_ids": sorted(test_run_ids),
        "training_run_ids": sorted(training_run_ids),
        "runs": cohort,
    }
    if supplemental_cohort is not None:
        cohort_record.update(
            source_cohort_path=supplemental_cohort["source_cohort_path"],
            source_cohort_sha256=supplemental_cohort["source_cohort_sha256"],
            parent_test_cohort_sha256=supplemental_cohort[
                "parent_test_cohort_sha256"
            ],
            training_base_sha256=supplemental_cohort["training_base_sha256"],
            supplemental_test_run_ids=supplemental_cohort[
                "supplemental_test_run_ids"
            ],
        )
    write_once(directory / "test_cohort.json", cohort_record)
    manifest = {
        "evaluation_id": evaluation_id,
        "protocol": (
            "supplemental_test_cohort_v1_exploratory"
            if supplemental_cohort is not None
            else "existing_test_base_v4_exploratory"
        ),
        "source_campaign_id": cohort_source_campaign_id,
        "source_campaign_path": cohort_source_campaign_path,
        "configuration_source_campaign_id": source_campaign.name,
        "configuration_source_campaign_path": str(source_campaign),
        "corpus_id": corpus_id,
        "corpus_sha256": base["sha256"],
        "evidence_context_projection": _evidence_context_projection_metadata(),
        "external_test_evaluation_id": evaluation_id,
        "external_test_base_sha256": base["sha256"],
        "opensearch_index": index_metadata,
        "retrieval_policy": {
            "include_unvalidated_procedural_experimental": (
                include_unvalidated_procedural_experimental
            ),
            "candidate_pair_count": len(candidate_pairs),
            "corpus_pair_status_counts": {
                "procedurally_validated": len(validated_pairs),
                "procedural_pending": len(pending_pairs),
            },
        },
        "test_case_count": len(cohort),
        "test_run_ids_overlap_training": False,
        "resource_signature_policy": resource_signature_policy,
        "resource_signature_text_sha256": (
            resource_signature_metadata(resource_signature_policy)["text_sha256"]
        ),
        "test_cohort_previously_evaluated": True,
        "independence_note": (
            (
                "Exploratory evaluation: 32 runs are reused from the previously "
                "evaluated parent cohort and three are new supplemental runs; "
                "the combined cohort is not a blind independent test."
                if supplemental_cohort is not None
                else "Exploratory evaluation: this held-out cohort was used in "
                "earlier RAG evaluations and is not a blind independent test."
            )
        ),
    }
    if supplemental_cohort is not None:
        manifest.update(
            source_cohort_path=supplemental_cohort["source_cohort_path"],
            source_cohort_sha256=supplemental_cohort["source_cohort_sha256"],
            parent_test_cohort_sha256=supplemental_cohort[
                "parent_test_cohort_sha256"
            ],
            previously_evaluated_parent_run_count=32,
            new_supplemental_test_run_ids=supplemental_cohort[
                "supplemental_test_run_ids"
            ],
        )
    manifest["sha256"] = digest(manifest)
    write_once(directory / "evaluation_manifest.json", manifest)
    return directory


def _factorial_schedule(
    cohort: list[dict[str, Any]], *, seed: int = FACTORIAL_SCHEDULE_SEED
) -> list[dict[str, Any]]:
    if not cohort or len(cohort) % 2:
        raise ValueError("factorial comparison requires an even, non-empty cohort")
    if len({item["cell"]["cell_id"] for item in cohort}) != len(cohort) or len(
        {item["run_id"] for item in cohort}
    ) != len(cohort):
        raise ValueError("factorial cohort must contain unique cells and runs")
    randomized = list(cohort)
    random.Random(seed).shuffle(randomized)
    base_first = {
        item["cell"]["cell_id"] for item in randomized[: len(randomized) // 2]
    }
    return [
        {
            "index": index,
            "cell_id": item["cell"]["cell_id"],
            "run_id": item["run_id"],
            "policy_order": (
                ["none", "cpu-memory-v1"]
                if item["cell"]["cell_id"] in base_first
                else ["cpu-memory-v1", "none"]
            ),
        }
        for index, item in enumerate(cohort, start=1)
    ]


def _sample_resource_cohort(
    cohort: list[dict[str, Any]], cases_per_type: int, *, seed: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if type(cases_per_type) is not int or cases_per_type < 1:
        raise ValueError("resource sample size must be a positive integer per type")
    strata = ("cpu-hog", "memory-hog")
    candidates = {
        fault_type: sorted(
            (item for item in cohort if item["cell"]["chaos_type"] == fault_type),
            key=lambda item: (item["cell"]["cell_id"], item["run_id"]),
        )
        for fault_type in strata
    }
    if any(len(candidates[fault_type]) < cases_per_type for fault_type in strata):
        raise ValueError(
            "resource sample size exceeds completed runs for cpu-hog or memory-hog"
        )
    generator = random.Random(seed)
    selected_by_type = {
        fault_type: generator.sample(candidates[fault_type], cases_per_type)
        for fault_type in strata
    }
    selected_ids = {
        item["run_id"] for selected in selected_by_type.values() for item in selected
    }
    selected_cohort = [item for item in cohort if item["run_id"] in selected_ids]
    selection = {
        "kind": "balanced_resource_fault_pilot",
        "strata": list(strata),
        "cases_per_type": cases_per_type,
        "population_by_type": {
            fault_type: len(candidates[fault_type]) for fault_type in strata
        },
        "selection_seed": seed,
        "selected_run_ids_by_type": {
            fault_type: sorted(item["run_id"] for item in selected)
            for fault_type, selected in selected_by_type.items()
        },
    }
    return selected_cohort, selection


def prepare_factorial_comparison(
    *,
    source_campaign: Path = DEFAULT_SOURCE_CAMPAIGN,
    corpus_id: str = DEFAULT_CORPUS_ID,
    api_base: str = "http://127.0.0.1:8012",
    mcp_sse_url: str = "http://127.0.0.1:18082/sse",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    retriever_version: str = OPENSEARCH_RETRIEVER_V6,
    resource_cases_per_type: int | None = None,
) -> Path:
    source_campaign = source_campaign.resolve()
    latest = _latest_ledger(source_campaign / "ledger.jsonl")
    source_config = yaml.safe_load(
        (source_campaign / "config.yaml").read_text(encoding="utf-8")
    )
    cohort = [
        {"cell": cell, "run_id": latest[cell["cell_id"]]["run_id"]}
        for cell in source_config.get("test_experiments") or []
        if latest.get(cell["cell_id"], {}).get("status") == "completed"
        and latest[cell["cell_id"]].get("run_id")
    ]
    if resource_cases_per_type is None:
        selection = {
            "kind": "full_completed_test_cohort",
            "cases_per_type": None,
            "selection_seed": None,
            "selected_run_ids": [item["run_id"] for item in cohort],
        }
    else:
        cohort, selection = _sample_resource_cohort(
            cohort,
            resource_cases_per_type,
            seed=FACTORIAL_SAMPLE_SEED,
        )
    schedule = _factorial_schedule(cohort)
    comparison_id = "cpu-memory-factorial-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    directory = output_root.resolve() / comparison_id
    directory.mkdir(parents=True, exist_ok=False)
    arms_root = directory / "arms"
    arms = {}
    for policy in FACTORIAL_POLICIES:
        arm = prepare_evaluation(
            source_campaign=source_campaign,
            corpus_id=corpus_id,
            api_base=api_base,
            mcp_sse_url=mcp_sse_url,
            output_root=arms_root / policy,
            retriever_version=retriever_version,
            resource_signature_policy=policy,
            selected_test_run_ids={item["run_id"] for item in cohort},
        )
        arms[policy] = str(arm.relative_to(directory))
    write_once(directory / "factorial_schedule.json", schedule)
    manifest = {
        "version": 1,
        "comparison_id": comparison_id,
        "design": "paired_2x2_skill_rag_resource_signature",
        "source_campaign_id": source_campaign.name,
        "source_campaign_path": str(source_campaign),
        "corpus_id": corpus_id,
        "policies": list(FACTORIAL_POLICIES),
        "methods": ["original", "exploratory_rag"],
        "arms": arms,
        "evidence_context_projection": _evidence_context_projection_metadata(),
        "schedule_seed": FACTORIAL_SCHEDULE_SEED,
        "schedule_sha256": _file_sha256(directory / "factorial_schedule.json"),
        "schedule_balanced_first_arm": len(schedule) // 2,
        "cohort_selection": selection,
        "analysis_plan": {
            "primary_outcome": "joint_service_and_fault_accuracy",
            "interpretation": (
                f"Exploratory balanced pilot with {resource_cases_per_type} "
                "cpu-hog and memory-hog cases; not confirmatory."
                if resource_cases_per_type is not None
                else "Exploratory comparison on the existing test cohort."
            ),
            "paired_contrasts": [
                "skill_cpu_memory_v1_minus_skill_none",
                "rag_cpu_memory_v1_minus_rag_none",
            ],
            "mcnemar": "exact_two_sided_binomial",
            "multiple_testing": "holm",
            "bootstrap_replicates": FACTORIAL_BOOTSTRAP_REPLICATES,
            "bootstrap_seed": FACTORIAL_BOOTSTRAP_SEED,
            "confidence_interval": "percentile_95",
            "scoring_after_all_four_conditions_sealed": True,
            "cohort_independence": (
                "The existing cohort has been inspected during development; "
                "results are exploratory, not a blind independent estimate."
            ),
        },
    }
    manifest["sha256"] = digest(manifest)
    write_once(directory / "factorial_manifest.json", manifest)
    return directory


def _observation_count(source: str, response: dict[str, Any]) -> int:
    data = response.get("data") or {}
    if source == "metrics":
        return sum(
            len(series.get("values") or []) + int("value" in series)
            for series in data.get("result") or []
        )
    if source == "logs":
        return sum(
            len(stream.get("values") or []) for stream in data.get("result") or []
        )
    return len(response.get("traces") or [])


def _queries_for_window(
    window: dict[str, str]
) -> list[tuple[str, str, dict[str, Any]]]:
    start, end = window["start"], window["end"]
    return [
        (
            "prometheus_range_query",
            "metrics",
            {
                "query": 'sum(up{kubernetes_namespace="sock-shop"})',
                "start": start,
                "end": end,
                "step": "60s",
            },
        ),
        (
            "loki_range_query",
            "logs",
            {
                "query": '{namespace="sock-shop"}',
                "start": start,
                "end": end,
                "limit": 5,
            },
        ),
        (
            "tempo_search_traces",
            "traces",
            {
                "query": '{ resource.k8s.namespace.name = "sock-shop" }',
                "start": start,
                "end": end,
                "limit": 1,
            },
        ),
    ]


def _query_result(
    client: Any,
    tool: str,
    source: str,
    phase: str,
    window: dict[str, str],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = client.call_tool_json(tool, arguments)
        if not isinstance(response, dict):
            raise ValueError("MCP returned a non-object response")
        if (
            response.get("status") in {"error", "failed"}
            or response.get("error")
            or response.get("isError") is True
        ):
            raise RuntimeError(str(response.get("error") or response)[:500])
        count = _observation_count(source, response)
        return {
            "source": source,
            "phase": phase,
            "tool": tool,
            "window": window,
            "arguments": arguments,
            "status": "ok" if count else "no_data",
            "observation_count": count,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "response_sha256": digest(response),
            "response": response,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "source": source,
            "phase": phase,
            "tool": tool,
            "window": window,
            "arguments": arguments,
            "status": "error",
            "observation_count": 0,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def collect_preinference_mcp(directory: Path) -> dict[str, Any]:
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort_data = json.loads(
        (directory / "test_cohort.json").read_text(encoding="utf-8")
    )
    client = MCPToolClient(
        config["historical_observability"]["mcp_sse_url"], timeout_seconds=60
    )
    api = ApiClient(
        config["platform"]["api_base"], role=config["platform"]["user_role"]
    )
    failures: list[str] = []
    case_hashes: dict[str, str] = {}
    query_count = 0
    collection_started = datetime.now(timezone.utc)

    for index, item in enumerate(cohort_data["runs"], start=1):
        cell = item["cell"]
        cell_id = cell["cell_id"]
        case_path = directory / "preinference_mcp" / f"{cell_id}.json"
        if case_path.exists():
            saved = json.loads(case_path.read_text(encoding="utf-8"))
            if (
                saved.get("run_id") != item["run_id"]
                or len(saved.get("queries") or []) != QUERY_COUNT_PER_CASE
            ):
                raise ValueError(f"incomplete existing MCP precollection for {cell_id}")
        else:
            window_data = api.get(
                f"/api/runs/{quote(item['run_id'], safe='')}/observation-window"
            )
            if (
                not isinstance(window_data, dict)
                or window_data.get("run_id") != item["run_id"]
                or window_data.get("status") != "completed"
            ):
                raise RuntimeError(
                    f"could not load phase windows for {item['run_id']}: "
                    f"{window_data!r}"
                )
            phases = window_data.get("phases") or {}
            queries = []
            for phase in PHASES:
                window = phases.get(phase)
                if (
                    not isinstance(window, dict)
                    or not isinstance(window.get("start"), str)
                    or not isinstance(window.get("end"), str)
                ):
                    raise ValueError(
                        f"{item['run_id']} is missing its {phase} observation window"
                    )
                queries.extend(
                    _query_result(client, tool, source, phase, window, arguments)
                    for tool, source, arguments in _queries_for_window(window)
                )
            saved = {
                "run_id": item["run_id"],
                "cell_id": cell_id,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "source": "historical_mcp",
                "queries": queries,
            }
            write_once(case_path, saved)

        queries = saved["queries"]
        query_count += len(queries)
        case_hashes[cell_id] = _file_sha256(case_path)
        case_errors = [
            f"{cell_id}:{query['source']}/{query['phase']}: {query.get('error')}"
            for query in queries
            if query.get("status") == "error"
        ]
        failures.extend(case_errors)
        print(
            f"[MCP precollection] {index}/{len(cohort_data['runs'])} "
            f"{cell_id}: {len(queries) - len(case_errors)}/{QUERY_COUNT_PER_CASE} "
            "queries without backend errors",
            flush=True,
        )

    all_queries = [
        query
        for item in cohort_data["runs"]
        for query in json.loads(
            (
                directory / "preinference_mcp" / f"{item['cell']['cell_id']}.json"
            ).read_text(encoding="utf-8")
        )["queries"]
    ]
    coverage = {
        source: {
            phase: {
                "ok": sum(
                    query["status"] == "ok"
                    for query in all_queries
                    if query["source"] == source and query["phase"] == phase
                ),
                "no_data": sum(
                    query["status"] == "no_data"
                    for query in all_queries
                    if query["source"] == source and query["phase"] == phase
                ),
                "errors": sum(
                    query["status"] == "error"
                    for query in all_queries
                    if query["source"] == source and query["phase"] == phase
                ),
                "observations": sum(
                    query["observation_count"]
                    for query in all_queries
                    if query["source"] == source and query["phase"] == phase
                ),
            }
            for phase in PHASES
        }
        for source in SOURCES
    }
    collection_finished = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "status": "complete" if not failures else "blocked",
        "evaluation_id": config["experiment_campaign_id"],
        "config_sha256": digest(config),
        "cohort_sha256": _file_sha256(directory / "test_cohort.json"),
        "collection_started_at": collection_started.isoformat(),
        "collected_at": collection_finished.isoformat(),
        "elapsed_seconds": round(
            (collection_finished - collection_started).total_seconds(), 3
        ),
        "case_count": len(cohort_data["runs"]),
        "queries_per_case": QUERY_COUNT_PER_CASE,
        "query_count": query_count,
        "status_counts": dict(Counter(query["status"] for query in all_queries)),
        "coverage_by_source_phase": coverage,
        "case_file_sha256": case_hashes,
        "errors": failures,
    }
    payload["sha256"] = digest(payload)
    write_once(directory / "precollection_manifest.json", payload)
    if failures or query_count != len(cohort_data["runs"]) * QUERY_COUNT_PER_CASE:
        raise RuntimeError(
            f"MCP precollection blocked: {len(failures)} errors across "
            f"{query_count} queries; no inference was started"
        )
    return payload


def validate_precollection(directory: Path) -> dict[str, Any]:
    manifest = json.loads(
        (directory / "precollection_manifest.json").read_text(encoding="utf-8")
    )
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    payload = {key: value for key, value in manifest.items() if key != "sha256"}
    if (
        manifest.get("sha256") != digest(payload)
        or manifest.get("status") != "complete"
    ):
        raise ValueError("complete, hash-valid MCP precollection is required")
    evaluation_manifest = json.loads(
        (directory / "evaluation_manifest.json").read_text(encoding="utf-8")
    )
    evaluation_manifest_payload = {
        key: value for key, value in evaluation_manifest.items() if key != "sha256"
    }
    if (
        evaluation_manifest.get("sha256") != digest(evaluation_manifest_payload)
        or evaluation_manifest.get("evaluation_id")
        != config.get("experiment_campaign_id")
        or evaluation_manifest.get("corpus_sha256")
        != config["knowledge_campaign"].get("external_test_base_sha256")
        or evaluation_manifest.get("retrieval_policy", {}).get(
            "include_unvalidated_procedural_experimental", False
        )
        != bool(
            config["knowledge_campaign"].get(
                "include_unvalidated_procedural_experimental", False
            )
        )
        or evaluation_manifest.get("resource_signature_policy", "none")
        != config["knowledge_campaign"].get("resource_signature_policy", "none")
        or evaluation_manifest.get("resource_signature_text_sha256")
        != resource_signature_metadata(
            config["knowledge_campaign"].get("resource_signature_policy", "none")
        )["text_sha256"]
    ):
        raise ValueError("evaluation manifest does not match the pinned base/config")
    cohort = json.loads((directory / "test_cohort.json").read_text(encoding="utf-8"))
    if (
        manifest.get("case_count") != len(cohort["runs"])
        or manifest.get("query_count") != len(cohort["runs"]) * QUERY_COUNT_PER_CASE
        or manifest.get("queries_per_case") != QUERY_COUNT_PER_CASE
        or manifest.get("evaluation_id") != config.get("experiment_campaign_id")
        or manifest.get("config_sha256") != digest(config)
        or manifest.get("cohort_sha256") != _file_sha256(directory / "test_cohort.json")
    ):
        raise ValueError("MCP precollection does not cover the full test cohort")
    for item in cohort["runs"]:
        cell_id = item["cell"]["cell_id"]
        path = directory / "preinference_mcp" / f"{cell_id}.json"
        if not path.is_file() or _file_sha256(path) != manifest["case_file_sha256"].get(
            cell_id
        ):
            raise ValueError(f"MCP precollection evidence changed for {cell_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        queries = record.get("queries") or []
        expected_pairs = {(source, phase) for source in SOURCES for phase in PHASES}
        seen_pairs = {(query.get("source"), query.get("phase")) for query in queries}
        if (
            record.get("run_id") != item["run_id"]
            or len(queries) != QUERY_COUNT_PER_CASE
            or seen_pairs != expected_pairs
            or any(query.get("status") == "error" for query in queries)
        ):
            raise ValueError(f"MCP precollection is incomplete for {cell_id}")
        for query in queries:
            response = query.get("response")
            window = query.get("window")
            if (
                not isinstance(response, dict)
                or query.get("response_sha256") != digest(response)
                or query.get("observation_count")
                != _observation_count(query["source"], response)
                or query.get("status")
                != ("ok" if query["observation_count"] else "no_data")
                or not isinstance(window, dict)
                or not isinstance(window.get("start"), str)
                or not isinstance(window.get("end"), str)
            ):
                raise ValueError(
                    f"MCP query evidence is malformed for {cell_id}: "
                    f"{query.get('source')}/{query.get('phase')}"
                )
            arguments = query.get("arguments")
            expected_by_source = {
                source: (tool, expected_arguments)
                for tool, source, expected_arguments in _queries_for_window(window)
            }
            if arguments is not None and (
                not isinstance(arguments, dict)
                or arguments.get("start") != query["window"]["start"]
                or arguments.get("end") != query["window"]["end"]
            ):
                raise ValueError(
                    f"MCP query window differs from its arguments for {cell_id}"
                )
            expected = expected_by_source.get(query["source"])
            if (
                expected is None
                or query.get("tool") != expected[0]
                or arguments != expected[1]
                or query.get("phase") not in PHASES
            ):
                raise ValueError(
                    f"MCP query is not the expected label-neutral search for {cell_id}"
                )
            if arguments is not None and (
                "service_name" in arguments
                or "container" in arguments
                or "fault_type" in arguments
                or "chaos_type" in arguments
            ):
                raise ValueError(f"MCP query contains target labels for {cell_id}")
    return manifest


def _diagnostic_methods(config: dict[str, Any]) -> tuple[str, ...]:
    selected = config["knowledge_campaign"].get("diagnostic_methods", list(METHODS))
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(method, str) for method in selected)
        or len(selected) != len(set(selected))
        or any(method not in METHODS for method in selected)
    ):
        raise ValueError("invalid test diagnostic method selection")
    scope = config["knowledge_campaign"].get("diagnostic_scope")
    if scope == "rag_only" and tuple(selected) != RAG_ONLY_METHODS:
        raise ValueError("RAG-only evaluation must run exploratory_rag only")
    if scope == "skill_and_rag" and tuple(selected) != SKILL_AND_RAG_METHODS:
        raise ValueError(
            "Skill-and-RAG evaluation must run original and exploratory_rag"
        )
    return tuple(selected)


def _validate_generation_support(
    api: ApiClient, *, require_resource_signature_policy: bool = True
) -> None:
    openapi = api.get("/openapi.json")
    schemas = (openapi.get("components") or {}).get("schemas") or {}
    properties = (schemas.get("RCABenchmarkOptions") or {}).get("properties") or {}
    required = {"model", "temperature", "seed"}
    if require_resource_signature_policy:
        required.add("resource_signature_policy")
    missing = required - set(properties)
    if missing:
        missing_fields = ", ".join(sorted(missing))
        raise RuntimeError(
            "The platform API has not loaded the evaluation generation settings "
            f"({missing_fields}; restart the API before inference)"
        )


def _load_factorial_manifest(
    directory: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Path]]:
    directory = directory.resolve()
    manifest = json.loads((directory / "factorial_manifest.json").read_text())
    payload = {key: value for key, value in manifest.items() if key != "sha256"}
    if manifest.get("sha256") != digest(payload):
        raise ValueError("factorial manifest hash is invalid")
    if manifest.get("policies") != list(FACTORIAL_POLICIES):
        raise ValueError("factorial manifest has an unexpected policy matrix")
    schedule_path = directory / "factorial_schedule.json"
    if _file_sha256(schedule_path) != manifest.get("schedule_sha256"):
        raise ValueError("factorial schedule changed after preparation")
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    control_first_count = sum(
        row.get("policy_order") == ["none", "cpu-memory-v1"] for row in schedule
    )
    if (
        not schedule
        or len(schedule) % 2
        or control_first_count != len(schedule) // 2
        or len({row.get("cell_id") for row in schedule}) != len(schedule)
        or len({row.get("run_id") for row in schedule}) != len(schedule)
        or any(
            len(row.get("policy_order") or []) != len(FACTORIAL_POLICIES)
            or set(row["policy_order"]) != set(FACTORIAL_POLICIES)
            for row in schedule
        )
        or (
            manifest.get("schedule_balanced_first_arm") is not None
            and manifest["schedule_balanced_first_arm"] != len(schedule) // 2
        )
    ):
        raise ValueError("factorial schedule is incomplete or unbalanced")
    arms: dict[str, Path] = {}
    for policy, relative in manifest["arms"].items():
        arm = (directory / relative).resolve()
        try:
            arm.relative_to(directory)
        except ValueError as exc:
            raise ValueError(
                "factorial arm path escapes the comparison directory"
            ) from exc
        arms[policy] = arm
    if set(arms) != set(FACTORIAL_POLICIES):
        raise ValueError("factorial comparison must contain exactly two policy arms")
    return manifest, schedule, arms


def precollect_factorial_comparison(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    manifest, _, arms = _load_factorial_manifest(directory)
    if (directory / "factorial_precollection.json").exists():
        raise FileExistsError("factorial MCP precollection is already sealed")
    collections = {}
    for policy in FACTORIAL_POLICIES:
        collections[policy] = collect_preinference_mcp(arms[policy])
    payload = {
        "status": "complete",
        "comparison_id": manifest["comparison_id"],
        "manifest_sha256": manifest["sha256"],
        "arms": {
            policy: {
                "evaluation_id": collections[policy]["evaluation_id"],
                "manifest_sha256": _file_sha256(
                    arms[policy] / "precollection_manifest.json"
                ),
                "query_count": collections[policy]["query_count"],
            }
            for policy in FACTORIAL_POLICIES
        },
    }
    payload["sha256"] = digest(payload)
    write_once(directory / "factorial_precollection.json", payload)
    return payload


def _mcnemar_exact_p_value(improved: int, regressed: int) -> float:
    discordant = improved + regressed
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, value) for value in range(min(improved, regressed) + 1)
    )
    return min(1.0, 2 * tail / (2**discordant))


def _bootstrap_delta_interval(
    deltas: list[int],
    *,
    seed: int,
    replicates: int = FACTORIAL_BOOTSTRAP_REPLICATES,
) -> list[float] | None:
    if not deltas:
        return None
    generator = random.Random(seed)
    samples = sorted(
        sum(generator.choice(deltas) for _ in deltas) / len(deltas)
        for _ in range(replicates)
    )

    def percentile(fraction: float) -> float:
        position = (len(samples) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return samples[lower]
        return samples[lower] * (upper - position) + samples[upper] * (position - lower)

    return [percentile(0.025), percentile(0.975)]


def _paired_condition_contrast(
    control_rows: list[dict[str, Any]],
    treatment_rows: list[dict[str, Any]],
    method: str,
) -> dict[str, Any]:
    control = {row["run_id"]: row for row in control_rows}
    treatment = {row["run_id"]: row for row in treatment_rows}
    if set(control) != set(treatment):
        raise ValueError("factorial conditions do not contain the same run IDs")
    deltas = []
    for run_id in sorted(control):
        before = control[run_id]
        after = treatment[run_id]
        if (
            before["cell_id"] != after["cell_id"]
            or before["service"] != after["service"]
            or before["fault_type"] != after["fault_type"]
        ):
            raise ValueError("factorial condition labels differ for a paired run")
        deltas.append(
            int(after["methods"][method]["joint_correct"])
            - int(before["methods"][method]["joint_correct"])
        )
    improved = sum(delta > 0 for delta in deltas)
    regressed = sum(delta < 0 for delta in deltas)
    return {
        "method": method,
        "paired_cases": len(deltas),
        "improved": improved,
        "regressed": regressed,
        "unchanged": len(deltas) - improved - regressed,
        "control_correct": sum(
            row["methods"][method]["joint_correct"] for row in control.values()
        ),
        "treatment_correct": sum(
            row["methods"][method]["joint_correct"] for row in treatment.values()
        ),
        "accuracy_delta": sum(deltas) / len(deltas) if deltas else None,
        "mcnemar_exact_two_sided_p": _mcnemar_exact_p_value(improved, regressed),
        "bootstrap_95_percentile_ci": _bootstrap_delta_interval(
            deltas,
            seed=FACTORIAL_BOOTSTRAP_SEED + (0 if method == "original" else 1),
        ),
    }


def _holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    adjusted: dict[str, float] = {}
    running_max = 0.0
    count = len(ordered)
    for index, (name, p_value) in enumerate(ordered):
        running_max = max(running_max, min(1.0, (count - index) * p_value))
        adjusted[name] = running_max
    return adjusted


def _seal_factorial_arm(directory: Path, elapsed_seconds: float) -> dict[str, Any]:
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort = json.loads((directory / "test_cohort.json").read_text(encoding="utf-8"))
    methods = _diagnostic_methods(config)
    if tuple(methods) != SKILL_AND_RAG_METHODS:
        raise ValueError("factorial arms must run both Skill and RAG")
    completion = directory / "diagnostics_complete.json"
    if completion.is_file():
        previous = json.loads(completion.read_text(encoding="utf-8"))
        payload = {key: value for key, value in previous.items() if key != "sha256"}
        if (
            previous.get("sha256") != digest(payload)
            or previous.get("config_sha256") != digest(config)
            or previous.get("evaluation_id") != config.get("experiment_campaign_id")
        ):
            raise ValueError("existing factorial completion marker is invalid")
        return previous
    completed = []
    for item in cohort["runs"]:
        cell = item["cell"]
        for method in methods:
            path = rag_campaign.result_path(directory, cell, method)
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("run_id") != item["run_id"]
                or record.get("response", {}).get("status") == "error"
            ):
                raise RuntimeError(
                    f"factorial diagnostic incomplete for {cell['cell_id']} ({method})"
                )
            completed.append(
                {
                    "cell_id": cell["cell_id"],
                    "method": method,
                    "sha256": _file_sha256(path),
                }
            )
    precollection = json.loads(
        (directory / "precollection_manifest.json").read_text(encoding="utf-8")
    )
    marker = {
        "status": "complete",
        "evaluation_id": config["experiment_campaign_id"],
        "config_sha256": digest(config),
        "precollection_sha256": precollection["sha256"],
        "methods": list(methods),
        "elapsed_seconds": elapsed_seconds,
        "diagnostic_count": len(completed),
        "records": completed,
    }
    marker["sha256"] = digest(marker)
    write_once(completion, marker)
    return marker


def _execute_factorial_comparison(
    directory: Path,
    manifest: dict[str, Any],
    schedule: list[dict[str, Any]],
    arms: dict[str, Path],
    precollection: dict[str, Any],
    configs: dict[str, dict[str, Any]],
    cohorts: dict[str, dict[str, Any]],
    apis: dict[str, ApiClient],
    telemetries: dict[str, EvaluationTelemetry | None],
) -> dict[str, Any]:
    report_path = directory / "factorial_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_payload = {
            key: value for key, value in report.items() if key != "sha256"
        }
        completion_path = directory / "factorial_complete.json"
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        completion_payload = {
            key: value for key, value in completion.items() if key != "sha256"
        }
        if (
            report.get("sha256") != digest(report_payload)
            or completion.get("sha256") != digest(completion_payload)
            or completion.get("report_sha256") != report.get("sha256")
            or completion.get("manifest_sha256") != manifest["sha256"]
            or completion.get("precollection_sha256") != precollection["sha256"]
        ):
            raise ValueError(
                "persisted factorial report or completion marker is invalid"
            )
        reports = {policy: build_report(arms[policy]) for policy in FACTORIAL_POLICIES}
        for policy in FACTORIAL_POLICIES:
            if (
                digest(reports[policy])
                != report["arm_reports"][policy]["report_sha256"]
            ):
                raise ValueError("factorial arm report changed after comparison")
            telemetry = telemetries[policy]
            if telemetry:
                telemetry.update_progress(
                    {
                        "evaluation_cases_expected": len(schedule),
                        "evaluation_cases_completed": len(schedule),
                        "evaluation_cases_failed": 0,
                        "evaluation_cases_in_progress": 0,
                        "evaluation_current_case_index": len(schedule),
                        "evaluation_diagnostics_expected": 2 * len(schedule),
                        "evaluation_diagnostics_completed": 2 * len(schedule),
                        "evaluation_completion_ratio": 1,
                    }
                )
                telemetry.set_scoring()
                telemetry.record_report(reports[policy])
                telemetry.complete()
        return report

    elapsed = {policy: 0.0 for policy in FACTORIAL_POLICIES}
    for schedule_row, cohort_row in zip(schedule, cohorts["none"]["runs"]):
        cell = cohort_row["cell"]
        for policy in schedule_row["policy_order"]:
            telemetry = telemetries[policy]
            index = schedule_row["index"]
            if telemetry:
                telemetry.update_progress(
                    {
                        "evaluation_cases_in_progress": 1,
                        "evaluation_current_case_index": index,
                        "evaluation_run_last_update_timestamp_seconds": time.time(),
                        "evaluation_elapsed_seconds": time.time()
                        - telemetry.started_at,
                    }
                )
            started = time.monotonic()
            try:
                if telemetry:
                    with telemetry.case_span(
                        case_id=f"{policy}:{cell['cell_id']}",
                        run_id=schedule_row["run_id"],
                        index=index,
                        strategy=f"{policy}:{','.join(SKILL_AND_RAG_METHODS)}",
                    ):
                        rag_campaign.process_cell(
                            arms[policy],
                            configs[policy],
                            cell,
                            {"run_id": schedule_row["run_id"]},
                            apis[policy],
                        )
                else:
                    rag_campaign.process_cell(
                        arms[policy],
                        configs[policy],
                        cell,
                        {"run_id": schedule_row["run_id"]},
                        apis[policy],
                    )
            except Exception:
                if telemetry:
                    telemetry.update_progress(
                        {
                            "evaluation_cases_failed": 1,
                            "evaluation_cases_in_progress": 0,
                            "evaluation_run_last_update_timestamp_seconds": time.time(),
                            "evaluation_elapsed_seconds": (
                                time.time() - telemetry.started_at
                            ),
                        }
                    )
                raise
            elapsed[policy] += time.monotonic() - started
            if telemetry:
                telemetry.update_progress(
                    {
                        "evaluation_cases_completed": index,
                        "evaluation_cases_in_progress": 0,
                        "evaluation_current_case_index": index,
                        "evaluation_diagnostics_completed": 2 * index,
                        "evaluation_completion_ratio": (
                            index / len(schedule) if schedule else 0
                        ),
                        "evaluation_run_last_update_timestamp_seconds": time.time(),
                        "evaluation_elapsed_seconds": time.time()
                        - telemetry.started_at,
                    }
                )

    sealed = {
        policy: _seal_factorial_arm(arms[policy], round(elapsed[policy], 3))
        for policy in FACTORIAL_POLICIES
    }
    if any(marker.get("status") != "complete" for marker in sealed.values()):
        raise RuntimeError("factorial scoring is blocked until both arms are sealed")
    for policy in FACTORIAL_POLICIES:
        telemetry = telemetries[policy]
        if telemetry:
            telemetry.set_scoring()

    # Ground-truth scoring starts only after all four conditions are sealed.
    reports = {policy: build_report(arms[policy]) for policy in FACTORIAL_POLICIES}
    control_rows = reports["none"]["per_run"]
    treatment_rows = reports["cpu-memory-v1"]["per_run"]
    contrasts = {
        method: _paired_condition_contrast(control_rows, treatment_rows, method)
        for method in SKILL_AND_RAG_METHODS
    }
    adjusted_p = _holm_adjust(
        {
            method: contrast["mcnemar_exact_two_sided_p"]
            for method, contrast in contrasts.items()
        }
    )
    for method, contrast in contrasts.items():
        contrast["mcnemar_holm_adjusted_p"] = adjusted_p[method]
    method_gap = {
        policy: (
            reports[policy]["diagnosis_summary"]["exploratory_rag"]["joint_accuracy"]
            - reports[policy]["diagnosis_summary"]["original"]["joint_accuracy"]
        )
        for policy in FACTORIAL_POLICIES
    }

    memory_confusion = {}
    for policy, report in reports.items():
        memory_confusion[policy] = {}
        for method in SKILL_AND_RAG_METHODS:
            matrix: dict[str, dict[str, int]] = {
                fault: {"cpu-hog": 0, "memory-hog": 0, "other": 0}
                for fault in ("cpu-hog", "memory-hog")
            }
            for row in report["per_run"]:
                if row["fault_type"] not in matrix:
                    continue
                prediction = row["predictions"][method]["fault_type"]
                bucket = (
                    prediction if prediction in {"cpu-hog", "memory-hog"} else "other"
                )
                matrix[row["fault_type"]][bucket] += 1
            memory_confusion[policy][method] = matrix
    result = {
        "version": 1,
        "comparison_id": manifest["comparison_id"],
        "design": manifest["design"],
        "status": "complete",
        "policies": list(FACTORIAL_POLICIES),
        "methods": list(SKILL_AND_RAG_METHODS),
        "cohort_size": len(control_rows),
        "cohort_selection": manifest.get("cohort_selection"),
        "analysis_plan": manifest["analysis_plan"],
        "paired_contrasts": contrasts,
        "rag_minus_skill_accuracy_gap_by_policy": method_gap,
        "orientation_interaction_rag_minus_skill": (
            contrasts["exploratory_rag"]["accuracy_delta"]
            - contrasts["original"]["accuracy_delta"]
        ),
        "resource_type_confusion": memory_confusion,
        "arm_reports": {
            policy: {
                "evaluation_id": reports[policy]["evaluation_id"],
                "directory": manifest["arms"][policy],
                "report_sha256": (
                    _file_sha256(arms[policy] / "test_base_rag_evaluation.json")
                    if (arms[policy] / "test_base_rag_evaluation.json").is_file()
                    else digest(reports[policy])
                ),
                "diagnostic_methods": reports[policy]["provenance"][
                    "diagnostic_methods"
                ],
                "diagnosis_summary": reports[policy]["diagnosis_summary"],
                "performance": reports[policy]["performance"],
            }
            for policy in FACTORIAL_POLICIES
        },
        "mcp_precollection": {
            policy: precollection["arms"][policy] for policy in FACTORIAL_POLICIES
        },
        "independence": manifest["analysis_plan"]["cohort_independence"],
    }
    result["sha256"] = digest(result)
    write_once(report_path, result)
    completion = {
        "status": "complete",
        "comparison_id": manifest["comparison_id"],
        "manifest_sha256": manifest["sha256"],
        "precollection_sha256": precollection["sha256"],
        "schedule_sha256": manifest["schedule_sha256"],
        "report_sha256": result["sha256"],
    }
    completion["sha256"] = digest(completion)
    write_once(directory / "factorial_complete.json", completion)
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))
    if persisted_report.get("sha256") != result["sha256"] or persisted_report.get(
        "sha256"
    ) != digest(
        {key: value for key, value in persisted_report.items() if key != "sha256"}
    ):
        raise ValueError("factorial report failed persisted integrity validation")
    for policy in FACTORIAL_POLICIES:
        telemetry = telemetries[policy]
        if telemetry:
            telemetry.record_report(reports[policy])
            telemetry.complete(elapsed_seconds=elapsed[policy])
    return result


def _run_factorial_with_telemetry(
    directory: Path,
    manifest: dict[str, Any],
    schedule: list[dict[str, Any]],
    arms: dict[str, Path],
    precollection: dict[str, Any],
    configs: dict[str, dict[str, Any]],
    cohorts: dict[str, dict[str, Any]],
    apis: dict[str, ApiClient],
) -> dict[str, Any]:
    telemetries: dict[str, EvaluationTelemetry | None] = {}
    try:
        for policy in FACTORIAL_POLICIES:
            config = configs[policy]
            knowledge = config["knowledge_campaign"]
            telemetry = create_evaluation_telemetry(
                evaluation_id=config["experiment_campaign_id"],
                retriever_version=knowledge["retriever_version"],
                expected_cases=len(schedule),
                expected_diagnostics=2 * len(schedule),
            )
            telemetries[policy] = telemetry
            if telemetry:
                apis[policy] = ApiClient(
                    config["platform"]["api_base"],
                    role=config["platform"]["user_role"],
                    tracer=telemetry.tracer,
                )
        return _execute_factorial_comparison(
            directory,
            manifest,
            schedule,
            arms,
            precollection,
            configs,
            cohorts,
            apis,
            telemetries,
        )
    except Exception:
        for telemetry in telemetries.values():
            if telemetry:
                telemetry.fail()
        raise
    finally:
        for telemetry in telemetries.values():
            if telemetry:
                telemetry.flush_and_shutdown()


def run_factorial_comparison(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    manifest, schedule, arms = _load_factorial_manifest(directory)
    precollection_path = directory / "factorial_precollection.json"
    if not precollection_path.is_file():
        raise ValueError("fresh MCP precollection is required for both factorial arms")
    precollection = json.loads(precollection_path.read_text(encoding="utf-8"))
    precollection_payload = {
        key: value for key, value in precollection.items() if key != "sha256"
    }
    if (
        precollection.get("sha256") != digest(precollection_payload)
        or precollection.get("manifest_sha256") != manifest["sha256"]
        or precollection.get("status") != "complete"
    ):
        raise ValueError("factorial precollection is invalid or mismatched")

    configs = {}
    cohorts = {}
    apis = {}
    for policy in FACTORIAL_POLICIES:
        validate_precollection(arms[policy])
        configs[policy] = yaml.safe_load(
            (arms[policy] / "config.yaml").read_text(encoding="utf-8")
        )
        cohorts[policy] = json.loads(
            (arms[policy] / "test_cohort.json").read_text(encoding="utf-8")
        )
        if (
            configs[policy]["knowledge_campaign"].get(
                "resource_signature_policy", "none"
            )
            != policy
        ):
            raise ValueError("factorial arm resource policy differs from its manifest")
        api = ApiClient(
            configs[policy]["platform"]["api_base"],
            role=configs[policy]["platform"]["user_role"],
        )
        api.get("/api/health")
        _validate_generation_support(api)
        apis[policy] = api
    run_sets = [
        [
            (
                item["cell"]["cell_id"],
                item["run_id"],
                item["cell"]["service"],
                item["cell"]["chaos_type"],
            )
            for item in cohorts[policy]["runs"]
        ]
        for policy in FACTORIAL_POLICIES
    ]
    if run_sets[0] != run_sets[1]:
        raise ValueError("factorial arms do not share the same ordered test cohort")
    if schedule and [(row["cell_id"], row["run_id"]) for row in schedule] != [
        (row[0], row[1]) for row in run_sets[0]
    ]:
        raise ValueError("factorial schedule does not match the held-out cohort")
    arm_knowledge = []
    for policy in FACTORIAL_POLICIES:
        settings = dict(configs[policy]["knowledge_campaign"])
        settings.pop("resource_signature_policy", None)
        settings.pop("external_test_evaluation_id", None)
        arm_knowledge.append(settings)
    if arm_knowledge[0] != arm_knowledge[1]:
        raise ValueError("factorial arms differ in shared knowledge settings")
    for section in ("platform", "historical_observability"):
        arm_settings = []
        for policy in FACTORIAL_POLICIES:
            settings = dict(configs[policy].get(section) or {})
            if section == "historical_observability":
                settings.pop("campaign_id", None)
            arm_settings.append(settings)
        if arm_settings[0] != arm_settings[1]:
            raise ValueError(f"factorial arms differ in shared {section} settings")
    return _run_factorial_with_telemetry(
        directory,
        manifest,
        schedule,
        arms,
        precollection,
        configs,
        cohorts,
        apis,
    )


def _token_usage_counts(response: dict[str, Any]) -> dict[str, int]:
    attempts = response.get("model_attempts") or []
    measured = sum(
        int(
            isinstance(attempt, dict)
            and isinstance(attempt.get("usage"), dict)
            and bool(attempt["usage"].get("total_tokens"))
        )
        for attempt in attempts
    )
    return {
        "attempts": len(attempts),
        "measured_calls": measured,
        "estimated_calls": len(attempts) - measured,
    }


def _mean(values: list[float | int]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def _method_performance(
    rows: list[dict[str, Any]],
    method: str,
    *,
    token_budget_per_case: int,
    call_budget_per_case: int,
) -> dict[str, Any]:
    scores = [row["methods"][method] for row in rows]
    request_times = [
        score["request_duration_ms"]
        for score in scores
        if score["request_duration_ms"] is not None
    ]
    response_times = [
        score["duration_ms"] for score in scores if score["duration_ms"] is not None
    ]
    token_values = [
        score["total_tokens"] for score in scores if score["total_tokens"] is not None
    ]
    measured_calls = sum(score["token_usage_measured_calls"] for score in scores)
    estimated_calls = sum(score["token_usage_estimated_calls"] for score in scores)
    total_tokens = sum(token_values)
    total_budget = token_budget_per_case * len(rows)
    return {
        "case_count": len(rows),
        "mean_response_duration_ms": _mean(response_times),
        "mean_request_duration_ms": _mean(request_times),
        "mean_total_tokens_per_case": _mean(token_values),
        "total_tokens": total_tokens if token_values else None,
        "token_usage_case_count": len(token_values),
        "mean_model_calls_per_case": _mean(
            [score["model_calls"] or 0 for score in scores]
        ),
        "total_model_calls": sum(score["model_calls"] or 0 for score in scores),
        "measured_token_usage_calls": measured_calls,
        "estimated_token_usage_calls": estimated_calls,
        "budget_tokens_per_case": token_budget_per_case,
        "call_budget_per_case": call_budget_per_case,
        "theoretical_campaign_budget_tokens": total_budget,
        "budget_utilization": (
            round(total_tokens / total_budget, 6)
            if token_values and total_budget
            else None
        ),
        "max_case_budget_utilization": (
            round(max(token_values) / token_budget_per_case, 6)
            if token_values and token_budget_per_case
            else None
        ),
    }


def _historical_comparison() -> dict[str, Any] | None:
    directory = DEFAULT_OUTPUT_ROOT / HISTORICAL_EVALUATION_ID
    report_path = directory / "test_base_rag_evaluation.json"
    if not report_path.is_file():
        return None
    historical = json.loads(report_path.read_text(encoding="utf-8"))
    historical_config = yaml.safe_load(
        (directory / "config.yaml").read_text(encoding="utf-8")
    )
    historical_knowledge = historical_config.get("knowledge_campaign") or {}
    markdown_report_path = directory / "test_base_rag_evaluation_report.md"
    rows = historical.get("per_run") or []
    performance: dict[str, Any] = {}
    for method in METHODS:
        response_times = []
        request_times = []
        token_values = []
        model_calls = []
        measured_calls = 0
        estimated_calls = 0
        for row in rows:
            path = rag_campaign.result_path(
                directory, {"cell_id": row["cell_id"]}, method
            )
            if not path.is_file():
                continue
            response = (
                json.loads(path.read_text(encoding="utf-8")).get("response") or {}
            )
            if isinstance(response.get("duration_ms"), (int, float)):
                response_times.append(response["duration_ms"])
            if isinstance(response.get("request_duration_ms"), (int, float)):
                request_times.append(response["request_duration_ms"])
            if isinstance(response.get("total_tokens"), (int, float)):
                token_values.append(response["total_tokens"])
            model_calls.append(
                response.get("model_calls") or len(response.get("model_attempts") or [])
            )
            if isinstance(response.get("total_tokens"), (int, float)):
                token_usage = _token_usage_counts(response)
                measured_calls += token_usage["measured_calls"]
                estimated_calls += token_usage["estimated_calls"]
        performance[method] = {
            "case_count": len(rows),
            "jointly_correct": (
                historical.get("diagnosis_summary", {})
                .get(method, {})
                .get("jointly_correct")
            ),
            "joint_accuracy": (
                historical.get("diagnosis_summary", {})
                .get(method, {})
                .get("joint_accuracy")
            ),
            "mean_response_duration_ms": _mean(response_times),
            "mean_request_duration_ms": _mean(request_times),
            "mean_total_tokens_per_case": _mean(token_values),
            "total_tokens": sum(token_values) if token_values else None,
            "token_usage_case_count": len(token_values),
            "mean_model_calls_per_case": _mean(model_calls),
            "total_model_calls": sum(model_calls),
            "measured_token_usage_calls": measured_calls,
            "estimated_token_usage_calls": estimated_calls,
        }
    return {
        "evaluation_id": historical.get("evaluation_id"),
        "report_path": str(report_path),
        "markdown_report_path": (
            str(markdown_report_path) if markdown_report_path.is_file() else None
        ),
        "retrieval": historical.get("retrieval"),
        "diagnosis_summary": historical.get("diagnosis_summary"),
        "budgets": {
            "max_total_tokens_per_case": historical_knowledge.get("max_total_tokens"),
            "max_model_calls_per_case": historical_knowledge.get("max_model_calls"),
        },
        "performance": performance,
        "per_run": rows,
    }


def run_diagnostics(
    directory: Path, *, telemetry: EvaluationTelemetry | None = None
) -> dict[str, Any]:
    validate_precollection(directory)
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort = json.loads((directory / "test_cohort.json").read_text(encoding="utf-8"))
    methods = _diagnostic_methods(config)
    expected_cases = len(cohort["runs"])
    expected_diagnostics = expected_cases * len(methods)
    if telemetry:
        telemetry.update_progress(
            {
                "evaluation_run_state": 0,
                "evaluation_cases_expected": expected_cases,
                "evaluation_cases_completed": 0,
                "evaluation_cases_failed": 0,
                "evaluation_cases_in_progress": 0,
                "evaluation_current_case_index": 0,
                "evaluation_diagnostics_expected": expected_diagnostics,
                "evaluation_diagnostics_completed": 0,
                "evaluation_completion_ratio": 0,
            }
        )
    api = ApiClient(
        config["platform"]["api_base"],
        role=config["platform"]["user_role"],
        tracer=telemetry.tracer if telemetry else None,
    )
    api.get("/api/health")
    _validate_generation_support(api)
    diagnostics_started = time.monotonic()
    for index, item in enumerate(cohort["runs"], start=1):
        cell = item["cell"]
        print(
            f"[diagnostics] {index}/{len(cohort['runs'])} {cell['cell_id']}",
            flush=True,
        )
        if telemetry:
            telemetry.update_progress(
                {
                    "evaluation_cases_in_progress": 1,
                    "evaluation_current_case_index": index,
                    "evaluation_run_last_update_timestamp_seconds": time.time(),
                    "evaluation_elapsed_seconds": time.time() - telemetry.started_at,
                }
            )
        try:
            if telemetry:
                with telemetry.case_span(
                    case_id=cell["cell_id"],
                    run_id=item["run_id"],
                    index=index,
                    strategy=",".join(methods),
                ):
                    rag_campaign.process_cell(
                        directory,
                        config,
                        cell,
                        {"run_id": item["run_id"]},
                        api,
                    )
            else:
                rag_campaign.process_cell(
                    directory,
                    config,
                    cell,
                    {"run_id": item["run_id"]},
                    api,
                )
        except Exception:
            if telemetry:
                telemetry.update_progress(
                    {
                        "evaluation_cases_failed": 1,
                        "evaluation_cases_in_progress": 0,
                        "evaluation_run_last_update_timestamp_seconds": time.time(),
                        "evaluation_elapsed_seconds": (
                            time.time() - telemetry.started_at
                        ),
                    }
                )
            raise
        if telemetry:
            completed_diagnostics = index * len(methods)
            telemetry.update_progress(
                {
                    "evaluation_cases_completed": index,
                    "evaluation_cases_in_progress": 0,
                    "evaluation_current_case_index": index,
                    "evaluation_diagnostics_completed": completed_diagnostics,
                    "evaluation_completion_ratio": (
                        index / expected_cases if expected_cases else 0
                    ),
                    "evaluation_run_last_update_timestamp_seconds": time.time(),
                    "evaluation_elapsed_seconds": time.time() - telemetry.started_at,
                }
            )

    completed = []
    for item in cohort["runs"]:
        cell = item["cell"]
        for method in methods:
            path = rag_campaign.result_path(directory, cell, method)
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                record.get("run_id") != item["run_id"]
                or record.get("response", {}).get("status") == "error"
            ):
                raise RuntimeError(
                    f"diagnostic did not complete cleanly for {cell['cell_id']} "
                    f"({method})"
                )
            completed.append(
                {
                    "cell_id": cell["cell_id"],
                    "method": method,
                    "sha256": _file_sha256(path),
                }
            )
    elapsed_seconds = round(time.monotonic() - diagnostics_started, 3)
    marker = {
        "status": "complete",
        "evaluation_id": config["experiment_campaign_id"],
        "config_sha256": digest(config),
        "precollection_sha256": json.loads(
            (directory / "precollection_manifest.json").read_text(encoding="utf-8")
        )["sha256"],
        "methods": list(methods),
        "elapsed_seconds": elapsed_seconds,
        "diagnostic_count": len(completed),
        "records": completed,
    }
    marker["sha256"] = digest(marker)
    write_once(directory / "diagnostics_complete.json", marker)
    if telemetry:
        telemetry.set_scoring()
    return marker


def _retrieval_row(
    retrieved: list[dict[str, Any]],
    expected_pair_ids: set[str],
    pair_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if len(retrieved) > 3:
        raise ValueError("RAG response returned more than the configured top-3")
    pair_ids = [item["pair_id"] for item in retrieved]
    if len(pair_ids) != len(set(pair_ids)):
        raise ValueError("RAG response contains duplicate retrieved pair IDs")
    if any(pair_id not in pair_by_id for pair_id in pair_ids):
        raise ValueError("RAG response contains a pair outside the frozen v4 corpus")
    relevant = [pair_id for pair_id in pair_ids if pair_id in expected_pair_ids]
    first_rank = next(
        (
            rank
            for rank, pair_id in enumerate(pair_ids, start=1)
            if pair_id in expected_pair_ids
        ),
        None,
    )
    return {
        "retrieved_pair_ids": pair_ids,
        "retrieved_pairs": [
            {
                "pair_id": pair_id,
                "guide_status": pair_by_id[pair_id].get("guide_status"),
                "b_procedural_status": pair_by_id[pair_id].get("b_procedural_status"),
                "relevant": pair_id in expected_pair_ids,
            }
            for pair_id in pair_ids
        ],
        "relevant_pair_ids": relevant,
        "relevant_pairs_in_top3": len(relevant),
        "hit_at_3": bool(relevant),
        "first_relevant_rank": first_rank,
        "reciprocal_rank_at_3": 1 / first_rank if first_rank else 0.0,
        "precision_at_3": len(relevant) / 3,
        "precision_at_returned": len(relevant) / len(pair_ids) if pair_ids else None,
    }


def _conditional_diagnosis_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    retrieved = [row for row in rows if row["retrieval"]["hit_at_3"]]
    not_retrieved = [row for row in rows if not row["retrieval"]["hit_at_3"]]

    def accuracy(subset: list[dict[str, Any]]) -> dict[str, Any]:
        correct = sum(
            row["methods"]["exploratory_rag"]["joint_correct"] for row in subset
        )
        return {
            "denominator": len(subset),
            "jointly_correct": correct,
            "joint_accuracy": correct / len(subset) if subset else None,
        }

    return {
        "definition": (
            "Correct diagnosis means the RAG final service and fault type jointly "
            "match the test labels; relevant retrieval means a top-3 pair has "
            "the same service and fault type."
        ),
        "with_relevant_retrieval": accuracy(retrieved),
        "without_relevant_retrieval": accuracy(not_retrieved),
        "overall": accuracy(rows),
    }


def _group_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    result = {}
    for value in sorted({row[field] for row in rows}):
        selected = [row for row in rows if row[field] == value]
        score = [row["methods"]["exploratory_rag"] for row in selected]
        result[value] = {
            "denominator": len(selected),
            "eligible_exact_pair_cases": sum(
                bool(row["eligible_exact_pair_ids"]) for row in selected
            ),
            "hit_at_3_cases": sum(row["retrieval"]["hit_at_3"] for row in selected),
            "service_correct": sum(row["service_correct"] for row in score),
            "fault_type_correct": sum(row["fault_type_correct"] for row in score),
            "jointly_correct": sum(row["joint_correct"] for row in score),
            "joint_accuracy": (
                sum(row["joint_correct"] for row in score) / len(selected)
                if selected
                else None
            ),
            "diagnosis_correct_given_retrieval": _conditional_diagnosis_metrics(
                selected
            )["with_relevant_retrieval"],
        }
    return result


def build_report(directory: Path) -> dict[str, Any]:
    validate_precollection(directory)
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort_data = json.loads(
        (directory / "test_cohort.json").read_text(encoding="utf-8")
    )
    methods = _diagnostic_methods(config)
    evaluation_manifest = json.loads(
        (directory / "evaluation_manifest.json").read_text(encoding="utf-8")
    )
    completion = json.loads(
        (directory / "diagnostics_complete.json").read_text(encoding="utf-8")
    )
    completion_payload = {
        key: value for key, value in completion.items() if key != "sha256"
    }
    if (
        completion.get("status") != "complete"
        or completion.get("sha256") != digest(completion_payload)
        or completion.get("diagnostic_count") != len(cohort_data["runs"]) * len(methods)
        or completion.get("methods") != list(methods)
        or completion.get("evaluation_id") != config.get("experiment_campaign_id")
        or completion.get("config_sha256") != digest(config)
        or completion.get("precollection_sha256")
        != json.loads(
            (directory / "precollection_manifest.json").read_text(encoding="utf-8")
        ).get("sha256")
    ):
        raise ValueError(
            "all persisted diagnostics are required before ground-truth scoring"
        )
    expected_record_hashes = {
        (record["cell_id"], record["method"]): record["sha256"]
        for record in completion["records"]
    }

    base = json.loads((directory / "rag_base.json").read_text(encoding="utf-8"))
    base_payload = {key: value for key, value in base.items() if key != "sha256"}
    knowledge = config["knowledge_campaign"]
    if base.get("sha256") != digest(base_payload) or base.get(
        "sha256"
    ) != knowledge.get("opensearch_base_sha256"):
        raise ValueError("evaluation corpus differs from the pinned v4 base")
    include_unvalidated_procedural = bool(
        knowledge.get("include_unvalidated_procedural_experimental", False)
    )
    eligible_pairs = [
        pair
        for pair in base["pairs"]
        if _eligible_v4_pair(
            pair,
            include_unvalidated_procedural=include_unvalidated_procedural,
        )
    ]
    pair_by_id = {pair["pair_id"]: pair for pair in eligible_pairs}
    rows = []
    for item in cohort_data["runs"]:
        cell = item["cell"]
        responses = {}
        for method in methods:
            path = rag_campaign.result_path(directory, cell, method)
            if _file_sha256(path) != expected_record_hashes.get(
                (cell["cell_id"], method)
            ):
                raise ValueError(
                    f"persisted diagnostic changed after completion: "
                    f"{cell['cell_id']} ({method})"
                )
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("run_id") != item["run_id"]:
                raise ValueError("diagnostic run ID differs from the held-out test run")
            responses[method] = record["response"]
        rag = responses["exploratory_rag"]
        if rag.get("base_sha256") != base["sha256"]:
            raise ValueError("persisted RAG response used a different frozen base")
        target_pair_ids = {
            pair["pair_id"]
            for pair in eligible_pairs
            if (pair.get("labels") or {}).get("service") == cell["service"]
            and (pair.get("labels") or {}).get("fault_type") == cell["chaos_type"]
        }
        retrieval = _retrieval_row(
            rag.get("retrieved") or [], target_pair_ids, pair_by_id
        )
        method_scores = {}
        predictions = {}
        for method in methods:
            response = responses[method]
            score = rag_campaign._score(response, cell["service"], cell["chaos_type"])
            token_usage = _token_usage_counts(response)
            score.update(
                request_duration_ms=response.get("request_duration_ms"),
                token_usage_measured_calls=token_usage["measured_calls"],
                token_usage_estimated_calls=token_usage["estimated_calls"],
            )
            method_scores[method] = score
            diagnosis = response.get("diagnosis") or {}
            predictions[method] = {
                "status": diagnosis.get("status", response.get("status")),
                "service": diagnosis.get("primary_service"),
                "fault_type": diagnosis.get("fault_type"),
            }
        rows.append(
            {
                "cell_id": cell["cell_id"],
                "run_id": item["run_id"],
                "service": cell["service"],
                "fault_type": cell["chaos_type"],
                "eligible_exact_pair_ids": sorted(target_pair_ids),
                "retrieval": retrieval,
                "predictions": predictions,
                "methods": method_scores,
            }
        )

    count = len(rows)
    matchable = [row for row in rows if row["eligible_exact_pair_ids"]]
    hit_rows = [row for row in rows if row["retrieval"]["hit_at_3"]]
    total_relevant = sum(row["retrieval"]["relevant_pairs_in_top3"] for row in rows)
    total_retrieved = sum(len(row["retrieval"]["retrieved_pair_ids"]) for row in rows)
    method_comparison = None
    if "original" in methods and "exploratory_rag" in methods:
        skill_correct = [row["methods"]["original"]["joint_correct"] for row in rows]
        rag_correct = [
            row["methods"]["exploratory_rag"]["joint_correct"] for row in rows
        ]
        method_comparison = {
            "paired_case_count": count,
            "skill_only_correct": sum(
                skill and not rag for skill, rag in zip(skill_correct, rag_correct)
            ),
            "rag_only_correct": sum(
                rag and not skill for skill, rag in zip(skill_correct, rag_correct)
            ),
            "both_correct": sum(
                skill and rag for skill, rag in zip(skill_correct, rag_correct)
            ),
            "neither_correct": sum(
                not skill and not rag for skill, rag in zip(skill_correct, rag_correct)
            ),
            "joint_correct_delta_rag_minus_skill": sum(rag_correct)
            - sum(skill_correct),
        }

    return {
        "version": 1,
        "evaluation_id": config["experiment_campaign_id"],
        "protocol": (
            "test_base_skill_and_rag_eligible_v6"
            if tuple(methods) == SKILL_AND_RAG_METHODS
            else (
                "test_base_rag_only_eligible_v6"
                if tuple(methods) == RAG_ONLY_METHODS
                else "test_base_multi_method_eligible_v6"
            )
        ),
        "independence": {
            "status": "not_blind",
            "explanation": (
                "The held-out test cohort was used in earlier RAG evaluations; "
                "results are exploratory and not an independent blind estimate."
            ),
        },
        "provenance": {
            "source_campaign_id": cohort_data["source_campaign_id"],
            "corpus_id": knowledge["corpus_id"],
            "corpus_sha256": base["sha256"],
            "opensearch_index": evaluation_manifest["opensearch_index"],
            "retrieval_policy": evaluation_manifest.get(
                "retrieval_policy",
                {
                    "include_unvalidated_procedural_experimental": False,
                    "candidate_pair_count": len(eligible_pairs),
                    "corpus_pair_status_counts": {
                        "procedurally_validated": sum(
                            _eligible_v4_pair(pair) for pair in base["pairs"]
                        ),
                        "procedural_pending": sum(
                            _pending_v4_pair(pair) for pair in base["pairs"]
                        ),
                    },
                },
            ),
            "candidate_pair_count": len(eligible_pairs),
            "test_run_count": count,
            "test_run_overlap_with_training": False,
            "retrieval_qrels_read_after_diagnostics_persisted": True,
            "diagnostic_methods": list(methods),
            "retriever_version": knowledge.get("retriever_version"),
            "diagnostic_scope": knowledge.get("diagnostic_scope", "all_methods"),
            "resource_signature_policy": knowledge.get(
                "resource_signature_policy", "none"
            ),
            "resource_signature_text_sha256": resource_signature_metadata(
                knowledge.get("resource_signature_policy", "none")
            )["text_sha256"],
        },
        "method_comparison": method_comparison,
        "retrieval": {
            "top_k": 3,
            "candidate_pair_count": len(eligible_pairs),
            "candidate_pair_status_counts": {
                "procedurally_validated": sum(
                    pair.get("b_procedural_status") == "passed"
                    for pair in eligible_pairs
                ),
                "procedural_pending": sum(
                    pair.get("b_procedural_status") == "pending"
                    for pair in eligible_pairs
                ),
            },
            "retrieved_pair_status_counts": {
                "procedurally_validated": sum(
                    item["b_procedural_status"] == "passed"
                    for row in rows
                    for item in row["retrieval"]["retrieved_pairs"]
                ),
                "procedural_pending": sum(
                    item["b_procedural_status"] == "pending"
                    for row in rows
                    for item in row["retrieval"]["retrieved_pairs"]
                ),
            },
            "test_case_denominator": count,
            "eligible_exact_pair_cases": len(matchable),
            "eligible_exact_pair_coverage": len(matchable) / count if count else None,
            "hit_at_3_cases": len(hit_rows),
            "hit_at_3": len(hit_rows) / count if count else None,
            "hit_at_3_given_eligible_exact_pair": (
                len([row for row in matchable if row["retrieval"]["hit_at_3"]])
                / len(matchable)
                if matchable
                else None
            ),
            "relevant_pairs_retrieved_in_top3": total_relevant,
            "precision_at_3": total_relevant / (3 * count) if count else None,
            "precision_at_returned": (
                total_relevant / total_retrieved if total_retrieved else None
            ),
            "mean_reciprocal_rank_at_3": (
                sum(row["retrieval"]["reciprocal_rank_at_3"] for row in rows) / count
                if count
                else None
            ),
            "maximum_possible_hit_at_3": len(matchable) / count if count else None,
        },
        "diagnosis_relevance": _conditional_diagnosis_metrics(rows),
        "diagnosis_summary": {
            method: {
                "denominator": count,
                "service_correct": sum(
                    row["methods"][method]["service_correct"] for row in rows
                ),
                "service_accuracy": (
                    sum(row["methods"][method]["service_correct"] for row in rows)
                    / count
                    if count
                    else None
                ),
                "fault_type_correct": sum(
                    row["methods"][method]["fault_type_correct"] for row in rows
                ),
                "fault_type_accuracy": (
                    sum(row["methods"][method]["fault_type_correct"] for row in rows)
                    / count
                    if count
                    else None
                ),
                "jointly_correct": sum(
                    row["methods"][method]["joint_correct"] for row in rows
                ),
                "joint_accuracy": (
                    sum(row["methods"][method]["joint_correct"] for row in rows) / count
                    if count
                    else None
                ),
                "classification_complete": sum(
                    row["methods"][method]["classification_complete"] for row in rows
                ),
                "confirmed": sum(row["methods"][method]["confirmed"] for row in rows),
                "inconclusive": sum(
                    row["methods"][method]["inconclusive"] for row in rows
                ),
                "errors": sum(
                    row["methods"][method]["status"] == "error" for row in rows
                ),
            }
            for method in methods
        },
        "performance": {
            method: _method_performance(
                rows,
                method,
                token_budget_per_case=int(
                    config["knowledge_campaign"]["max_total_tokens"]
                ),
                call_budget_per_case=int(
                    config["knowledge_campaign"]["max_model_calls"]
                ),
            )
            for method in methods
        },
        "budgets": {
            "max_total_tokens_per_case": int(
                config["knowledge_campaign"]["max_total_tokens"]
            ),
            "max_model_calls_per_case": int(
                config["knowledge_campaign"]["max_model_calls"]
            ),
            "timeout_seconds_per_case": int(
                config["knowledge_campaign"]["timeout_seconds"]
            ),
            "max_rounds_per_case": int(config["knowledge_campaign"]["max_rounds"]),
            "history_char_budget": int(
                config["knowledge_campaign"].get("history_char_budget", 24000)
            ),
        },
        "generation_settings": {
            "model": knowledge.get("model")
            or knowledge.get("opensearch_current_model"),
            "temperature": knowledge.get("temperature"),
            "seed": knowledge.get("seed"),
        },
        "telemetry_collection": json.loads(
            (directory / "precollection_manifest.json").read_text(encoding="utf-8")
        ),
        "by_service": _group_summary(rows, "service"),
        "by_fault": _group_summary(rows, "fault_type"),
        "historical_comparison": _historical_comparison(),
        "per_run": rows,
    }


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _format_seconds(milliseconds: float | None) -> str:
    return "n/d" if milliseconds is None else f"{milliseconds / 1000:.1f} s"


def _format_tokens(tokens: float | int | None) -> str:
    return "n/d" if tokens is None else f"{tokens:,.0f}"


def _write_markdown_report(
    directory: Path, report: dict[str, Any], destination: Path
) -> Path:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    historical = report.get("historical_comparison")
    retrieval = report["retrieval"]
    diagnostic_methods = report["provenance"]["diagnostic_methods"]
    rag_performance = report["performance"]["exploratory_rag"]
    method_comparison = report.get("method_comparison")
    telemetry = report["telemetry_collection"]
    diagnosis_rows = []
    for method in diagnostic_methods:
        summary = report["diagnosis_summary"][method]
        diagnosis_rows.append(
            f"| {METHOD_LABELS[method]} | "
            f"{summary['service_correct']}/{summary['denominator']} "
            f"({_format_percent(summary['service_accuracy'])}) | "
            f"{summary['fault_type_correct']}/{summary['denominator']} "
            f"({_format_percent(summary['fault_type_accuracy'])}) | "
            f"{summary['jointly_correct']}/{summary['denominator']} "
            f"({_format_percent(summary['joint_accuracy'])}) | "
            f"{summary['classification_complete']}/{summary['denominator']} | "
            f"{summary['confirmed']} / {summary['inconclusive']} / {summary['errors']} |"
        )
    retrieval_path = os.path.relpath(
        directory / "test_base_rag_evaluation.json", destination.parent
    )
    precollection_path = os.path.relpath(
        directory / "precollection_manifest.json", destination.parent
    )
    historical_report_path = (
        os.path.relpath(historical["report_path"], destination.parent)
        if historical
        else None
    )
    historical_markdown_path = (
        os.path.relpath(historical["markdown_report_path"], destination.parent)
        if historical and historical.get("markdown_report_path")
        else None
    )
    executive_lines = []
    for method in diagnostic_methods:
        summary = report["diagnosis_summary"][method]
        executive_lines.append(
            f"- **{METHOD_LABELS[method]}:** acerto conjunto "
            f"{summary['jointly_correct']}/{summary['denominator']} "
            f"({_format_percent(summary['joint_accuracy'])})."
        )
    if method_comparison:
        executive_lines.append(
            f"- **Comparação pareada:** Skill-only "
            f"{method_comparison['skill_only_correct']}; RAG-only "
            f"{method_comparison['rag_only_correct']}; ambos corretos "
            f"{method_comparison['both_correct']}; delta RAG-Skill "
            f"{method_comparison['joint_correct_delta_rag_minus_skill']:+d}."
        )

    lines = [
        (
            "# Avaliação comparativa Skill/RAG com guias elegíveis"
            if "original" in diagnostic_methods
            else "# Avaliação RAG-only com guias elegíveis"
        ),
        "",
        f"- **Avaliação:** `{report['evaluation_id']}`",
        f"- **Coorte:** {report['provenance']['test_run_count']} runs históricos concluídos",
        f"- **Corpus:** `{report['provenance']['corpus_id']}` "
        f"(SHA-256 `{report['provenance']['corpus_sha256']}`)",
        f"- **Recuperador:** `{report['provenance']['retriever_version']}`; "
        f"Top-{retrieval['top_k']}; candidatos elegíveis somente",
        f"- **Geração:** modelo `{report['generation_settings']['model'] or 'default'}`; "
        f"temperatura `{report['generation_settings']['temperature'] if report['generation_settings']['temperature'] is not None else 'default do servidor'}`; "
        f"seed `{report['generation_settings']['seed'] if report['generation_settings']['seed'] is not None else 'default do servidor'}`.",
        f"- **Estratégias:** {', '.join(METHOD_LABELS[method] for method in diagnostic_methods)}",
        "- **Natureza:** retrospectiva/exploratória; a coorte já apareceu em avaliações "
        "anteriores, portanto não é uma validação cega independente.",
        "",
        "## Resumo executivo",
        "",
        *executive_lines,
        "",
        (
            f"A recuperação teve Hit@3 em **{retrieval['hit_at_3_cases']}/"
            f"{retrieval['test_case_denominator']}** casos "
            f"({_format_percent(retrieval['hit_at_3'])}); entre casos com par exato "
            "elegível na base, "
            f"{sum(bool(row['eligible_exact_pair_ids']) and row['retrieval']['hit_at_3'] for row in report['per_run'])}/"
            f"{retrieval['eligible_exact_pair_cases']} "
            f"({_format_percent(retrieval['hit_at_3_given_eligible_exact_pair'])})."
        ),
        (
            f"Tempo médio do diagnóstico: **{_format_seconds(rag_performance['mean_response_duration_ms'])}** "
            f"interno e **{_format_seconds(rag_performance['mean_request_duration_ms'])}** "
            "por request HTTP. Tokens médios por caso: "
            f"**{_format_tokens(rag_performance['mean_total_tokens_per_case'])}** "
            f"({_format_tokens(rag_performance['total_tokens'])} no total; teto de "
            f"{rag_performance['budget_tokens_per_case']:,}/caso, "
            f"{rag_performance['theoretical_campaign_budget_tokens']:,} no conjunto)."
        ),
        "",
        "## Protocolo e elegibilidade",
        "",
        f"- Foram considerados {retrieval['test_case_denominator']} incidentes de teste; "
        f"o índice congelado contém {report['provenance']['retrieval_policy']['corpus_pair_status_counts']['procedurally_validated']} "
        "guias proceduralmente validados e "
        f"{report['provenance']['retrieval_policy']['corpus_pair_status_counts']['procedural_pending']} "
        "pendentes. A recuperação recebeu somente "
        f"{retrieval['candidate_pair_count']} candidatos validados; os pendentes foram excluídos.",
        f"- Há par exato elegível para {retrieval['eligible_exact_pair_cases']}/"
        f"{retrieval['test_case_denominator']} incidentes. Esse é o denominador de "
        "Hit@3 condicionado, não o denominador de acurácia diagnóstica.",
        "- Os rótulos foram consultados apenas depois de todos os diagnósticos estarem "
        "persistidos e validados. Eles não foram enviados como evidência ao modelo.",
        "- O diagnóstico foi executado pela API contra telemetria histórica online; a "
        "pré-coleta abaixo serve para comprovar cobertura MCP, não é uma resposta congelada "
        "reutilizada como evidência do diagnóstico.",
        "- A avaliação não cria novos incidentes nem injeta caos. Resultados não estimam "
        "desempenho em uma coorte independente.",
        "",
        "## Recuperação",
        "",
        "| Métrica | Resultado |",
        "|---|---:|",
        f"| Candidatos elegíveis | {retrieval['candidate_pair_count']} |",
        f"| Casos com par exato elegível | {retrieval['eligible_exact_pair_cases']}/{retrieval['test_case_denominator']} "
        f"({_format_percent(retrieval['eligible_exact_pair_coverage'])}) |",
        f"| Resultados recuperados elegíveis / pendentes | "
        f"{retrieval['retrieved_pair_status_counts']['procedurally_validated']} / "
        f"{retrieval['retrieved_pair_status_counts']['procedural_pending']} |",
        f"| Hit@3, todos os casos | {retrieval['hit_at_3_cases']}/{retrieval['test_case_denominator']} "
        f"({_format_percent(retrieval['hit_at_3'])}) |",
        f"| Hit@3, condicionado a par elegível | "
        f"{sum(bool(row['eligible_exact_pair_ids']) and row['retrieval']['hit_at_3'] for row in report['per_run'])}/"
        f"{retrieval['eligible_exact_pair_cases']} "
        f"({_format_percent(retrieval['hit_at_3_given_eligible_exact_pair'])}) |",
        f"| Precisão@3 | {_format_percent(retrieval['precision_at_3'])} |",
        f"| MRR@3 | {retrieval['mean_reciprocal_rank_at_3']:.3f} |",
        f"| Teto de Hit@3 dado o corpus | {_format_percent(retrieval['maximum_possible_hit_at_3'])} |",
        "",
        "A precisão@3 usa como denominador três posições por caso, inclusive quando "
        "menos de três resultados são retornados. O MRR é calculado sobre todos os "
        "casos, atribuindo zero quando nenhum par exato é recuperado.",
        "",
        "## Diagnóstico e dependência da recuperação",
        "",
        "| Método | Serviço correto | Falha correta | Acerto conjunto | Classificação completa | Confirmados / inconclusivos / erros |",
        "|---|---:|---:|---:|---:|---:|",
        *diagnosis_rows,
        "",
        (
            f"Na comparação pareada de {method_comparison['paired_case_count']} casos, "
            f"apenas Skill acertou {method_comparison['skill_only_correct']}, "
            f"apenas RAG acertou {method_comparison['rag_only_correct']}, "
            f"ambos acertaram {method_comparison['both_correct']} e ambos erraram "
            f"{method_comparison['neither_correct']}; delta líquido RAG-Skill: "
            f"{method_comparison['joint_correct_delta_rag_minus_skill']:+d} casos."
            if method_comparison
            else "A avaliação não contém as duas estratégias para comparação pareada."
        ),
        "",
        "| Relação com recuperação | Acertos conjuntos |",
        "|---|---:|",
        f"| Com par relevante no Top-3 | "
        f"{report['diagnosis_relevance']['with_relevant_retrieval']['jointly_correct']}/"
        f"{report['diagnosis_relevance']['with_relevant_retrieval']['denominator']} "
        f"({_format_percent(report['diagnosis_relevance']['with_relevant_retrieval']['joint_accuracy'])}) |",
        f"| Sem par relevante no Top-3 | "
        f"{report['diagnosis_relevance']['without_relevant_retrieval']['jointly_correct']}/"
        f"{report['diagnosis_relevance']['without_relevant_retrieval']['denominator']} "
        f"({_format_percent(report['diagnosis_relevance']['without_relevant_retrieval']['joint_accuracy'])}) |",
        "",
        "### Resultado da RAG por serviço",
        "",
        "| Serviço | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for service, values in report["by_service"].items():
        lines.append(
            f"| {service} | {values['denominator']} | "
            f"{values['eligible_exact_pair_cases']} | {values['hit_at_3_cases']} | "
            f"{values['service_correct']} | {values['fault_type_correct']} | "
            f"{values['jointly_correct']} ({_format_percent(values['joint_accuracy'])}) |"
        )
    lines.extend(
        [
            "",
            "### Resultado da RAG por tipo de falha",
            "",
            "| Falha | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for fault, values in report["by_fault"].items():
        lines.append(
            f"| {fault} | {values['denominator']} | "
            f"{values['eligible_exact_pair_cases']} | {values['hit_at_3_cases']} | "
            f"{values['service_correct']} | {values['fault_type_correct']} | "
            f"{values['jointly_correct']} ({_format_percent(values['joint_accuracy'])}) |"
        )

    lines.extend(
        [
            "",
            "## Tempo, tokens e orçamento",
            "",
            "| Execução | Tempo interno médio | Request HTTP médio | Tokens médios/caso | Tokens totais | Teto tokens/caso | Chamadas/caso | Medidos / estimados |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for method in diagnostic_methods:
        performance = report["performance"][method]
        lines.append(
            f"| {METHOD_LABELS[method]} | "
            f"{_format_seconds(performance['mean_response_duration_ms'])} | "
            f"{_format_seconds(performance['mean_request_duration_ms'])} | "
            f"{_format_tokens(performance['mean_total_tokens_per_case'])} | "
            f"{_format_tokens(performance['total_tokens'])} | "
            f"{_format_tokens(performance['budget_tokens_per_case'])} | "
            f"{performance['mean_model_calls_per_case']} | "
            f"{performance['measured_token_usage_calls']} / "
            f"{performance['estimated_token_usage_calls']} |"
        )
    if historical:
        for method, label in (
            ("original", "Skill original (histórico)"),
            ("exploratory_control", "Controle exploratório (histórico)"),
            ("exploratory_rag", "RAG v4 exploratória, 32 candidatos (histórico)"),
        ):
            metric = historical["performance"][method]
            token_budget = (
                historical["budgets"]["max_total_tokens_per_case"]
                if method != "original"
                else None
            )
            token_usage = (
                f"{metric['measured_token_usage_calls']} / "
                f"{metric['estimated_token_usage_calls']}"
                if metric["token_usage_case_count"]
                else "n/d"
            )
            lines.append(
                f"| {label} | {_format_seconds(metric['mean_response_duration_ms'])} | "
                f"{_format_seconds(metric['mean_request_duration_ms'])} | "
                f"{_format_tokens(metric['mean_total_tokens_per_case'])} | "
                f"{_format_tokens(metric['total_tokens'])} | "
                f"{_format_tokens(token_budget)} | "
                f"{metric['mean_model_calls_per_case']} | "
                f"{token_usage} |"
            )
    lines.extend(
        [
            "",
            f"O orçamento máximo comum por método/caso foi de "
            f"{rag_performance['budget_tokens_per_case']:,} tokens e "
            f"{report['budgets']['max_model_calls_per_case']} chamadas por caso; "
            f"Skill e RAG receberam esses mesmos limites. O teto teórico para "
            f"{rag_performance['case_count']} casos por método é "
            f"{rag_performance['theoretical_campaign_budget_tokens']:,} tokens. "
            f"O uso agregado foi "
            f"{_format_percent(rag_performance['budget_utilization'])} desse teto; "
            f"o maior uso individual foi "
            f"{_format_percent(rag_performance['max_case_budget_utilization'])}. "
            "O orçamento é por caso, não um pool compartilhado entre casos. O consumo de tokens "
            "usa a contagem do provedor quando disponível e, nos demais casos, a "
            "estimativa por caracteres do backend.",
            f"Outros limites comuns: "
            f"{report['budgets']['max_rounds_per_case']} rodadas adicionais/checks e "
            f"{report['budgets']['timeout_seconds_per_case']} s por caso. "
            f"A RAG também limita seu histórico a "
            f"{report['budgets']['history_char_budget']:,} caracteres; esse limite "
            "específico da recuperação não se aplica à Skill.",
            "",
            "Tempos internos vêm de `duration_ms` no endpoint e cobrem a investigação; "
            "request HTTP inclui o tempo de transporte/serialização. Pré-coleta MCP "
            "não está incluída nesses tempos.",
            "",
            "## Cobertura da telemetria histórica MCP",
            "",
            f"Foram feitas {telemetry['query_count']} consultas novas "
            f"({telemetry['case_count']} casos × {telemetry['queries_per_case']} "
            f"consultas: Prometheus, Loki e Tempo em baseline, fault e post), em "
            f"{telemetry['elapsed_seconds']} s. Estados agregados: "
            f"{telemetry['status_counts']}. `no_data` é limitação de cobertura, não "
            "falha de backend; erros de backend bloquearam a inferência.",
            "",
            "| Fonte | Fase | OK | no_data | Erros | Observações |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for source in SOURCES:
        for phase in PHASES:
            values = telemetry["coverage_by_source_phase"][source][phase]
            lines.append(
                f"| {source} | {phase} | {values['ok']} | {values['no_data']} | "
                f"{values['errors']} | {values['observations']} |"
            )
    lines.extend(
        [
            "",
            "As consultas de logs e traces usam filtro neutro de namespace, sem "
            "selecionar o serviço ou tipo de falha do rótulo-alvo. As consultas "
            "abrangem o namespace `sock-shop`; isso aumenta a chance de cobertura, "
            "mas pode incluir ruído de outros serviços. O modelo consulta os "
            "datasources históricos online para cada diagnóstico; a evidência "
            "pré-coletada não é seu fallback.",
            "",
            "## Comparação histórica (não pareada)",
            "",
        ]
    )
    if historical:
        lines.extend(
            [
                f"Referência: avaliação `{historical['evaluation_id']}` "
                "(estratégias não executadas novamente nesta avaliação). A referência "
                "usou outra recuperação e telemetria; comparações não isolam efeito "
                "causal. O teste atual filtra guias elegíveis, ao passo que a RAG "
                "exploratória anterior permitia os 32 cartões.",
                "",
                "| Estratégia anterior | Acerto conjunto | Hit@3 anterior | MRR@3 anterior |",
                "|---|---:|---:|---:|",
            ]
        )
        old_retrieval = historical.get("retrieval") or {}
        for method, label in (
            ("original", "Skill original"),
            ("exploratory_control", "Controle exploratório"),
            ("exploratory_rag", "RAG v4 exploratória"),
        ):
            metric = historical["performance"][method]
            old_hit = (
                f"{old_retrieval.get('hit_at_3_cases')}/"
                f"{old_retrieval.get('test_case_denominator')} "
                f"({_format_percent(old_retrieval.get('hit_at_3'))})"
                if method == "exploratory_rag"
                else "n/a"
            )
            old_mrr = (
                f"{old_retrieval['mean_reciprocal_rank_at_3']:.3f}"
                if method == "exploratory_rag"
                and old_retrieval.get("mean_reciprocal_rank_at_3") is not None
                else "n/a"
            )
            correct = metric.get("jointly_correct")
            accuracy = metric.get("joint_accuracy")
            lines.append(
                f"| {label} | {correct}/{metric['case_count']} "
                f"({_format_percent(accuracy)}) | {old_hit} | {old_mrr} |"
            )
        if historical_markdown_path:
            lines.extend(
                [
                    "",
                    f"Relatório Markdown anterior: "
                    f"[{HISTORICAL_EVALUATION_ID}]({historical_markdown_path}).",
                ]
            )
        elif historical_report_path:
            lines.extend(
                [
                    "",
                    f"Relatório JSON anterior: [{HISTORICAL_EVALUATION_ID}]"
                    f"({historical_report_path}).",
                ]
            )
    else:
        lines.append("O relatório comparador anterior não estava disponível.")

    lines.extend(
        [
            "",
            "## Resultado por run",
            "",
        ]
    )
    if "original" in diagnostic_methods:
        lines.extend(
            [
                "| Run | Serviço/falha real | Skill: predição | Skill: conjunto correto | RAG: predição | RAG: conjunto correto | Top-3 IDs | Hit@3 |",
                "|---|---|---|---:|---|---:|---|---:|",
            ]
        )
        for row in report["per_run"]:
            skill_prediction = row["predictions"]["original"]
            rag_prediction = row["predictions"]["exploratory_rag"]
            skill_score = row["methods"]["original"]
            rag_score = row["methods"]["exploratory_rag"]
            skill_label = (
                f"{skill_prediction['service'] or '—'} / "
                f"{skill_prediction['fault_type'] or '—'}"
            )
            rag_label = (
                f"{rag_prediction['service'] or '—'} / "
                f"{rag_prediction['fault_type'] or '—'}"
            )
            top3 = (
                ", ".join(
                    pair_id[:12] for pair_id in row["retrieval"]["retrieved_pair_ids"]
                )
                or "—"
            )
            lines.append(
                f"| `{row['run_id']}` | {row['service']} / {row['fault_type']} | "
                f"{skill_label} | {'sim' if skill_score['joint_correct'] else 'não'} | "
                f"{rag_label} | {'sim' if rag_score['joint_correct'] else 'não'} | "
                f"{top3} | {'sim' if row['retrieval']['hit_at_3'] else 'não'} |"
            )
    else:
        lines.extend(
            [
                "| Run | Serviço real | Falha real | Predição serviço/falha | Top-3 IDs (prefixos) | Hit@3 | Serviço correto | Falha correta | Conjunto correto | Tempo interno | Tokens |",
                "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in report["per_run"]:
            prediction = row["predictions"]["exploratory_rag"]
            score = row["methods"]["exploratory_rag"]
            predicted = (
                f"{prediction['service'] or '—'} / {prediction['fault_type'] or '—'}"
            )
            top3 = (
                ", ".join(
                    pair_id[:12] for pair_id in row["retrieval"]["retrieved_pair_ids"]
                )
                or "—"
            )
            lines.append(
                f"| `{row['run_id']}` | {row['service']} | {row['fault_type']} | "
                f"{predicted} | {top3} | "
                f"{'sim' if row['retrieval']['hit_at_3'] else 'não'} | "
                f"{'sim' if score['service_correct'] else 'não'} | "
                f"{'sim' if score['fault_type_correct'] else 'não'} | "
                f"{'sim' if score['joint_correct'] else 'não'} | "
                f"{_format_seconds(score['duration_ms'])} | "
                f"{_format_tokens(score['total_tokens'])} |"
            )
    lines.extend(
        [
            "",
            "## Limitações e leitura",
            "",
            "- A amostra de 32 casos é reutilizada e pequena; não permite afirmar "
            "generalização nem uma melhoria causal sobre as estratégias históricas.",
            "- Só 18 casos têm par exato elegível na base; os demais não são falhas "
            "de ranking mensuráveis sob esse qrel.",
            "- A avaliação mede classificação de serviço e tipo de falha, não a "
            "validade causal completa de cada explicação. Um acerto conjunto não "
            "substitui revisão de evidências e causalidade.",
            "- Resultado `no_data`, truncamento e ruído de consultas namespace-wide "
            "precisam ser considerados na interpretação por fase e fonte.",
            "- Para evidência independente, o próximo passo é adicionar novos runs "
            "elegíveis, sem reutilizar esta coorte para ajuste e validação.",
            "",
            "## Artefatos",
            "",
            f"- Dados completos e respostas por run: [{Path(retrieval_path).name}]"
            f"({retrieval_path})",
            f"- Manifesto de consultas MCP: [{Path(precollection_path).name}]"
            f"({precollection_path})",
            "",
        ]
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(destination)
    return destination


def run_evaluation(
    directory: Path, *, report_output: Path | None = None
) -> dict[str, Any]:
    directory = directory.resolve()
    validate_precollection(directory)
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort = json.loads((directory / "test_cohort.json").read_text(encoding="utf-8"))
    methods = _diagnostic_methods(config)
    expected_cases = len(cohort["runs"])
    expected_diagnostics = expected_cases * len(methods)
    completion_path = directory / "diagnostics_complete.json"
    completed_elapsed_seconds = None
    started_at = None
    if completion_path.is_file():
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        completed_elapsed_seconds = float(completion["elapsed_seconds"])
        started_at = completion_path.stat().st_mtime - completed_elapsed_seconds
    telemetry = create_evaluation_telemetry(
        evaluation_id=config["experiment_campaign_id"],
        retriever_version=config["knowledge_campaign"]["retriever_version"],
        expected_cases=expected_cases,
        expected_diagnostics=expected_diagnostics,
        started_at=started_at,
    )
    try:
        if not completion_path.is_file():
            run_diagnostics(directory, telemetry=telemetry)
        elif telemetry:
            telemetry.update_progress(
                {
                    "evaluation_cases_expected": expected_cases,
                    "evaluation_cases_completed": expected_cases,
                    "evaluation_cases_failed": 0,
                    "evaluation_cases_in_progress": 0,
                    "evaluation_current_case_index": expected_cases,
                    "evaluation_diagnostics_expected": expected_diagnostics,
                    "evaluation_diagnostics_completed": expected_diagnostics,
                    "evaluation_completion_ratio": 1,
                }
            )
        if telemetry:
            telemetry.set_scoring()
        report = build_report(directory)
        report_path = directory / "test_base_rag_evaluation.json"
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(report_path)
        markdown_path = _write_markdown_report(
            directory,
            report,
            report_output or directory / "test_base_rag_evaluation_report.md",
        )
        if telemetry:
            telemetry.record_report(report)
            telemetry.complete(elapsed_seconds=completed_elapsed_seconds)
        print(report_path)
        print(markdown_path)
        return report
    except Exception:
        if telemetry:
            telemetry.fail()
        raise
    finally:
        if telemetry:
            telemetry.flush_and_shutdown()


def publish_existing_metrics(directory: Path) -> None:
    directory = directory.resolve()
    report = build_report(directory)
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    cohort = json.loads((directory / "test_cohort.json").read_text(encoding="utf-8"))
    methods = _diagnostic_methods(config)
    expected_cases = len(cohort["runs"])
    expected_diagnostics = expected_cases * len(methods)
    completion_path = directory / "diagnostics_complete.json"
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    elapsed_seconds = float(completion["elapsed_seconds"])
    started_at = completion_path.stat().st_mtime - elapsed_seconds
    telemetry = create_evaluation_telemetry(
        evaluation_id=config["experiment_campaign_id"],
        retriever_version=config["knowledge_campaign"]["retriever_version"],
        expected_cases=expected_cases,
        expected_diagnostics=expected_diagnostics,
        started_at=started_at,
    )
    if telemetry is None:
        raise RuntimeError(
            "Cannot publish evaluation metrics without an OTLP collector"
        )
    try:
        telemetry.update_progress(
            {
                "evaluation_run_state": 1,
                "evaluation_run_started_timestamp_seconds": started_at,
                "evaluation_run_last_update_timestamp_seconds": time.time(),
                "evaluation_elapsed_seconds": elapsed_seconds,
                "evaluation_cases_expected": expected_cases,
                "evaluation_cases_completed": expected_cases,
                "evaluation_cases_failed": 0,
                "evaluation_cases_in_progress": 0,
                "evaluation_current_case_index": expected_cases,
                "evaluation_diagnostics_expected": expected_diagnostics,
                "evaluation_diagnostics_completed": expected_diagnostics,
                "evaluation_completion_ratio": 1,
            }
        )
        telemetry.record_report(report)
        telemetry.complete(elapsed_seconds=elapsed_seconds)
    except Exception:
        telemetry.fail()
        raise
    finally:
        telemetry.flush_and_shutdown()
    print(
        f"Published metrics for {config['experiment_campaign_id']} "
        "without rerunning diagnostics."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--prepare-comparison", action="store_true")
    actions.add_argument("--precollect", type=Path)
    actions.add_argument("--precollect-comparison", type=Path)
    actions.add_argument("--run", type=Path)
    actions.add_argument("--run-comparison", type=Path)
    actions.add_argument(
        "--publish-metrics",
        type=Path,
        help="publish metrics for an already completed evaluation without rerunning it",
    )
    actions.add_argument("--prepare-and-run", action="store_true")
    parser.add_argument("--source-campaign", type=Path, default=DEFAULT_SOURCE_CAMPAIGN)
    parser.add_argument(
        "--test-cohort",
        type=Path,
        help="validated derived supplemental test-cohort manifest for --prepare",
    )
    parser.add_argument("--corpus-id", default=DEFAULT_CORPUS_ID)
    parser.add_argument("--api-base", default="http://127.0.0.1:8012")
    parser.add_argument("--mcp-sse-url", default="http://127.0.0.1:18082/sse")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--report-output",
        type=Path,
        help="optional Markdown report destination (defaults to research/diario)",
    )
    parser.add_argument(
        "--retriever-version",
        choices=[
            OPENSEARCH_RETRIEVER_V4,
            OPENSEARCH_RETRIEVER_V5,
            OPENSEARCH_RETRIEVER_V6,
        ],
        default=OPENSEARCH_RETRIEVER_V6,
        help="retriever for newly prepared evaluations; existing configs remain pinned",
    )
    parser.add_argument(
        "--include-unvalidated-procedural-experimental",
        action="store_true",
        help="include procedural_pending v4 guides for this external test evaluation",
    )
    parser.add_argument(
        "--resource-signature-policy",
        choices=FACTORIAL_POLICIES,
        default="none",
        help="optional shared CPU/memory prompt policy for a single evaluation",
    )
    parser.add_argument(
        "--resource-cases-per-type",
        type=int,
        help=(
            "prepare a balanced CPU-hog/memory-hog pilot with this many cases "
            "of each type (only with --prepare-comparison)"
        ),
    )
    args = parser.parse_args(argv)
    if args.test_cohort is not None and not (args.prepare or args.prepare_and_run):
        parser.error("--test-cohort requires --prepare or --prepare-and-run")
    if args.resource_cases_per_type is not None and not args.prepare_comparison:
        parser.error("--resource-cases-per-type requires --prepare-comparison")
    if args.resource_cases_per_type is not None and args.resource_cases_per_type < 1:
        parser.error("--resource-cases-per-type must be positive")
    if args.precollect_comparison:
        precollect_factorial_comparison(args.precollect_comparison)
        return 0
    if args.run_comparison:
        report = run_factorial_comparison(args.run_comparison)
        print(args.run_comparison / "factorial_report.json")
        print(f"Completed paired factorial comparison: {report['comparison_id']}")
        return 0
    if args.prepare_comparison:
        directory = prepare_factorial_comparison(
            source_campaign=args.source_campaign,
            corpus_id=args.corpus_id,
            api_base=args.api_base,
            mcp_sse_url=args.mcp_sse_url,
            output_root=args.output_root,
            retriever_version=args.retriever_version,
            resource_cases_per_type=args.resource_cases_per_type,
        )
        print(directory)
        return 0
    if args.precollect:
        collect_preinference_mcp(args.precollect)
        return 0
    if args.publish_metrics:
        publish_existing_metrics(args.publish_metrics)
        return 0
    if args.run:
        run_evaluation(
            args.run,
            report_output=args.report_output
            or REPO_ROOT
            / "research"
            / "diario"
            / f"{args.run.name}-relatorio-skill-rag.md",
        )
        return 0
    if args.prepare_and_run:
        directory = prepare_evaluation(
            source_campaign=args.source_campaign,
            corpus_id=args.corpus_id,
            test_cohort_path=args.test_cohort,
            api_base=args.api_base,
            mcp_sse_url=args.mcp_sse_url,
            output_root=args.output_root,
            include_unvalidated_procedural_experimental=(
                args.include_unvalidated_procedural_experimental
            ),
            retriever_version=args.retriever_version,
            resource_signature_policy=args.resource_signature_policy,
        )
        print(directory)
        collect_preinference_mcp(directory)
        run_evaluation(
            directory,
            report_output=args.report_output
            or REPO_ROOT
            / "research"
            / "diario"
            / f"{directory.name}-relatorio-skill-rag.md",
        )
        return 0
    if args.prepare:
        directory = prepare_evaluation(
            source_campaign=args.source_campaign,
            corpus_id=args.corpus_id,
            test_cohort_path=args.test_cohort,
            api_base=args.api_base,
            mcp_sse_url=args.mcp_sse_url,
            output_root=args.output_root,
            include_unvalidated_procedural_experimental=(
                args.include_unvalidated_procedural_experimental
            ),
            retriever_version=args.retriever_version,
            resource_signature_policy=args.resource_signature_policy,
        )
        print(directory)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
