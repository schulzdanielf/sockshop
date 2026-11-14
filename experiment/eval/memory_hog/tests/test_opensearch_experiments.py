from __future__ import annotations

import json

import numpy as np
import pytest

from experiment.eval.memory_hog import opensearch_experiments
from experiment.eval.memory_hog.opensearch_experiments import (
    OpenSearchError,
    OpenSearchExperimentIndex,
)
from experiment.platform.backend import rca_knowledge as knowledge


def frozen_base():
    observation = {
        "description": "CPU saturation increased during the fault phase.",
        "evidence_ids": ["E1"],
    }

    def pair(pair_id, effect_validated):
        return {
            "pair_id": pair_id,
            "source_run_id": f"train-{pair_id}",
            "effect_validated": effect_validated,
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "labels": {"service": "front-end", "fault_type": "cpu-hog"},
            "a": {
                "observed_services": ["front-end"],
                "service_relations": [],
                "symptoms": [observation],
                "metric_changes": [],
                "log_patterns": [],
                "trace_patterns": [],
                "temporal_sequence": [],
                "preliminary_hypotheses": ["private-label"],
                "limitations": [],
                "text_version": "incident-a-v2",
                "metric_facts": [],
                "fact_limitations": [],
            },
            "b": {
                "symptom_relation": "The historical service had CPU pressure.",
                "investigation_steps": [],
                "applicability": [],
                "recommended_checks": ["Compare service CPU by phase."],
                "limitations": [],
                "diagnosis": {"fault_type": "cpu-hog"},
            },
            "evidence": [
                {
                    "id": "E1",
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
                        "top": [{"service": "front-end", "value": 70}],
                    },
                }
            ],
            "executed_checks": [
                {
                    "check": {
                        "kind": "cpu",
                        "service": "front-end",
                        "phase": "fault",
                        "hypothesis": "CPU saturation increased.",
                        "expected_support": "Saturation is high.",
                        "expected_refutation": "Saturation is normal.",
                        "patterns": [],
                        "historical_pair_ids": [],
                    },
                    "evidence_id": "E1",
                    "status": "ok",
                }
            ],
        }

    pairs = [pair("pair-unvalidated", False), pair("pair-validated", True)]
    return {
        "sha256": "a" * 64,
        "embedding_provider": "hashing-v1",
        "embedding_dim": 2,
        "pairs": pairs,
        "index": {
            "pair-unvalidated": [1.0, 0.0],
            "pair-validated": [0.0, 1.0],
        },
    }


def test_index_base_stores_all_pairs_without_searchable_labels(monkeypatch):
    base = frozen_base()
    index = OpenSearchExperimentIndex("http://127.0.0.1:9200", base["sha256"])
    documents = {}

    def fake_request(
        method,
        path,
        payload=None,
        *,
        content_type="application/json",
        allow_not_found=False,
    ):
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            assert allow_not_found
            return None
        if method == "PUT" and path == f"/{index.index_name}":
            assert payload["mappings"]["properties"]["a_vector"]["dimension"] == 2
            assert payload["mappings"]["properties"]["history"]["enabled"] is False
            assert payload["mappings"]["_meta"]["schema_version"] == 2
            assert payload["mappings"]["properties"]["a_metric_facts"]["type"] == "text"
            return {"acknowledged": True}
        if method == "POST" and path == f"/{index.index_name}/_bulk":
            assert content_type == "application/x-ndjson"
            lines = payload.decode("utf-8").splitlines()
            for action, source in zip(lines[::2], lines[1::2]):
                action_id = json.loads(action)["index"]["_id"]
                document = json.loads(source)
                assert "labels" not in document
                assert "diagnosis" not in document
                documents[action_id] = document
            return {
                "errors": False,
                "items": [{"index": {"status": 201}} for _ in documents],
            }
        if method == "POST" and path == f"/{index.index_name}/_refresh":
            return {}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": len(documents)}
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    metadata = index.index_base(base, a_text_version="incident-a-v2")

    assert metadata["document_count"] == 2
    assert metadata["opensearch_version"] == "2.19.0"
    assert set(documents) == {"pair-unvalidated", "pair-validated"}
    assert documents["pair-unvalidated"]["effect_validated"] is False
    assert documents["pair-unvalidated"]["history"]["source_effect_validated"] is False
    assert documents["pair-unvalidated"]["a_vector"] == [1.0, 0.0]
    assert documents["pair-unvalidated"]["history"]["pair_id"] == "pair-unvalidated"
    assert documents["pair-unvalidated"]["history"]["executed_checks"][0][
        "historical_result"
    ]["evidence_id"] == "E1"
    assert documents["pair-unvalidated"]["a_symptoms"] == [
        "cpu saturation increased during the fault phase."
    ]


