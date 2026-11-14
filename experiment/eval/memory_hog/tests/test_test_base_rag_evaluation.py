import json
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from experiment.eval.memory_hog import test_base_rag_evaluation as evaluation
from experiment.platform.backend import rca_knowledge as knowledge


@pytest.mark.parametrize("pair_count", [32, 35])
def test_v4_corpus_pair_count_uses_manifest_with_legacy_32_default(pair_count):
    base = {"pairs": [{} for _ in range(pair_count)], "b_rebuild_version": "v4"}
    manifest = {} if pair_count == 32 else {"source_pair_count": pair_count}

    evaluation._validate_v4_pair_count(base, manifest)


def test_v4_corpus_pair_count_rejects_manifest_mismatch():
    with pytest.raises(ValueError, match="pair count differs"):
        evaluation._validate_v4_pair_count(
            {"pairs": [{} for _ in range(35)], "b_rebuild_version": "v4"},
            {"source_pair_count": 32},
        )


def _write_supplemental_test_cohort(tmp_path, monkeypatch):
    parent_path = tmp_path / "parent-test-cohort.json"
    parent_runs = [
        {
            "cell": {"cell_id": f"parent-{i}", "service": "orders"},
            "run_id": f"test-{i}",
            "status": "completed",
        }
        for i in range(32)
    ]
    parent_path.write_text(
        json.dumps({"runs": parent_runs}), encoding="utf-8"
    )
    parent_sha256 = evaluation._file_sha256(parent_path)
    monkeypatch.setattr(
        evaluation.supplemental_campaign, "REFERENCE_TEST_COHORT", parent_path
    )
    monkeypatch.setattr(
        evaluation.supplemental_campaign, "REFERENCE_TEST_SHA256", parent_sha256
    )

    training_run_ids = [f"train-{i}" for i in range(35)]
    object_store = tmp_path / "object_store"
    installed_base = (
        object_store
        / ".rca_knowledge"
        / "corpus"
        / "exploratory_base.json"
    )
    installed_base.parent.mkdir(parents=True)
    installed_base.write_text("frozen test base", encoding="utf-8")
    monkeypatch.setattr(evaluation, "OBJECT_STORE", object_store)

    supplemental_runs = []
    for index, case in enumerate(
        case
        for case in evaluation.supplemental_campaign.CASES
        if case["phase"] == "test"
    ):
        cell = {
            key: value for key, value in case.items() if key != "previous_run_ids"
        }
        supplemental_runs.append(
            {
                "cell": cell,
                "run_id": f"supplemental-{index}",
                "status": "completed",
            }
        )
    data = {
        "protocol_version": evaluation.supplemental_campaign.PROTOCOL_VERSION,
        "parent_test_cohort_sha256": parent_sha256,
        "training_base_sha256": evaluation._file_sha256(installed_base),
        "test_run_ids": [
            run["run_id"] for run in parent_runs + supplemental_runs
        ],
        "training_run_ids": training_run_ids,
        "runs": parent_runs + supplemental_runs,
    }
    source = tmp_path / "supplemental" / "derived_test_source_cohort.json"
    source.parent.mkdir()
    source.write_text(json.dumps(data), encoding="utf-8")
    return source, training_run_ids


def test_supplemental_test_cohort_validates_parent_and_three_new_cases(
    tmp_path, monkeypatch
):
    path, training_run_ids = _write_supplemental_test_cohort(tmp_path, monkeypatch)

    cohort = evaluation._load_supplemental_test_cohort(path)

    assert len(cohort["runs"]) == 35
    assert cohort["training_run_ids"] == training_run_ids
    assert len(cohort["supplemental_test_run_ids"]) == 3
    assert cohort["source_cohort_sha256"] == evaluation._file_sha256(path)


def test_supplemental_test_cohort_rejects_changed_parent_run(tmp_path, monkeypatch):
    path, _ = _write_supplemental_test_cohort(tmp_path, monkeypatch)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["runs"][0]["run_id"] = "changed-parent-run"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="preserve all 32 parent runs"):
        evaluation._load_supplemental_test_cohort(path)


