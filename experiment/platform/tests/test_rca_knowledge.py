import copy
import json

import pytest

from experiment.platform.backend import rca_knowledge as knowledge
from experiment.platform.tests.test_rca_skill import PHASES, FakeMCP, diagnosis


def document(ref="E001"):
    observation = {
        "description": "Latencia elevada, memoria estavel, timeout na dependencia",
        "evidence_ids": [ref],
    }
    return {
        "observed_services": ["front-end"],
        "service_relations": [],
        "symptoms": [observation],
        "metric_changes": [observation],
        "log_patterns": [],
        "trace_patterns": [],
        "temporal_sequence": [],
        "preliminary_hypotheses": ["Rede ou CPU"],
        "limitations": [],
    }


class Model:
    def __init__(
        self,
        *,
        query=False,
        unsupported=False,
        invalid_history=False,
        repeat_check=False,
        missing_query_check=False,
    ):
        self.payloads = []
        self.query = query
        self.unsupported = unsupported
        self.invalid_history = invalid_history
        self.repeat_check = repeat_check
        self.missing_query_check = missing_query_check

    def __call__(self, messages, tools, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(copy.deepcopy(payload))
        stage = payload["stage"]
        context = payload.get("context", {})
        items = payload.get("evidence", context.get("evidence", []))
        ids = {}
        for item in items:
            if item["status"] == "ok" and item["fault_observation_count"] > 0:
                ids[item["family"]] = item["id"]
        value = diagnosis(
            fault_type="network-latency",
            supporting_evidence=[
                ids[family] for family in ("metrics", "logs") if family in ids
            ],
        )
        if self.unsupported:
            value["supporting_evidence"] = ["E999"]
        if stage == "plan":
            value = {
                "candidates": ["front-end"],
                "hypotheses": ["Rede", "CPU"],
                "reasoning": "Comparar sinais",
            }
        elif stage == "build_a":
            value = document()
        elif stage in {"annotation_check", "diagnostic_check"}:
            value = {
                "action": "stop",
                "reason": "Evidencias suficientes ou fonte indisponivel",
                "check": None,
            }
            if self.missing_query_check and context["round"] == 0:
                value = {
                    "action": "query",
                    "reason": "Verificar timeout",
                    "check": None,
                }
            elif self.query and (
                context["round"] == 0 or (self.repeat_check and context["round"] == 1)
            ):
                value = {
                    "action": "query",
                    "reason": "Verificar timeout",
                    "check": {
                        "kind": "logs",
                        "service": "front-end",
                        "phase": "fault",
                        "hypothesis": "Espera por dependencia",
                        "expected_support": "Timeout na janela fault",
                        "expected_refutation": "Erro local anterior",
                        "historical_pair_ids": (
                            ["missing-pair"]
                            if self.invalid_history
                            else [pair["pair_id"] for pair in context["history"]]
                        ),
                        "patterns": ["timeout"],
                    },
                }
        elif stage == "build_b":
            value = {
                "diagnosis": value,
                "symptom_relation": "Timeout explica a espera observada",
                "investigation_steps": ["Comparar referencia e falha"],
                "applicability": ["Mesma operacao e carga comparavel"],
                "recommended_checks": ["Consultar eventos se houver restart"],
                "limitations": [],
            }
        return {"content": json.dumps(value), "usage": {"total_tokens": 10}}


def investigate(
    tmp_path,
    *,
    mode="annotate",
    model=None,
    client=None,
    run_id="train",
    effect=True,
    **options,
):
    return knowledge.investigate(
        client=client or FakeMCP(),
        phases=PHASES,
        namespace="sock-shop",
        window_metadata={},
        options=knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode=mode,
            possible_fault_types=["network-latency"],
            **options,
        ),
        run_id=run_id,
        store=knowledge.KnowledgeStore(tmp_path, "corpus"),
        llm=model or Model(),
        labels=(
            {"service": "front-end", "fault_type": "network-latency"}
            if mode == "annotate"
            else None
        ),
        effect_validated=effect,
    )


def test_model_builds_a_blind_and_b_guided_with_actual_queries(tmp_path):
    model = Model(query=True)
    result = investigate(tmp_path, model=model)
    assert result["status"] == "confirmed", result
    pair = result["pair"]
    assert pair["guide_status"] == "validated"
    assert pair["causal_status"] == "supported"
    build_a = next(payload for payload in model.payloads if payload["stage"] == "build_a")
    evidence_ids_by_family = build_a["context"]["evidence_ids_by_family"]
    for family in ("metrics", "logs", "traces"):
        expected = sorted(
            item["id"]
            for item in build_a["context"]["evidence"]
            if item["family"] == family
            and item["status"] in {"ok", "partial"}
            and item["observation_count"] > 0
        )
        assert evidence_ids_by_family[family] == expected
    assert result["additional_checks"][0]["evidence_id"] in {
        item["id"] for item in result["evidence"]
    }
    assert '|~ "timeout"' in result["calls"][-1]["arguments"]["query"]
    for payload in model.payloads:
        if payload["stage"] in {"plan", "diagnosis", "build_a"}:
            assert "training_annotation_labels" not in json.dumps(payload)
        else:
            assert payload["context"]["training_annotation_labels"] == pair["labels"]
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    initial = store.read_record("train", "initial")
    assert initial["a"] == pair["a"]
    assert len(initial["skill"]["evidence"]) < len(pair["evidence"])
    assert result["model_calls"] == len(model.payloads)
    assert result["total_tokens"] == len(model.payloads) * 10
    final_payload = next(
        payload for payload in reversed(model.payloads) if payload["stage"] == "build_b"
    )
    check_evidence_id = result["additional_checks"][0]["evidence_id"]
    assert check_evidence_id in {
        item["id"] for item in final_payload["context"]["evidence"]
    }
    for payload in model.payloads:
        if payload["stage"] in {"annotation_check", "build_b"}:
            assert (
                len(knowledge.canonical(payload["context"]["evidence"]))
                <= knowledge.MODEL_EVIDENCE_CONTEXT_CHARS
            )
    assert all("digest_summary" not in item for item in result["evidence"])


def test_incident_a_family_error_identifies_valid_trace_ids():
    candidate = document()
    candidate["trace_patterns"] = [
        {"description": "Trace relacionado", "evidence_ids": ["E001"]}
    ]
    incident = knowledge.IncidentA.model_validate(candidate)
    evidence = [
        {
            "id": "E001",
            "family": "metrics",
            "status": "ok",
            "observation_count": 1,
        },
        {
            "id": "E002",
            "family": "traces",
            "status": "ok",
            "observation_count": 1,
        },
    ]

    with pytest.raises(ValueError, match="use only traces evidence IDs: E002"):
        knowledge.validate_a(incident, evidence, {"front-end"})