def test_connect_existing_verifies_frozen_metadata_and_document_ids(monkeypatch):
    base = frozen_base()
    index = OpenSearchExperimentIndex("http://127.0.0.1:9200", base["sha256"])
    metadata = {
        "schema_version": 2,
        "base_sha256": base["sha256"],
        "a_text_version": "incident-a-v2",
        "embedding_provider": "hashing-v1",
        "embedding_dim": 2,
        "document_count": 2,
    }

    def document(pair_id, effect_validated):
        return {
            "pair_id": pair_id,
            "source_run_id": f"train-{pair_id}",
            "effect_validated": effect_validated,
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "history": {
                "pair_id": pair_id,
                "source_effect_validated": effect_validated,
            },
        }

    def fake_request(method, path, payload=None, **kwargs):
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            return {index.index_name: {"mappings": {"_meta": metadata}}}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": 2}
        if method == "POST" and path == f"/{index.index_name}/_search":
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": "pair-unvalidated",
                            "_source": document("pair-unvalidated", False),
                        },
                        {
                            "_id": "pair-validated",
                            "_source": document("pair-validated", True),
                        },
                    ]
                }
            }
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    result = index.connect_existing(
        expected_pair_ids={"pair-unvalidated", "pair-validated"},
        expected_a_text_version="incident-a-v2",
    )

    assert result["base_sha256"] == base["sha256"]
    assert result["document_count"] == 2
    assert result["a_text_version"] == "incident-a-v2"
    assert index._index_metadata == metadata


def test_connect_existing_rejects_pair_id_drift(monkeypatch):
    base = frozen_base()
    index = OpenSearchExperimentIndex("http://127.0.0.1:9200", base["sha256"])
    metadata = {
        "schema_version": 2,
        "base_sha256": base["sha256"],
        "a_text_version": "incident-a-v2",
        "embedding_provider": "hashing-v1",
        "embedding_dim": 2,
        "document_count": 1,
    }
    source = {
        "pair_id": "unrelated",
        "source_run_id": "train-unrelated",
        "effect_validated": True,
        "guide_status": "pending",
        "causal_status": "inconclusive",
        "history": {"pair_id": "unrelated"},
    }

    def fake_request(method, path, payload=None, **kwargs):
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            return {index.index_name: {"mappings": {"_meta": metadata}}}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": 1}
        if method == "POST" and path == f"/{index.index_name}/_search":
            return {"hits": {"hits": [{"_id": "unrelated", "_source": source}]}}
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    with pytest.raises(OpenSearchError, match="IDs differ"):
        index.connect_existing(
            expected_pair_ids={"pair-validated"},
            expected_a_text_version="incident-a-v2",
        )