def test_supplemental_test_cohort_prepares_all_35_runs_without_inference(
    tmp_path, monkeypatch
):
    cohort_path, training_run_ids = _write_supplemental_test_cohort(
        tmp_path, monkeypatch
    )
    source = tmp_path / "source"
    source.mkdir()
    source_config = {
        "test_experiments": [],
        "platform": {},
        "knowledge_campaign": {},
        "output": {},
    }
    (source / "config.yaml").write_text(
        yaml.safe_dump(source_config), encoding="utf-8"
    )
    base = {
        "sha256": "a" * 64,
        "skill_sha256": "b" * 64,
        "model": knowledge.QWEN35_MODEL_ID,
        "training_run_ids": training_run_ids,
        "pairs": [
            {
                "pair_id": f"validated-{i}",
                "guide_status": "procedurally_validated",
                "b_procedural_status": "passed",
            }
            for i in range(20)
        ]
        + [
            {
                "pair_id": f"pending-{i}",
                "guide_status": "procedural_pending",
                "b_procedural_status": "pending",
            }
            for i in range(15)
        ],
    }
    monkeypatch.setattr(
        evaluation,
        "_load_v4_corpus",
        lambda corpus_id, *, retriever_version: (
            base,
            {"eligible_pair_count": 20, "retriever_version": retriever_version},
        ),
    )

    directory = evaluation.prepare_evaluation(
        source_campaign=source,
        corpus_id="corpus",
        output_root=tmp_path / "out",
        test_cohort_path=cohort_path,
    )

    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    prepared_cohort = json.loads(
        (directory / "test_cohort.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (directory / "evaluation_manifest.json").read_text(encoding="utf-8")
    )
    assert len(config["test_experiments"]) == 35
    assert len(prepared_cohort["runs"]) == 35
    assert len(prepared_cohort["supplemental_test_run_ids"]) == 3
    assert manifest["test_case_count"] == 35
    assert manifest["previously_evaluated_parent_run_count"] == 32
    assert manifest["protocol"] == "supplemental_test_cohort_v1_exploratory"
    assert manifest["evidence_context_projection"] == (
        evaluation._evidence_context_projection_metadata()
    )


def test_v4_corpus_loader_uses_indexed_base_hash_for_rebuilt_corpus(
    tmp_path, monkeypatch
):
    corpus_id = "indexed-35"
    skill_sha256 = "b" * 64
    indexed_base_sha256 = "c" * 64
    base = {
        "sha256": indexed_base_sha256,
        "skill_sha256": skill_sha256,
        "model": knowledge.QWEN35_MODEL_ID,
        "b_rebuild_version": "v4",
        "training_run_ids": [f"train-{i}" for i in range(35)],
        "pairs": [{"pair_id": f"pair-{i}"} for i in range(35)],
    }
    object_store = tmp_path / "object_store"
    store_directory = object_store / ".rca_knowledge" / corpus_id
    store_directory.mkdir(parents=True)
    (store_directory / "exploratory_base.json").write_text(
        json.dumps({"skill_sha256": skill_sha256}), encoding="utf-8"
    )

    class FakeStore:
        directory = store_directory

        def load_exploratory_base(self, *, expected_skill_sha256, expected_model):
            assert expected_skill_sha256 == skill_sha256
            assert expected_model == knowledge.QWEN35_MODEL_ID
            return base

    index_record = {
        "index_name": "index-35",
        "base_sha256": indexed_base_sha256,
        "document_count": 35,
    }
    manifest_path = (
        tmp_path / "out" / "knowledge_rebuild" / corpus_id / "manifest.json"
    )
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "corpus_id": corpus_id,
                "base_sha256": "d" * 64,
                "indexed_base_sha256": indexed_base_sha256,
                "source_pair_count": 35,
                "index": index_record,
            }
        ),
        encoding="utf-8",
    )

    class FakeIndex:
        def __init__(self, _url, base_sha256, *, retriever_version):
            assert base_sha256 == indexed_base_sha256

        def connect_existing(self, *, expected_pair_ids, expected_a_text_version):
            assert len(expected_pair_ids) == 35
            assert expected_a_text_version == "incident-a-v2"
            return {"document_count": 35}

    monkeypatch.setattr(evaluation, "OBJECT_STORE", object_store)
    monkeypatch.setattr(evaluation, "HERE", tmp_path)
    monkeypatch.setattr(evaluation, "KnowledgeStore", lambda *_args: FakeStore())
    monkeypatch.setattr(
        evaluation, "load_skill", lambda: (None, None, skill_sha256)
    )
    monkeypatch.setattr(evaluation, "OpenSearchExperimentIndex", FakeIndex)

    loaded_base, metadata = evaluation._load_v4_corpus(corpus_id)

    assert loaded_base is base
    assert metadata["index_name"] == "index-35"
    assert metadata["live_index"]["document_count"] == 35


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
    assert skill_options["max_model_calls"] == 30
    assert skill_options["max_total_tokens"] == 300_000
    assert skill_options["timeout_seconds"] == 600
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


