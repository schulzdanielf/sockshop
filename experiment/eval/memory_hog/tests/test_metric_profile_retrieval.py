from __future__ import annotations

import numpy as np
import pytest

from experiment.eval.memory_hog.metric_profile_benchmark import audit_inputs, score
from experiment.eval.memory_hog.metric_profile_retrieval import (
    PROFILE_METHODS,
    MetricProfileIndex,
    ProfileCandidate,
    fuse_rankings,
)
from experiment.eval.memory_hog.opensearch_experiments import (
    OpenSearchError,
    OpenSearchExperimentIndex,
)
from experiment.platform.backend.rca_knowledge import OPENSEARCH_RETRIEVER_V6
from experiment.platform.backend.rca_knowledge import IncidentA, MetricFact


def incident(changes: dict[str, float], multiplier: float = 1.0) -> IncidentA:
    facts = [
        MetricFact(
            evidence_id="E1",
            source="history",
            source_path="/digest/metrics/cpu/top_delta/0",
            service=service,
            metric="cpu_saturation_pct",
            unit="pct",
            baseline_value=10 * multiplier,
            fault_value=(10 + delta) * multiplier,
            delta=delta * multiplier,
            baseline_window={
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:01:00Z",
            },
            fault_window={
                "start": "2026-01-01T00:02:00Z",
                "end": "2026-01-01T00:03:00Z",
            },
            baseline_samples=10,
            fault_samples=10,
            status="ok",
        )
        for service, delta in changes.items()
    ]
    return IncidentA(
        observed_services=list(changes) or ["orders"],
        service_relations=[],
        symptoms=[{"description": "Observed metric change", "evidence_ids": ["E1"]}],
        metric_changes=[],
        log_patterns=[],
        trace_patterns=[],
        temporal_sequence=[],
        preliminary_hypotheses=[],
        limitations=[],
        text_version="incident-a-v2",
        metric_facts=facts,
    )


def index(multiplier: float = 1.0) -> MetricProfileIndex:
    return MetricProfileIndex(
        [
            ProfileCandidate("a", "run-a", incident({"carts": 1}, multiplier)),
            ProfileCandidate("b", "run-b", incident({"orders": 20}, multiplier)),
            ProfileCandidate("c", "run-c", incident({"carts": 2}, multiplier)),
        ]
    )


def test_quantitative_route_keeps_service_metric_association():
    retriever = index()
    query = incident({"orders": 40})
    assert retriever.strength(query) == 20
    assert (
        retriever.rank(query, source_run_id="test", allowed_pair_ids={"a", "b", "c"})[0]
        == "b"
    )
    assert (
        retriever.rerank(
            ["a", "c", "b"],
            query,
            source_run_id="test",
            allowed_pair_ids={"a", "b", "c"},
        )[0]
        == "b"
    )


def test_weak_signal_retains_lexical_order_exactly():
    retriever = index()
    assert retriever.rerank(
        ["c", "a", "b"],
        incident({"orders": 1}),
        source_run_id="test",
        allowed_pair_ids={"a", "b", "c"},
    ) == ["c", "a", "b"]


@pytest.mark.parametrize("changes", [{}, {"orders": 0}, {"orders": -8}])
def test_absent_or_nonpositive_signal_does_not_override_lexical(changes):
    assert index().rerank(
        ["a", "b"], incident(changes), source_run_id="test", allowed_pair_ids={"a", "b"}
    ) == ["a", "b"]


def test_eligibility_and_self_exclusion_apply_to_numeric_route():
    retriever = index()
    query = incident({"orders": 40})
    assert "b" not in retriever.rank(
        query, source_run_id="test", allowed_pair_ids={"a", "c"}
    )
    assert "b" not in retriever.rank(
        query, source_run_id="run-b", allowed_pair_ids={"a", "b", "c"}
    )
    with pytest.raises(ValueError, match="eligibility or exclusion"):
        retriever.rerank(
            ["b", "a"], query, source_run_id="run-b", allowed_pair_ids={"a", "b"}
        )