@pytest.mark.parametrize("include_unvalidated", [True, False])
def test_search_fuses_opensearch_knn_and_bm25_and_filters_validation(
    monkeypatch, include_unvalidated
):
    base = frozen_base()
    index = OpenSearchExperimentIndex("http://localhost:9200", base["sha256"])
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    requests = []

    def source(pair_id, effect_validated):
        return {
            "pair_id": pair_id,
            "source_run_id": f"train-{pair_id}",
            "effect_validated": effect_validated,
            "guide_status": "pending",
            "causal_status": "inconclusive",
            "history": {
                "pair_id": pair_id,
                "source_effect_validated": effect_validated,
            },
        }

    dense = [
        {
            "_id": "pair-unvalidated",
            "_score": 0.9,
            "_source": source("pair-unvalidated", False),
        },
        {
            "_id": "pair-validated",
            "_score": 0.8,
            "_source": source("pair-validated", True),
        },
    ]
    lexical = [
        {
            "_id": "pair-validated",
            "_score": 2.0,
            "_source": source("pair-validated", True),
        }
    ]

    def fake_request(method, path, payload=None, **kwargs):
        assert method == "POST"
        requests.append(payload)
        if "knn" in payload["query"]:
            return {"hits": {"hits": dense}}
        return {"hits": {"hits": lexical}}

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        "cpu saturation",
        np.asarray([1.0, 0.0], dtype=np.float32),
        source_run_id="test-run",
        top_k=2,
        include_unvalidated_experimental=include_unvalidated,
    )

    assert [item["pair_id"] for item in results] == [
        "pair-validated",
        "pair-unvalidated",
    ]
    assert results[0]["score_components"] == {"dense_rank": 2, "bm25_rank": 1}
    assert all(
        request["query"].get("knn")
        or request["query"]["bool"]["must"][0].get("multi_match")
        for request in requests
    )
    lexical_query = next(
        request["query"]["bool"]["must"][0]["multi_match"]
        for request in requests
        if "bool" in request["query"]
    )
    assert "a_metric_facts" in lexical_query["fields"]
    assert "a_trace_patterns" in lexical_query["fields"]
    knn_filter = requests[0]["query"]["knn"]["a_vector"]["filter"]["bool"]["filter"]
    assert ({"term": {"effect_validated": True}} in knn_filter) is (
        not include_unvalidated
    )


def test_search_bounds_long_lexical_queries_per_family(monkeypatch):
    base = frozen_base()
    index = OpenSearchExperimentIndex("http://127.0.0.1:9200", base["sha256"])
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    requests = []
    fields = {
        field: [" ".join(f"term{term}" for term in range(500))]
        for field in opensearch_experiments._LEXICAL_FIELDS
    }

    def fake_request(method, path, payload=None, **kwargs):
        requests.append(payload)
        if "knn" in payload["query"]:
            return {"hits": {"hits": []}}
        clauses = payload["query"]["bool"]["must"][0]["bool"]["should"]
        assert len(clauses) == len(opensearch_experiments._LEXICAL_FIELDS)
        assert all(
            len(
                opensearch_experiments._LEXICAL_TOKEN.findall(
                    next(iter(clause["match"].values()))["query"]
                )
            )
            <= opensearch_experiments._MAX_LEXICAL_TERMS_PER_FIELD
            for clause in clauses
        )
        assert {
            next(iter(clause["match"]))
            for clause in clauses
        } == set(opensearch_experiments._LEXICAL_FIELDS)
        return {"hits": {"hits": []}}

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        " ".join(f"query{term}" for term in range(5000)),
        np.asarray([1.0, 0.0], dtype=np.float32),
        source_run_id="test-run",
        top_k=2,
        include_unvalidated_experimental=False,
        min_score=0,
        query_fields=fields,
    )

    assert results == []
    assert len(requests) == 2


