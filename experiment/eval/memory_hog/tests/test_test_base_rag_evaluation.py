import json

import pytest
import yaml

from experiment.eval.memory_hog import test_base_rag_evaluation as evaluation
from experiment.platform.backend import rca_knowledge as knowledge


@pytest.mark.parametrize(
    "version", [knowledge.OPENSEARCH_RETRIEVER_V6, knowledge.OPENSEARCH_RETRIEVER_V5]
)
def test_new_evaluation_pins_retriever_without_modifying_source(
    tmp_path, monkeypatch, version
):
    source = tmp_path / "source"
    source.mkdir()
    cells = [
        {"cell_id": f"cell-{i}", "service": "orders", "chaos_type": "cpu-hog"}
        for i in range(32)
    ]
    source_config = {
        "test_experiments": cells,
        "platform": {},
        "knowledge_campaign": {"retriever_version": knowledge.OPENSEARCH_RETRIEVER_V4},
        "output": {},
    }
    (source / "config.yaml").write_text(yaml.safe_dump(source_config), encoding="utf-8")
    (source / "ledger.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"cell_id": c["cell_id"], "run_id": f"test-{i}", "status": "completed"}
            )
            for i, c in enumerate(cells)
        ),
        encoding="utf-8",
    )
    base = {
        "sha256": "a" * 64,
        "skill_sha256": "b" * 64,
        "model": knowledge.QWEN35_MODEL_ID,
        "training_run_ids": ["train"],
        "pairs": [
            {
                "pair_id": "passed",
                "guide_status": "procedurally_validated",
                "b_procedural_status": "passed",
            },
            {
                "pair_id": "pending",
                "guide_status": "procedural_pending",
                "b_procedural_status": "pending",
            },
        ],
    }

    def load(corpus_id, *, retriever_version):
        assert retriever_version == version
        return base, {"eligible_pair_count": 1, "retriever_version": version}

    monkeypatch.setattr(evaluation, "_load_v4_corpus", load)
    kwargs = (
        {}
        if version == knowledge.OPENSEARCH_RETRIEVER_V6
        else {"retriever_version": version}
    )
    directory = evaluation.prepare_evaluation(
        source_campaign=source,
        corpus_id="corpus",
        output_root=tmp_path / "out",
        **kwargs,
    )
    prepared = yaml.safe_load((directory / "config.yaml").read_text())
    config = prepared["knowledge_campaign"]
    assert config["retriever_version"] == version
    assert config["min_score"] == (
        0 if version == knowledge.OPENSEARCH_RETRIEVER_V6 else 0.05
    )
    assert config["include_unvalidated_procedural_experimental"] is False
    assert config["opensearch_a_text_version"] == "incident-a-v2"
    assert config["external_test_evaluation_id"] == directory.name
    assert config["diagnostic_scope"] == "skill_and_rag"
    assert config["diagnostic_methods"] == ["original", "exploratory_rag"]
    assert config["model"] == knowledge.QWEN35_MODEL_ID
    assert config["temperature"] == evaluation.EVALUATION_TEMPERATURE
    assert config["seed"] == evaluation.EVALUATION_SEED
    options = evaluation.rag_campaign.options_for(prepared, "exploratory_rag")
    assert "diagnostic_scope" not in options
    assert "diagnostic_methods" not in options
    assert options["temperature"] == evaluation.EVALUATION_TEMPERATURE
    assert options["seed"] == evaluation.EVALUATION_SEED
    skill_options = evaluation.rag_campaign.options_for(prepared, "original")
    assert skill_options["model"] == knowledge.QWEN35_MODEL_ID
    assert skill_options["temperature"] == evaluation.EVALUATION_TEMPERATURE
    assert skill_options["seed"] == evaluation.EVALUATION_SEED
    assert prepared["historical_observability"]["required"] is True
    assert yaml.safe_load((source / "config.yaml").read_text()) == source_config


def test_precollection_queries_are_namespace_wide_and_label_independent():
    window = {"start": "2026-11-06T00:00:00Z", "end": "2026-11-06T01:00:00Z"}

    queries = evaluation._queries_for_window(window)

    assert [source for _, source, _ in queries] == [
        "metrics",
        "logs",
        "traces",
    ]
    for _, _, arguments in queries:
        assert arguments["start"] == window["start"]
        assert arguments["end"] == window["end"]
        assert not {
            "service_name",
            "container",
            "fault_type",
            "chaos_type",
        }.intersection(arguments)
    assert queries[1][2]["query"] == '{namespace="sock-shop"}'
    assert queries[2][2]["query"] == ('{ resource.k8s.namespace.name = "sock-shop" }')