def test_model_evidence_context_is_bounded_and_keeps_all_evidence_ids():
    evidence = [
        {
            "id": f"E{index:03d}",
            "tool": "prometheus_range_query",
            "family": "metrics",
            "source": "live",
            "status": "ok",
            "observation_count": 8,
            "fault_observation_count": 4,
            "arguments": {"query": 'metric{service="front-end"}' * 100},
            "digest": {"observations": ["latency increased"] * 400},
        }
        for index in range(1, 36)
    ]

    compacted = knowledge.compact_evidence_for_model(evidence)

    assert len(knowledge.canonical(compacted)) <= knowledge.MODEL_EVIDENCE_CONTEXT_CHARS
    assert [item["id"] for item in compacted] == [item["id"] for item in evidence]
    assert all(
        item["digest_summary"]["projection_omitted_observations_count"] > 0
        for item in compacted
    )
    assert all(
        "digest_summary" not in item and "observations" in item["digest"]
        for item in evidence
    )


def test_model_session_drops_prior_stage_payloads_before_budgeting():
    evidence = [
        {
            "id": f"E{index:03d}",
            "tool": "prometheus_range_query",
            "family": "metrics",
            "source": "live",
            "status": "ok",
            "observation_count": 8,
            "fault_observation_count": 4,
            "arguments": {"query": 'metric{service="orders"}' * 100},
            "digest": {"observations": ["cpu saturation increased"] * 400},
        }
        for index in range(1, 36)
    ]
    messages = [
        {"role": "system", "content": "Skill instructions"},
        {"role": "user", "content": '{"namespace":"sock-shop"}'},
        {"role": "user", "content": "Evidence contract reference"},
        {
            "role": "user",
            "content": knowledge.canonical(
                {
                    "stage": "plan",
                    "context": {"evidence": evidence},
                    "evidence": evidence,
                }
            ),
        },
        {"role": "assistant", "content": '{"candidates":["orders"]}'},
        {"role": "user", "content": "Causal checklist reference"},
        {
            "role": "user",
            "content": knowledge.canonical(
                {
                    "stage": "diagnosis",
                    "context": {"plan": ["orders"], "evidence": evidence},
                    "evidence": evidence,
                }
            ),
        },
        {"role": "user", "content": "Resposta invalida: corrija o JSON"},
    ]
    original_messages = copy.deepcopy(messages)
    captured = []
    session = knowledge.ModelSession(
        lambda compacted, tools, **kwargs: (
            captured.extend(compacted)
            or {"content": "{}", "usage": {"total_tokens": 1}}
        ),
        knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode="annotate",
            possible_fault_types=["cpu-hog"],
        ),
    )

    session.invoke(messages, [])

    stage_payloads = [
        json.loads(message["content"])
        for message in captured
        if knowledge._is_model_stage_message(message)
    ]
    assert [payload["stage"] for payload in stage_payloads] == ["diagnosis"]
    assert len(knowledge.canonical(stage_payloads[0]["evidence"])) <= (
        knowledge.MODEL_EVIDENCE_CONTEXT_CHARS
    )
    assert (
        len(knowledge.canonical(stage_payloads[0]["context"]["evidence"]))
        <= knowledge.MODEL_EVIDENCE_CONTEXT_CHARS
    )
    assert [message["content"] for message in captured if message["role"] == "user"][
        :3
    ] == [
        '{"namespace":"sock-shop"}',
        "Evidence contract reference",
        "Causal checklist reference",
    ]
    assert captured[-1]["content"] == "Resposta invalida: corrija o JSON"
    assert messages == original_messages


def test_model_generation_repairs_invalid_json_and_evidence_references():
    responses = iter(
        [
            {"content": "", "usage": {"total_tokens": 1}},
            {"content": '{"observed_services": [}', "usage": {"total_tokens": 1}},
            {"content": json.dumps(document("E999")), "usage": {"total_tokens": 1}},
            {"content": json.dumps(document()), "usage": {"total_tokens": 1}},
        ]
    )
    model_options = []

    def call_model(messages, tools, **kwargs):
        model_options.append(kwargs)
        return next(responses)

    session = knowledge.ModelSession(
        call_model,
        knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode="annotate",
            possible_fault_types=["network-latency"],
        ),
    )

    evidence = [
        {
            "id": "E001",
            "family": "metrics",
            "status": "ok",
            "observation_count": 1,
        }
    ]
    generated = session.generate(
        "build_a",
        knowledge.IncidentA,
        {"evidence": evidence},
        validator=lambda value: knowledge.validate_a(
            knowledge.IncidentA.model_validate(value),
            evidence,
            {"front-end"},
        ),
        allowed_evidence_ids=["E001"],
    )

    assert generated["observed_services"] == ["front-end"]
    observation_schema = model_options[0]["response_format"]["json_schema"]["schema"][
        "$defs"
    ]["Observation"]
    assert observation_schema["properties"]["evidence_ids"]["items"]["enum"] == ["E001"]
    assert len(session.attempts) == 4
    assert [attempt["stage"] for attempt in session.attempts] == [
        "build_a",
        "build_a",
        "build_a",
        "build_a",
    ]
    assert [attempt["status"] for attempt in session.attempts] == [
        "invalid_response",
        "invalid_response",
        "invalid_response",
        "returned",
    ]
    assert "E999" in session.attempts[2]["error"]