@pytest.mark.parametrize("method", PROFILE_METHODS)
def test_profile_is_invariant_to_consistent_unit_rescaling(method):
    original = index()
    scaled = index(1000)
    query = incident({"orders": 40, "carts": 1})
    scaled_query = incident({"orders": 40, "carts": 1}, 1000)
    np.testing.assert_allclose(
        original.profile(query, method), scaled.profile(scaled_query, method)
    )
    assert original.strength(query) == scaled.strength(scaled_query)


def test_unseen_feature_does_not_trigger_numeric_route():
    retriever = index()
    assert retriever.strength(incident({"new-service": 100})) == 0


def test_duplicate_facts_and_candidate_ids_are_rejected():
    query = incident({"orders": 10})
    query.metric_facts.append(query.metric_facts[0])
    with pytest.raises(ValueError, match="Duplicate metric profile fact"):
        MetricProfileIndex([ProfileCandidate("a", "run-a", query)])
    with pytest.raises(ValueError, match="unique IDs"):
        MetricProfileIndex(
            [
                ProfileCandidate("a", "run-a", incident({"orders": 1})),
                ProfileCandidate("a", "run-b", incident({"orders": 2})),
            ]
        )


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_route_configuration_fails_explicitly(value):
    with pytest.raises(ValueError):
        index().rerank(
            ["a"],
            incident({"orders": 1}),
            source_run_id="test",
            allowed_pair_ids={"a"},
            threshold=value,
        )
    with pytest.raises(ValueError):
        fuse_rankings(["a"], ["b"], value)


def test_rrf_zero_weights_do_not_inject_other_route_candidates():
    assert fuse_rankings(["a"], ["b"], 0) == ["a"]
    assert fuse_rankings(["a"], ["b"], 1) == ["b"]


def test_scoring_distinguishes_coverage_from_retrieval():
    rows = [
        {"targets": ["b"], "rankings": {"test": ["a", "b"]}},
        {"targets": [], "rankings": {"test": ["a"]}},
    ]
    result = score(rows, "test")
    assert result["n"] == 2
    assert result["covered"] == 1
    assert result["hit_at_3"] == 1
    assert result["hit_at_3_covered"] == 1
    assert result["mrr"] == 0.25
    assert result["mrr_covered"] == 0.5


def test_unit_mismatch_does_not_match_a_metric_feature():
    query = incident({"orders": 40})
    query.metric_facts[0].unit = "incompatible-unit"
    retriever = index()
    assert retriever.strength(query) == 0
    assert (
        retriever.rank(query, source_run_id="test", allowed_pair_ids={"a", "b", "c"})
        == []
    )


def test_fitting_is_corpus_only_and_query_narrative_is_not_numeric_input():
    retriever = index()
    scales = dict(retriever.scales)
    query = incident({"orders": 40})
    original = retriever.profile(query, "positive")
    query.preliminary_hypotheses = ["Different hypothetical diagnosis"]
    query.limitations = ["Different narrative"]
    np.testing.assert_array_equal(original, retriever.profile(query, "positive"))
    assert retriever.scales == scales


def test_temporal_overlap_is_rejected_even_with_different_run_ids():
    with pytest.raises(ValueError, match="overlap"):
        audit_inputs(
            [ProfileCandidate("train", "run-train", incident({"orders": 1}))],
            [ProfileCandidate("test", "run-test", incident({"orders": 40}))],
        )