def test_v5_bm25_uses_corpus_idf_and_preserves_http_codes(monkeypatch):
    base = frozen_base()
    query_document = dict(base["pairs"][0]["a"])
    query_document["symptoms"] = [
        {
            "description": "Gateway HTTP status code 503 returned for catalogue lookup.",
            "evidence_ids": ["E1"],
        }
    ]
    incident = knowledge.IncidentA.model_validate(query_document)
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V5,
    )
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    requests = []
    source = {
        "pair_id": "pair-validated",
        "source_run_id": "train-pair-validated",
        "effect_validated": True,
        "guide_status": "procedurally_validated",
        "causal_status": "not_assessed",
        "b_procedural_status": "passed",
        "history": {
            "pair_id": "pair-validated",
            "historical_status": "procedurally_validated",
            "procedural_status": "passed",
            "validated_outcome": {"service": "front-end"},
        },
    }

    def fake_request(method, path, payload=None, **kwargs):
        requests.append(payload)
        return {
            "hits": {
                "hits": [{"_id": "pair-validated", "_score": 2.0, "_source": source}]
            }
        }

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        incident.retrieval_text(),
        None,
        source_run_id="test-run",
        top_k=3,
        include_unvalidated_experimental=False,
        query_incident=incident,
        corpus_pairs=base["pairs"],
    )

    assert len(requests) == 1
    assert "knn" not in requests[0]["query"]
    assert requests[0]["query"]["bool"]["filter"] == [
        {"term": {"guide_status": "procedurally_validated"}},
        {"term": {"b_procedural_status": "passed"}},
    ]
    clauses = requests[0]["query"]["bool"]["must"][0]["bool"]["should"]
    symptom_query = next(
        clause["match"]["a_symptoms"]
        for clause in clauses
        if "a_symptoms" in clause["match"]
    )
    assert symptom_query["boost"] == 1.0
    assert {"gateway", "503"} <= set(symptom_query["query"].split())
    assert results[0]["pair_id"] == "pair-validated"
    assert results[0]["score_components"] == {"dense_rank": None, "bm25_rank": 1}


def test_v1_index_remains_available_with_legacy_mapping_and_projection(monkeypatch):
    base = frozen_base()
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version="opensearch-hybrid-rrf-v1",
    )
    requests = []

    def fake_request(method, path, payload=None, **kwargs):
        requests.append((method, path, payload))
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            return None
        if method == "PUT" and path == f"/{index.index_name}":
            properties = payload["mappings"]["properties"]
            assert "a_metric_facts" not in properties
            assert payload["mappings"]["_meta"]["schema_version"] == 1
            return {"acknowledged": True}
        if method == "POST" and path == f"/{index.index_name}/_bulk":
            documents = [
                json.loads(line)
                for line in payload.decode("utf-8").splitlines()[1::2]
            ]
            assert all("executed_checks" not in row["history"] for row in documents)
            return {
                "errors": False,
                "items": [{"index": {"status": 201}} for _ in documents],
            }
        if method == "POST" and path == f"/{index.index_name}/_refresh":
            return {}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": len(base["pairs"])}
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    index.index_base(base, a_text_version="incident-a-v2")

    assert index.index_name.startswith("sockshop-rca-experiments-v1-")
    assert any(path.endswith("/_bulk") for _, path, _ in requests)


def test_index_frozen_base_checks_hash_and_selects_v2_without_network(
    monkeypatch, tmp_path
):
    base = frozen_base()
    payload = {key: value for key, value in base.items() if key != "sha256"}
    base["sha256"] = opensearch_experiments.digest(payload)
    base_path = tmp_path / "exploratory_base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    calls = []

    class FakeIndex:
        def __init__(self, url, base_sha256, *, retriever_version):
            calls.append(("init", url, base_sha256, retriever_version))

        def index_base(self, indexed_base, *, a_text_version):
            calls.append(("index", indexed_base, a_text_version))
            return {"index_name": "sockshop-rca-experiments-v2-test"}

    monkeypatch.setattr(opensearch_experiments, "OpenSearchExperimentIndex", FakeIndex)

    result = opensearch_experiments.index_frozen_base(
        base_path, url="http://127.0.0.1:9200"
    )

    assert result["index_name"].endswith("-v2-test")
    assert calls[0][3] == opensearch_experiments.OPENSEARCH_RETRIEVER_VERSION
    assert calls[1][2] == "incident-a-v2"

    base["sha256"] = "b" * 64
    base_path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(OpenSearchError, match="checksum"):
        opensearch_experiments.index_frozen_base(
            base_path, url="http://127.0.0.1:9200"
        )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9200",
        "http://example.com:9200",
        "http://user:secret@127.0.0.1:9200",
        "http://127.0.0.1:9200/index?token=secret",
    ],
)
def test_opensearch_evaluation_requires_credential_free_loopback_url(url):
    with pytest.raises(OpenSearchError, match="loopback"):
        OpenSearchExperimentIndex(url, "a" * 64)