def test_rag_only_configuration_rejects_other_diagnostic_methods():
    config = {
        "knowledge_campaign": {
            "diagnostic_scope": "rag_only",
            "diagnostic_methods": ["exploratory_rag"],
        }
    }

    assert evaluation._diagnostic_methods(config) == ("exploratory_rag",)
    config["knowledge_campaign"]["diagnostic_methods"] = [
        "original",
        "exploratory_rag",
    ]
    with pytest.raises(ValueError, match="RAG-only"):
        evaluation._diagnostic_methods(config)


def test_skill_and_rag_scope_requires_exact_method_pair():
    config = {
        "knowledge_campaign": {
            "diagnostic_scope": "skill_and_rag",
            "diagnostic_methods": ["original", "exploratory_rag"],
        }
    }

    assert evaluation._diagnostic_methods(config) == ("original", "exploratory_rag")
    config["knowledge_campaign"]["diagnostic_methods"] = [
        "original",
        "exploratory_control",
        "exploratory_rag",
    ]
    with pytest.raises(ValueError, match="Skill-and-RAG"):
        evaluation._diagnostic_methods(config)


def test_generation_support_check_rejects_api_without_sampling_options():
    class Api:
        def get(self, path):
            assert path == "/openapi.json"
            return {
                "components": {
                    "schemas": {
                        "RCABenchmarkOptions": {
                            "properties": {"model": {}, "temperature": {}}
                        }
                    }
                }
            }

    with pytest.raises(RuntimeError, match="seed"):
        evaluation._validate_generation_support(Api())


def test_v4_pending_pairs_require_explicit_experimental_inclusion():
    validated = {
        "guide_status": "procedurally_validated",
        "b_procedural_status": "passed",
    }
    pending = {
        "guide_status": "procedural_pending",
        "b_procedural_status": "pending",
    }

    assert evaluation._eligible_v4_pair(validated)
    assert not evaluation._eligible_v4_pair(pending)
    assert evaluation._eligible_v4_pair(pending, include_unvalidated_procedural=True)


def test_v4_retrieval_marks_only_exact_service_and_fault_pairs():
    pairs = {
        "right": {
            "labels": {"service": "orders", "fault_type": "memory-hog"},
        },
        "wrong-fault": {
            "labels": {"service": "orders", "fault_type": "cpu-hog"},
        },
        "wrong-service": {
            "labels": {"service": "shipping", "fault_type": "memory-hog"},
        },
    }

    row = evaluation._retrieval_row(
        [
            {"pair_id": "wrong-fault"},
            {"pair_id": "right"},
            {"pair_id": "wrong-service"},
        ],
        {"right"},
        pairs,
    )

    assert row["hit_at_3"] is True
    assert row["relevant_pair_ids"] == ["right"]
    assert row["retrieved_pairs"][1]["relevant"] is True
    assert row["first_relevant_rank"] == 2
    assert row["reciprocal_rank_at_3"] == pytest.approx(0.5)
    assert row["precision_at_3"] == pytest.approx(1 / 3)


def test_v4_retrieval_rejects_unknown_or_duplicate_pair_ids():
    with pytest.raises(ValueError, match="outside the frozen"):
        evaluation._retrieval_row([{"pair_id": "unknown"}], set(), {})
    with pytest.raises(ValueError, match="duplicate"):
        evaluation._retrieval_row(
            [{"pair_id": "pair"}, {"pair_id": "pair"}],
            set(),
            {"pair": {}},
        )


def test_diagnosis_accuracy_is_conditioned_on_relevant_top3_retrieval():
    rows = [
        {
            "retrieval": {"hit_at_3": True},
            "methods": {"exploratory_rag": {"joint_correct": True}},
        },
        {
            "retrieval": {"hit_at_3": True},
            "methods": {"exploratory_rag": {"joint_correct": False}},
        },
        {
            "retrieval": {"hit_at_3": False},
            "methods": {"exploratory_rag": {"joint_correct": True}},
        },
    ]

    result = evaluation._conditional_diagnosis_metrics(rows)

    assert result["with_relevant_retrieval"] == {
        "denominator": 2,
        "jointly_correct": 1,
        "joint_accuracy": 0.5,
    }
    assert result["without_relevant_retrieval"]["joint_accuracy"] == 1
    assert result["overall"]["joint_accuracy"] == pytest.approx(2 / 3)