def test_nested_guide_fault_type_is_constrained_and_repaired():
    valid_guide = {
        "diagnosis": diagnosis(fault_type="http-status-code"),
        "symptom_relation": "Erros de resposta acompanham a indisponibilidade observada.",
        "investigation_steps": ["Comparar a taxa de erros entre baseline e fault."],
        "applicability": ["Falhas HTTP em microservicos."],
        "recommended_checks": ["Consultar traces da chamada atual."],
        "limitations": ["A amostragem de traces pode ser parcial."],
    }
    responses = iter(
        [
            {
                "content": json.dumps(
                    {
                        **valid_guide,
                        "diagnosis": diagnosis(fault_type="network-latency"),
                    }
                ),
                "usage": {"total_tokens": 1},
            },
            {"content": json.dumps(valid_guide), "usage": {"total_tokens": 1}},
        ]
    )
    payloads = []
    model_options = []

    def call_model(messages, tools, **kwargs):
        model_options.append(kwargs)
        payloads.append(
            next(
                json.loads(message["content"])
                for message in reversed(messages)
                if knowledge._is_model_stage_message(message)
            )
        )
        return next(responses)

    def validate_fault_type(value):
        if value["diagnosis"]["fault_type"] != "http-status-code":
            raise ValueError("fault_type must be http-status-code")

    session = knowledge.ModelSession(
        call_model,
        knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode="annotate",
            possible_fault_types=["http-status-code", "network-latency"],
        ),
    )

    generated = session.generate(
        "build_b",
        knowledge.DiagnosticGuide,
        {},
        validator=validate_fault_type,
        allowed_fault_types=["http-status-code"],
        allowed_evidence_ids=["E001"],
    )

    nested_schema = payloads[0]["output_schema"]["$defs"]["Diagnosis"]
    assert nested_schema["properties"]["fault_type"]["enum"] == ["http-status-code"]
    assert "fault_type" in nested_schema["required"]
    assert generated["diagnosis"]["fault_type"] == "http-status-code"
    assert model_options[0]["response_format"]["type"] == "json_schema"
    assert (
        model_options[0]["response_format"]["json_schema"]["name"] == "DiagnosticGuide"
    )
    assert model_options[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert model_options[0]["response_format"]["json_schema"]["schema"]["$defs"][
        "Diagnosis"
    ]["properties"]["fault_type"]["enum"] == ["http-status-code"]
    assert model_options[0]["response_format"]["json_schema"]["schema"]["$defs"][
        "Diagnosis"
    ]["properties"]["supporting_evidence"]["items"]["enum"] == ["E001"]
    assert [attempt["status"] for attempt in session.attempts] == [
        "invalid_response",
        "returned",
    ]


@pytest.mark.parametrize("unsupported,effect", [(True, True), (False, False)])
def test_label_does_not_validate_a_guide_without_support(tmp_path, unsupported, effect):
    result = investigate(tmp_path, model=Model(unsupported=unsupported), effect=effect)
    assert result["pair"]["guide_status"] == "pending"
    assert result["pair"]["causal_status"] == "inconclusive"
    with pytest.raises(ValueError, match="empty base"):
        knowledge.KnowledgeStore(tmp_path, "corpus").freeze(["train"])


def test_unretrieved_pair_reference_is_rejected_without_aborting_annotation(tmp_path):
    result = investigate(
        tmp_path,
        model=Model(query=True, invalid_history=True),
    )

    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "invalid_historical_pair_reference"
    assert result["additional_checks"][0]["status"] == "rejected"
    assert (
        "missing-pair" in result["additional_checks"][0]["check"]["historical_pair_ids"]
    )
    assert "evidence_id" not in result["additional_checks"][0]
    assert len(result["evidence"]) == len(result["pair"]["evidence"])
    assert result["pair"]["guide_status"] == "pending"


def test_repeated_check_is_recorded_and_marks_diagnosis_inconclusive(tmp_path):
    result = investigate(
        tmp_path,
        model=Model(query=True, repeat_check=True),
    )

    assert result["status"] == "inconclusive"
    assert result["stop_reason"] == "reserved_final_budget"
    assert result["unresolved_check_rejection"] == "repeated_check"
    assert [item["status"] for item in result["additional_checks"]] == [
        "ok",
        "rejected",
    ]
    assert (
        result["additional_checks"][1]["reason"]
        == "check repeats an executed verification"
    )
    assert any(
        "Aprofundamento nao concluido" in item
        for item in result["diagnosis"]["limitations"]
    )
    assert result["pair"]["guide_status"] == "pending"


def test_query_without_check_is_rejected_and_corrected(tmp_path):
    model = Model(missing_query_check=True)
    result = investigate(tmp_path, model=model)

    assert result["status"] == "inconclusive"
    assert "error" not in result
    assert result["stop_reason"] == "missing_query_check"
    assert result["additional_checks"] == [
        {
            "check": None,
            "status": "rejected",
            "reason": "query decision omitted a check",
        }
    ]
    check_requests = [
        payload for payload in model.payloads
        if payload["stage"] == "annotation_check"
    ]
    assert len(check_requests) == 2
    assert check_requests[-1]["context"]["proposal_corrections_remaining"] == 0


def test_freeze_retrieve_pairs_and_keep_control_blind(tmp_path):
    train = investigate(tmp_path)
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    base = store.freeze(["train"])
    assert store.freeze(["train"]) == base
    assert store.load_base() == base
    model = Model(query=True)
    result = investigate(tmp_path, mode="rag", model=model, run_id="test")
    assert result["status"] == "confirmed", result
    assert result["base_sha256"] == base["sha256"]
    assert result["retrieved"][0]["pair_id"] == train["pair"]["pair_id"]
    assert result["provided_pair_ids"] == [train["pair"]["pair_id"]]
    assert "evidence" not in result["retrieved"][0]
    assert all(
        "training_annotation_labels" not in json.dumps(payload)
        for payload in model.payloads
    )
    control = Model()
    result = investigate(tmp_path, mode="control", model=control, run_id="test")
    assert result["status"] == "confirmed"
    assert result["retrieved"] == []
    assert all(
        not payload.get("context", {}).get("history") for payload in control.payloads
    )
    assert not store.record_path("test", "pair").exists()
    with pytest.raises(ValueError, match="frozen"):
        investigate(tmp_path)


def test_exploratory_rag_retrieves_inconclusive_pairs_without_promoting_them(
    tmp_path, monkeypatch
):
    import numpy as np

    train = investigate(tmp_path)
    pair = train["pair"]
    pair["guide_status"] = "pending"
    pair["causal_status"] = "inconclusive"
    pair["review_status"] = "assistant_reviewed_boundary_only"
    pair["b"]["diagnosis"]["status"] = "inconclusive"
    pair["labels"]["causal_category"] = pair["b"]["diagnosis"]["category"]
    pair["sha256"] = knowledge.digest(
        {key: value for key, value in pair.items() if key != "sha256"}
    )
    unvalidated = copy.deepcopy(pair)
    unvalidated["pair_id"] = knowledge.digest(["unvalidated", pair["pair_id"]])
    unvalidated["source_run_id"] = "train-unvalidated"
    unvalidated["effect_validated"] = False
    unvalidated["sha256"] = knowledge.digest(
        {key: value for key, value in unvalidated.items() if key != "sha256"}
    )
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    provider = type(
        "Provider",
        (),
        {
            "name": "test-provider",
            "dim": 2,
            "embed": staticmethod(lambda _text: np.asarray([1.0, 0.0])),
        },
    )()
    monkeypatch.setattr(knowledge, "get_embedding_provider", lambda: provider)
    monkeypatch.setattr(knowledge, "cosine_similarity", lambda _left, _right: 0.9)
    monkeypatch.setattr(knowledge, "load_skill", lambda: ("", {}, pair["skill_hash"]))
    payload = {
        "version": knowledge.VERSION,
        "a_text_version": pair["a"]["text_version"],
        "mode": "exploratory",
        "embedding_provider": provider.name,
        "embedding_dim": provider.dim,
        "skill_sha256": pair["skill_hash"],
        "model": knowledge.DEFAULT_LLM_MODEL,
        "training_run_ids": ["train", "train-unvalidated"],
        "pairs": [pair, unvalidated],
        "index": {
            pair["pair_id"]: [1.0, 0.0],
            unvalidated["pair_id"]: [1.0, 0.0],
        },
    }
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "exploratory_base.json").write_text(
        json.dumps({**payload, "sha256": knowledge.digest(payload)}),
        encoding="utf-8",
    )

    model = Model()
    result = investigate(
        tmp_path, mode="exploratory_rag", model=model, run_id="exploratory-test"
    )

    assert result["retrieved"][0]["guide_status"] == "pending"
    assert result["retrieved"][0]["causal_status"] == "inconclusive"
    assert result["retrieved"][0]["review_status"] == "assistant_reviewed_boundary_only"
    assert result["retrieved"][0]["pair_id"] == pair["pair_id"]
    assert result["retrieval"]["candidate_count"] == 2
    assert result["retrieval"]["eligible_candidate_count"] == 1
    assert result["retrieval"]["validated_candidate_count"] == 1
    assert result["retrieval"]["unvalidated_candidate_count"] == 1
    assert result["retrieval"]["included_unvalidated_candidate_count"] == 0
    assert result["retrieval"]["excluded_unvalidated_count"] == 1
    assert result["retrieval"]["eligibility_policy"] == "effect_validated_only"
    assert result["retrieval"]["retriever_version"] == "hybrid-rrf-bm25-v1"
    assert result["retrieval"]["retrieved_pair_count"] == 1
    assert result["retrieval"]["provided_pair_count"] == 1
    assert result["retrieval"]["omitted_pair_count"] == 0
    assert (
        result["retrieval"]["retrieved_history_characters"]
        == result["retrieval"]["provided_history_characters"]
        <= result["retrieval"]["history_char_budget"]
    )
    assert result["status"] == "confirmed"
    model_contexts = [
        payload.get("context", {})
        for payload in model.payloads
        if payload.get("stage") in {"diagnostic_check", "build_b"}
    ]
    assert model_contexts
    for context in model_contexts:
        history = context["history"]
        assert history
        assert all("labels" not in item and "diagnosis" not in item for item in history)
        assert all(item["pair_id"] == pair["pair_id"] for item in history)
    assert result["retrieved"][0]["labels"] == pair["labels"]
    assert any(
        payload.get("context", {}).get("historical_guidance_notice")
        for payload in model.payloads
    )

    expanded_model = Model()
    expanded = investigate(
        tmp_path,
        mode="exploratory_rag",
        model=expanded_model,
        run_id="exploratory-test-expanded",
        include_unvalidated_experimental=True,
    )
    assert {item["pair_id"] for item in expanded["retrieved"]} == {
        pair["pair_id"],
        unvalidated["pair_id"],
    }
    assert expanded["retrieval"]["eligible_candidate_count"] == 2
    assert expanded["retrieval"]["validated_candidate_count"] == 1
    assert expanded["retrieval"]["unvalidated_candidate_count"] == 1
    assert expanded["retrieval"]["included_unvalidated_candidate_count"] == 1
    assert expanded["retrieval"]["excluded_unvalidated_count"] == 0
    assert (
        expanded["retrieval"]["eligibility_policy"]
        == "experimental_include_unvalidated"
    )
    expanded_unvalidated = next(
        item
        for item in expanded["retrieved"]
        if item["pair_id"] == unvalidated["pair_id"]
    )
    assert expanded_unvalidated["effect_validated"] is False
    model_histories = [
        payload.get("context", {}).get("history", [])
        for payload in expanded_model.payloads
        if payload.get("stage") in {"diagnostic_check", "build_b"}
    ]
    unvalidated_history = [
        item
        for history in model_histories
        for item in history
        if item["pair_id"] == unvalidated["pair_id"]
    ]
    assert unvalidated_history
    assert all(item["source_effect_validated"] is False for item in unvalidated_history)
    expanded_contexts = [
        payload.get("context", {})
        for payload in expanded_model.payloads
        if payload.get("stage") in {"diagnostic_check", "build_b"}
    ]
    assert expanded_contexts
    for context in expanded_contexts:
        notice = context["historical_guidance_notice"]
        assert "source_effect_validated" in notice
        assert "false indica efeito nao validado" in notice
        assert "Os exemplos fornecidos tiveram efeito" not in notice
    with pytest.raises(ValueError, match="cannot be frozen as a validated base"):
        store.freeze(["train"])