def test_factorial_schedule_is_reproducible_and_balanced():
    cohort = [
        {
            "cell": {"cell_id": f"cell-{i}"},
            "run_id": f"run-{i}",
        }
        for i in range(32)
    ]

    schedule = evaluation._factorial_schedule(cohort)

    assert schedule == evaluation._factorial_schedule(cohort)
    assert len(schedule) == 32
    assert (
        sum(row["policy_order"] == ["none", "cpu-memory-v1"] for row in schedule) == 16
    )
    assert all(
        set(row["policy_order"]) == {"none", "cpu-memory-v1"} for row in schedule
    )


def test_resource_pilot_sampling_is_balanced_and_reproducible():
    cohort = [
        {
            "cell": {
                "cell_id": f"{fault_type}-{index}",
                "chaos_type": fault_type,
            },
            "run_id": f"run-{fault_type}-{index}",
        }
        for fault_type in ("cpu-hog", "memory-hog", "http-status-code")
        for index in range(7 if fault_type != "http-status-code" else 2)
    ]

    sample, selection = evaluation._sample_resource_cohort(
        cohort, 3, seed=evaluation.FACTORIAL_SAMPLE_SEED
    )
    repeat, repeated_selection = evaluation._sample_resource_cohort(
        cohort, 3, seed=evaluation.FACTORIAL_SAMPLE_SEED
    )
    schedule = evaluation._factorial_schedule(sample)

    assert sample == repeat
    assert selection == repeated_selection
    assert len(sample) == 6
    assert {item["cell"]["chaos_type"] for item in sample} == {
        "cpu-hog",
        "memory-hog",
    }
    assert selection["population_by_type"] == {"cpu-hog": 7, "memory-hog": 7}
    assert len(selection["selected_run_ids_by_type"]["cpu-hog"]) == 3
    assert len(selection["selected_run_ids_by_type"]["memory-hog"]) == 3
    assert (
        sum(row["policy_order"] == ["none", "cpu-memory-v1"] for row in schedule) == 3
    )


def test_factorial_paired_contrast_and_holm_adjustment():
    control = [
        {
            "run_id": f"run-{i}",
            "cell_id": f"cell-{i}",
            "service": "orders",
            "fault_type": "cpu-hog",
            "methods": {"original": {"joint_correct": i == 0}},
        }
        for i in range(4)
    ]
    treatment = [
        {
            **row,
            "methods": {"original": {"joint_correct": i in {1, 2}}},
        }
        for i, row in enumerate(control)
    ]

    contrast = evaluation._paired_condition_contrast(control, treatment, "original")
    adjusted = evaluation._holm_adjust({"original": 0.01, "exploratory_rag": 0.04})

    assert contrast["improved"] == 2
    assert contrast["regressed"] == 1
    assert contrast["unchanged"] == 1
    assert contrast["accuracy_delta"] == 0.25
    assert contrast["bootstrap_95_percentile_ci"] is not None
    assert adjusted == {"original": 0.02, "exploratory_rag": 0.04}


