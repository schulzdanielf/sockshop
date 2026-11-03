import json

from experiment.eval.memory_hog import rag_campaign


def test_exploratory_prepare_test_uses_nonvalidated_base_status(tmp_path):
    config = {
        "knowledge_campaign": {
            "corpus_id": "pilot-corpus",
            "exploratory": True,
            "version": "skill-rag-ab-v1",
        },
        "train_experiments": [
            {"cell_id": "train-cell", "phase": "train"},
        ],
        "test_experiments": [
            {"cell_id": "pilot-cell", "phase": "test"},
        ],
    }
    annotation = rag_campaign.result_path(
        tmp_path, config["train_experiments"][0], "annotate"
    )
    annotation.parent.mkdir(parents=True)
    annotation.write_text(
        json.dumps({"run_id": "train-run", "response": {"status": "inconclusive"}}),
        encoding="utf-8",
    )
    api = type(
        "Api", (), {
            "get": lambda self, path: {
                "version": "skill-rag-ab-v1",
                "mode": "exploratory",
                "sha256": "exploratory-hash",
                "candidate_count": 1,
                "source_run_ids": ["historical-run"],
                "source_skill_sha256": "a" * 64,
                "current_skill_sha256": "a" * 64,
                "skill_hashes_match": True,
                "legacy_skill_hash_allowed": False,
                "source_model": "qwen-14b",
                "current_model": "qwen-14b",
                "models_match": True,
                "legacy_model_mismatch_allowed": False,
            }
        }
    )()

    rag_campaign.prepare_test(
        tmp_path,
        config,
        {"train-cell": {"status": "completed", "run_id": "train-run"}},
        api,
    )

    base = json.loads((tmp_path / "rag_base.json").read_text())
    assert base["sha256"] == "exploratory-hash"
    assert base["training_run_ids"] == ["historical-run"]
    assert base["parent_training_run_ids"] == ["train-run"]
    rag_campaign.prepare_test(
        tmp_path,
        config,
        {"train-cell": {"status": "completed", "run_id": "train-run"}},
        api,
    )


def test_pilot_ledger_imports_latest_training_state_without_historical_retries():
    from experiment.eval.memory_hog.exploratory_rag_pilot import _pilot_training_ledger

    cells = [
        {"cell_id": "retried-train-cell"},
        {"cell_id": "single-train-cell"},
    ]
    latest = {
        "retried-train-cell": {
            "cell_id": "retried-train-cell",
            "run_id": "source-retry",
            "status": "completed",
            "attempt": 2,
        },
        "single-train-cell": {
            "cell_id": "single-train-cell",
            "run_id": "source-single",
            "status": "aborted",
            "attempt": 1,
        },
    }

    rows = _pilot_training_ledger(cells, latest)

    assert [row["cell_id"] for row in rows] == [
        "retried-train-cell", "single-train-cell"
    ]
    assert [row["attempt"] for row in rows] == [1, 1]
    assert [row["source_attempt"] for row in rows] == [2, 1]


def test_exploratory_evaluation_is_a_one_cell_separate_report(tmp_path):
    from experiment.eval.memory_hog.rag_campaign import (
        EXPLORATORY_METHODS,
        result_path,
        write_evaluation,
    )

    cell = {
        "cell_id": "payment-http-status-code-r1",
        "service": "payment",
        "chaos_type": "http-status-code",
        "phase": "test",
    }
    config = {
        "experiment_campaign_id": "pilot",
        "knowledge_campaign": {"exploratory": True},
        "train_experiments": [
            {"cell_id": "historical-training-cell", "phase": "train"},
        ],
        "test_experiments": [cell],
    }
    (tmp_path / "ledger.jsonl").write_text(
        json.dumps({
            "cell_id": cell["cell_id"],
            "run_id": "pilot-run",
            "status": "completed",
            "gates": {
                "intervention_confirmed_on_target": True,
                "mechanism_observed": {"passed": True},
            },
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "rag_base.json").write_text(
        json.dumps({"sha256": "base-sha"}), encoding="utf-8"
    )
    for method in EXPLORATORY_METHODS:
        diagnosis = {
            "primary_service": "payment",
            "fault_type": "http-status-code",
            "supporting_evidence": [],
        }
        response = {
            "status": "inconclusive",
            "diagnosis": diagnosis,
            "base_sha256": "base-sha" if method == "exploratory_rag" else None,
            "retrieved": (
                [{"pair_id": "train-pair", "score": 0.8}]
                if method == "exploratory_rag" else []
            ),
        }
        result_path(tmp_path, cell, method).parent.mkdir(parents=True, exist_ok=True)
        result_path(tmp_path, cell, method).write_text(
            json.dumps({"run_id": "pilot-run", "response": response}),
            encoding="utf-8",
        )

    report = write_evaluation(tmp_path, config)

    assert report["planned"] == {"test": 1}
    assert report["protocol"].startswith("single-run exploratory pilot")
    assert report["summary"]["exploratory_rag"]["denominator"] == 1
    assert report["per_run"][0]["retrieval"]["pair_ids"] == ["train-pair"]
    assert (tmp_path / "exploratory_rag_evaluation.json").is_file()
    assert not (tmp_path / "rag_evaluation.json").exists()