def rebuilt_qwen35_base():
    a = {
        "observed_services": ["user"],
        "service_relations": [],
        "symptoms": [
            {
                "description": "User responses became unavailable during the fault phase.",
                "evidence_ids": ["E1"],
            }
        ],
        "metric_changes": [],
        "log_patterns": [],
        "trace_patterns": [],
        "temporal_sequence": [],
        "preliminary_hypotheses": ["Container lifecycle failure"],
        "limitations": [],
        "text_version": "incident-a-v2",
        "metric_facts": [],
        "fact_limitations": [],
    }

    def pair(pair_id, validated):
        labels = {
            "service": "user",
            "fault_type": "container-kill",
            "causal_category": "pod-failure",
        }
        diagnosis = {
            "status": "confirmed" if validated else "inconclusive",
            "primary_service": "user",
            "fault_type": "container-kill",
            "category": "pod-failure",
            "supporting_evidence": ["E1", "E2"] if validated else [],
            "contradicting_evidence": [],
        }
        history = {
            "pair_id": pair_id,
            "historical_status": (
                "validated_success" if validated else "insufficient_evidence"
            ),
            "notice": "Exemplo histórico; não é evidência do incidente atual.",
        }
        if validated:
            history["validated_outcome"] = {
                "service": "user",
                "fault_type": "container-kill",
                "category": "pod-failure",
                "supporting_evidence": ["E1", "E2"],
            }
            history["investigation_steps"] = [
                {"step_id": "S1", "evidence_ids": ["E1", "E2"]}
            ]
        result = {
            "pair_id": pair_id,
            "source_run_id": f"train-{pair_id}",
            "effect_validated": True,
            "guide_status": (
                "validated_success" if validated else "insufficient_evidence"
            ),
            "causal_status": "supported" if validated else "inconclusive",
            "labels": labels,
            "a": a,
            "b": {
                "diagnosis": diagnosis,
                "rebuild_version": knowledge.TRAINING_B_SCHEMA_VERSION,
            },
            "b_rebuild_version": knowledge.TRAINING_B_SCHEMA_VERSION,
            "b_rebuild_gate_reasons": [] if validated else ["blind replay failed"],
            "retrieval_history": history,
        }
        result["sha256"] = opensearch_experiments.digest(
            {key: value for key, value in result.items() if key != "sha256"}
        )
        return result

    pairs = [pair("pair-valid", True), pair("pair-not-valid", False)]
    payload = {
        "version": knowledge.VERSION,
        "mode": "exploratory",
        "model": knowledge.QWEN35_MODEL_ID,
        "b_rebuild_version": knowledge.TRAINING_B_SCHEMA_VERSION,
        "a_text_version": "incident-a-v2",
        "embedding_provider": "hashing-v1",
        "embedding_dim": 2,
        "skill_sha256": "b" * 64,
        "training_run_ids": [pair["source_run_id"] for pair in pairs],
        "pairs": pairs,
        "index": {
            "pair-valid": [1.0, 0.0],
            "pair-not-valid": [0.0, 1.0],
        },
    }
    return {**payload, "sha256": opensearch_experiments.digest(payload)}