def test_unvalidated_experimental_inclusion_requires_exploratory_rag():
    with pytest.raises(ValueError, match="requires exploratory_rag mode"):
        knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode="rag",
            possible_fault_types=["network-latency"],
            include_unvalidated_experimental=True,
        )


def test_legacy_exploratory_model_requires_pinned_opensearch_opt_in(
    tmp_path, monkeypatch
):
    pair = investigate(tmp_path)["pair"]
    pair["guide_status"] = "pending"
    pair["causal_status"] = "inconclusive"
    pair["review_status"] = "assistant_reviewed_boundary_only"
    pair["b"]["diagnosis"]["status"] = "inconclusive"
    pair["labels"]["causal_category"] = pair["b"]["diagnosis"]["category"]
    pair["sha256"] = knowledge.digest(
        {key: value for key, value in pair.items() if key != "sha256"}
    )
    provider = type("Provider", (), {"name": "test-provider", "dim": 2})()
    monkeypatch.setattr(knowledge, "get_embedding_provider", lambda: provider)
    monkeypatch.setattr(
        knowledge, "load_skill", lambda: ("", {}, pair["skill_hash"])
    )
    source_model = "qwen-14b"
    current_model = "qwen3.5-9b-grafana"
    payload = {
        "version": knowledge.VERSION,
        "a_text_version": pair["a"]["text_version"],
        "mode": "exploratory",
        "embedding_provider": provider.name,
        "embedding_dim": provider.dim,
        "skill_sha256": pair["skill_hash"],
        "model": source_model,
        "training_run_ids": [pair["source_run_id"]],
        "pairs": [pair],
        "index": {pair["pair_id"]: [1.0, 0.0]},
    }
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "exploratory_base.json").write_text(
        json.dumps({**payload, "sha256": knowledge.digest(payload)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge, "DEFAULT_LLM_MODEL", current_model)

    with pytest.raises(ValueError, match="differs from the current inference model"):
        store.load_exploratory_base(
            expected_skill_sha256=pair["skill_hash"],
            expected_model=source_model,
        )
    loaded = store.load_exploratory_base(
        expected_skill_sha256=pair["skill_hash"],
        expected_model=source_model,
        allow_legacy_model_mismatch=True,
    )
    assert loaded["model"] == source_model

    options = {
        "corpus_id": "corpus",
        "mode": "exploratory_rag",
        "possible_fault_types": ["network-latency"],
        "retrieval_backend": "opensearch",
        "opensearch_base_sha256": "a" * 64,
        "opensearch_source_skill_sha256": pair["skill_hash"],
        "opensearch_source_model": source_model,
        "opensearch_current_model": current_model,
        "retriever_version": knowledge.OPENSEARCH_RETRIEVER_VERSION,
    }
    with pytest.raises(ValueError, match="legacy model compatibility"):
        knowledge.KnowledgeOptions(**options)
    allowed = knowledge.KnowledgeOptions(
        **options, allow_legacy_model_mismatch=True
    )
    assert allowed.allow_legacy_model_mismatch is True
    with pytest.raises(ValueError, match="pinned current model"):
        knowledge.KnowledgeOptions(
            **{
                **options,
                "opensearch_current_model": source_model,
                "allow_legacy_model_mismatch": True,
            }
        )


def test_exploratory_history_projection_is_bounded_and_excludes_labels():
    long_text = "x" * 1000
    pair = {
        "pair_id": "pair",
        "a": {
            field: [{"description": long_text} for _ in range(3)]
            for field in (
                "service_relations",
                "symptoms",
                "metric_changes",
                "log_patterns",
                "trace_patterns",
                "temporal_sequence",
            )
        },
        "b": {
            field: [long_text] * 5
            for field in (
                "investigation_steps",
                "applicability",
                "recommended_checks",
                "limitations",
            )
        },
        "labels": {"fault_type": "hidden-label"},
        "diagnosis": {"category": "hidden-diagnosis"},
    }

    compacted = knowledge.compact_exploratory_history(pair)
    encoded = knowledge.canonical(compacted)

    assert len(encoded) <= knowledge.EXPLORATORY_HISTORY_DOCUMENT_CHAR_LIMIT
    assert "hidden-label" not in encoded
    assert "hidden-diagnosis" not in encoded


def test_exploratory_history_projects_check_results_with_local_evidence_refs():
    pair = {
        "pair_id": "pair",
        "a": {
            "observed_services": ["front-end"],
            "service_relations": [],
            "symptoms": [{"description": "CPU saturation", "evidence_ids": ["E01"]}],
            "metric_changes": [],
            "log_patterns": [],
            "trace_patterns": [],
            "temporal_sequence": [],
            "preliminary_hypotheses": [],
            "limitations": [],
            "text_version": "incident-a-v2",
            "metric_facts": [],
            "fact_limitations": [],
        },
        "b": {},
        "labels": {"fault_type": "hidden-label"},
        "evidence": [
            {
                "id": "E01",
                "family": "metrics",
                "source": "live",
                "tool": "get_cpu_saturation_top",
                "status": "ok",
                "observation_count": 1,
                "fault_observation_count": 1,
                "arguments": {},
                "digest": {
                    "status": "ok",
                    "signal_detected": True,
                    "query": "internal query omitted from guidance",
                    "top": [{"service": "front-end", "value": 70}],
                },
            },
            {
                "id": "E02",
                "family": "traces",
                "source": "live",
                "tool": "query_traces",
                "status": "no_data",
                "observation_count": 0,
                "fault_observation_count": 0,
                "arguments": {},
                "digest": {"status": "no_data", "reason": "No spans found"},
            },
        ],
        "executed_checks": [
            {
                "check": {
                    "kind": "cpu",
                    "service": "front-end",
                    "phase": "fault",
                    "hypothesis": "CPU saturation is present.",
                    "expected_support": "Saturation is high.",
                    "expected_refutation": "Saturation is normal.",
                    "patterns": [],
                    "historical_pair_ids": [],
                },
                "evidence_id": "E01",
                "status": "ok",
            },
            {
                "check": {
                    "kind": "traces",
                    "service": "front-end",
                    "phase": "fault",
                    "hypothesis": "A trace path may be delayed.",
                    "expected_support": "A span is delayed.",
                    "expected_refutation": "Spans are absent.",
                    "patterns": [],
                    "historical_pair_ids": [],
                },
                "evidence_id": "E02",
                "status": "no_data",
            },
        ],
    }

    legacy_projection = knowledge.compact_exploratory_history(pair)
    projected = knowledge.compact_exploratory_history(
        pair, include_executed_checks=True
    )
    checks = projected["executed_checks"]

    assert "executed_checks" not in legacy_projection
    assert [item["status"] for item in checks] == ["ok", "no_data"]
    assert checks[0]["historical_result"]["evidence_id"] == "E01"
    assert checks[0]["historical_result"]["digest"]["top"][0]["value"] == 70
    assert "query" not in knowledge.canonical(projected)
    assert checks[1]["historical_result"]["status"] == "no_data"
    assert checks[1]["historical_result"]["fault_observation_count"] == 0
    assert "hidden-label" not in knowledge.canonical(projected)
    assert "par historico citado, nao ao run atual" in (
        knowledge.exploratory_guidance_notice(False)
    )


def test_exploratory_history_bounds_many_long_checks_and_marks_omissions():
    long_text = "x" * 600
    pair = {
        "pair_id": "pair",
        "a": document(),
        "b": {
            "investigation_steps": [long_text] * 3,
            "applicability": [long_text] * 3,
            "recommended_checks": [long_text] * 3,
            "limitations": [long_text] * 3,
        },
        "executed_checks": [
            {
                "check": {
                    "kind": "traces",
                    "service": "front-end",
                    "phase": "fault",
                    "hypothesis": long_text,
                    "expected_support": long_text,
                    "expected_refutation": long_text,
                    "patterns": [long_text] * 5,
                    "historical_pair_ids": [],
                },
                "status": "rejected",
                "reason": long_text,
            }
            for _ in range(5)
        ],
    }

    projected = knowledge.compact_exploratory_history(
        pair, include_executed_checks=True
    )
    encoded = knowledge.canonical(projected)

    assert len(projected["executed_checks"]) == 4
    assert projected["omitted_executed_check_count"] == 1
    assert projected["executed_checks"][0]["check"]["patterns"]["omitted_count"] == 4
    assert "[truncated]" in encoded
    assert len(encoded) <= knowledge.EXPLORATORY_HISTORY_DOCUMENT_CHAR_LIMIT


def test_exploratory_retrieval_uses_lexical_match_or_dense_score_floor(monkeypatch):
    import numpy as np

    provider = type(
        "Provider",
        (),
        {
            "name": "test-provider",
            "dim": 2,
            "embed": staticmethod(lambda _text: np.asarray([1.0, 0.0])),
        },
    )()
    monkeypatch.setattr(knowledge, "get_embedding_provider", lambda: provider)
    current = knowledge.IncidentA.model_validate(document())
    candidate_a = document()
    pair = {
        "pair_id": "candidate",
        "source_run_id": "train",
        "guide_status": "pending",
        "causal_status": "inconclusive",
        "effect_validated": True,
        "a": candidate_a,
    }
    base = {
        "training_run_ids": ["train"],
        "embedding_provider": provider.name,
        "embedding_dim": provider.dim,
        "pairs": [pair],
        "index": {"candidate": [-1.0, 0.0]},
    }

    lexical_match = knowledge.retrieve_pairs(
        current, base, "test-run", 3, 0.05, exploratory=True
    )
    assert len(lexical_match) == 1
    assert lexical_match[0]["score_components"]["dense_cosine"] == -1.0
    assert lexical_match[0]["score_components"]["bm25"] > 0

    for field in (
        "service_relations",
        "symptoms",
        "metric_changes",
        "log_patterns",
        "trace_patterns",
        "temporal_sequence",
    ):
        for item in candidate_a[field]:
            item["description"] = "xenoblaster qzxw"
    candidate_a["observed_services"] = ["other-service"]
    assert (
        knowledge.retrieve_pairs(current, base, "test-run", 3, 0.05, exploratory=True)
        == []
    )


def test_hybrid_text_ranker_fuses_dense_and_lexical_rankings():
    import numpy as np

    from experiment.platform.backend.analysis.retrieval import rank_hybrid_text

    ranked = rank_hybrid_text(
        np.asarray([1.0, 0.0], dtype=np.float32),
        "orders cpu saturation",
        [
            {
                "pair_id": "dense",
                "vector": np.asarray([1.0, 0.0], dtype=np.float32),
                "text": "payment network latency",
            },
            {
                "pair_id": "lexical",
                "vector": np.asarray([0.0, 1.0], dtype=np.float32),
                "text": "orders cpu",
            },
            {
                "pair_id": "both",
                "vector": np.asarray([0.7, 0.7], dtype=np.float32),
                "text": "orders cpu saturation latency",
            },
        ],
        limit=2,
    )

    assert [item["pair_id"] for item in ranked] == ["both", "dense"]
    assert ranked[0]["score_components"]["dense_rank"] == 2
    assert ranked[0]["score_components"]["bm25_rank"] == 1
    assert ranked[0]["score"] == pytest.approx((1 / 62 + 1 / 61) / (2 / 61))
    assert all("vector" not in item and "text" not in item for item in ranked)

    no_lexical_tokens = rank_hybrid_text(
        np.asarray([1.0, 0.0], dtype=np.float32),
        "",
        [
            {
                "pair_id": "z",
                "vector": np.asarray([1.0, 0.0], dtype=np.float32),
                "text": "unmatched",
            },
            {
                "pair_id": "a",
                "vector": np.asarray([1.0, 0.0], dtype=np.float32),
                "text": "unmatched",
            },
        ],
        limit=2,
    )
    assert [item["pair_id"] for item in no_lexical_tokens] == ["a", "z"]
    assert [item["score"] for item in no_lexical_tokens] == pytest.approx(
        [0.5, 61 / 124]
    )
    assert all(
        item["score_components"]["bm25_rank"] is None for item in no_lexical_tokens
    )
    assert (
        rank_hybrid_text(np.asarray([1.0, 0.0], dtype=np.float32), "", [], limit=2)
        == []
    )


def test_base_is_required_and_self_retrieval_rejected(tmp_path):
    with pytest.raises(ValueError, match="frozen"):
        investigate(tmp_path, mode="rag", run_id="test")
    investigate(tmp_path)
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    store.freeze(["train"])
    with pytest.raises(ValueError, match="training set"):
        investigate(tmp_path, mode="rag")


def test_no_matching_neighbors_and_history_budget_are_auditable(tmp_path):
    investigate(tmp_path)
    knowledge.KnowledgeStore(tmp_path, "corpus").freeze(["train"])
    result = investigate(tmp_path, mode="rag", run_id="test", history_char_budget=1000)
    assert result["retrieved"]
    assert not result["provided_pair_ids"]
    assert result["omitted_pair_ids"]


def test_invalid_references_in_a_are_rejected():
    a = knowledge.IncidentA.model_validate(document("E999"))
    with pytest.raises(ValueError, match="unusable evidence"):
        knowledge.validate_a(a, [], {"front-end"})


def test_hypotheses_are_not_embedded():
    first = knowledge.IncidentA.model_validate(document())
    second = first.model_copy(
        update={"preliminary_hypotheses": ["secret-injection-label"]}
    )
    assert first.retrieval_text() == second.retrieval_text()
    assert "secret-injection-label" not in second.retrieval_text()


def metric_evidence():
    return [
        {
            "id": "E001",
            "tool": "get_run_golden_signals",
            "family": "metrics",
            "source": "live",
            "status": "ok",
            "observation_count": 6,
            "arguments": {
                "baseline_start": PHASES["baseline"]["start"],
                "baseline_end": PHASES["baseline"]["end"],
                "fault_start": PHASES["fault"]["start"],
                "fault_end": PHASES["fault"]["end"],
            },
            "digest": {
                "metrics": {
                    "cpu_saturation_pct": {
                        "status": "ok",
                        "unit": "%",
                        "phases": {
                            "baseline": {"status": "ok", "sample_count": 3},
                            "fault": {"status": "ok", "sample_count": 3},
                        },
                        "top_delta": [
                            {
                                "service": "catalogue",
                                "baseline": 15.07,
                                "fault": 54.73,
                                "delta": 39.66,
                            }
                        ],
                    }
                }
            },
        }
    ]


def test_a_v2_extracts_metric_links_from_evidence_not_model_descriptions():
    value = document()
    value["metric_changes"] = [
        {
            "description": "CPU de orders subiu para 99%",
            "evidence_ids": ["E001"],
        }
    ]
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(value),
        metric_evidence(),
        {"catalogue", "front-end", "orders"},
    )
    fact = a.metric_facts[0]
    assert fact.service == "catalogue"
    assert fact.baseline_value == 15.07
    assert fact.fault_value == 54.73
    assert fact.delta == pytest.approx(39.66)
    assert fact.baseline_window.start == PHASES["baseline"]["start"]
    assert fact.fault_window.end == PHASES["fault"]["end"]
    assert fact.source_path == "/digest/metrics/cpu_saturation_pct/top_delta/0"
    assert "orders" not in a.retrieval_text()
    assert "metric=cpu_saturation_pct service=catalogue" in a.retrieval_text()
    assert "E001" not in a.retrieval_text()
    assert PHASES["fault"]["start"] not in a.retrieval_text()


def test_historical_metric_projection_is_diverse_and_retains_source_links():
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()),
        metric_evidence(),
        {"catalogue", "front-end", "orders"},
    )
    fact = a.metric_facts[0]
    cpu_facts = [
        fact.model_copy(update={"delta": delta, "fault_value": 15.07 + delta})
        for delta in (1, 2, 3, 39.66)
    ]
    memory = fact.model_copy(
        update={
            "metric": "memory_saturation_pct",
            "baseline_value": 41,
            "fault_value": 65,
            "delta": 24,
            "source_path": "/digest/metrics/memory_saturation_pct/top_delta/0",
        }
    )
    a.metric_facts = cpu_facts + [memory]
    history = knowledge.compact_exploratory_history(
        {
            "pair_id": "pair",
            "a": a.model_dump(),
            "b": {},
        }
    )
    assert [fact["metric"] for fact in history["metric_facts"]] == [
        "cpu_saturation_pct",
        "memory_saturation_pct",
    ]
    assert history["metric_facts"][0]["delta"] == 39.66
    assert history["metric_facts"][1]["source_path"] == memory.source_path
    assert history["omitted_metric_fact_count"] == 3


