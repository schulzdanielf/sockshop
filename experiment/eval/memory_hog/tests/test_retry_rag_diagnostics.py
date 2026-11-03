import copy
import json

import pytest

from experiment.eval.memory_hog import retry_rag_diagnostics as retry
from experiment.platform.backend import rca_knowledge as knowledge
from experiment.platform.tests.test_rca_knowledge import Model, investigate


def test_model_migration_preserves_source_and_does_not_weaken_loader(
    tmp_path, monkeypatch
):
    investigate(tmp_path, model=Model())
    store = knowledge.KnowledgeStore(tmp_path, "corpus")
    source = store.freeze(["train"])
    source["mode"] = "exploratory"
    source["model"] = "old-model"
    for pair in source["pairs"]:
        pair.update(
            guide_status="pending",
            causal_status="inconclusive",
            review_status="assistant_label_consistency_audited",
        )
        pair["b"]["diagnosis"]["status"] = "inconclusive"
        pair["labels"]["causal_category"] = pair["b"]["diagnosis"]["category"]
        pair["sha256"] = knowledge.digest(
            {k: v for k, v in pair.items() if k != "sha256"}
        )
    source["sha256"] = knowledge.digest(
        {k: v for k, v in source.items() if k != "sha256"}
    )
    original = copy.deepcopy(source)
    monkeypatch.setattr(knowledge, "DEFAULT_LLM_MODEL", "new-model")
    monkeypatch.setattr(retry, "DEFAULT_LLM_MODEL", "new-model")
    derived_store = knowledge.KnowledgeStore(tmp_path, "retry")
    derived = retry.derive_base(source, source["sha256"], derived_store)
    assert source == original
    assert derived["model"] == "new-model"
    assert derived["sha256"] != source["sha256"]
    assert derived["pairs"] == source["pairs"]
    assert derived["index"] == source["index"]
    assert derived_store.load_exploratory_base() == derived
    monkeypatch.setattr(knowledge, "DEFAULT_LLM_MODEL", "another-model")
    with pytest.raises(ValueError, match="mismatch"):
        derived_store.load_exploratory_base()


def test_migration_rejects_modified_source(tmp_path):
    source = {"model": "old", "pairs": [], "sha256": "bad"}
    with pytest.raises(ValueError, match="integrity"):
        retry.derive_base(source, "bad", knowledge.KnowledgeStore(tmp_path, "retry"))


def test_selection_only_completed_compatibility_errors(tmp_path):
    rows = []
    for cell, state, status, error in (
        ("eligible", "completed", "error", retry.MISMATCH),
        ("aborted", "aborted", "error", retry.MISMATCH),
        ("success", "completed", "inconclusive", ""),
        ("different", "completed", "error", "timeout"),
    ):
        rows.append({"cell_id": cell, "run_status": state, "run_id": cell})
        path = tmp_path / "rag_results" / cell / "exploratory_rag.json"
        knowledge.write_once(
            path, {"run_id": cell, "response": {"status": status, "error": error}}
        )
    assert retry.failed_cases(tmp_path, {"per_run": rows}) == [rows[0]]


def test_retry_evaluation_preserves_original_and_missing_denominators(tmp_path):
    missing = retry._score({}, "payment", "cpu-hog")
    row = {
        "run_id": "run-one",
        "run_status": "completed",
        "ground_truth_service": "payment",
        "chaos_type": "cpu-hog",
        "effect_validated": False,
        "methods": {method: copy.deepcopy(missing) for method in retry.METHODS},
    }
    original = {"campaign_id": "campaign", "per_run": [row]}
    before = copy.deepcopy(original)
    knowledge.write_once(
        tmp_path / "results" / "run-one.json",
        {
            "run_id": "run-one",
            "response": {
                "status": "inconclusive",
                "base_sha256": "derived",
                "diagnosis": {"primary_service": "payment", "fault_type": "cpu-hog"},
                "retrieved": [{"pair_id": "training-pair"}],
            },
        },
    )
    report = retry.evaluate_retries(original, [row], tmp_path, {"sha256": "derived"})
    assert original == before
    assert report["retry_summary"]["exploratory_rag"]["joint_correct"] == 1
    assert report["retry_summary"]["original"]["statuses"] == {"missing": 1}
    assert report["retry_count"] == 1
    assert report["per_run"][0]["retry_retrieval"]["retrieved"] == [
        {"pair_id": "training-pair"}
    ]
    assert report["retry_by_effect"]["true"]["original"]["denominator"] == 0
    assert json.dumps(report)