def procedural_qwen35_base():
    base = json.loads(json.dumps(rebuilt_qwen35_base()))
    base["b_rebuild_version"] = knowledge.TRAINING_B_PROCEDURAL_SCHEMA_VERSION
    for pair in base["pairs"]:
        passed = pair["pair_id"] == "pair-valid"
        pair["guide_status"] = (
            "procedurally_validated" if passed else "procedural_pending"
        )
        pair["review_status"] = (
            knowledge.BLIND_LIVE_REVIEW_BYPASSED_STATUS
            if passed else knowledge.BLIND_LIVE_REVIEW_STATUS
        )
        pair["causal_status"] = "not_assessed"
        pair["b_procedural_status"] = "passed" if passed else "pending"
        pair["b_rebuild_version"] = knowledge.TRAINING_B_PROCEDURAL_SCHEMA_VERSION
        pair["b_rebuild_gate_reasons"] = [] if passed else ["metric missing"]
        pair["b_evidence_index"] = [{"evidence_id": "E1"}]
        pair["b_collection_evidence"] = [{"id": "E1"}]
        pair["b"]["rebuild_version"] = knowledge.TRAINING_B_PROCEDURAL_SCHEMA_VERSION
        pair["b"]["service_cause_explanation"] = (
            "O reinício em user diferencia a origem do sintoma propagado."
        )
        pair["b"]["metric_confirmation_step_id"] = "S1"
        pair["b"]["investigation_steps"] = [{
            "step_id": "S1", "kind": "observed", "family": "lifecycle",
            "service": "user", "phase": "fault", "evidence_ids": ["E1"],
            "objective": "Confirmar o ciclo de vida no alvo",
            "verification": "Consultar restarts no MCP",
            "observed_result": "Um restart foi observado no container user.",
            "interpretation": "O sinal está no serviço-alvo.",
            "expected_support": "Restart do alvo",
            "expected_refutation": "Sem restart",
            "hypothesis_ids": ["H1"], "next_step_id": None,
        }] if passed else []
        pair["b"]["procedural_review"] = {
            "result": "pass" if passed else "fail",
            "target_matches": passed,
            "path_reaches_target": passed,
            "metric_confirmation": passed,
            "alternatives_explained": passed,
            "unsupported_claims": [],
            "unsupported_evidence_references": [],
            "scope_violations": [],
            "rationale": "Procedimento comparado ao rótulo.",
        }
        history = {
            "pair_id": pair["pair_id"],
            "historical_status": pair["guide_status"],
            "procedural_status": pair["b_procedural_status"],
            "source_effect_validated": False,
            "notice": "Procedimento histórico; não é evidência atual.",
        }
        if passed:
            history["validated_outcome"] = {
                "service": "user", "fault_type": "container-kill",
                "category": "pod-failure", "supporting_evidence": ["E1", "E2"],
            }
            history["investigation_steps"] = pair["b"]["investigation_steps"]
            history["service_cause_explanation"] = pair["b"]["service_cause_explanation"]
            history["metric_confirmation_step_id"] = "S1"
        pair["retrieval_history"] = history
        pair["sha256"] = opensearch_experiments.digest({
            key: value for key, value in pair.items() if key != "sha256"
        })
    payload = {key: value for key, value in base.items() if key != "sha256"}
    return {**payload, "sha256": opensearch_experiments.digest(payload)}