def test_a_v2_deduplicates_normalizes_and_preserves_observation_groups():
    value = document()
    value["symptoms"] = [
        {"description": "Latência   elevada", "evidence_ids": ["E001"]},
        {"description": "LATENCIA elevada", "evidence_ids": ["E001"]},
    ]
    value["service_relations"] = [value["symptoms"][0]]
    value["temporal_sequence"] = [
        {
            "description": "CPU aumentou antes dos erros",
            "evidence_ids": ["E001"],
        }
    ]
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(value),
        metric_evidence(),
        {"catalogue", "front-end"},
    )
    text = a.retrieval_text()
    assert text.count("latencia elevada") == 1
    assert "temporal_sequence: cpu aumentou antes dos erros" in text
    assert "preliminary_hypotheses" not in text


@pytest.mark.parametrize("change", ["service", "fault_value", "source_path"])
def test_a_v2_rejects_tampered_metric_links(change):
    evidence = metric_evidence()
    known = {"catalogue", "front-end", "orders"}
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()), evidence, known
    )
    a.metric_facts[0] = a.metric_facts[0].model_copy(
        update={
            change: {"service": "orders", "fault_value": 99.0, "source_path": "/fake"}[
                change
            ]
        }
    )
    with pytest.raises(ValueError, match="metric facts differ"):
        knowledge.validate_a(a, evidence, known)


