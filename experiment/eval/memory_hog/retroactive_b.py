"""Generate a versioned, ground-truth-guided B revision for completed train runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from experiment.eval.memory_hog import runner
from experiment.platform.backend.analysis.llm_client import (
    DEFAULT_LLM_MODEL,
    call_llm_tool_messages,
    llm_health,
)
from experiment.platform.backend.analysis.system_card import load_system_card
from experiment.platform.backend.rca_knowledge import (
    VERSION,
    KnowledgeStore,
    StrictModel,
    canonical,
    compact_evidence_for_model,
    constrain_evidence_reference_schema,
    constrain_fault_type_schema,
    digest,
    usable_evidence_ids,
    write_once,
)
from experiment.platform.backend.rca_skill import (
    Diagnosis,
    _families,
    _json_content,
    load_skill,
    validate_diagnosis,
)
from experiment.eval.memory_hog.controlled_campaign import (
    campaign_cells,
    read_ledger,
)

RETRO_B_VERSION = "skill-rag-ab-retro-b-v1"
ANNOTATION_SOURCE = "ground_truth_label"
CAMPAIGN_DIR = Path(__file__).resolve().parent
REPO_ROOT = CAMPAIGN_DIR.parents[2]
OBJECT_STORE = REPO_ROOT / "experiment" / "platform" / "data" / "object_store"


class TargetResult(StrictModel):
    service: str
    fault_type: str
    causal_category: str


class EvidenceAssessment(StrictModel):
    relation: Literal["supports", "contradicts", "ambiguous"]
    finding: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(max_length=10)


class InvestigationStep(StrictModel):
    kind: Literal["observed", "recommended"]
    family: Literal["metrics", "logs", "traces", "lifecycle"]
    service: str
    phase: Literal["baseline", "fault", "post"]
    objective: str = Field(min_length=1, max_length=600)
    verification: str = Field(min_length=1, max_length=1000)
    expected_support: str = Field(min_length=1, max_length=600)
    expected_refutation: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(max_length=10)


class RetroactiveB(StrictModel):
    target_result: TargetResult
    target_result_source: Literal["ground_truth_label"]
    diagnosis: Diagnosis
    evidence_assessment: list[EvidenceAssessment] = Field(max_length=20)
    symptom_relation: str = Field(min_length=1, max_length=2000)
    investigation_steps: list[InvestigationStep] = Field(min_length=1, max_length=12)
    applicability: list[str] = Field(min_length=1, max_length=10)
    recommended_checks: list[str] = Field(max_length=10)
    limitations: list[str] = Field(max_length=20)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _retro_corpus_id(original_corpus_id: str) -> str:
    suffix = "-retro-b-v1"
    prefix = original_corpus_id[: 100 - len(suffix)]
    value = f"{prefix}{suffix}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("could not construct a valid retroactive corpus ID")
    return value


def _training_cohort(campaign_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    config_path = campaign_dir / "config.yaml"
    plan_path = campaign_dir / "plan.json"
    ledger_path = campaign_dir / "ledger.jsonl"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    cells = campaign_cells(config)
    if plan != cells or len(cells) != 70:
        raise ValueError("retroactive B requires the original immutable 70-cell plan")
    train_cells = [cell for cell in cells if cell.get("phase") == "train"]
    test_cells = [cell for cell in cells if cell.get("phase") == "test"]
    if len(train_cells) != 35 or len(test_cells) != 35:
        raise ValueError("retroactive B requires the 35/35 train/test split")

    ledger_rows = read_ledger(ledger_path)
    latest = {row["cell_id"]: row for row in ledger_rows}
    terminal = {"completed", "blocked", "aborted", "failed", "stopped"}
    if any(latest.get(cell["cell_id"], {}).get("status") not in terminal for cell in train_cells):
        raise ValueError("all training cells must be terminal before retroactive annotation")
    if any(latest.get(cell["cell_id"], {}).get("run_id") for cell in test_cells):
        raise ValueError("retroactive training annotation is forbidden after test runs start")
    if (campaign_dir / "rag_base.json").exists():
        raise ValueError("the original RAG base is already frozen; use a new reviewed protocol")

    source_hashes = {
        "config.yaml": _file_sha256(config_path),
        "plan.json": _file_sha256(plan_path),
        "ledger.jsonl": _file_sha256(ledger_path),
    }
    return config, train_cells, {
        "latest": latest,
        "source_hashes": source_hashes,
        "test_cell_count": len(test_cells),
        "completed_cells": [
            cell["cell_id"]
            for cell in train_cells
            if latest.get(cell["cell_id"], {}).get("status") == "completed"
        ],
    }


def _source_annotation(
    campaign_dir: Path,
    cell: dict[str, Any],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    path = campaign_dir / "rag_results" / cell["cell_id"] / "annotate.json"
    if not path.is_file():
        raise ValueError(f"completed training run lacks its annotation: {cell['cell_id']}")
    record = json.loads(path.read_text(encoding="utf-8"))
    response = record.get("response") or {}
    pair = response.get("pair") or {}
    target = {
        "service": cell["service"],
        "fault_type": cell["chaos_type"],
    }
    if (
        entry.get("status") != "completed"
        or record.get("run_id") != entry.get("run_id")
        or pair.get("source_run_id") != entry.get("run_id")
        or pair.get("split") != "train"
        or pair.get("labels") != target
    ):
        raise ValueError(f"training annotation provenance mismatch: {cell['cell_id']}")
    if pair.get("sha256") != digest(
        {key: value for key, value in pair.items() if key != "sha256"}
    ):
        raise ValueError(f"source training pair hash mismatch: {cell['cell_id']}")
    if not isinstance(response.get("a"), dict) or not isinstance(response.get("evidence"), list):
        raise ValueError(f"training annotation lacks A or evidence: {cell['cell_id']}")
    if response["a"] != pair.get("a") or response["evidence"] != pair.get("evidence"):
        raise ValueError(f"training A/evidence differ between persisted response and pair")
    gates = entry.get("gates") or {}
    effect_validated = (
        gates.get("intervention_confirmed_on_target") is True
        and (gates.get("mechanism_observed") or {}).get("passed") is True
    )
    if pair.get("effect_validated") is not effect_validated:
        raise ValueError(f"effect-validation provenance mismatch: {cell['cell_id']}")
    category = runner._fault_category_for(config, cell["chaos_type"])
    if not category or category == "unknown":
        raise ValueError(f"no causal category mapping for {cell['chaos_type']}")
    return {
        "path": path,
        "record": record,
        "response": response,
        "pair": pair,
        "labels": {**target, "causal_category": category},
        "effect_validated": effect_validated,
    }


def _validate_retro_b(
    value: dict[str, Any],
    *,
    expected: dict[str, str],
    known_services: set[str],
    categories: set[str],
    evidence: list[dict[str, Any]],
    possible_fault_types: list[str],
) -> None:
    target = value.get("target_result") or {}
    diagnosis = value.get("diagnosis") or {}
    if value.get("target_result_source") != ANNOTATION_SOURCE:
        raise ValueError("target result must be explicitly marked as a ground-truth annotation")
    if target != expected:
        raise ValueError("target_result must exactly reproduce the training labels")
    if expected["service"] not in known_services or expected["causal_category"] not in categories:
        raise ValueError("annotation target is not in the system card vocabulary")
    if (
        diagnosis.get("primary_service") != expected["service"]
        or diagnosis.get("fault_type") != expected["fault_type"]
        or diagnosis.get("category") != expected["causal_category"]
    ):
        raise ValueError("diagnosis fields must match the annotated service, fault and cause category")
    if diagnosis.get("fault_type") not in possible_fault_types:
        raise ValueError("diagnosis fault_type is not allowed by the campaign")
    allowed = set(usable_evidence_ids(evidence))
    for relation in value.get("evidence_assessment") or []:
        if not set(relation.get("evidence_ids") or []).issubset(allowed):
            raise ValueError("evidence assessment references unavailable evidence")
    for step in value.get("investigation_steps") or []:
        refs = set(step.get("evidence_ids") or [])
        if not refs.issubset(allowed):
            raise ValueError("investigation step references unavailable evidence")
        if step.get("kind") == "observed" and not refs:
            raise ValueError("observed investigation steps must cite existing evidence")
        if step.get("kind") == "recommended" and refs:
            raise ValueError("recommended future checks must not claim executed evidence")


def _generate_retro_b(
    *,
    llm: Any,
    labels: dict[str, str],
    document_a: dict[str, Any],
    evidence: list[dict[str, Any]],
    original_diagnosis: dict[str, Any],
    executed_checks: list[dict[str, Any]],
    known_services: set[str],
    categories: set[str],
    possible_fault_types: list[str],
    max_attempts: int = 4,
    focused_evidence: list[dict[str, Any]] | None = None,
    restarted_services: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    schema = RetroactiveB.model_json_schema()
    constrain_fault_type_schema(schema, possible_fault_types)
    if focused_evidence is not None:
        definitions = schema["$defs"]
        for field, expected in labels.items():
            definitions["TargetResult"]["properties"][field] = {
                "type": "string", "enum": [expected],
            }
        for field, expected in (
            ("primary_service", labels["service"]),
            ("fault_type", labels["fault_type"]),
            ("category", labels["causal_category"]),
        ):
            definitions["Diagnosis"]["properties"][field] = {
                "type": "string", "enum": [expected],
            }
        if restarted_services == []:
            definitions["Diagnosis"]["properties"]["restart_analysis"]["maxItems"] = 0
    evidence_ids = usable_evidence_ids(evidence)
    constrain_evidence_reference_schema(schema, evidence_ids)
    model_evidence = compact_evidence_for_model(evidence)
    context = {
        "stage": "retroactive_build_b",
        "annotation_target": labels,
        "annotation_target_source": ANNOTATION_SOURCE,
        "observed_incident_a": document_a,
        "persisted_run_evidence": model_evidence,
        "previous_diagnosis_for_contrast_only": original_diagnosis,
        "checks_already_executed": executed_checks,
        "known_services": sorted(known_services),
        "allowed_categories": sorted(categories),
        "application_context": load_system_card("sock-shop"),
        "focused_observed_evidence": focused_evidence or [],
        "observed_restarted_services": restarted_services,
        "instructions": [
            "Reproduza exatamente annotation_target em target_result e diagnosis.",
            "O rótulo alvo é ground truth de treinamento, não evidência observada.",
            "Construa B como um guia que ensina quais verificações levam ao resultado anotado.",
            "Relacione cada achado observado apenas a evidence_ids fornecidos e descreva contradições.",
            "Não invente consultas executadas, métricas, logs, traces, eventos ou motivos de restart.",
            "Passos recomendados são futuros: use kind=recommended e evidence_ids=[].",
            "Passos observados exigem IDs existentes e devem ser classificados como observed.",
            "Status confirmed só pode ser usado se os dados fault disponíveis sustentarem causa, serviço e mecanismo com duas famílias independentes e cadeia causal.",
            "Se os dados não sustentarem o rótulo, mantenha diagnosis.status=inconclusive, explique a lacuna e ainda registre o resultado-alvo separadamente.",
            "focused_observed_evidence preserva detalhes dos mesmos IDs que podem ter sido truncados nos resumos gerais; use esses detalhes, inclusive destino de chamadas com erro.",
            "Se observed_restarted_services estiver presente, preencha restart_analysis somente para esses serviços; lista vazia significa nenhum restart observado.",
            "Não exija OOM ou restart para memory-hog, nem saturação absoluta para cpu-hog: compare a variação com baseline e a corroboração independente. Ausência de OOM não refuta aumento de memória.",
            "Erro HTTP pode ser gerado pelo proxy antes da aplicação: ausência de logs no destino não refuta erro na chamada. Não afirme erro interno da aplicação sem evidência.",
            "Não classifique um percentual intermediário de memória como saturação crítica sem um limiar sustentado. Não invente relações de dependência; confira application_context e as arestas dos traces.",
            "Narrativas em português; JSON estrito conforme output_schema.",
        ],
        "output_schema": schema,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Você gera um guia diagnóstico retrospectivo para treinamento. "
                "O resultado-alvo fornecido é uma anotação ground truth autorizada; "
                "não o apresente como telemetria nem como evidência. "
                "Evidência só existe nos registros fornecidos e deve ser citada por ID. "
                "Diferencie achados observados de verificações recomendadas. "
                "Não transforme o resultado-alvo em confirmação causal quando os dados "
                "não o sustentarem. Retorne somente JSON estrito."
            ),
        },
        {"role": "user", "content": canonical(context)},
    ]
    attempts: list[dict[str, Any]] = []
    total_tokens = 0
    inconclusive_seen = False
    for index in range(max_attempts):
        usage: dict[str, Any] = {}
        raw: dict[str, Any] = {}
        started = time.monotonic()
        try:
            raw = llm(
                messages,
                [],
                tool_choice="none",
                parallel_tool_calls=False,
                max_new_tokens=4096,
                chat_template_kwargs={"enable_thinking": False},
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "RetroactiveB", "schema": schema},
                },
                usage=usage,
            )
            value = _json_content(raw)
            status = (value.get("diagnosis") or {}).get("status")
            if inconclusive_seen and status == "confirmed":
                raise ValueError("contract repair cannot strengthen inconclusive to confirmed")
            inconclusive_seen = inconclusive_seen or status == "inconclusive"
            validated = RetroactiveB.model_validate(value).model_dump()
            _validate_retro_b(
                validated,
                expected=labels,
                known_services=known_services,
                categories=categories,
                evidence=evidence,
                possible_fault_types=possible_fault_types,
            )
            generated_diagnosis = validated["diagnosis"]
            if set(generated_diagnosis["supporting_evidence"]) & set(
                generated_diagnosis["contradicting_evidence"]
            ):
                raise ValueError("use distinct evidence IDs for support and contradiction")
            if restarted_services is not None and {
                row["service"] for row in generated_diagnosis["restart_analysis"]
            } != set(restarted_services):
                raise ValueError("restart_analysis must cover exactly observed_restarted_services")
        except (ValueError, KeyError, TypeError) as exc:
            attempts.append({
                "status": "invalid_response",
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.monotonic() - started) * 1000),
                "usage": usage,
                "response": raw,
            })
            if index + 1 == max_attempts:
                raise
            if isinstance(raw.get("content"), str):
                messages.append({"role": "assistant", "content": raw["content"]})
            messages.append({
                "role": "user",
                "content": (
                    f"Corrija apenas o contrato e as regras de anotação: {exc}. "
                    "Não acrescente fatos nem transforme inconclusive em confirmed "
                    "durante um reparo de formato sem novas evidências."
                ),
            })
            continue
        attempts.append({
            "status": "returned",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "usage": usage,
            "response": raw,
        })
        total_tokens += int(usage.get("total_tokens") or 0)
        return validated, attempts, total_tokens
    raise RuntimeError("unreachable retroactive B generation state")


def annotate_campaign(
    campaign_dir: Path,
    *,
    llm: Any = call_llm_tool_messages,
    model_health: Any = llm_health,
    object_store_root: Path = OBJECT_STORE,
) -> dict[str, Any]:
    campaign_dir = campaign_dir.resolve()
    config, train_cells, cohort = _training_cohort(campaign_dir)
    health = model_health(timeout=5)
    if health.get("ok") is not True:
        raise RuntimeError(f"configured LLM unavailable: {health.get('error', health)}")

    original_corpus_id = config["knowledge_campaign"]["corpus_id"]
    corpus_id = _retro_corpus_id(original_corpus_id)
    output_dir = campaign_dir / "retro_b_v1"
    manifest_path = output_dir / "manifest.json"
    source_run_ids = []
    for cell in train_cells:
        entry = cohort["latest"].get(cell["cell_id"], {})
        if entry.get("status") == "completed":
            source_run_ids.append(entry["run_id"])
    source_run_ids.sort()
    metadata = {
        "version": RETRO_B_VERSION,
        "knowledge_version": VERSION,
        "campaign_id": config["experiment_campaign_id"],
        "original_corpus_id": original_corpus_id,
        "retro_corpus_id": corpus_id,
        "source_hashes": cohort["source_hashes"],
        "training_run_ids": source_run_ids,
        "excluded_cells": {
            cell["cell_id"]: cohort["latest"].get(cell["cell_id"], {}).get("status", "not_run")
            for cell in train_cells
            if cohort["latest"].get(cell["cell_id"], {}).get("status") != "completed"
        },
        "test_run_count": 0,
        "annotation_source": ANNOTATION_SOURCE,
        "model": DEFAULT_LLM_MODEL,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in (
            "version", "campaign_id", "original_corpus_id", "retro_corpus_id",
            "source_hashes", "training_run_ids", "excluded_cells",
        ):
            if existing.get(key) != metadata.get(key):
                raise ValueError("retroactive B manifest does not match campaign provenance")
    else:
        _atomic_json(manifest_path, {**metadata, "status": "in_progress"})

    known = set((load_system_card("sock-shop") or {}).get("services") or {})
    categories = set(
        (load_system_card("sock-shop") or {}).get("allowed_fault_categories") or []
    )
    if not known or not categories:
        raise ValueError("Sock Shop system card lacks services or allowed RCA categories")
    possible_fault_types = sorted({cell["chaos_type"] for cell in train_cells})
    _, _, current_skill_hash = load_skill()
    store = KnowledgeStore(object_store_root / ".rca_knowledge", corpus_id)
    pair_summaries = []

    with store.locked():
        if (store.directory / "base.json").exists():
            raise ValueError("retroactive corpus is already frozen")
        for cell in train_cells:
            entry = cohort["latest"].get(cell["cell_id"], {})
            if entry.get("status") != "completed":
                continue
            source = _source_annotation(campaign_dir, cell, entry, config)
            source_pair = source["pair"]
            if source_pair.get("skill_hash") != current_skill_hash:
                raise ValueError("source A was built with a different Skill RCA version")
            result_path = output_dir / "annotations" / f"{cell['cell_id']}.json"
            if result_path.exists():
                record = json.loads(result_path.read_text(encoding="utf-8"))
                if (
                    record.get("run_id") != entry["run_id"]
                    or record.get("source_annotation_sha256") != _file_sha256(source["path"])
                    or record.get("target_result") != source["labels"]
                ):
                    raise ValueError(f"retroactive B result provenance mismatch: {cell['cell_id']}")
                retro_pair = record["pair"]
            else:
                guide, attempts, total_tokens = _generate_retro_b(
                    llm=llm,
                    labels=source["labels"],
                    document_a=source["response"]["a"],
                    evidence=source["response"]["evidence"],
                    original_diagnosis=source["response"].get("diagnosis") or {},
                    executed_checks=source["response"].get("additional_checks") or [],
                    known_services=known,
                    categories=categories,
                    possible_fault_types=possible_fault_types,
                )
                diagnosis = Diagnosis.model_validate(guide["diagnosis"])
                diagnosis = validate_diagnosis(
                    diagnosis,
                    source["response"]["evidence"],
                    known,
                    categories,
                    [
                        row["service"]
                        for row in source["response"]["initial_diagnosis"]["restart_analysis"]
                    ],
                    possible_fault_types,
                )
                target_service_evidence = any(
                    evidence_item["id"] in diagnosis.supporting_evidence
                    and evidence_item["status"] == "ok"
                    and evidence_item["fault_observation_count"] > 0
                    and re.search(
                        rf"(?<![a-z0-9-]){re.escape(source['labels']['service'])}(?![a-z0-9-])",
                        canonical(evidence_item.get("digest", {})),
                    )
                    for evidence_item in source["response"]["evidence"]
                )
                if not target_service_evidence:
                    diagnosis.status = "inconclusive"
                    diagnosis.limitations.append(
                        "Evidencia fault nao identifica o servico-alvo do rotulo"
                    )
                guide["diagnosis"] = diagnosis.model_dump()
                supported = (
                    source["effect_validated"]
                    and diagnosis.status == "confirmed"
                    and diagnosis.primary_service == source["labels"]["service"]
                    and diagnosis.fault_type == source["labels"]["fault_type"]
                    and diagnosis.category == source["labels"]["causal_category"]
                    and len(_families(
                        diagnosis.supporting_evidence,
                        source["response"]["evidence"],
                    )) >= 2
                    and target_service_evidence
                )
                retro_pair = {
                    "version": VERSION,
                    "annotation_revision": RETRO_B_VERSION,
                    "pair_id": digest([corpus_id, entry["run_id"]]),
                    "source_run_id": entry["run_id"],
                    "split": "train",
                    "labels": {
                        "service": source["labels"]["service"],
                        "fault_type": source["labels"]["fault_type"],
                    },
                    "a": source["response"]["a"],
                    "b": guide,
                    "guide_status": "validated" if supported else "pending",
                    "causal_status": "supported" if supported else "inconclusive",
                    "effect_validated": source["effect_validated"],
                    "phases": source_pair["phases"],
                    "evidence": source["response"]["evidence"],
                    "executed_checks": source["response"].get("additional_checks") or [],
                    "skill_hash": source_pair["skill_hash"],
                    "validation": "retroactive model annotation; support and effect gates unchanged",
                    "source_annotation_sha256": _file_sha256(source["path"]),
                    "options_sha256": digest({
                        "retro_b_version": RETRO_B_VERSION,
                        "source_options_sha256": source_pair["options_sha256"],
                    }),
                    "generation": {
                        "model": DEFAULT_LLM_MODEL,
                        "attempts": attempts,
                        "model_calls": len(attempts),
                        "total_tokens": total_tokens,
                    },
                }
                retro_pair["sha256"] = digest(retro_pair)
                record = {
                    "run_id": entry["run_id"],
                    "cell_id": cell["cell_id"],
                    "target_result": source["labels"],
                    "source_annotation_sha256": _file_sha256(source["path"]),
                    "pair": retro_pair,
                }
                write_once(result_path, record)

            existing_pair = store.read_record(entry["run_id"], "pair")
            if existing_pair is None:
                store.save_record(entry["run_id"], "pair", retro_pair)
            elif existing_pair.get("sha256") != retro_pair.get("sha256"):
                raise ValueError(f"retro corpus contains a conflicting pair: {cell['cell_id']}")
            pair_summaries.append({
                "cell_id": cell["cell_id"],
                "run_id": entry["run_id"],
                "guide_status": retro_pair["guide_status"],
                "causal_status": retro_pair["causal_status"],
                "effect_validated": retro_pair["effect_validated"],
                "diagnosis_status": (retro_pair["b"].get("diagnosis") or {}).get("status"),
            })

        base_result: dict[str, Any]
        try:
            base = store.freeze(source_run_ids)
        except ValueError as exc:
            if str(exc) != "no supported training pairs; cannot freeze an empty base":
                raise
            base_result = {
                "status": "blocked_empty",
                "eligible_count": 0,
                "error": str(exc),
            }
        else:
            base_result = {
                "status": "frozen",
                "sha256": base["sha256"],
                "eligible_count": len(base["pairs"]),
                "excluded_count": base["excluded_count"],
            }

    summary = {
        **metadata,
        "status": base_result["status"],
        "annotations": {
            "count": len(pair_summaries),
            "guide_status": dict(Counter(item["guide_status"] for item in pair_summaries)),
            "causal_status": dict(Counter(item["causal_status"] for item in pair_summaries)),
            "effect_validated": dict(Counter(str(item["effect_validated"]).lower() for item in pair_summaries)),
            "diagnosis_status": dict(Counter(item["diagnosis_status"] for item in pair_summaries)),
            "per_run": pair_summaries,
        },
        "base": base_result,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(output_dir / "summary.json", summary)
    _atomic_json(manifest_path, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    args = parser.parse_args(argv)
    result = annotate_campaign(args.campaign)
    print(json.dumps({
        "campaign_id": result["campaign_id"],
        "status": result["status"],
        "annotated_runs": result["annotations"]["count"],
        "eligible_pairs": result["base"]["eligible_count"],
        "retro_corpus_id": result["retro_corpus_id"],
        "summary": str(args.campaign.resolve() / "retro_b_v1" / "summary.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "frozen" else 1


if __name__ == "__main__":
    raise SystemExit(main())