def v6_search(monkeypatch, *, pending_ids=(), source_run_id="test", changes=None):
    candidates = index().candidates
    pairs = [
        {
            "pair_id": candidate.pair_id,
            "source_run_id": candidate.source_run_id,
            "a": candidate.incident.model_dump(),
            "labels": {
                "service": "private-label-canary",
                "fault_type": "private-label-canary",
            },
            "b": {"diagnosis": "private-label-canary"},
        }
        for candidate in candidates
    ]
    sources = {
        c.pair_id: {
            "pair_id": c.pair_id,
            "source_run_id": c.source_run_id,
            "effect_validated": False,
            "causal_status": "not_assessed",
            "guide_status": (
                "procedural_pending"
                if c.pair_id in pending_ids
                else "procedurally_validated"
            ),
            "b_procedural_status": "pending" if c.pair_id in pending_ids else "passed",
            "history": {
                "pair_id": c.pair_id,
                "historical_status": (
                    "procedural_pending"
                    if c.pair_id in pending_ids
                    else "procedurally_validated"
                ),
                "procedural_status": (
                    "pending" if c.pair_id in pending_ids else "passed"
                ),
                **({} if c.pair_id in pending_ids else {"validated_outcome": {}}),
            },
        }
        for c in candidates
    }
    requests = []
    retriever = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200", "a" * 64, retriever_version=OPENSEARCH_RETRIEVER_V6
    )
    retriever.document_count = 3
    retriever._index_metadata = {"embedding_dim": 256}

    def request(method, path, payload=None):
        requests.append(payload)
        body = payload["query"]["bool"]
        assert body["must_not"] == [{"term": {"source_run_id": source_run_id}}]
        assert body["filter"] == [
            {"term": {"guide_status": "procedurally_validated"}},
            {"term": {"b_procedural_status": "passed"}},
        ]
        assert "private-label-canary" not in str(payload)
        order = ["a", "c", "b"] if "must" in body else ["a", "b", "c"]
        return {
            "hits": {
                "hits": [
                    {"_id": pid, "_score": float(3 - rank), "_source": sources[pid]}
                    for rank, pid in enumerate(order)
                    if pid not in pending_ids
                    and sources[pid]["source_run_id"] != source_run_id
                ]
            }
        }

    monkeypatch.setattr(retriever, "_request", request)
    query = incident({"orders": 40} if changes is None else changes)
    hits = retriever.search(
        query.retrieval_text(),
        None,
        source_run_id=source_run_id,
        top_k=3,
        include_unvalidated_experimental=False,
        min_score=0,
        query_incident=query,
        corpus_pairs=pairs,
    )
    return retriever, requests, hits


def test_integrated_v6_activates_numeric_route_and_keeps_labels_out(monkeypatch):
    retriever, requests, hits = v6_search(monkeypatch)
    assert retriever.schema_version == 4
    assert len(requests) == 2
    assert [h["pair_id"] for h in hits] == ["b", "a", "c"]
    assert hits[0]["score_components"] == {
        "dense_rank": None,
        "bm25_rank": 3,
        "numeric_rank": 1,
    }
    assert all(0 <= h["score"] <= 1 for h in hits)
    assert retriever.search_metadata["numeric_route_active"] is True
    assert retriever.search_metadata["numeric_strength"] == 20


def test_integrated_v6_weak_signal_preserves_bm25_scores(monkeypatch):
    retriever, requests, hits = v6_search(monkeypatch, changes={"orders": 1})
    assert len(requests) == 1
    assert [h["pair_id"] for h in hits] == ["a", "c", "b"]
    assert [h["score"] for h in hits] == [1, 0.666667, 0.333333]
    assert retriever.search_metadata["numeric_route_active"] is False


def test_integrated_v6_filters_pending_before_numeric_fusion(monkeypatch):
    _, _, hits = v6_search(monkeypatch, pending_ids={"b"})
    assert "b" not in [h["pair_id"] for h in hits]


def test_integrated_v6_excludes_query_source_from_both_routes(monkeypatch):
    _, _, hits = v6_search(monkeypatch, source_run_id="run-b")
    assert "b" not in [h["pair_id"] for h in hits]


def test_integrated_v6_rejects_missing_corpus(monkeypatch):
    retriever = OpenSearchExperimentIndex(
        "http://127.0.0.1:9200", "a" * 64, retriever_version=OPENSEARCH_RETRIEVER_V6
    )
    retriever.document_count = 3
    retriever._index_metadata = {"embedding_dim": 256}
    with pytest.raises(OpenSearchError, match="frozen A corpus"):
        retriever.search(
            "orders",
            None,
            source_run_id="test",
            top_k=3,
            include_unvalidated_experimental=False,
        )
