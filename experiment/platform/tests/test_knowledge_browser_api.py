import json

import pytest
from fastapi.testclient import TestClient

from experiment.platform.backend import api
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage


@pytest.fixture
def knowledge_client(monkeypatch, tmp_path):
    storage = SqliteStorage(tmp_path / "platform.db", tmp_path / "objects")
    monkeypatch.setattr(api, "SqliteStorage", lambda **kwargs: storage)
    monkeypatch.setattr(api, "setup_telemetry", lambda app: None)
    monkeypatch.setattr(api, "configure_llm_io_audit", lambda path: None)
    return TestClient(api.create_app()), storage


def install_base(storage, corpus_id, mode="validated"):
    corpus_dir = storage.object_store_root / ".rca_knowledge" / corpus_id
    corpus_dir.mkdir(parents=True)
    pairs = [
        {
            "pair_id": "pair-1",
            "source_run_id": "run-train-1",
            "a": {
                "symptoms": [{"description": "CPU increased", "evidence_ids": ["E1"]}]
            },
            "b": {
                "diagnosis": {"status": "inconclusive"},
                "recommended_checks": ["Check CPU"],
            },
            "labels": {"service": "carts", "fault_type": "cpu-hog"},
            "guide_status": "pending" if mode == "exploratory" else "validated",
            "causal_status": "inconclusive" if mode == "exploratory" else "supported",
            "effect_validated": False,
        }
    ]
    base = {
        "version": "skill-rag-ab-v1",
        "mode": "exploratory" if mode == "exploratory" else None,
        "model": "qwen-test",
        "skill_sha256": "a" * 64,
        "training_run_ids": ["run-train-1"],
        "pairs": pairs,
        "index": {"pair-1": [0.1, 0.2]},
    }
    if mode != "exploratory":
        base.pop("mode")
    base["sha256"] = api.knowledge_digest(
        {key: value for key, value in base.items() if key != "sha256"}
    )
    filename = "exploratory_base.json" if mode == "exploratory" else "base.json"
    (corpus_dir / filename).write_text(json.dumps(base), encoding="utf-8")


def test_knowledge_browser_lists_and_returns_a_b_pairs(knowledge_client):
    client, storage = knowledge_client
    install_base(storage, "campaign-a", mode="exploratory")
    headers = {"X-User-Role": "admin"}

    corpora = client.get("/api/skill-rag/corpora", headers=headers)
    assert corpora.status_code == 200
    assert len(corpora.json()) == 1
    assert corpora.json()[0]["corpus_id"] == "campaign-a"
    assert corpora.json()[0]["mode"] == "exploratory"
    assert corpora.json()[0]["pair_count"] == 1

    response = client.get("/api/skill-rag/campaign-a/pairs", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "exploratory"
    assert data["pairs"][0]["a"]["symptoms"][0]["description"] == "CPU increased"
    assert data["pairs"][0]["b"]["recommended_checks"] == ["Check CPU"]
    assert data["pairs"][0]["b_eligible"] is None
    assert "index" not in data


def test_knowledge_browser_identifies_latest_evaluation_for_corpus(
    knowledge_client, monkeypatch
):
    client, storage = knowledge_client
    install_base(storage, "campaign-a", mode="exploratory")
    base_path = (
        storage.object_store_root
        / ".rca_knowledge"
        / "campaign-a"
        / "exploratory_base.json"
    )
    base = json.loads(base_path.read_text(encoding="utf-8"))
    evaluation_id = "test-base-v6-20261107T215646147881Z"
    evaluation_root = storage.object_store_root.parent / "knowledge_evaluation"
    report_dir = evaluation_root / evaluation_id
    report_dir.mkdir(parents=True)
    (report_dir / "test_base_rag_evaluation.json").write_text(
        json.dumps(
            {
                "evaluation_id": evaluation_id,
                "provenance": {
                    "corpus_id": "campaign-a",
                    "corpus_sha256": base["sha256"],
                    "retriever_version": "opensearch-bm25-metric-gated-rrf-v6",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "KNOWLEDGE_EVALUATION_ROOT", evaluation_root)

    response = client.get(
        "/api/skill-rag/corpora",
        headers={"X-User-Role": "admin"},
    )

    assert response.status_code == 200
    assert response.json()[0]["latest_evaluation"] == {
        "evaluation_id": evaluation_id,
        "retriever_version": "opensearch-bm25-metric-gated-rrf-v6",
    }


def test_knowledge_browser_reports_procedural_b_eligibility(knowledge_client):
    client, storage = knowledge_client
    install_base(storage, "campaign-a", mode="exploratory")
    path = (
        storage.object_store_root
        / ".rca_knowledge"
        / "campaign-a"
        / "exploratory_base.json"
    )
    base = json.loads(path.read_text(encoding="utf-8"))
    eligible_pair = base["pairs"][0]
    eligible_pair.update(
        guide_status="procedurally_validated",
        causal_status="supported",
        b_procedural_status="passed",
        effect_validated=False,
    )
    pending_pair = {
        **eligible_pair,
        "pair_id": "pair-2",
        "source_run_id": "run-train-2",
        "guide_status": "procedural_pending",
        "causal_status": "inconclusive",
        "b_procedural_status": "pending",
    }
    base["training_run_ids"].append("run-train-2")
    base["pairs"].append(pending_pair)
    base["sha256"] = api.knowledge_digest(
        {key: value for key, value in base.items() if key != "sha256"}
    )
    path.write_text(json.dumps(base), encoding="utf-8")

    response = client.get(
        "/api/skill-rag/campaign-a/pairs",
        headers={"X-User-Role": "admin"},
    )

    assert response.status_code == 200
    pairs = {pair["pair_id"]: pair for pair in response.json()["pairs"]}
    assert pairs["pair-1"]["effect_validated"] is False
    assert pairs["pair-1"]["b_eligible"] is True
    assert pairs["pair-2"]["b_procedural_status"] == "pending"
    assert pairs["pair-2"]["b_eligible"] is False


def test_knowledge_browser_requires_an_authorized_role(knowledge_client):
    client, _ = knowledge_client

    assert client.get("/api/skill-rag/corpora").status_code == 403
    assert (
        client.get(
            "/api/skill-rag/missing/pairs",
            headers={"X-User-Role": "viewer"},
        ).status_code
        == 403
    )


def test_knowledge_browser_rejects_a_corrupt_frozen_base(knowledge_client):
    client, storage = knowledge_client
    install_base(storage, "campaign-a")
    path = storage.object_store_root / ".rca_knowledge" / "campaign-a" / "base.json"
    base = json.loads(path.read_text(encoding="utf-8"))
    base["pairs"][0]["a"]["symptoms"][0]["description"] = "tampered"
    path.write_text(json.dumps(base), encoding="utf-8")

    response = client.get(
        "/api/skill-rag/campaign-a/pairs",
        headers={"X-User-Role": "admin"},
    )
    assert response.status_code == 409
    assert "integrity validation" in response.json()["detail"]