def test_runner_is_diagnostic_only_immutable_and_resumable(tmp_path, monkeypatch):
    campaign = tmp_path / "campaign"
    output = campaign / "diagnostic_retries" / "attempt-one"
    failed = {
        "run_id": "run-one",
        "response": {
            "status": "error",
            "error": retry.MISMATCH,
        },
    }
    row = {
        "cell_id": "test-cell",
        "run_id": "run-one",
        "run_status": "completed",
        "ground_truth_service": "payment",
        "chaos_type": "cpu-hog",
        "effect_validated": True,
        "methods": {
            method: retry._score({}, "payment", "cpu-hog") for method in retry.METHODS
        },
    }
    phases = {
        phase: {"start": "2026-10-29T10:00:00Z", "end": "2026-10-29T10:01:00Z"}
        for phase in ("baseline", "fault", "post")
    }
    for path, data in (
        (
            "config.yaml",
            {
                "experiment_campaign_id": "campaign",
                "knowledge_campaign": {"exploratory": True, "corpus_id": "original"},
            },
        ),
        ("rag_base.json", {"sha256": "source"}),
        ("source-base.json", {"sha256": "source", "model": "old"}),
        (
            "exploratory_rag_evaluation.json",
            {"campaign_id": "campaign", "per_run": [row]},
        ),
        ("rag_results/test-cell/exploratory_rag.json", failed),
        (
            "attempts/test-cell/run.json",
            {
                "run_id": "run-one",
                "status": "completed",
                "timings": {
                    "controlled_observation_window": {
                        "phases": phases,
                        "sampling_interval_seconds": 30,
                    }
                },
            },
        ),
        (
            "attempts/test-cell/spec.json",
            {
                "scope": {"namespace": "sock-shop"},
                "observability": {
                    "metrics": {"mcp_sse_url": "http://127.0.0.1:18080/sse"}
                },
            },
        ),
    ):
        knowledge.write_once(campaign / path, data)
    originals = {p: p.read_bytes() for p in campaign.rglob("*") if p.is_file()}
    base = {"sha256": "derived"}
    calls = []

    def diagnostic(**kwargs):
        assert kwargs["run_id"] == "run-one"
        assert kwargs["phases"] == phases
        assert kwargs["namespace"] == "sock-shop"
        assert kwargs.get("labels") is None
        assert kwargs["options"].mode == "exploratory_rag"
        assert kwargs["options"].corpus_id != "original"
        calls.append(kwargs)
        return {
            "status": "inconclusive",
            "base_sha256": "derived",
            "diagnosis": {"primary_service": "payment", "fault_type": "cpu-hog"},
        }

    monkeypatch.setattr(retry, "DEFAULT_LLM_MODEL", "new")
    monkeypatch.setattr(retry, "llm_health", lambda: {"ok": True, "model": "new"})
    monkeypatch.setattr(retry, "derive_base", lambda *args: base)
    monkeypatch.setattr(
        knowledge.KnowledgeStore, "load_exploratory_base", lambda self: base
    )
    monkeypatch.setattr(
        retry,
        "options_for",
        lambda *_args: {
            "corpus_id": "original",
            "mode": "exploratory_rag",
        },
    )
    monkeypatch.setattr(retry, "MCPToolClient", lambda url, timeout: object())
    monkeypatch.setattr(retry, "investigate", diagnostic)
    assert retry.run(campaign, output, campaign / "source-base.json") == 0
    result_bytes = (output / "results" / "run-one.json").read_bytes()
    report_bytes = (output / "evaluation.json").read_bytes()
    assert retry.run(campaign, output, campaign / "source-base.json") == 0
    assert len(calls) == 1
    assert (output / "results" / "run-one.json").read_bytes() == result_bytes
    assert (output / "evaluation.json").read_bytes() == report_bytes
    assert all(path.read_bytes() == content for path, content in originals.items())
    (campaign / "config.yaml").write_text((campaign / "config.yaml").read_text() + "\n")
    with pytest.raises(ValueError, match="immutable record already exists"):
        retry.run(campaign, output, campaign / "source-base.json")
