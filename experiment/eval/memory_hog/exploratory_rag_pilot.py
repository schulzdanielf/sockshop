"""Prepare or run one isolated exploratory RAG test using inconclusive guidance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from experiment.eval.memory_hog import controlled_campaign
from experiment.eval.memory_hog.retroactive_b import (
    OBJECT_STORE,
    _atomic_json,
    _source_annotation,
    _training_cohort,
)
from experiment.platform.backend.analysis.embeddings import get_embedding_provider
from experiment.platform.backend.analysis.llm_client import DEFAULT_LLM_MODEL
from experiment.platform.backend.rca_knowledge import (
    VERSION,
    DiagnosticGuide,
    Diagnosis,
    IncidentA,
    KnowledgeStore,
    base_a_text_version,
    digest,
    load_skill,
    write_once,
)

DEFAULT_PILOT_CELL = "payment-http-status-code-r1"
SOURCE_B = "payment-http-status-code-r0"
EXPLORATORY_MODE = "exploratory"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reviewed_pair(
    source_campaign: Path, config: dict[str, Any], cohort: dict[str, Any]
) -> dict[str, Any]:
    cell = next(
        row for row in controlled_campaign.campaign_cells(config)
        if row["cell_id"] == SOURCE_B
    )
    entry = cohort["latest"][SOURCE_B]
    source = _source_annotation(source_campaign, cell, entry, config)
    if not source["effect_validated"]:
        raise ValueError("the reviewed source run did not validate its intervention effect")

    review_path = (
        source_campaign
        / "retro_b_recheck_v2"
        / "reviewed_b"
        / f"{SOURCE_B}.json"
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    pair = source["pair"]
    if (
        review.get("run_id") != entry["run_id"]
        or review.get("source_pair_id") != pair["pair_id"]
        or review.get("source_bundle_sha256")
        != json.loads(
            (
                source_campaign
                / "retro_b_recheck_v2"
                / "evidence"
                / f"{SOURCE_B}.json"
            ).read_text(encoding="utf-8")
        )["sha256"]
        or review.get("promoted_to_corpus") is not False
    ):
        raise ValueError("reviewed guide provenance does not match the training source")

    document = review["b"]
    diagnosis = Diagnosis.model_validate(document["diagnosis"])
    diagnosis.status = "inconclusive"
    guide = DiagnosticGuide(
        diagnosis=diagnosis,
        symptom_relation=document["symptom_relation"],
        investigation_steps=[
            (
                f"{step['objective']} Verificação histórica: {step['verification']} "
                f"Esperado: {step['expected_support']} Refutação: "
                f"{step['expected_refutation']}"
            )
            for step in document["investigation_steps"]
        ],
        applicability=document["applicability"],
        recommended_checks=document["recommended_checks"],
        limitations=[
            *document["limitations"],
            "Guia exploratório inconclusivo; resultado histórico não é evidência do run atual.",
        ],
    )
    a = IncidentA.model_validate(pair["a"])
    source_pair = {
        "pair_id": pair["pair_id"],
        "source_run_id": entry["run_id"],
        "split": "train",
        "a": a.model_dump(),
        "b": guide.model_dump(),
        "labels": {
            **pair["labels"],
            "causal_category": document["target_result"]["causal_category"],
        },
        "effect_validated": source["effect_validated"],
        "guide_status": "pending",
        "causal_status": "inconclusive",
        "review_status": "assistant_reviewed_boundary_only",
        "skill_hash": pair["skill_hash"],
    }
    return {**source_pair, "sha256": digest(source_pair)}


def _install_exploratory_base(
    corpus_id: str, pairs: list[dict[str, Any]]
) -> dict[str, Any]:
    provider = get_embedding_provider()
    _, _, skill_hash = load_skill()
    if not pairs:
        raise ValueError("exploratory corpus requires at least one source pair")
    pair_ids = [pair["pair_id"] for pair in pairs]
    run_ids = [pair["source_run_id"] for pair in pairs]
    if len(set(pair_ids)) != len(pair_ids) or len(set(run_ids)) != len(run_ids):
        raise ValueError("exploratory source pairs must have unique IDs")
    index = {}
    for pair in pairs:
        if pair["skill_hash"] != skill_hash:
            raise ValueError("source guide uses a different Skill RCA version")
        vector = provider.embed(IncidentA.model_validate(pair["a"]).retrieval_text())
        values = vector.tolist()
        if len(values) != provider.dim or not all(math.isfinite(value) for value in values):
            raise ValueError("embedding provider returned an invalid exploratory index")
        index[pair["pair_id"]] = values
    payload = {
        "version": VERSION,
        "a_text_version": IncidentA.model_validate(pairs[0]["a"]).text_version,
        "mode": EXPLORATORY_MODE,
        "embedding_provider": provider.name,
        "embedding_dim": provider.dim,
        "skill_sha256": skill_hash,
        "model": DEFAULT_LLM_MODEL,
        "training_run_ids": sorted(run_ids),
        "pairs": pairs,
        "index": index,
    }
    base_a_text_version(payload)
    base = {**payload, "sha256": digest(payload)}
    store = KnowledgeStore(OBJECT_STORE / ".rca_knowledge", corpus_id)
    with store.locked():
        if (store.directory / "base.json").exists():
            raise ValueError("exploratory corpus ID collides with a validated base")
        write_once(store.directory / "exploratory_base.json", base)
        return store.load_exploratory_base()


def _pilot_training_ledger(
    train_cells: list[dict[str, Any]], latest: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for cell in train_cells:
        entry = latest.get(cell["cell_id"])
        if not entry:
            continue
        imported = dict(entry)
        imported["source_attempt"] = int(entry.get("attempt", 1))
        imported["attempt"] = 1
        rows.append(imported)
    return rows


def prepare_pilot(
    source_campaign: Path,
    *,
    pilot_cell_id: str = DEFAULT_PILOT_CELL,
    output_root: Path | None = None,
) -> Path:
    source_campaign = source_campaign.resolve()
    config, train_cells, cohort = _training_cohort(source_campaign)
    if pilot_cell_id != DEFAULT_PILOT_CELL:
        raise ValueError(
            "the first exploratory pilot is limited to the reviewed payment HTTP test cell"
        )
    plan = json.loads((source_campaign / "plan.json").read_text(encoding="utf-8"))
    pilot = next(
        (
            cell for cell in plan
            if cell["cell_id"] == pilot_cell_id and cell["phase"] == "test"
        ),
        None,
    )
    if pilot is None:
        raise ValueError("selected exploratory pilot is absent from the source test split")
    if any(
        cohort["latest"].get(cell["cell_id"], {}).get("run_id")
        for cell in plan if cell["phase"] == "test"
    ):
        raise ValueError("source test phase already contains a run; refusing test leakage")
    source_pair = _reviewed_pair(source_campaign, config, cohort)
    output_root = output_root or (
        controlled_campaign.HERE / "out" / "exploratory_campaigns"
    )
    campaign_id = "exploratory-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = output_root / campaign_id
    directory.mkdir(parents=True, exist_ok=False)

    new_config = json.loads(json.dumps(config))
    new_config["experiment_campaign_id"] = campaign_id
    new_config["train_experiments"] = train_cells
    new_config["test_experiments"] = [pilot]
    new_config["knowledge_campaign"].update(
        corpus_id=campaign_id,
        exploratory=True,
        parent_campaign_id=source_campaign.name,
        top_k=1,
        min_score=0.0,
    )
    new_config["output"].update(
        results_dir=str(directory),
        runs_csv="runs.csv",
        manifests_dir="manifests",
        evaluation_csv="exploratory_rag_evaluation.csv",
    )
    new_cells = [*train_cells, pilot]
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
        write_once(destination, json.loads(source_annotation.read_text(encoding="utf-8")))

    base = _install_exploratory_base(campaign_id, [source_pair])
    hashes = {
        name: _sha256(source_campaign / name)
        for name in ("config.yaml", "plan.json", "ledger.jsonl")
    }
    _atomic_json(
        directory / "exploratory_manifest.json",
        {
            "mode": EXPLORATORY_MODE,
            "source_campaign_id": source_campaign.name,
            "source_hashes": hashes,
            "pilot_cell_id": pilot_cell_id,
            "source_pair_id": source_pair["pair_id"],
            "source_run_id": source_pair["source_run_id"],
            "corpus_id": campaign_id,
            "base_sha256": base["sha256"],
            "candidate_count": len(base["pairs"]),
            "guide_status": source_pair["guide_status"],
            "causal_status": source_pair["causal_status"],
            "test_runs_started": 0,
            "base_is_validated": False,
        },
    )
    return directory


def run_pilot(directory: Path) -> int:
    directory = directory.resolve()
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    if not config.get("knowledge_campaign", {}).get("exploratory"):
        raise ValueError("campaign is not an exploratory pilot")
    return controlled_campaign.execute(directory, resume=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-campaign", type=Path)
    parser.add_argument("--pilot-cell", default=DEFAULT_PILOT_CELL)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", type=Path, help="Prepared pilot campaign directory")
    args = parser.parse_args()
    if args.prepare == bool(args.run):
        parser.error("choose exactly one of --prepare or --run")
    if args.prepare:
        if not args.source_campaign:
            parser.error("--prepare requires --source-campaign")
        directory = prepare_pilot(args.source_campaign, pilot_cell_id=args.pilot_cell)
        print(f"Prepared exploratory pilot; no test run started: {directory}")
        return
    raise SystemExit(run_pilot(args.run))


if __name__ == "__main__":
    main()
