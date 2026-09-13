from __future__ import annotations

from experiment.eval.memory_hog.runner import _compute_prediction_scores
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage


def test_run_retrieval_is_scoped_to_experiment_version(tmp_path):
    storage = SqliteStorage(tmp_path / "platform.db", tmp_path / "object_store")

    storage.upsert_run_features(
        "run_1",
        "exp-1",
        {
            "chaos_type": "latency",
            "verdict": "fault",
            "summary_text": "first summary",
            "tags": ["tag-a"],
            "is_training": True,
        },
        experiment_version=1,
    )
    storage.upsert_run_features(
        "run_2",
        "exp-1",
        {
            "chaos_type": "latency",
            "verdict": "fault",
            "summary_text": "second summary",
            "tags": ["tag-b"],
            "is_training": True,
        },
        experiment_version=2,
    )
    storage.upsert_run_features(
        "run_3",
        "exp-2",
        {
            "chaos_type": "latency",
            "verdict": "fault",
            "summary_text": "other campaign",
            "tags": ["tag-c"],
            "is_training": True,
        },
        experiment_version=1,
    )

    storage.upsert_run_embedding("run_1", "demo", 3, b"\x01\x00\x00")
    storage.upsert_run_embedding("run_2", "demo", 3, b"\x02\x00\x00")
    storage.upsert_run_embedding("run_3", "demo", 3, b"\x03\x00\x00")

    summaries = storage.list_run_summaries(experiment_id="exp-1", experiment_version=1)
    embedding_rows = storage.iter_run_embeddings(
        experiment_id="exp-1",
        experiment_version=1,
    )

    assert {s["run_id"] for s in summaries} == {"run_1"}
    assert {r["run_id"] for r in embedding_rows} == {"run_1"}


def test_raw_and_final_scores_are_separated():
    raw, final = _compute_prediction_scores(
        raw_rca="orders",
        raw_fault_category="memory-exhaustion",
        final_rca="payments",
        final_fault_category="memory-exhaustion",
        chaos_target="orders",
        expected_fault_category="memory-exhaustion",
    )

    assert raw["correct_target"] is True
    assert raw["correct_fault"] is True
    assert raw["correct_full"] is True

    assert final["correct_target"] is False
    assert final["correct_fault"] is True
    assert final["correct_full"] is False


def test_raw_and_final_scores_handle_missing_values():
    raw, final = _compute_prediction_scores(
        raw_rca=None,
        raw_fault_category=None,
        final_rca=None,
        final_fault_category=None,
        chaos_target="orders",
        expected_fault_category="memory-exhaustion",
    )

    assert raw["correct_target"] is False
    assert raw["correct_fault"] is False
    assert raw["correct_full"] is False
    assert final["correct_target"] is False
    assert final["correct_fault"] is False
    assert final["correct_full"] is False
