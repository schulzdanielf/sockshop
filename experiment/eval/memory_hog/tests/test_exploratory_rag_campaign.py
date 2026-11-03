import json

import yaml

from experiment.eval.memory_hog import controlled_campaign
from experiment.eval.memory_hog import exploratory_rag_campaign as exploratory
from experiment.platform.backend.rca_knowledge import digest


def test_test_only_preparation_reuses_existing_corpus_and_open_search(
    tmp_path, monkeypatch
):
    source_config = yaml.safe_load(
        (controlled_campaign.HERE / "config.yaml").read_text(encoding="utf-8")
    )
    config = controlled_campaign.build_config(source_config, tmp_path, "rag", 42, {})
    train_cells = config["train_experiments"]
    test_cells = config["test_experiments"]
    source_campaign = tmp_path / "source-campaign"
    source_campaign.mkdir()
    run_ids = [f"source-run-{index}" for index in range(len(train_cells))]
    latest = {
        cell["cell_id"]: {
            "cell_id": cell["cell_id"],
            "run_id": run_id,
            "status": (
                "completed"
                if index < 32
                else ("blocked" if index == 32 else "aborted")
            ),
            "attempt": 1,
        }
        for index, (cell, run_id) in enumerate(zip(train_cells, run_ids))
    }
    cohort = {
        "latest": latest,
        "source_hashes": {"config.yaml": "source-hash"},
    }
    monkeypatch.setattr(
        exploratory,
        "_training_cohort",
        lambda _: (config, train_cells, cohort),
    )

    pairs = [
        {
            "pair_id": f"pair-{index}",
            "source_run_id": run_ids[index],
            "effect_validated": index < 24,
        }
        for index in range(32)
    ]
    base = {
        "sha256": "c" * 64,
        "skill_sha256": "d" * 64,
        "model": "qwen-14b",
        "training_run_ids": run_ids[:32],
        "pairs": pairs,
        "embedding_provider": "hashing-v1",
        "embedding_dim": 256,
    }

    corpus_id = "existing-corpus"
    store_directory = tmp_path / "fake-store" / corpus_id
    store_directory.mkdir(parents=True)
    (store_directory / "exploratory_base.json").write_text(
        json.dumps({"skill_sha256": "d" * 64, "model": "qwen-14b"}),
        encoding="utf-8",
    )

    class Store:
        def __init__(self, *_):
            self.directory = store_directory

        def load_exploratory_base(
            self,
            *,
            expected_skill_sha256,
            expected_model,
            allow_legacy_model_mismatch,
        ):
            assert expected_skill_sha256 == "d" * 64
            assert expected_model == "qwen-14b"
            assert allow_legacy_model_mismatch is True
            return base

    class Index:
        def __init__(self, url, sha256):
            assert url == exploratory.DEFAULT_OPENSEARCH_URL
            assert sha256 == "b" * 64

        def connect_existing(self, *, expected_pair_ids, expected_a_text_version):
            assert expected_pair_ids == {pair["pair_id"] for pair in pairs}
            assert expected_a_text_version == "incident-a-v2"
            return {
                "index_name": "frozen-index",
                "base_sha256": "b" * 64,
                "document_count": 32,
                "embedding_provider": "hashing-v1",
                "embedding_dim": 256,
                "a_text_version": "incident-a-v2",
            }

    monkeypatch.setattr(exploratory, "KnowledgeStore", Store)
    monkeypatch.setattr(exploratory, "OpenSearchExperimentIndex", Index)
    monkeypatch.setattr(exploratory, "DEFAULT_LLM_MODEL", "qwen3.5-9b-grafana")
    monkeypatch.setattr(exploratory, "CAMPAIGN_DIR", tmp_path / "eval")

    corpus_owner = (
        exploratory.CAMPAIGN_DIR
        / "out"
        / "exploratory_campaigns"
        / corpus_id
    )
    corpus_owner.mkdir(parents=True)
    audit_payload = {
        "counts": {"exploratory_pairs": len(pairs)},
        "source_campaign_id": "original-source",
    }
    audit = {**audit_payload, "sha256": digest(audit_payload)}
    (corpus_owner / "label_audit.json").write_text(
        json.dumps(audit), encoding="utf-8"
    )

    for cell in train_cells[:32]:
        annotation = source_campaign / "rag_results" / cell["cell_id"] / "annotate.json"
        annotation.parent.mkdir(parents=True)
        annotation.write_text(
            json.dumps({"run_id": latest[cell["cell_id"]]["run_id"]}),
            encoding="utf-8",
        )

    output_root = tmp_path / "campaigns"
    directory = exploratory.prepare_test_only_campaign(
        source_campaign,
        corpus_id=corpus_id,
        opensearch_base_sha256="b" * 64,
        allow_legacy_skill_hash=True,
        allow_legacy_model_mismatch=True,
        include_unvalidated_experimental=True,
        pilot_cell=test_cells[0]["cell_id"],
        output_root=output_root,
    )

    prepared = yaml.safe_load((directory / "config.yaml").read_text())
    manifest = json.loads((directory / "exploratory_manifest.json").read_text())
    ledger = (directory / "ledger.jsonl").read_text().splitlines()
    assert prepared["knowledge_campaign"]["corpus_id"] == corpus_id
    assert prepared["knowledge_campaign"]["retrieval_backend"] == "opensearch"
    assert prepared["knowledge_campaign"]["opensearch_base_sha256"] == "b" * 64
    assert prepared["knowledge_campaign"]["opensearch_source_skill_sha256"] == "d" * 64
    assert prepared["knowledge_campaign"]["allow_legacy_skill_hash"] is True
    assert prepared["knowledge_campaign"]["opensearch_source_model"] == "qwen-14b"
    assert prepared["knowledge_campaign"]["opensearch_current_model"] == "qwen3.5-9b-grafana"
    assert prepared["knowledge_campaign"]["allow_legacy_model_mismatch"] is True
    assert (
        prepared["knowledge_campaign"]["include_unvalidated_experimental"]
        is True
    )
    assert (
        prepared["knowledge_campaign"]["max_total_tokens"]
        == exploratory.EXPLORATORY_MAX_TOTAL_TOKENS
    )
    assert prepared["historical_observability"]["required"] is True
    assert prepared["historical_observability"]["mode"] == "dual_write"
    assert prepared["historical_observability"]["timeout_seconds"] == 90
    assert prepared["test_experiments"] == [test_cells[0]]
    assert len(ledger) == 35
    assert manifest["test_only"] is True
    assert manifest["training_runs_executed"] == 0
    assert manifest["training_runs_reused"] == 32
    assert manifest["source_training_status_counts"] == {
        "aborted": 2,
        "blocked": 1,
        "completed": 32,
    }
    assert manifest["skill_compatibility"]["hashes_match"] is False
    assert manifest["model_compatibility"] == {
        "source_model": "qwen-14b",
        "current_model": "qwen3.5-9b-grafana",
        "models_match": False,
        "legacy_model_mismatch_allowed": True,
    }
    assert manifest["opensearch_index"]["index_name"] == "frozen-index"
    assert manifest["unvalidated_pair_count"] == 8
    assert manifest["include_unvalidated_experimental"] is True