@pytest.mark.parametrize("missing", ["samples", "window", "unit", "no_data"])
def test_a_v2_missing_coverage_is_explicit_and_never_becomes_zero(missing):
    evidence = metric_evidence()
    metric = evidence[0]["digest"]["metrics"]["cpu_saturation_pct"]
    if missing == "samples":
        metric["phases"]["fault"]["sample_count"] = 0
    elif missing == "window":
        evidence[0]["arguments"].pop("fault_end")
    elif missing == "unit":
        metric["unit"] = None
    else:
        metric["status"] = "no_data"
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()),
        evidence,
        {"catalogue", "front-end"},
    )
    assert a.metric_facts == []
    assert a.fact_limitations


def test_legacy_a_text_remains_byte_identical():
    a = knowledge.IncidentA.model_validate(document())
    assert a.retrieval_text() == (
        "Latencia elevada, memoria estavel, timeout na dependencia\n"
        "Latencia elevada, memoria estavel, timeout na dependencia"
    )
    enriched = knowledge.enrich_incident_a(
        a, metric_evidence(), {"catalogue", "front-end"}
    )
    assert enriched.retrieval_text(version=knowledge.LEGACY_A_TEXT_VERSION) == (
        a.retrieval_text()
    )


@pytest.mark.parametrize(
    "start,end",
    [
        ("invalid", PHASES["fault"]["end"]),
        ("2026-10-02T10:02:00", "2026-10-02T10:03:00"),
        (PHASES["fault"]["end"], PHASES["fault"]["start"]),
    ],
)
def test_a_v2_rejects_invalid_metric_windows(start, end):
    evidence = metric_evidence()
    evidence[0]["arguments"].update(fault_start=start, fault_end=end)
    with pytest.raises(ValueError):
        knowledge.enrich_incident_a(
            knowledge.IncidentA.model_validate(document()),
            evidence,
            {"catalogue", "front-end"},
        )


