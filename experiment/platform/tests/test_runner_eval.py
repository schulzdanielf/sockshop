from __future__ import annotations

import yaml

from experiment.eval.memory_hog.runner import _compute_prediction_scores
from experiment.eval.memory_hog.runner import RunRecord
from experiment.eval.memory_hog.runner import build_spec
from experiment.eval.memory_hog.runner import evaluate_strategy
from experiment.platform.backend.analysis.summary import build_run_summary_l2
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


def test_training_corpus_retrieval_does_not_require_test_experiment_id(tmp_path):
    storage = SqliteStorage(tmp_path / "platform.db", tmp_path / "object_store")

    storage.upsert_run_features(
        "train_run",
        "memhog-catalogue-r0-training",
        {
            "chaos_type": "memory-hog",
            "verdict": "degraded_recoverable",
            "summary_text": "training summary",
            "tags": ["chaos:memory-hog"],
            "is_training": True,
        },
        experiment_version=1,
    )
    storage.upsert_run_features(
        "test_run",
        "memhog-user-r0-test",
        {
            "chaos_type": "memory-hog",
            "verdict": "degraded_recoverable",
            "summary_text": "test summary",
            "tags": ["chaos:memory-hog"],
            "is_training": False,
        },
        experiment_version=1,
    )
    storage.upsert_run_embedding("train_run", "demo", 1, b"\x01")
    storage.upsert_run_embedding("test_run", "demo", 1, b"\x02")

    candidates = storage.iter_run_embeddings(
        provider="demo",
        is_training_only=True,
        limit=10,
    )

    assert {row["run_id"] for row in candidates} == {"train_run"}


def test_harness_reuses_stable_campaign_id_for_train_and_test():
    with open("experiment/eval/memory_hog/config.yaml", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    train_spec = build_spec(config, "catalogue", 0, "memory-hog")
    test_spec = build_spec(config, "user", 0, "memory-hog")

    assert train_spec["experiment"]["id"] == "multi-chaos-eval-v2"
    assert test_spec["experiment"]["id"] == "multi-chaos-eval-v2"

    query_ids = {
        query["id"] for query in train_spec["observability"]["metrics"]["queries"]
    }
    assert {
        "network_receive_bytes_rate",
        "network_transmit_bytes_rate",
        "network_receive_errors_rate",
        "network_transmit_errors_rate",
        "network_receive_dropped_rate",
        "network_transmit_dropped_rate",
    }.issubset(query_ids)


def test_l2_summary_includes_global_network_phase_signals():
    features = {
        "verdict": "degraded_recoverable",
        "chaos_type": "network-loss",
        "slo_thresholds": {"error_rate": 0.05, "latency_p95_ms": 800},
        "phase_metrics": {
            "network_receive_bytes_rate": {
                "baseline": {"count": 2, "mean": 100.0, "p95": 110.0},
                "fault": {"count": 2, "mean": 250.0, "p95": 270.0},
                "post": {"count": 2, "mean": 120.0, "p95": 130.0},
            },
            "network_receive_dropped_rate": {
                "baseline": {"count": 2, "mean": 0.0, "p95": 0.0},
                "fault": {"count": 2, "mean": 0.25, "p95": 0.3},
                "post": {"count": 2, "mean": 0.0, "p95": 0.0},
            },
        },
        "metric_hotspots": {},
        "slo_violations": [],
        "affected_services": [],
        "trace_summary": {"trace_count": 0, "error_trace_count": 0},
    }

    summary, _tags = build_run_summary_l2(
        "run-network", "multi-chaos-eval-v2", features, {}
    )

    assert "## Network phase signals" in summary
    assert "network_receive_bytes_rate" in summary
    assert "network_receive_dropped_rate" in summary


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


def test_gemini_eval_caps_tokens_and_sets_request_timeout(monkeypatch):
    captured = {}

    class _FakeApi:
        def post(self, path, body=None, request_timeout=None, **query):
            captured["path"] = path
            captured["body"] = body
            captured["request_timeout"] = request_timeout
            captured["query"] = query
            return {
                "analysis": {
                    "rca": "user",
                    "fault_category": "memory-exhaustion",
                    "confidence": 0.8,
                    "prompt_meta": {"prompt_tokens_estimate": 100},
                    "rag_mode": "embedding",
                }
            }

    monkeypatch.setenv("GEMINI_MAX_NEW_TOKENS", "384")
    monkeypatch.setenv("EVAL_LLM_REQUEST_TIMEOUT_SECONDS", "45")

    result = evaluate_strategy(
        _FakeApi(),
        RunRecord(
            service="user",
            replica_idx=0,
            phase="test",
            chaos_target="user",
            experiment_id="exp",
            run_id="run-1",
            status="completed",
            chaos_type="memory-hog",
        ),
        {
            "name": "S0_no_rag",
            "mode": "embedding",
            "limit": 0,
            "system_id": "sock-shop",
            "budget_tokens": 3200,
            "max_new_tokens": 1024,
        },
        "memory-exhaustion",
        llm_provider="gemini",
    )

    assert result.raw_error == ""
    assert captured["path"] == "/api/runs/run-1/llm-analysis"
    assert captured["request_timeout"] == 45
    assert captured["query"]["max_new_tokens"] == 384
    assert captured["query"]["provider"] == "gemini"