def test_factorial_runner_scores_only_after_every_arm_is_sealed(tmp_path, monkeypatch):
    cohort = [
        {
            "cell": {
                "cell_id": f"cell-{i}",
                "service": "orders",
                "chaos_type": "cpu-hog",
            },
            "run_id": f"run-{i}",
        }
        for i in range(32)
    ]
    schedule = evaluation._factorial_schedule(cohort)
    schedule_path = tmp_path / "factorial_schedule.json"
    schedule_path.write_text(json.dumps(schedule))
    arms = {}
    for policy in evaluation.FACTORIAL_POLICIES:
        arm = tmp_path / "arms" / policy
        arm.mkdir(parents=True)
        config = {
            "experiment_campaign_id": f"eval-{policy}",
            "platform": {"api_base": "http://api", "user_role": "admin"},
            "knowledge_campaign": {
                "resource_signature_policy": policy,
                "retriever_version": knowledge.OPENSEARCH_RETRIEVER_V6,
                "diagnostic_methods": ["original", "exploratory_rag"],
                "diagnostic_scope": "skill_and_rag",
            },
        }
        (arm / "config.yaml").write_text(yaml.safe_dump(config))
        (arm / "test_cohort.json").write_text(
            json.dumps({"runs": cohort, "source_campaign_id": "source"})
        )
        arms[policy] = str(arm.relative_to(tmp_path))

    manifest = {
        "version": 1,
        "comparison_id": "comparison-test",
        "design": "paired_2x2_skill_rag_resource_signature",
        "policies": list(evaluation.FACTORIAL_POLICIES),
        "methods": ["original", "exploratory_rag"],
        "arms": arms,
        "schedule_sha256": evaluation._file_sha256(schedule_path),
        "analysis_plan": {
            "cohort_independence": "exploratory",
        },
    }
    manifest["sha256"] = knowledge.digest(manifest)
    (tmp_path / "factorial_manifest.json").write_text(json.dumps(manifest))
    precollection = {
        "status": "complete",
        "comparison_id": manifest["comparison_id"],
        "manifest_sha256": manifest["sha256"],
        "arms": {
            policy: {
                "evaluation_id": f"eval-{policy}",
                "manifest_sha256": "a" * 64,
                "query_count": 288,
            }
            for policy in evaluation.FACTORIAL_POLICIES
        },
    }
    precollection["sha256"] = knowledge.digest(precollection)
    (tmp_path / "factorial_precollection.json").write_text(json.dumps(precollection))

    events = []

    class Telemetry:
        def __init__(self, evaluation_id):
            self.evaluation_id = evaluation_id
            self.started_at = 0.0
            self.tracer = object()

        def update_progress(self, progress):
            pass

        def case_span(self, **kwargs):
            return nullcontext()

        def set_scoring(self):
            events.append(("telemetry_scoring", self.evaluation_id, None))

        def record_report(self, report):
            events.append(("telemetry_report", report["evaluation_id"], None))

        def complete(self, **kwargs):
            events.append(("telemetry_complete", self.evaluation_id, None))

        def fail(self):
            events.append(("telemetry_failed", self.evaluation_id, None))

        def flush_and_shutdown(self):
            events.append(("telemetry_flushed", self.evaluation_id, None))

    class Api:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/openapi.json":
                return {
                    "components": {
                        "schemas": {
                            "RCABenchmarkOptions": {
                                "properties": {
                                    "model": {},
                                    "temperature": {},
                                    "seed": {},
                                    "resource_signature_policy": {},
                                }
                            }
                        }
                    }
                }
            return {"status": "ok"}

    monkeypatch.setattr(evaluation, "ApiClient", Api)
    monkeypatch.setattr(
        evaluation,
        "create_evaluation_telemetry",
        lambda **kwargs: Telemetry(kwargs["evaluation_id"]),
    )
    monkeypatch.setattr(evaluation, "validate_precollection", lambda path: {})
    monkeypatch.setattr(
        evaluation.rag_campaign,
        "process_cell",
        lambda directory, config, cell, entry, api: events.append(
            ("process", Path(directory).name, cell["cell_id"])
        ),
    )

    def seal(directory, elapsed):
        events.append(("sealed", Path(directory).name, None))
        return {"status": "complete"}

    monkeypatch.setattr(evaluation, "_seal_factorial_arm", seal)

    def report(directory):
        assert sum(event[0] == "sealed" for event in events) == 2
        policy = Path(directory).name
        events.append(("scored", policy, None))
        rows = [
            {
                "run_id": item["run_id"],
                "cell_id": item["cell"]["cell_id"],
                "service": "orders",
                "fault_type": "cpu-hog",
                "methods": {
                    "original": {"joint_correct": False},
                    "exploratory_rag": {"joint_correct": True},
                },
                "predictions": {
                    "original": {"fault_type": "memory-hog"},
                    "exploratory_rag": {"fault_type": "cpu-hog"},
                },
            }
            for item in cohort
        ]
        return {
            "evaluation_id": f"eval-{policy}",
            "per_run": rows,
            "diagnosis_summary": {
                "original": {"joint_accuracy": 0.0},
                "exploratory_rag": {"joint_accuracy": 1.0},
            },
            "performance": {},
            "provenance": {"diagnostic_methods": ["original", "exploratory_rag"]},
        }

    monkeypatch.setattr(evaluation, "build_report", report)
    result = evaluation.run_factorial_comparison(tmp_path)

    assert result["status"] == "complete"
    assert sum(event[0] == "scored" for event in events) == 2
    assert events.index(("sealed", "none", None)) < events.index(
        ("scored", "none", None)
    )
    assert events.index(("sealed", "cpu-memory-v1", None)) < events.index(
        ("scored", "cpu-memory-v1", None)
    )
    sealed_indices = [i for i, event in enumerate(events) if event[0] == "sealed"]
    scoring_indices = [
        i for i, event in enumerate(events) if event[0] == "telemetry_scoring"
    ]
    assert len(scoring_indices) == 2
    assert min(scoring_indices) > max(sealed_indices)
    report_indices = [
        i for i, event in enumerate(events) if event[0] == "telemetry_report"
    ]
    scored_indices = [i for i, event in enumerate(events) if event[0] == "scored"]
    assert len(report_indices) == 2
    assert min(report_indices) > max(scored_indices)
    by_run = {}
    for event, policy, cell_id in events:
        if event == "process":
            by_run.setdefault(cell_id, []).append(policy)
    assert by_run == {row["cell_id"]: row["policy_order"] for row in schedule}