def test_new_annotations_freeze_a_versioned_index_and_never_ask_model_for_facts(
    tmp_path,
):
    model = Model()
    result = investigate(tmp_path, model=model)
    assert result["a"]["text_version"] == knowledge.A_TEXT_VERSION
    assert result["a"]["metric_facts"][0]["metric"] == "error_rate"
    assert result["a"]["metric_facts"][0]["fault_value"] == 20
    base = knowledge.KnowledgeStore(tmp_path, "corpus").freeze(["train"])
    assert base["a_text_version"] == knowledge.A_TEXT_VERSION
    assert knowledge.base_a_text_version(base) == knowledge.A_TEXT_VERSION
    assert (
        "metric_facts"
        not in knowledge.IncidentNarrative.model_json_schema()["properties"]
    )
    build_a = next(
        payload for payload in model.payloads if payload["stage"] == "build_a"
    )
    assert "training_annotation_labels" not in knowledge.canonical(build_a)


def test_v2_retrieval_distinguishes_local_metric_service_from_shared_symptoms():
    evidence = metric_evidence()
    known = {"catalogue", "orders", "front-end"}
    current = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()), evidence, known
    )
    wrong_evidence = copy.deepcopy(evidence)
    wrong_evidence[0]["digest"]["metrics"]["cpu_saturation_pct"]["top_delta"][0][
        "service"
    ] = "orders"
    wrong = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()), wrong_evidence, known
    )
    provider = knowledge.get_embedding_provider()
    pairs = [
        {
            "pair_id": identifier,
            "source_run_id": identifier,
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "effect_validated": True,
            "a": a.model_dump(),
        }
        for identifier, a in (("wrong", wrong), ("right", current))
    ]
    base = {
        "a_text_version": knowledge.A_TEXT_VERSION,
        "embedding_provider": provider.name,
        "embedding_dim": provider.dim,
        "training_run_ids": ["wrong", "right"],
        "pairs": pairs,
        "index": {
            pair["pair_id"]: provider.embed(
                knowledge.IncidentA.model_validate(pair["a"]).retrieval_text()
            ).tolist()
            for pair in pairs
        },
    }
    ranked = knowledge.retrieve_pairs(current, base, "test", 2, 0.05, exploratory=True)
    assert ranked[0]["pair_id"] == "right"
    base["a_text_version"] = knowledge.LEGACY_A_TEXT_VERSION
    with pytest.raises(ValueError, match="text version mismatch"):
        knowledge.retrieve_pairs(current, base, "test", 2, 0.05, exploratory=True)


def test_v2_history_retains_metric_provenance_without_promoting_labels():
    a = knowledge.enrich_incident_a(
        knowledge.IncidentA.model_validate(document()),
        metric_evidence(),
        {"catalogue", "front-end"},
    )
    projected = knowledge.compact_exploratory_history(
        {
            "pair_id": "historical",
            "a": a.model_dump(),
            "b": {},
            "labels": {"fault_type": "secret-injection-label"},
        }
    )
    assert projected["metric_facts"][0]["service"] == "catalogue"
    assert projected["metric_facts"][0]["source_path"].startswith("/digest/")
    assert "secret-injection-label" not in knowledge.canonical(projected)
    assert len(knowledge.canonical(projected)) <= (
        knowledge.EXPLORATORY_HISTORY_DOCUMENT_CHAR_LIMIT
    )


def test_corrupt_base_is_rejected(tmp_path):
    investigate(tmp_path)
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    store.freeze(["train"])
    path = store.directory / "base.json"
    base = json.loads(path.read_text())
    base["pairs"][0]["labels"]["service"] = "orders"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="mismatch"):
        store.load_base()