def test_v3_indexes_only_label_free_validated_guide_projections(monkeypatch):
    base = rebuilt_qwen35_base()
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V3,
    )
    documents = {}

    def fake_request(method, path, payload=None, **kwargs):
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            return None
        if method == "PUT" and path == f"/{index.index_name}":
            metadata = payload["mappings"]["_meta"]
            assert metadata["schema_version"] == 3
            assert metadata["model"] == knowledge.QWEN35_MODEL_ID
            return {"acknowledged": True}
        if method == "POST" and path == f"/{index.index_name}/_bulk":
            lines = payload.decode("utf-8").splitlines()
            for source in lines[1::2]:
                document = json.loads(source)
                assert "labels" not in document
                assert "diagnosis" not in document["history"]
                documents[document["pair_id"]] = document
            return {
                "errors": False,
                "items": [{"index": {"status": 201}} for _ in documents],
            }
        if method == "POST" and path == f"/{index.index_name}/_refresh":
            return {}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": len(documents)}
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    index.index_base(base, a_text_version="incident-a-v2")

    assert documents["pair-valid"]["history"]["historical_status"] == "validated_success"
    assert documents["pair-not-valid"]["history"]["historical_status"] == "insufficient_evidence"


def test_v3_search_filters_to_published_successes(monkeypatch):
    base = rebuilt_qwen35_base()
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V3,
    )
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    document = {
        "pair_id": "pair-valid",
        "source_run_id": "train-pair-valid",
        "effect_validated": True,
        "guide_status": "validated_success",
        "causal_status": "supported",
        "history": base["pairs"][0]["retrieval_history"],
    }
    requests = []

    def fake_request(method, path, payload=None, **kwargs):
        requests.append(payload)
        return {
            "hits": {
                "hits": [
                    {"_id": "pair-valid", "_score": 0.5, "_source": document}
                ]
            }
        }

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        "restart user",
        np.asarray([1.0, 0.0], dtype=np.float32),
        source_run_id="test-run",
        top_k=1,
        include_unvalidated_experimental=False,
        min_score=0,
    )

    assert [item["pair_id"] for item in results] == ["pair-valid"]
    for request in requests:
        filters = request["query"]["knn"]["a_vector"]["filter"]["bool"]["filter"] if "knn" in request["query"] else request["query"]["bool"]["filter"]
        assert {"term": {"guide_status": "validated_success"}} in filters
        assert {"term": {"causal_status": "supported"}} in filters
        assert {"term": {"effect_validated": True}} in filters


def test_v4_indexes_procedural_guides_independently_of_effect_validation(monkeypatch):
    base = procedural_qwen35_base()
    base["pairs"][0]["effect_validated"] = False
    base["pairs"][0]["sha256"] = opensearch_experiments.digest({
        key: value for key, value in base["pairs"][0].items() if key != "sha256"
    })
    payload = {key: value for key, value in base.items() if key != "sha256"}
    base["sha256"] = opensearch_experiments.digest(payload)
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V4,
    )
    documents = {}

    def fake_request(method, path, payload=None, **kwargs):
        if method == "GET" and path == "/":
            return {"version": {"number": "2.19.0"}}
        if method == "GET" and path == f"/{index.index_name}":
            return None
        if method == "PUT" and path == f"/{index.index_name}":
            assert payload["mappings"]["_meta"]["schema_version"] == 4
            assert payload["mappings"]["_meta"]["b_rebuild_version"] == (
                knowledge.TRAINING_B_PROCEDURAL_SCHEMA_VERSION
            )
            assert "b_procedural_status" in payload["mappings"]["properties"]
            assert "review_status" in payload["mappings"]["properties"]
            return {"acknowledged": True}
        if method == "POST" and path == f"/{index.index_name}/_bulk":
            lines = payload.decode("utf-8").splitlines()
            for source in lines[1::2]:
                document = json.loads(source)
                assert "labels" not in document
                assert document["history"]["procedural_status"] == "passed" or (
                    document["history"]["procedural_status"] == "pending"
                )
                documents[document["pair_id"]] = document
            return {"errors": False, "items": [{"index": {"status": 201}} for _ in documents]}
        if method == "POST" and path == f"/{index.index_name}/_refresh":
            return {}
        if method == "GET" and path == f"/{index.index_name}/_count":
            return {"count": len(documents)}
        raise AssertionError(f"Unexpected OpenSearch request: {method} {path}")

    monkeypatch.setattr(index, "_request", fake_request)
    index.index_base(base, a_text_version="incident-a-v2")
    assert documents["pair-valid"]["effect_validated"] is False
    assert documents["pair-valid"]["b_procedural_status"] == "passed"
    assert documents["pair-valid"]["review_status"] == (
        knowledge.BLIND_LIVE_REVIEW_BYPASSED_STATUS
    )
    assert documents["pair-not-valid"]["b_procedural_status"] == "pending"