def test_prepare_factorial_comparison_pins_two_shared_prompt_arms(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    cells = [
        {
            "cell_id": f"cell-{i}",
            "service": "orders",
            "chaos_type": "cpu-hog" if i % 2 == 0 else "memory-hog",
        }
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
                {
                    "cell_id": cell["cell_id"],
                    "run_id": f"test-{index}",
                    "status": "completed",
                }
            )
            for index, cell in enumerate(cells)
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
    monkeypatch.setattr(
        evaluation,
        "_load_v4_corpus",
        lambda corpus_id, *, retriever_version: (
            base,
            {"eligible_pair_count": 1, "retriever_version": retriever_version},
        ),
    )

    comparison = evaluation.prepare_factorial_comparison(
        source_campaign=source,
        corpus_id="corpus",
        output_root=tmp_path / "out",
    )
    manifest = json.loads((comparison / "factorial_manifest.json").read_text())

    assert manifest["policies"] == ["none", "cpu-memory-v1"]
    assert manifest["analysis_plan"]["scoring_after_all_four_conditions_sealed"]
    assert manifest["evidence_context_projection"] == {
        "version": evaluation.SPLIT_PROJECTION_VERSION,
        "budget_characters": evaluation.MODEL_EVIDENCE_CONTEXT_CHARS,
        "summary_threshold_characters": (
            evaluation.MODEL_EVIDENCE_SUMMARY_THRESHOLD_CHARS
        ),
        "partition_unit": "evidence_id",
        "summarization": "neutral_per_partition",
        "summary_item_cache": "source_hash_per_evidence_id",
        "columnar_encoding": "lossless_nested_json_pointer",
        "summary_max_new_tokens": evaluation.EVIDENCE_SUMMARY_MAX_NEW_TOKENS,
        "numeric_claim_validation": "source_values_only_with_one_repair_then_redact",
    }
    for policy in manifest["policies"]:
        arm = comparison / manifest["arms"][policy]
        config = yaml.safe_load((arm / "config.yaml").read_text())
        assert config["knowledge_campaign"]["resource_signature_policy"] == policy
        assert config["knowledge_campaign"]["diagnostic_methods"] == [
            "original",
            "exploratory_rag",
        ]
    schedule = json.loads((comparison / "factorial_schedule.json").read_text())
    assert len(schedule) == 32

    sample_comparison = evaluation.prepare_factorial_comparison(
        source_campaign=source,
        corpus_id="corpus",
        output_root=tmp_path / "out-sample",
        resource_cases_per_type=3,
    )
    sample_manifest = json.loads(
        (sample_comparison / "factorial_manifest.json").read_text()
    )
    sample_schedule = json.loads(
        (sample_comparison / "factorial_schedule.json").read_text()
    )
    assert sample_manifest["cohort_selection"]["kind"] == (
        "balanced_resource_fault_pilot"
    )
    assert sample_manifest["cohort_selection"]["cases_per_type"] == 3
    assert sample_manifest["schedule_balanced_first_arm"] == 3
    assert len(sample_schedule) == 6
    for policy in sample_manifest["policies"]:
        arm = sample_comparison / sample_manifest["arms"][policy]
        cohort = json.loads((arm / "test_cohort.json").read_text())
        assert len(cohort["runs"]) == 6
        assert {item["cell"]["chaos_type"] for item in cohort["runs"]} == {
            "cpu-hog",
            "memory-hog",
        }


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
