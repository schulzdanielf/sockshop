from __future__ import annotations

import json

import numpy as np
import pytest

from experiment.eval.memory_hog import opensearch_experiments
from experiment.eval.memory_hog.opensearch_experiments import (
    OpenSearchError,
    OpenSearchExperimentIndex,
)


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