def test_v4_search_filters_on_procedural_status_not_effect_validation(monkeypatch):
    base = procedural_qwen35_base()
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V4,
    )
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    document = {
        "pair_id": "pair-valid",
        "source_run_id": "train-pair-valid",
        "effect_validated": False,
        "guide_status": "procedurally_validated",
        "causal_status": "not_assessed",
        "b_procedural_status": "passed",
        "history": base["pairs"][0]["retrieval_history"],
    }
    requests = []

    def fake_request(method, path, payload=None, **kwargs):
        requests.append(payload)
        return {
            "hits": {
                "hits": [{"_id": "pair-valid", "_score": 0.5, "_source": document}]
            }
        }

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        "restart user", np.asarray([1.0, 0.0], dtype=np.float32),
        source_run_id="test-run", top_k=1,
        include_unvalidated_experimental=False, min_score=0,
    )
    assert [item["pair_id"] for item in results] == ["pair-valid"]
    for request in requests:
        filters = (
            request["query"]["knn"]["a_vector"]["filter"]["bool"]["filter"]
            if "knn" in request["query"]
            else request["query"]["bool"]["filter"]
        )
        assert {"term": {"guide_status": "procedurally_validated"}} in filters
        assert {"term": {"b_procedural_status": "passed"}} in filters
        assert not any("effect_validated" in item.get("term", {}) for item in filters)


def test_v4_exploratory_search_includes_pending_guides_without_labels(monkeypatch):
    base = procedural_qwen35_base()
    index = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200",
        base["sha256"],
        retriever_version=knowledge.OPENSEARCH_RETRIEVER_V4,
    )
    index.document_count = 2
    index._index_metadata = {"embedding_dim": 2}
    pair = base["pairs"][1]
    document = {
        "pair_id": pair["pair_id"],
        "source_run_id": pair["source_run_id"],
        "effect_validated": pair["effect_validated"],
        "guide_status": pair["guide_status"],
        "causal_status": pair["causal_status"],
        "b_procedural_status": pair["b_procedural_status"],
        "history": pair["retrieval_history"],
    }
    requests = []

    def fake_request(method, path, payload=None, **kwargs):
        requests.append(payload)
        return {
            "hits": {
                "hits": [
                    {"_id": pair["pair_id"], "_score": 0.5, "_source": document}
                ]
            }
        }

    monkeypatch.setattr(index, "_request", fake_request)
    results = index.search(
        "restart user",
        np.asarray([1.0, 0.0], dtype=np.float32),
        source_run_id="test-run",
        top_k=1,
        include_unvalidated_experimental=False,
        include_unvalidated_procedural_experimental=True,
        min_score=0,
    )

    assert [item["pair_id"] for item in results] == [pair["pair_id"]]
    assert results[0]["b_procedural_status"] == "pending"
    assert "labels" not in results[0]["history"]
    for request in requests:
        assert "procedurally_validated" in json.dumps(request)
        assert "procedural_pending" in json.dumps(request)
        assert '"passed"' in json.dumps(request)
        assert '"pending"' in json.dumps(request)

    with pytest.raises(OpenSearchError, match="outside its procedural policy"):
        index.search(
            "restart user",
            np.asarray([1.0, 0.0], dtype=np.float32),
            source_run_id="test-run",
            top_k=1,
            include_unvalidated_experimental=False,
            min_score=0,
        )
