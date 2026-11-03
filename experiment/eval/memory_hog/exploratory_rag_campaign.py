"""Prepare or execute a separate 35-test RAG classification campaign."""

from __future__ import annotations

import argparse
import json
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from experiment.eval.memory_hog import controlled_campaign
from experiment.eval.memory_hog.opensearch_experiments import (
    DEFAULT_OPENSEARCH_URL,
    OpenSearchExperimentIndex,
)
from experiment.eval.memory_hog.exploratory_rag_pilot import (
    DEFAULT_PILOT_CELL,
    EXPLORATORY_MODE,
    _install_exploratory_base,
    _pilot_training_ledger,
)
from experiment.eval.memory_hog.retroactive_b import (
    OBJECT_STORE,
    _atomic_json,
    _source_annotation,
    _training_cohort,
)
from experiment.eval.memory_hog.rag_campaign import (
    EXPLORATORY_MAX_TOTAL_TOKENS,
)
from experiment.platform.backend.rca_knowledge import (
    DEFAULT_LLM_MODEL,
    OPENSEARCH_RETRIEVER_VERSION,
    VERSION,
    DiagnosticGuide,
    Diagnosis,
    IncidentA,
    KnowledgeStore,
    digest,
    load_skill,
    write_once,
)

CAMPAIGN_DIR = Path(__file__).resolve().parent
RECHECK_DIR = "retro_b_recheck_v2"
RECORD_SOURCES = (
    ("reviewed_b", "assistant_reviewed_boundary_only"),
    ("contract_revalidations", "assistant_label_consistency_audited"),
    ("contract_repairs", "assistant_label_consistency_audited"),
    ("annotations", "assistant_label_consistency_audited"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_b(
    source_campaign: Path,
    cell: dict[str, Any],
    entry: dict[str, Any],
    source: dict[str, Any],
) -> tuple[dict[str, Any], Path, str] | None:
    root = source_campaign / RECHECK_DIR
    filename = f"{cell['cell_id']}.json"
    evidence_path = root / "evidence" / filename
    if not evidence_path.is_file():
        return None
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    source_pair = source["pair"]
    expected_run_id = entry["run_id"]
    expected_annotation_sha256 = _sha256(source["path"])

    for folder, review_status in RECORD_SOURCES:
        path = root / folder / filename
        if not path.is_file():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        document = record.get("b")
        if not isinstance(document, dict):
            continue
        if folder != "reviewed_b" and record.get("status") == "error":
            continue
        if record.get("run_id") != expected_run_id:
            raise ValueError(f"{cell['cell_id']} B source run ID mismatch")
        if folder == "reviewed_b":
            if (
                record.get("source_pair_id") != source_pair["pair_id"]
                or record.get("source_bundle_sha256") != evidence.get("sha256")
                or record.get("promoted_to_corpus") is not False
            ):
                raise ValueError(f"{cell['cell_id']} reviewed B provenance mismatch")
        elif (
            record.get("bundle_sha256") != evidence.get("sha256")
            or record.get("source_annotation_sha256") != expected_annotation_sha256
        ):
            raise ValueError(f"{cell['cell_id']} B evidence bundle provenance mismatch")
        return document, path, review_status
    return None


def _exploratory_pair(
    cell: dict[str, Any],
    entry: dict[str, Any],
    source: dict[str, Any],
    document: dict[str, Any],
    source_path: Path,
    review_status: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = source["labels"]
    target = document.get("target_result") or {}
    diagnosis = Diagnosis.model_validate(document["diagnosis"])
    label_match = (
        document.get("target_result_source") == "ground_truth_label"
        and target == expected
        and diagnosis.primary_service == expected["service"]
        and diagnosis.fault_type == expected["fault_type"]
        and diagnosis.category == expected["causal_category"]
    )
    audit = {
        "cell_id": cell["cell_id"],
        "run_id": entry["run_id"],
        "pair_id": source["pair"]["pair_id"],
        "b_source": source_path.name,
        "b_source_sha256": _sha256(source_path),
        "target_result_source": document.get("target_result_source"),
        "target_labels": target,
        "diagnosis_labels": {
            "service": diagnosis.primary_service,
            "fault_type": diagnosis.fault_type,
            "causal_category": diagnosis.category,
        },
        "labels_match": label_match,
        "effect_validated": source["effect_validated"],
        "used_in_exploratory_corpus": label_match,
        "validation_scope": "label consistency only; not independent causal validation",
    }
    if not label_match:
        return {}, audit

    diagnosis.status = "inconclusive"
    steps = []
    for step in document["investigation_steps"]:
        steps.append(
            f"Objetivo histórico: {step['objective']} Verifique novamente no run "
            f"atual: {step['verification']} Apoio esperado: {step['expected_support']} "
            f"Refutação: {step['expected_refutation']}. IDs e resultados históricos "
            "não são evidência do incidente atual."
        )
    limitations = list(document["limitations"])
    limitations.extend(
        [
            "Guia exploratório: a concordância com o rótulo não é validação causal independente.",
            "O rótulo experimental foi fornecido durante a anotação deste B.",
            "O resultado histórico não constitui evidência do incidente atual.",
        ]
    )
    if not source["effect_validated"]:
        limitations.append(
            "O efeito da intervenção histórica não foi validado; use este par como analogia fraca."
        )
    guide = DiagnosticGuide(
        diagnosis=diagnosis,
        symptom_relation=document["symptom_relation"],
        investigation_steps=steps,
        applicability=document["applicability"],
        recommended_checks=document["recommended_checks"],
        limitations=list(dict.fromkeys(limitations)),
    )
    source_pair = source["pair"]
    pair = {
        "pair_id": source_pair["pair_id"],
        "source_run_id": entry["run_id"],
        "split": "train",
        "a": IncidentA.model_validate(source_pair["a"]).model_dump(),
        "b": guide.model_dump(),
        "labels": expected,
        "effect_validated": source["effect_validated"],
        "guide_status": "pending",
        "causal_status": "inconclusive",
        "review_status": review_status,
        "source_cell_id": cell["cell_id"],
        "source_pair_sha256": source_pair["sha256"],
        "source_b_sha256": audit["b_source_sha256"],
        "skill_hash": source_pair["skill_hash"],
    }
    pair["sha256"] = digest(pair)
    return pair, audit


def prepare_campaign(
    source_campaign: Path, *, output_root: Path | None = None
) -> Path:
    source_campaign = source_campaign.resolve()
    config, train_cells, cohort = _training_cohort(source_campaign)
    all_cells = controlled_campaign.campaign_cells(config)
    test_cells = [cell for cell in all_cells if cell["phase"] == "test"]
    if len(train_cells) != 35 or len(test_cells) != 35:
        raise ValueError("exploratory campaign requires the original 35/35 split")
    if any(cohort["latest"].get(cell["cell_id"], {}).get("run_id") for cell in test_cells):
        raise ValueError("source test phase already contains a run; refusing test leakage")

    pairs = []
    audit_rows = []
    for cell in train_cells:
        entry = cohort["latest"].get(cell["cell_id"], {})
        if entry.get("status") != "completed":
            audit_rows.append(
                {
                    "cell_id": cell["cell_id"],
                    "run_status": entry.get("status", "not_run"),
                    "labels_match": None,
                    "used_in_exploratory_corpus": False,
                    "reason": "no completed source run to annotate",
                }
            )
            continue
        source = _source_annotation(source_campaign, cell, entry, config)
        candidate = _source_b(source_campaign, cell, entry, source)
        if candidate is None:
            audit_rows.append(
                {
                    "cell_id": cell["cell_id"],
                    "run_id": entry["run_id"],
                    "run_status": entry["status"],
                    "labels_match": None,
                    "used_in_exploratory_corpus": False,
                    "reason": "no usable B record in the latest recheck",
                }
            )
            continue
        document, source_path, review_status = candidate
        pair, audit = _exploratory_pair(
            cell, entry, source, document, source_path, review_status
        )
        audit["run_status"] = entry["status"]
        audit_rows.append(audit)
        if pair:
            pairs.append(pair)

    mismatches = [row for row in audit_rows if row.get("labels_match") is False]
    if mismatches:
        raise ValueError(
            "B label audit found mismatches; no exploratory corpus was installed: "
            + ", ".join(row["cell_id"] for row in mismatches)
        )
    if not pairs:
        raise ValueError("no label-consistent B pairs are available for exploration")

    audit = {
        "version": "exploratory-b-label-audit-v1",
        "source_campaign_id": source_campaign.name,
        "scope": (
            "Compares B fields with the experiment labels supplied during annotation. "
            "This is label consistency, not independent causal validation."
        ),
        "counts": {
            "planned_training_cells": len(train_cells),
            "completed_training_cells": sum(
                row.get("run_status") == "completed" for row in audit_rows
            ),
            "b_records_audited": sum(
                row.get("labels_match") is not None for row in audit_rows
            ),
            "label_matches": sum(row.get("labels_match") is True for row in audit_rows),
            "label_mismatches": sum(row.get("labels_match") is False for row in audit_rows),
            "effect_validated_pairs": sum(
                pair["effect_validated"] for pair in pairs
            ),
            "effect_unvalidated_pairs": sum(
                not pair["effect_validated"] for pair in pairs
            ),
            "exploratory_pairs": len(pairs),
        },
        "source_hashes": cohort["source_hashes"],
        "records": audit_rows,
    }
    audit["sha256"] = digest(audit)

    output_root = output_root or (CAMPAIGN_DIR / "out" / "exploratory_campaigns")
    campaign_id = "exploratory-rag-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    directory = output_root / campaign_id
    directory.mkdir(parents=True, exist_ok=False)

    new_config = json.loads(json.dumps(config))
    new_config["experiment_campaign_id"] = campaign_id
    new_config["train_experiments"] = train_cells
    new_config["test_experiments"] = test_cells
    new_config["knowledge_campaign"].update(
        corpus_id=campaign_id,
        exploratory=True,
        parent_campaign_id=source_campaign.name,
        top_k=3,
        min_score=0.05,
        max_rounds=2,
    )
    new_config["output"].update(
        results_dir=str(directory),
        runs_csv="runs.csv",
        manifests_dir="manifests",
        evaluation_csv="exploratory_rag_evaluation.csv",
    )
    new_cells = [*train_cells, *test_cells]
    (directory / "config.yaml").write_text(
        yaml.safe_dump(new_config, sort_keys=False), encoding="utf-8"
    )
    (directory / "plan.json").write_text(
        json.dumps(new_cells, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (directory / "ledger.jsonl").open("w", encoding="utf-8") as handle:
        for row in _pilot_training_ledger(train_cells, cohort["latest"]):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    for cell in train_cells:
        entry = cohort["latest"].get(cell["cell_id"], {})
        if entry.get("status") != "completed":
            continue
        source_annotation = (
            source_campaign / "rag_results" / cell["cell_id"] / "annotate.json"
        )
        destination = directory / "rag_results" / cell["cell_id"] / "annotate.json"
        write_once(
            destination,
            json.loads(source_annotation.read_text(encoding="utf-8")),
        )

    _atomic_json(directory / "label_audit.json", audit)
    base = _install_exploratory_base(campaign_id, pairs)
    _atomic_json(
        directory / "exploratory_manifest.json",
        {
            "mode": EXPLORATORY_MODE,
            "source_campaign_id": source_campaign.name,
            "source_hashes": cohort["source_hashes"],
            "label_audit_sha256": audit["sha256"],
            "planned_test_count": len(test_cells),
            "corpus_id": campaign_id,
            "base_sha256": base["sha256"],
            "candidate_count": len(base["pairs"]),
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "require_best_guess": True,
            "test_runs_started": 0,
            "base_is_validated": False,
        },
    )
    return directory


def prepare_test_only_campaign(
    source_campaign: Path,
    *,
    corpus_id: str,
    opensearch_base_sha256: str,
    allow_legacy_skill_hash: bool = False,
    allow_legacy_model_mismatch: bool = False,
    include_unvalidated_experimental: bool = False,
    pilot_cell: str | None = None,
    historical_mcp_sse_url: str = "http://127.0.0.1:18082/sse",
    platform_api_base: str = "http://127.0.0.1:8012",
    output_root: Path | None = None,
) -> Path:
    source_campaign = source_campaign.resolve()
    config, train_cells, cohort = _training_cohort(source_campaign)
    all_cells = controlled_campaign.campaign_cells(config)
    test_cells = [cell for cell in all_cells if cell["phase"] == "test"]
    if len(train_cells) != 35 or len(test_cells) != 35:
        raise ValueError("test-only campaign requires the original 35/35 split")
    if pilot_cell is not None:
        selected = [cell for cell in test_cells if cell["cell_id"] == pilot_cell]
        if len(selected) != 1:
            raise ValueError(f"pilot cell is not in the planned test split: {pilot_cell}")
        test_cells = selected

    completed_training_ids = {
        entry["run_id"]
        for cell in train_cells
        if (entry := cohort["latest"].get(cell["cell_id"], {})).get("status")
        == "completed"
    }
    store = KnowledgeStore(OBJECT_STORE / ".rca_knowledge", corpus_id)
    base_path = store.directory / "exploratory_base.json"
    if not base_path.is_file():
        raise ValueError("reused corpus has no installed exploratory base")
    stored_base = json.loads(base_path.read_text(encoding="utf-8"))
    source_skill_sha256 = stored_base.get("skill_sha256")
    source_model = stored_base.get("model")
    current_model = DEFAULT_LLM_MODEL
    _, _, current_skill_sha256 = load_skill()
    legacy_skill_mismatch = source_skill_sha256 != current_skill_sha256
    legacy_model_mismatch = source_model != current_model
    if legacy_skill_mismatch and not allow_legacy_skill_hash:
        raise ValueError(
            "existing corpus Skill hash differs; pass the explicit legacy-corpus "
            "authorization to continue"
        )
    if not isinstance(source_model, str) or not source_model:
        raise ValueError("existing corpus has no valid source model")
    if legacy_model_mismatch and not allow_legacy_model_mismatch:
        raise ValueError(
            "existing corpus model differs; pass the explicit exploratory model "
            "compatibility authorization to continue"
        )
    base = store.load_exploratory_base(
        expected_skill_sha256=source_skill_sha256,
        expected_model=source_model,
        allow_legacy_model_mismatch=allow_legacy_model_mismatch,
    )
    if not set(base["training_run_ids"]).issubset(completed_training_ids):
        raise ValueError("reused corpus contains runs outside the historical train split")
    index = OpenSearchExperimentIndex(DEFAULT_OPENSEARCH_URL, opensearch_base_sha256)
    index_metadata = index.connect_existing(
        expected_pair_ids={pair["pair_id"] for pair in base["pairs"]},
        expected_a_text_version="incident-a-v2",
    )
    if (
        index_metadata["embedding_provider"] != base["embedding_provider"]
        or index_metadata["embedding_dim"] != base["embedding_dim"]
    ):
        raise ValueError("OpenSearch index does not match the reused RAG corpus")

    parsed_mcp_url = urlparse(historical_mcp_sse_url)
    if (
        parsed_mcp_url.scheme != "http"
        or parsed_mcp_url.hostname not in {"localhost", "127.0.0.1"}
        or parsed_mcp_url.username
        or parsed_mcp_url.password
        or parsed_mcp_url.path != "/sse"
        or parsed_mcp_url.query
        or parsed_mcp_url.fragment
    ):
        raise ValueError("historical MCP must use a credential-free loopback /sse URL")
    parsed_api_url = urlparse(platform_api_base)
    if (
        parsed_api_url.scheme != "http"
        or parsed_api_url.hostname not in {"localhost", "127.0.0.1"}
        or parsed_api_url.username
        or parsed_api_url.password
        or parsed_api_url.path
        or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise ValueError("test-only campaign API must use a credential-free loopback URL")

    corpus_campaign = CAMPAIGN_DIR / "out" / "exploratory_campaigns" / corpus_id
    audit_path = corpus_campaign / "label_audit.json"
    if not audit_path.is_file():
        raise ValueError("reused corpus has no persisted label audit")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("sha256") != digest(
        {key: value for key, value in audit.items() if key != "sha256"}
    ):
        raise ValueError("reused corpus label audit checksum mismatch")
    if audit.get("counts", {}).get("exploratory_pairs") != len(base["pairs"]):
        raise ValueError("reused corpus label audit does not match its pair count")

    output_root = output_root or (CAMPAIGN_DIR / "out" / "exploratory_campaigns")
    campaign_id = "exploratory-rag-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    directory = output_root / campaign_id
    directory.mkdir(parents=True, exist_ok=False)

    new_config = json.loads(json.dumps(config))
    new_config["experiment_campaign_id"] = campaign_id
    new_config["train_experiments"] = train_cells
    new_config["test_experiments"] = test_cells
    knowledge = new_config.setdefault("knowledge_campaign", {})
    knowledge.update(
        version=VERSION,
        corpus_id=corpus_id,
        exploratory=True,
        parent_campaign_id=source_campaign.name,
        retrieval_backend="opensearch",
        opensearch_base_sha256=opensearch_base_sha256,
        opensearch_source_skill_sha256=source_skill_sha256,
        opensearch_current_skill_sha256=current_skill_sha256,
        allow_legacy_skill_hash=allow_legacy_skill_hash,
        opensearch_source_model=source_model,
        opensearch_current_model=current_model,
        allow_legacy_model_mismatch=allow_legacy_model_mismatch,
        opensearch_a_text_version=index_metadata["a_text_version"],
        retriever_version=OPENSEARCH_RETRIEVER_VERSION,
        include_unvalidated_experimental=include_unvalidated_experimental,
        top_k=3,
        min_score=0.05,
        max_rounds=2,
        max_total_tokens=EXPLORATORY_MAX_TOTAL_TOKENS,
    )
    new_config["historical_observability"] = {
        "mcp_sse_url": historical_mcp_sse_url,
        "required": True,
        "mode": "dual_write",
        "campaign_id": campaign_id,
        "timeout_seconds": 90,
        "visibility_timeout_seconds": 600,
    }
    new_config["platform"]["api_base"] = platform_api_base
    new_config["output"].update(
        results_dir=str(directory),
        runs_csv="runs.csv",
        manifests_dir="manifests",
        evaluation_csv="exploratory_rag_evaluation.csv",
    )
    new_cells = [*train_cells, *test_cells]
    (directory / "config.yaml").write_text(
        yaml.safe_dump(new_config, sort_keys=False), encoding="utf-8"
    )
    (directory / "plan.json").write_text(
        json.dumps(new_cells, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (directory / "ledger.jsonl").open("w", encoding="utf-8") as handle:
        for row in _pilot_training_ledger(train_cells, cohort["latest"]):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    for cell in train_cells:
        entry = cohort["latest"].get(cell["cell_id"], {})
        if entry.get("status") != "completed":
            continue
        source_annotation = (
            source_campaign / "rag_results" / cell["cell_id"] / "annotate.json"
        )
        if not source_annotation.is_file():
            raise ValueError(
                f"historical annotation is missing for {cell['cell_id']}"
            )
        annotation = json.loads(source_annotation.read_text(encoding="utf-8"))
        if annotation.get("run_id") != entry["run_id"]:
            raise ValueError(
                f"historical annotation run ID mismatch for {cell['cell_id']}"
            )
        destination = directory / "rag_results" / cell["cell_id"] / "annotate.json"
        write_once(destination, annotation)

    _atomic_json(directory / "label_audit.json", audit)
    _atomic_json(
        directory / "exploratory_manifest.json",
        {
            "mode": EXPLORATORY_MODE,
            "source_campaign_id": source_campaign.name,
            "source_hashes": cohort["source_hashes"],
            "label_audit_sha256": audit["sha256"],
            "planned_test_count": len(test_cells),
            "test_only": True,
            "training_runs_executed": 0,
            "training_runs_reused": len(completed_training_ids),
            "source_training_status_counts": {
                status: sum(
                    cohort["latest"].get(cell["cell_id"], {}).get("status") == status
                    for cell in train_cells
                )
                for status in sorted(
                    {
                        cohort["latest"].get(cell["cell_id"], {}).get("status")
                        for cell in train_cells
                    }
                )
            },
            "corpus_id": corpus_id,
            "base_sha256": base["sha256"],
            "opensearch_base_sha256": opensearch_base_sha256,
            "opensearch_index": index_metadata,
            "skill_compatibility": {
                "source_skill_sha256": source_skill_sha256,
                "current_skill_sha256": current_skill_sha256,
                "hashes_match": not legacy_skill_mismatch,
                "legacy_hash_allowed": allow_legacy_skill_hash,
            },
            "model_compatibility": {
                "source_model": source_model,
                "current_model": current_model,
                "models_match": not legacy_model_mismatch,
                "legacy_model_mismatch_allowed": (
                    allow_legacy_model_mismatch
                ),
            },
            "candidate_count": len(base["pairs"]),
            "unvalidated_pair_count": sum(
                not pair["effect_validated"] for pair in base["pairs"]
            ),
            "include_unvalidated_experimental": include_unvalidated_experimental,
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "require_best_guess": True,
            "test_runs_started": 0,
            "base_is_validated": False,
        },
    )
    return directory


def run_campaign(
    directory: Path,
    *,
    resume: bool = False,
    stop_after_first_test: bool = False,
) -> int:
    directory = directory.resolve()
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    if not config.get("knowledge_campaign", {}).get("exploratory"):
        raise ValueError("campaign is not an exploratory RAG campaign")
    if len(config.get("test_experiments") or []) not in {1, 35}:
        raise ValueError("exploratory RAG campaign must contain one pilot or 35 test cells")
    return controlled_campaign.execute(
        directory,
        resume=resume,
        stop_after_first_test=stop_after_first_test,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-campaign", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument(
        "--prepare-test-only",
        action="store_true",
        help="Prepare a pilot/full test campaign using an existing corpus and OpenSearch index",
    )
    parser.add_argument("--reuse-corpus-id")
    parser.add_argument("--opensearch-base-sha256")
    parser.add_argument(
        "--allow-legacy-skill-corpus",
        action="store_true",
        help="Explicitly allow a pinned exploratory corpus built with a different Skill hash",
    )
    parser.add_argument(
        "--allow-legacy-model-mismatch",
        action="store_true",
        help=(
            "Explicitly allow an exploratory corpus produced by a different model; "
            "the online model remains the configured LLM_MODEL"
        ),
    )
    parser.add_argument(
        "--include-unvalidated-experimental",
        action="store_true",
        help=(
            "Make effect-unvalidated pairs eligible as weak analogies in this "
            "test-only exploratory RAG campaign"
        ),
    )
    parser.add_argument(
        "--pilot-cell",
        default=DEFAULT_PILOT_CELL,
        help="One planned test cell for a canary; omit with --full-test-only",
    )
    parser.add_argument("--full-test-only", action="store_true")
    parser.add_argument(
        "--historical-mcp-sse-url",
        default="http://127.0.0.1:18082/sse",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8012")
    parser.add_argument("--run", type=Path, help="Prepared one-cell canary or 35-test campaign")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--stop-after-first-test",
        action="store_true",
        help="Pause after processing the first completed test, including on resume",
    )
    args = parser.parse_args()
    preparation_modes = sum(
        (args.prepare, args.prepare_test_only, bool(args.run))
    )
    if preparation_modes != 1:
        parser.error("choose exactly one of --prepare, --prepare-test-only or --run")
    if args.resume and not args.run:
        parser.error("--resume requires --run")
    if args.stop_after_first_test and not args.run:
        parser.error("--stop-after-first-test requires --run")
    if args.full_test_only and not args.prepare_test_only:
        parser.error("--full-test-only requires --prepare-test-only")
    if args.include_unvalidated_experimental and not args.prepare_test_only:
        parser.error("--include-unvalidated-experimental requires --prepare-test-only")
    if args.prepare:
        if not args.source_campaign:
            parser.error("--prepare requires --source-campaign")
        directory = prepare_campaign(args.source_campaign)
        print(f"Prepared exploratory 35-test campaign; no test run started: {directory}")
        return
    if args.prepare_test_only:
        if not args.source_campaign or not args.reuse_corpus_id:
            parser.error(
                "--prepare-test-only requires --source-campaign and --reuse-corpus-id"
            )
        if not args.opensearch_base_sha256:
            parser.error("--prepare-test-only requires --opensearch-base-sha256")
        if args.full_test_only and args.pilot_cell != DEFAULT_PILOT_CELL:
            parser.error("--full-test-only cannot be combined with --pilot-cell")
        directory = prepare_test_only_campaign(
            args.source_campaign,
            corpus_id=args.reuse_corpus_id,
            opensearch_base_sha256=args.opensearch_base_sha256,
            allow_legacy_skill_hash=args.allow_legacy_skill_corpus,
            allow_legacy_model_mismatch=args.allow_legacy_model_mismatch,
            include_unvalidated_experimental=(
                args.include_unvalidated_experimental
            ),
            pilot_cell=None if args.full_test_only else args.pilot_cell,
            historical_mcp_sse_url=args.historical_mcp_sse_url,
            platform_api_base=args.api_base,
        )
        scope = "35-test" if args.full_test_only else f"pilot {args.pilot_cell}"
        print(
            f"Prepared test-only {scope} campaign; no test run started: {directory}"
        )
        return
    raise SystemExit(
        run_campaign(
            args.run,
            resume=args.resume,
            stop_after_first_test=args.stop_after_first_test,
        )
    )


if __name__ == "__main__":
    main()
