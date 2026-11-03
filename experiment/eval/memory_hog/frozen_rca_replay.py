"""Disabled legacy runner for RCA evaluation on frozen evidence."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from experiment.eval.memory_hog import campaign_knowledge
from experiment.eval.memory_hog.opensearch_experiments import (
    DEFAULT_OPENSEARCH_URL,
    OPENSEARCH_RETRIEVER_VERSION,
    OpenSearchExperimentIndex,
)
from experiment.platform.backend.analysis.llm_client import (
    call_llm_tool_messages,
)
from experiment.platform.backend.evidence_projection import (
    EVIDENCE_FORMAT_NOTICE,
    PROJECTION_VERSION,
)
from experiment.platform.backend.rca_knowledge import (
    A_TEXT_VERSION,
    EXPLORATORY_RETRIEVER_VERSION,
    LEGACY_A_TEXT_VERSION,
    IncidentA,
    base_a_text_version,
    compact_evidence_for_model,
    compact_exploratory_history,
    digest,
    enrich_incident_a,
    get_embedding_provider,
    retrieve_pairs,
    validate_a,
)
from experiment.platform.backend.rca_skill import Diagnosis, validate_diagnosis

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CAMPAIGN = (
    ROOT
    / "experiment/eval/memory_hog/out/exploratory_campaigns/"
    / "exploratory-rag-20261028T191703038725Z"
)
DEFAULT_RETRY_DIR = DEFAULT_CAMPAIGN / "diagnostic_retries/qwen35-model-migration-v1"
DEFAULT_OBJECT_STORE = ROOT / "experiment/platform/data/object_store"
EVALUATION_SKILL = ROOT / ".github/skills/rca-evaluation/SKILL.md"
CONDITIONS = ("skill", "control", "rag")
RETRIEVAL_POLICIES = ("hybrid", "oracle_exact_pair_in_top3")
RETRIEVAL_BACKENDS = ("local", "opensearch")
FAULT_TYPES = (
    "cpu-hog",
    "memory-hog",
    "network-latency",
    "container-kill",
    "http-status-code",
)
HISTORY_CHAR_BUDGET = 24_000
OBSERVATION_FIELDS = (
    "observed_services",
    "service_relations",
    "symptoms",
    "metric_changes",
    "log_patterns",
    "trace_patterns",
    "temporal_sequence",
    "limitations",
)


def build_messages(
    skill: str,
    contract: dict[str, Any],
    incident: dict[str, Any],
    application_context: str,
    condition: str,
    history: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown replay condition: {condition}")
    payload: dict[str, Any] = {
        "task": (
            "Analise somente o pacote congelado. Historico nao e evidencia do "
            "incidente atual; confirme mecanismos apenas com evidencias atuais."
        ),
        "contract": contract,
        "incident": incident,
        "evidence_format_notice": EVIDENCE_FORMAT_NOTICE,
    }
    if condition != "skill":
        payload["application_context"] = application_context
    if condition == "rag":
        if any(
            {"labels", "diagnosis", "source_run_id"} & set(item) for item in history
        ):
            raise ValueError("historical labels or diagnoses cannot enter the prompt")
        payload["historical_guidance_notice"] = (
            "Experiencias historicas sao hipoteses inconclusivas, nao fatos nem "
            "evidencia atual. Use-as apenas para sugerir alternativas; nao copie "
            "rotulos ou diagnosticos historicos. "
            "source_effect_validated=false indica que o efeito experimental de "
            "origem nao foi validado; trate o par como analogia fraca."
        )
        payload["past_experiences"] = history
    return [
        {"role": "system", "content": skill},
        {
            "role": "user",
            "content": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        },
    ]


def build_incident(
    a: IncidentA,
    evidence: list[dict[str, Any]],
    known_services: set[str],
) -> dict[str, Any]:
    validate_a(a, evidence, known_services)
    return {
        "observations": a.model_dump(exclude={"preliminary_hypotheses"}),
        "evidence": compact_evidence_for_model(evidence),
    }


def local_model(
    url: str, model: str, max_output_tokens: int, max_input_chars: int
) -> Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]:
    parsed = urlparse(url)
    if (
        parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise campaign_knowledge.EvaluationError(
            "Only a credential-free local model URL is allowed"
        )
    if not model.strip() or max_output_tokens < 1 or max_input_chars < 1:
        raise campaign_knowledge.EvaluationError(
            "Model and positive input/output budgets are required"
        )

    def call(messages: list[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
        if len(campaign_knowledge.canonical(messages)) > max_input_chars:
            raise campaign_knowledge.EvaluationError(
                "Input budget exceeded; no truncation or inference performed"
            )
        usage: dict[str, Any] = {}
        response = call_llm_tool_messages(
            messages,
            [],
            tool_choice="none",
            max_new_tokens=max_output_tokens,
            url=url,
            timeout=180,
            model=model,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "rca_diagnosis",
                    "strict": False,
                    "schema": schema,
                },
            },
            chat_template_kwargs={"enable_thinking": False},
            usage=usage,
        )
        return {**response, "usage": usage}

    return call


def _read_response(path: Path) -> dict[str, Any]:
    value = campaign_knowledge.read_json(path)
    response = value.get("response", value)
    if not isinstance(response, dict):
        raise campaign_knowledge.EvaluationError(
            f"Missing response object in frozen artifact: {path}"
        )
    return response


def _load_base(retry_dir: Path) -> dict[str, Any]:
    paths = sorted((retry_dir / "knowledge").glob("*/exploratory_base.json"))
    if len(paths) != 1:
        raise campaign_knowledge.EvaluationError(
            "Expected exactly one derived exploratory knowledge base"
        )
    base = campaign_knowledge.read_json(paths[0])
    content = {key: value for key, value in base.items() if key != "sha256"}
    if base.get("sha256") != digest(content):
        raise campaign_knowledge.EvaluationError(
            "Frozen exploratory knowledge base failed integrity validation"
        )
    return base


def _load_responses(
    retry_dir: Path,
    object_store: Path,
    evaluation: dict[str, Any],
    retry_manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    campaign_id = evaluation["campaign_id"]
    cases = [
        case for case in evaluation["per_run"] if case.get("run_status") == "completed"
    ]
    expected_case_ids = {case["cell_id"] for case in cases}
    responses: dict[str, dict[str, Any]] = {}
    for case in cases:
        path = (
            object_store
            / case["run_id"]
            / f"skill_knowledge_{campaign_id}_exploratory_rag.json"
        )
        if path.exists():
            responses[case["cell_id"]] = _read_response(path)

    prefix = f"{campaign_id}-"
    for item in retry_manifest.get("inputs", []):
        experiment_id = item["window"]["experiment_id"]
        if not experiment_id.startswith(prefix):
            raise campaign_knowledge.EvaluationError(
                "Retry input belongs to a different campaign"
            )
        case_id = experiment_id.removeprefix(prefix)
        if case_id not in expected_case_ids:
            raise campaign_knowledge.EvaluationError(
                "Retry input is not part of the frozen completed cases"
            )
        path = retry_dir / "results" / f"{item['run_id']}.json"
        responses[case_id] = _read_response(path)

    if set(responses) != expected_case_ids:
        missing = sorted(expected_case_ids - set(responses))
        raise campaign_knowledge.EvaluationError(
            f"Frozen RAG response missing for cases: {', '.join(missing)}"
        )
    return responses


def _prepare_cases(
    evaluation: dict[str, Any],
    responses: dict[str, dict[str, Any]],
    base: dict[str, Any],
    contract: dict[str, Any],
    *,
    a_text_version: str,
    retrieval_policy: str = "hybrid",
    retrieval_backend: str = "local",
    opensearch_index: OpenSearchExperimentIndex | None = None,
    include_unvalidated_experimental: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if retrieval_policy not in RETRIEVAL_POLICIES:
        raise campaign_knowledge.EvaluationError("Unknown retrieval policy")
    if retrieval_backend not in RETRIEVAL_BACKENDS:
        raise campaign_knowledge.EvaluationError("Unknown retrieval backend")
    if retrieval_backend == "opensearch" and (
        retrieval_policy != "hybrid" or opensearch_index is None
    ):
        raise campaign_knowledge.EvaluationError(
            "OpenSearch requires the non-oracle policy and a prepared index"
        )
    if retrieval_backend == "local" and opensearch_index is not None:
        raise campaign_knowledge.EvaluationError(
            "An OpenSearch index was supplied to the local retriever"
        )
    if retrieval_backend == "opensearch":
        provider = get_embedding_provider()
    cases = []
    excluded_cases = []
    for row in evaluation["per_run"]:
        if row.get("run_status") != "completed":
            continue
        case_id = row["cell_id"]
        response = responses[case_id]
        if response.get("status") == "error":
            raise campaign_knowledge.EvaluationError(
                f"Cannot replay failed source response for {case_id}"
            )
        a = IncidentA.model_validate(response["a"])
        evidence = response["evidence"]
        if not isinstance(evidence, list):
            raise campaign_knowledge.EvaluationError(
                f"Invalid evidence snapshot for {case_id}"
            )
        if a_text_version == A_TEXT_VERSION:
            a = enrich_incident_a(a, evidence, set(contract["known_services"]))
        elif a.text_version != LEGACY_A_TEXT_VERSION:
            raise campaign_knowledge.EvaluationError(
                f"Incident A version mismatch in {case_id}"
            )
        validate_a(a, evidence, set(contract["known_services"]))
        incident = build_incident(a, evidence, set(contract["known_services"]))
        source_run_id = response.get("source_run_id")
        if not isinstance(source_run_id, str) or not source_run_id:
            raise campaign_knowledge.EvaluationError(
                f"Missing source run ID for {case_id}"
            )
        labels = {
            "service": row["ground_truth_service"],
            "fault_type": row["chaos_type"],
        }
        if labels["fault_type"] not in contract["possible_fault_types"]:
            raise campaign_knowledge.EvaluationError(
                f"Unknown ground-truth fault type in {case_id}"
            )
        oracle_pair = None
        if retrieval_policy == "oracle_exact_pair_in_top3":
            exact_pairs = [
                pair
                for pair in base["pairs"]
                if pair.get("labels", {}).get("service") == labels["service"]
                and pair.get("labels", {}).get("fault_type") == labels["fault_type"]
            ]
            if len(exact_pairs) > 1:
                raise campaign_knowledge.EvaluationError(
                    f"Ambiguous exact training pair for {case_id}"
                )
            if not exact_pairs:
                excluded_cases.append(
                    {"case_id": case_id, "reason": "no_matching_training_pair"}
                )
                continue
            oracle_pair = exact_pairs[0]
            ranked = retrieve_pairs(
                a,
                base,
                source_run_id,
                top_k=3,
                min_score=0.05,
                exploratory=True,
                include_unvalidated_experimental=True,
            )
            ranked_by_id = {pair["pair_id"]: pair for pair in ranked}
            retrieved = [
                {
                    **ranked_by_id.get(oracle_pair["pair_id"], oracle_pair),
                    "oracle_forced": True,
                }
            ]
            retrieved.extend(
                pair
                for pair in ranked
                if pair["pair_id"] != oracle_pair["pair_id"]
            )
            retrieved = retrieved[:3]
        else:
            if retrieval_backend == "opensearch":
                query_text = a.retrieval_text(version=a_text_version)
                retrieved = opensearch_index.search(
                    query_text,
                    provider.embed(query_text),
                    source_run_id=source_run_id,
                    top_k=3,
                    min_score=0.05,
                    include_unvalidated_experimental=(
                        include_unvalidated_experimental
                    ),
                )
            else:
                retrieved = retrieve_pairs(
                    a,
                    base,
                    source_run_id,
                    top_k=3,
                    min_score=0.05,
                    exploratory=True,
                    include_unvalidated_experimental=(
                        include_unvalidated_experimental
                    ),
                )
        history = []
        history_chars = 0
        for pair in retrieved:
            compacted = (
                pair.get("history")
                if retrieval_backend == "opensearch"
                else compact_exploratory_history(pair)
            )
            if not isinstance(compacted, dict) or compacted.get("pair_id") != pair.get(
                "pair_id"
            ):
                raise campaign_knowledge.EvaluationError(
                    "Retrieved pair has no valid compact history"
                )
            size = len(campaign_knowledge.canonical(compacted))
            if history_chars + size <= HISTORY_CHAR_BUDGET:
                history.append(compacted)
                history_chars += size
        if oracle_pair and (
            not history or history[0]["pair_id"] != oracle_pair["pair_id"]
        ):
            raise campaign_knowledge.EvaluationError(
                f"Exact training pair exceeded the history budget for {case_id}"
            )
        cases.append(
            {
                "case_id": case_id,
                "source_run_id": source_run_id,
                "source_response_sha256": digest(response),
                "incident": incident,
                "evidence": evidence,
                "retrieved": retrieved,
                "history": history,
                "history_characters": history_chars,
                "labels": labels,
                "oracle_pair_id": oracle_pair["pair_id"] if oracle_pair else None,
                "oracle_pair_effect_validated": (
                    bool(oracle_pair["effect_validated"]) if oracle_pair else None
                ),
            }
        )
    return cases, excluded_cases


def _version_base(
    base: dict[str, Any],
    campaign_dir: Path,
    contract: dict[str, Any],
    a_text_version: str,
) -> dict[str, Any]:
    if a_text_version == base_a_text_version(base):
        return base
    if a_text_version != A_TEXT_VERSION:
        raise campaign_knowledge.EvaluationError(
            f"Unsupported incident A text version: {a_text_version}"
        )
    provider = get_embedding_provider()
    versioned = copy.deepcopy(base)
    versioned["a_text_version"] = a_text_version
    for pair in versioned["pairs"]:
        cell_id = pair.get("source_cell_id")
        if not isinstance(cell_id, str) or not cell_id:
            raise campaign_knowledge.EvaluationError(
                "Training pair has no source cell for A v2 reindexing"
            )
        path = campaign_dir / "rag_results" / cell_id / "annotate.json"
        response = _read_response(path)
        if response.get("source_run_id") != pair.get("source_run_id"):
            raise campaign_knowledge.EvaluationError(
                f"Training evidence source mismatch for {cell_id}"
            )
        evidence = (response.get("initial_result") or {}).get("evidence")
        if not isinstance(evidence, list):
            raise campaign_knowledge.EvaluationError(
                f"Initial training evidence is missing for {cell_id}"
            )
        a = enrich_incident_a(
            IncidentA.model_validate(pair["a"]),
            evidence,
            set(contract["known_services"]),
        )
        pair["a"] = a.model_dump()
        versioned["index"][pair["pair_id"]] = provider.embed(
            a.retrieval_text(version=a_text_version)
        ).tolist()
    payload = {key: value for key, value in versioned.items() if key != "sha256"}
    versioned["sha256"] = digest(payload)
    base_a_text_version(versioned)
    return versioned


def _contract(card: dict[str, Any]) -> dict[str, Any]:
    services = card.get("services")
    categories = card.get("allowed_fault_categories")
    if not isinstance(services, dict) or not services:
        raise campaign_knowledge.EvaluationError("System card has no services")
    if not isinstance(categories, list) or not categories:
        raise campaign_knowledge.EvaluationError(
            "System card has no allowed fault categories"
        )
    return {
        "known_services": sorted(services),
        "possible_fault_types": list(FAULT_TYPES),
        "allowed_categories": sorted(set(categories) | {"unknown"}),
    }


def _input_manifest(
    campaign_id: str,
    evaluation: dict[str, Any],
    retry_manifest: dict[str, Any],
    base: dict[str, Any],
    cases: list[dict[str, Any]],
    skill: str,
    card: dict[str, Any],
    settings: dict[str, Any],
    a_text_version: str,
    retrieval_policy: str,
    excluded_cases: list[dict[str, str]],
    retrieval_backend: str = "local",
    include_unvalidated_experimental: bool = False,
    opensearch_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    retriever_version = (
        OPENSEARCH_RETRIEVER_VERSION
        if retrieval_backend == "opensearch"
        else EXPLORATORY_RETRIEVER_VERSION
    )
    replay_version = (
        "frozen-rca-replay-v7"
        if retrieval_backend == "opensearch"
        else (
            "frozen-rca-replay-v6"
            if retrieval_policy == "oracle_exact_pair_in_top3"
            else "frozen-rca-replay-v5"
        )
    )
    manifest = {
        "schema_version": 1,
        "replay_version": replay_version,
        "evidence_projection_version": PROJECTION_VERSION,
        "campaign_id": campaign_id,
        "source_evaluation_sha256": digest(evaluation),
        "source_retry_manifest_sha256": digest(retry_manifest),
        "base_sha256": base["sha256"],
        "retriever_version": retriever_version,
        "a_text_version": a_text_version,
        "retrieval_policy": retrieval_policy,
        "oracle_selection_uses_ground_truth": (
            retrieval_policy == "oracle_exact_pair_in_top3"
        ),
        "excluded_cases": excluded_cases,
        "evaluation_skill_sha256": digest(skill),
        "system_card_sha256": digest(card),
        "model_settings": settings,
        "conditions": list(CONDITIONS),
        "history_char_budget": HISTORY_CHAR_BUDGET,
        "cases": [
            {
                "case_id": case["case_id"],
                "source_run_id": case["source_run_id"],
                "source_response_sha256": case["source_response_sha256"],
                "incident_sha256": digest(case["incident"]),
                "evidence_sha256": digest(case["evidence"]),
                "retrieved_pair_ids": [pair["pair_id"] for pair in case["retrieved"]],
                "provided_pair_ids": [pair["pair_id"] for pair in case["history"]],
                "retrieved_pairs_effect_validated": [
                    bool(pair["effect_validated"]) for pair in case["retrieved"]
                ],
                "oracle_pair_id": case["oracle_pair_id"],
                "oracle_pair_effect_validated": case[
                    "oracle_pair_effect_validated"
                ],
                "history_characters": case["history_characters"],
            }
            for case in cases
        ],
        "limitations": [
            f"{len(evaluation['per_run'])} completed cases were already inspected; this is exploratory replay, not independent validation.",
            "The same frozen incident package is compared across conditions; investigation order and live telemetry are not tested.",
            "Matching the injected fault label is classification accuracy, not causal confirmation.",
            *(
                [
                    "Oracle exact-pair selection uses evaluation labels and is a non-deployable upper bound, not retrieval performance.",
                    "Unvalidated training pairs remain marked unvalidated; they are not promoted to causal knowledge.",
                    f"{len(excluded_cases)} cases without a corresponding training pair are excluded from every condition.",
                ]
                if retrieval_policy == "oracle_exact_pair_in_top3"
                else []
            ),
            *(
                [
                    "OpenSearch hybrid retrieval fuses BM25 and k-NN rankings "
                    "with reciprocal-rank fusion.",
                    "The local OpenSearch index contains compact guidance and "
                    "embeddings, not evaluation labels or historical diagnoses.",
                    "Unvalidated training pairs remain marked unvalidated; they "
                    "are not promoted to causal knowledge.",
                ]
                if retrieval_backend == "opensearch"
                else []
            ),
            *(
                [
                    "Experimental retrieval explicitly includes pairs with "
                    "effect_validated=false; their validation state remains "
                    "unchanged."
                ]
                if include_unvalidated_experimental
                else []
            ),
            "No operational telemetry APIs, application/observability cluster access, or fault injections are used by this runner.",
        ],
    }
    if retrieval_backend != "local" or include_unvalidated_experimental:
        manifest["retrieval_backend"] = retrieval_backend
        manifest["include_unvalidated_experimental"] = (
            include_unvalidated_experimental
        )
    if opensearch_metadata is not None:
        manifest["opensearch_index"] = opensearch_metadata
    return manifest


def _existing_or_write(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    sealed = campaign_knowledge.seal(value)
    if path.exists():
        existing = campaign_knowledge.load_sealed(path)
        if existing != sealed:
            raise campaign_knowledge.EvaluationError(
                f"Existing artifact differs: {path}"
            )
        return existing
    campaign_knowledge.write_new(path, sealed)
    return sealed


def _score_case(
    case: dict[str, Any],
    condition: str,
    skill: str,
    contract: dict[str, Any],
    application_context: str,
    call: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]],
    output: Path,
) -> dict[str, Any]:
    history = case["history"] if condition == "rag" else []
    messages = build_messages(
        skill,
        contract,
        case["incident"],
        application_context,
        condition,
        history,
    )
    fingerprint = digest(
        {
            "messages": messages,
            "base_sha256": case.get("base_sha256"),
            "model_settings": case["model_settings"],
        }
    )
    path = output / "attempts" / f"{case['case_id']}-{condition}.json"
    if path.exists():
        saved = campaign_knowledge.load_sealed(path)
        if saved.get("input_sha256") != fingerprint:
            raise campaign_knowledge.EvaluationError(
                "Existing attempt has different frozen inputs"
            )
        return saved

    parsed, raw_response = campaign_knowledge.model_json(
        call,
        messages,
        Diagnosis,
        evidence=case["evidence"],
        contract=contract,
        audit_prefix=(
            output / "private/contract_attempts" / f"{case['case_id']}-{condition}"
        ),
    )
    if not isinstance(parsed, Diagnosis):
        raise campaign_knowledge.EvaluationError(
            "Model returned an incompatible diagnosis type"
        )
    known = set(contract["known_services"])
    validated = validate_diagnosis(
        parsed,
        case["evidence"],
        known,
        set(contract["allowed_categories"]),
        campaign_knowledge.restarted_services({"evidence": case["evidence"]}, known),
        list(contract["possible_fault_types"]),
    )
    labels = case["labels"]
    service_correct = validated.primary_service == labels["service"]
    fault_correct = validated.fault_type == labels["fault_type"]
    result = {
        "case_id": case["case_id"],
        "condition": condition,
        "input_sha256": fingerprint,
        "incident_sha256": digest(case["incident"]),
        "retrieved_pair_ids": [pair["pair_id"] for pair in case["retrieved"]],
        "provided_pair_ids": [pair["pair_id"] for pair in case["history"]],
        "diagnosis": validated.model_dump(),
        "service_correct": service_correct,
        "fault_type_correct": fault_correct,
        "joint_correct": service_correct and fault_correct,
        "response": raw_response,
        "messages": messages,
    }
    campaign_knowledge.write_new(path, campaign_knowledge.seal(result))
    return result


def _summarize(
    results: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    base: dict[str, Any],
    input_sha256: str,
    retrieval_policy: str,
    excluded_cases: list[dict[str, str]],
    retrieval_backend: str = "local",
    include_unvalidated_experimental: bool = False,
    opensearch_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    labels = {case["case_id"]: case["labels"] for case in cases}
    pairs_by_id = {pair["pair_id"]: pair for pair in base["pairs"]}
    summaries = {}
    for condition in CONDITIONS:
        rows = [row for row in results if row["condition"] == condition]
        summaries[condition] = {
            "cases": len(rows),
            "service_correct": sum(row["service_correct"] for row in rows),
            "fault_type_correct": sum(row["fault_type_correct"] for row in rows),
            "joint_correct": sum(row["joint_correct"] for row in rows),
            "confirmed": sum(row["diagnosis"]["status"] == "confirmed" for row in rows),
            "errors": 0,
        }
    per_case = []
    for case in cases:
        matching = {
            row["condition"]: row
            for row in results
            if row["case_id"] == case["case_id"]
        }
        per_case.append(
            {
                "case_id": case["case_id"],
                "ground_truth_service": labels[case["case_id"]]["service"],
                "ground_truth_fault": labels[case["case_id"]]["fault_type"],
                "retrieved_pair_ids": [pair["pair_id"] for pair in case["retrieved"]],
                "oracle_pair_id": case["oracle_pair_id"],
                "oracle_pair_effect_validated": case[
                    "oracle_pair_effect_validated"
                ],
                "retrieval_exact_at_3": any(
                    pair_id in pairs_by_id
                    and pairs_by_id[pair_id]["labels"]["service"]
                    == labels[case["case_id"]]["service"]
                    and pairs_by_id[pair_id]["labels"]["fault_type"]
                    == labels[case["case_id"]]["fault_type"]
                    for pair_id in (
                        pair["pair_id"] for pair in case["retrieved"]
                    )
                ),
                "conditions": {
                    condition: {
                        "status": row["diagnosis"]["status"],
                        "service": row["diagnosis"]["primary_service"],
                        "fault_type": row["diagnosis"]["fault_type"],
                        "service_correct": row["service_correct"],
                        "fault_type_correct": row["fault_type_correct"],
                        "joint_correct": row["joint_correct"],
                    }
                    for condition, row in matching.items()
                },
            }
        )
    cases_with_pair = [
        case
        for case in cases
        if any(
            pair["labels"]["service"] == case["labels"]["service"]
            and pair["labels"]["fault_type"] == case["labels"]["fault_type"]
            for pair in base["pairs"]
        )
    ]
    exact_retrieved_count = sum(
        row["retrieval_exact_at_3"] for row in per_case
    )
    report = {
        "input_manifest_sha256": input_sha256,
        "base_sha256": base["sha256"],
        "retriever_version": (
            OPENSEARCH_RETRIEVER_VERSION
            if retrieval_backend == "opensearch"
            else EXPLORATORY_RETRIEVER_VERSION
        ),
        "retrieval_policy": retrieval_policy,
        "oracle_selection_uses_ground_truth": (
            retrieval_policy == "oracle_exact_pair_in_top3"
        ),
        "excluded_cases": excluded_cases,
        "case_count": len(cases),
        "retrieval_summary": {
            "cases_with_corresponding_pair": len(cases_with_pair),
            "cases_without_corresponding_pair": len(cases) - len(cases_with_pair),
            "exact_pairs_retrieved_at_3": exact_retrieved_count,
            "exact_pair_recall_at_3": (
                round(exact_retrieved_count / len(cases_with_pair), 4)
                if cases_with_pair
                else None
            ),
            "forced_pairs_with_unvalidated_effect": sum(
                case["oracle_pair_effect_validated"] is False for case in cases
            ),
            "unvalidated_pairs_in_top3": sum(
                not pair["effect_validated"]
                for case in cases
                for pair in case["retrieved"]
            ),
            "cases_with_unvalidated_pair_in_top3": sum(
                any(not pair["effect_validated"] for pair in case["retrieved"])
                for case in cases
            ),
        },
        "conditions": summaries,
        "per_case": per_case,
        "paired_joint_deltas": {
            "control_minus_skill": sum(
                matching["control"]["joint_correct"]
                - matching["skill"]["joint_correct"]
                for matching in (
                    {
                        row["condition"]: row
                        for row in results
                        if row["case_id"] == case["case_id"]
                    }
                    for case in cases
                )
            ),
            "rag_minus_control": sum(
                matching["rag"]["joint_correct"] - matching["control"]["joint_correct"]
                for matching in (
                    {
                        row["condition"]: row
                        for row in results
                        if row["case_id"] == case["case_id"]
                    }
                    for case in cases
                )
            ),
            "rag_minus_skill": sum(
                matching["rag"]["joint_correct"]
                - matching["skill"]["joint_correct"]
                for matching in (
                    {
                        row["condition"]: row
                        for row in results
                        if row["case_id"] == case["case_id"]
                    }
                    for case in cases
                )
            ),
        },
        "rag_vs_skill_joint": {
            "rag_wins": sum(
                next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "rag"
                )
                and not next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "skill"
                )
                for case in cases
            ),
            "skill_wins": sum(
                next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "skill"
                )
                and not next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "rag"
                )
                for case in cases
            ),
            "ties": sum(
                next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "rag"
                )
                == next(
                    row["joint_correct"]
                    for row in results
                    if row["case_id"] == case["case_id"]
                    and row["condition"] == "skill"
                )
                for case in cases
            ),
        },
        "limitations": [
            f"Replay is paired on frozen inputs and exploratory only; {len(cases)} cases were previously inspected.",
            "Correct service/fault labels do not establish causal mechanism; review diagnosis evidence and restart gates separately.",
            "Skill-only omits the system card; control adds the card; RAG adds the card and historical guidance.",
            *(
                [
                    "Oracle RAG forces the exact training pair into the top-3 using evaluation labels; this is an information upper bound, not a deployable retrieval result.",
                    "Training pairs with effect_validated=false are explicitly included and remain unvalidated.",
                    f"{len(excluded_cases)} cases without an exact training pair are excluded from all conditions.",
                ]
                if retrieval_policy == "oracle_exact_pair_in_top3"
                else []
            ),
            *(
                [
                    "OpenSearch hybrid retrieval fuses BM25 and k-NN rankings "
                    "with reciprocal-rank fusion.",
                    "Experimental retrieval explicitly includes "
                    "effect_validated=false pairs, which remain marked "
                    "unvalidated.",
                ]
                if retrieval_backend == "opensearch"
                and include_unvalidated_experimental
                else []
            ),
            "No investigation tools, operational telemetry queries, "
            "application/observability cluster access, or fault injections "
            "were run.",
        ],
    }
    if retrieval_backend != "local" or include_unvalidated_experimental:
        report["retrieval_backend"] = retrieval_backend
        report["include_unvalidated_experimental"] = (
            include_unvalidated_experimental
        )
    if opensearch_metadata is not None:
        report["opensearch_index"] = opensearch_metadata
    return report


def run_replay(
    *,
    campaign_dir: Path,
    retry_dir: Path,
    object_store: Path,
    output: Path,
    call: Any,
    settings: dict[str, Any],
    a_text_version: str = LEGACY_A_TEXT_VERSION,
    retrieval_policy: str = "hybrid",
    retrieval_backend: str = "local",
    opensearch_url: str = DEFAULT_OPENSEARCH_URL,
    include_unvalidated_experimental: bool = False,
) -> dict[str, Any]:
    raise campaign_knowledge.EvaluationError(
        "Frozen-evidence RCA replay is disabled. Start a new campaign_knowledge "
        "evaluation and collect historical telemetry online through MCP."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_known_args(argv)
    print(
        "frozen_rca_replay is disabled; collect historical telemetry through MCP "
        "with campaign_knowledge instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