def test_query_failure_keeps_evidence_and_reserves_final_synthesis(tmp_path):
    class BrokenFollowup(FakeMCP):
        def call_tool_json(self, name, arguments):
            if name == "loki_range_query" and "|~" in arguments.get("query", ""):
                raise RuntimeError("Loki unavailable")
            return super().call_tool_json(name, arguments)

    model = Model(query=True)
    result = investigate(tmp_path, model=model, client=BrokenFollowup())
    assert result["status"] == "inconclusive"
    assert result["evidence"]
    assert result["additional_checks"][0]["status"] == "tool_error"
    assert result["pair"]["guide_status"] == "pending"
    assert model.payloads[-1]["stage"] == "build_b"


def test_single_phase_metrics_do_not_count_baseline_as_fault():
    class CPU(FakeMCP):
        def list_tools(self):
            return super().list_tools() + [{"name": "get_cpu_saturation_top"}]

    check = knowledge.Check(
        kind="cpu",
        service="front-end",
        phase="baseline",
        hypothesis="CPU",
        expected_support="Saturacao",
        expected_refutation="CPU estavel",
        historical_pair_ids=[],
    )
    evidence = []
    knowledge.execute_check(
        check, CPU(), "sock-shop", PHASES, {"front-end"}, evidence, []
    )
    assert evidence[0]["observation_count"] > 0
    assert evidence[0]["fault_observation_count"] == 0


def test_historical_query_evidence_is_labeled_as_history():
    class LiveLabeledMCP(FakeMCP):
        def list_tools(self):
            return super().list_tools() + [{"name": "get_cpu_saturation_top"}]

        def call_tool_json(self, name, arguments):
            result = super().call_tool_json(name, arguments)
            if isinstance(result, dict):
                return {**result, "evidence_source": "live"}
            return result

    check = knowledge.Check(
        kind="cpu",
        service="front-end",
        phase="fault",
        hypothesis="CPU",
        expected_support="Saturacao",
        expected_refutation="CPU estavel",
        historical_pair_ids=[],
    )
    evidence = []
    knowledge.execute_check(
        check,
        LiveLabeledMCP(),
        "sock-shop",
        PHASES,
        {"front-end"},
        evidence,
        [],
        evidence_source="history",
    )

    assert evidence[0]["source"] == "history"


def test_unavailable_trace_is_preflighted_then_one_alternative_is_executed(tmp_path):
    class CorrectingModel(Model):
        def __call__(self, messages, tools, **kwargs):
            response = super().__call__(messages, tools, **kwargs)
            context = self.payloads[-1].get("context") or {}
            stage = self.payloads[-1]["stage"]
            if stage == "annotation_check" and context["round"] == 0:
                decision = json.loads(response["content"])
                decision["check"]["kind"] = "trace_detail"
                response["content"] = json.dumps(decision)
            elif stage == "annotation_check" and context["round"] == 1:
                decision = {
                    "action": "query",
                    "reason": "Alternativa viavel",
                    "check": {
                        "kind": "logs",
                        "service": "front-end",
                        "phase": "fault",
                        "hypothesis": "Timeout",
                        "expected_support": "Timeout",
                        "expected_refutation": "Erro local",
                        "historical_pair_ids": [],
                        "patterns": ["timeout"],
                    },
                }
                assert context["proposal_corrections_remaining"] == 0
                assert context["previous_checks"][0]["status"] == "rejected"
                assert "measurement_limits" in context["check_inventory"]
                response["content"] = json.dumps(decision)
            return response

    result = investigate(tmp_path, model=CorrectingModel(query=True))
    assert [check["status"] for check in result["additional_checks"]] == [
        "rejected",
        "ok",
    ]
    assert (
        result["additional_checks"][0]["reason"]
        == "no current trace candidate available"
    )
    assert "unresolved_check_rejection" not in result


def test_inventory_reports_concrete_duplicates_before_model_query():
    check = knowledge.Check(
        kind="cpu",
        service="front-end",
        phase="fault",
        hypothesis="CPU",
        expected_support="CPU alta",
        expected_refutation="CPU estavel",
        historical_pair_ids=[],
    )
    tools = {"get_cpu_saturation_top": {}}
    name, arguments, _, _ = knowledge.prepare_check(
        check, "sock-shop", PHASES, {"front-end"}, [], tools
    )
    calls = [{"tool": name, "arguments": arguments}]
    inventory = knowledge.check_inventory(
        tools, "sock-shop", PHASES, {"front-end"}, calls
    )
    assert {"kind": "cpu", "service": "front-end", "phase": "fault"} not in (
        inventory["executable_checks"]
    )
    with pytest.raises(knowledge.CheckUnavailable, match="already executed"):
        knowledge.prepare_check(check, "sock-shop", PHASES, {"front-end"}, calls, tools)


@pytest.mark.parametrize(
    "kind,metric",
    [
        ("cpu_throttling", "container_cpu_cfs_throttled_periods_total"),
        ("gc", "jvm_gc_collection_seconds_sum"),
        ("http", "istio_requests_total"),
        ("network", "istio_request_duration_milliseconds_sum"),
        ("lifecycle", "kube_pod_container_status_last_terminated_"),
    ],
)
def test_fixed_discriminative_queries_are_namespaced_bounded_and_never_arbitrary(
    kind, metric
):
    check = knowledge.Check(
        kind=kind,
        service="carts",
        phase="fault",
        hypothesis="Verificar",
        expected_support="Sinal",
        expected_refutation="Sinal estavel",
        historical_pair_ids=[],
    )
    name, arguments, family, _ = knowledge.prepare_check(
        check,
        "sock-shop",
        PHASES,
        {"carts"},
        [],
        {"prometheus_range_query": {}},
    )
    assert name == "prometheus_range_query"
    assert family == "metrics"
    assert metric in arguments["query"]
    assert '"sock-shop"' in arguments["query"]
    assert arguments["start"] == PHASES["baseline"]["start"]
    assert arguments["end"] == PHASES["post"]["end"]


def test_immutable_records_and_lock(tmp_path):
    path = tmp_path / "record.json"
    knowledge.write_once(path, {"a": 1})
    knowledge.write_once(path, {"a": 1})
    with pytest.raises(ValueError, match="immutable"):
        knowledge.write_once(path, {"a": 2})
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    with store.locked():
        with pytest.raises(ValueError, match="busy"):
            with store.locked():
                pass


def test_rag_prompt_treats_historical_ground_truth_as_nontransferable():
    prompts = []

    def capture(messages, tools, **kwargs):
        prompts.append(messages[0]["content"])
        return {"content": json.dumps(document()), "usage": {"total_tokens": 10}}

    session = knowledge.ModelSession(
        capture,
        knowledge.KnowledgeOptions(
            corpus_id="corpus",
            mode="rag",
            possible_fault_types=["network-latency"],
        ),
    )
    session.generate(
        "rag_final",
        knowledge.IncidentA,
        {"history": [{"b": {"target_result_source": "ground_truth_label"}}]},
    )

    assert "target_result" in prompts[0]
    assert "descrevem somente o caso historico" in prompts[0]
    assert "nao transfira sua conclusao para o caso atual" in prompts[0]
