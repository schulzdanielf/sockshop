import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from experiment.platform.backend import api
from experiment.platform.backend.domain import RunRecord, RunStatus
from experiment.platform.backend.storage.sqlite_storage import SqliteStorage


@pytest.fixture
def platform(monkeypatch, tmp_path):
    storage = SqliteStorage(tmp_path / "platform.db", tmp_path / "objects")
    storage.create_experiment(
        "test",
        {
            "experiment": {"id": "test"},
            "scope": {"namespace": "sock-shop"},
            "chaos_profile": {"secret_ground_truth": "must-not-reach-model"},
            "observability": {"metrics": {"mcp_sse_url": "http://mcp/sse"}},
        },
    )
    run = RunRecord(
        run_id="run-test",
        experiment_id="test",
        experiment_version=1,
        status=RunStatus.COMPLETED,
        initiated_by="test",
        started_at="2026-10-02T10:00:00Z",
        ended_at="2026-10-02T10:04:00Z",
        timings={
            "t_start": "2026-10-02T10:00:00Z",
            "t_warmup_start": "2026-10-02T10:01:00Z",
            "t_warmup_end": "2026-10-02T10:02:00Z",
            "t_fault_start": "2026-10-02T10:02:00Z",
            "t_fault_end": "2026-10-02T10:03:00Z",
            "t_recovery_observed": "2026-10-02T10:04:00Z",
            "fault_window_source": "manifest_observed",
        },
    )
    storage.create_run(run)
    monkeypatch.setattr(api, "SqliteStorage", lambda **kwargs: storage)
    monkeypatch.setattr(api, "setup_telemetry", lambda app: None)
    monkeypatch.setattr(api, "configure_llm_io_audit", lambda path: None)
    return TestClient(api.create_app()), storage, run


def result(status="confirmed"):
    return {
        "status": status,
        "generated_at": "2026-10-13T12:00:00+00:00",
        "error": "Qwen unavailable" if status == "error" else None,
        "evidence": [{"id": "E001"}],
    }


def test_exploratory_base_api_pins_legacy_model_compatibility(
    platform, monkeypatch
):
    client, _, _ = platform
    requested = {}
    source_skill = "a" * 64
    current_skill = "b" * 64
    source_model = "qwen-14b"
    current_model = "qwen3.5-9b-grafana"

    class Store:
        def __init__(self, *_):
            pass

        def load_exploratory_base(self, **kwargs):
            requested.update(kwargs)
            return {
                "version": "skill-rag-ab-v1",
                "mode": "exploratory",
                "sha256": "c" * 64,
                "pairs": [{}],
                "training_run_ids": ["train-run"],
                "skill_sha256": source_skill,
                "model": source_model,
            }

    monkeypatch.setattr(api, "KnowledgeStore", Store)
    monkeypatch.setattr(api, "load_skill", lambda: ("", {}, current_skill))
    monkeypatch.setattr(api, "DEFAULT_LLM_MODEL", current_model)

    response = client.get(
        "/api/skill-rag/corpus/exploratory-base",
        params={
            "expected_skill_sha256": source_skill,
            "allow_legacy_skill_hash": "true",
            "expected_model": source_model,
            "allow_legacy_model_mismatch": "true",
        },
        headers={"X-User-Role": "admin"},
    )

    assert response.status_code == 200, response.text
    assert requested == {
        "expected_skill_sha256": source_skill,
        "expected_model": source_model,
        "allow_legacy_model_mismatch": True,
    }
    assert response.json()["source_model"] == source_model
    assert response.json()["current_model"] == current_model
    assert response.json()["models_match"] is False
    assert response.json()["legacy_model_mismatch_allowed"] is True
    assert response.json()["legacy_skill_hash_allowed"] is True


def test_api_uses_pinned_run_config_and_preserves_attempts(platform, monkeypatch):
    client, storage, run = platform
    calls = []
    mcp_endpoints = []

    def investigate(**kwargs):
        calls.append(kwargs)
        return result()

    monkeypatch.setattr(
        api,
        "MCPToolClient",
        lambda url, timeout: mcp_endpoints.append((url, timeout)) or object(),
    )
    monkeypatch.setattr(api, "answer_skill_rca", investigate)
    first = client.post(
        "/api/runs/run-test/skill-rca",
        json={
            "possible_fault_types": ["container-kill", "cpu-hog"],
            "require_best_guess": True,
        },
    ).json()
    second = client.post("/api/runs/run-test/skill-rca").json()
    assert first["attempt_id"] != second["attempt_id"]
    assert Path(first["artifact_path"]).exists()
    persisted = json.loads(Path(first["artifact_path"]).read_text())
    assert persisted["attempt_id"] == first["attempt_id"]
    assert persisted["model"] == api.DEFAULT_LLM_MODEL
    assert calls[0]["namespace"] == "sock-shop"
    assert calls[0]["possible_fault_types"] == ["container-kill", "cpu-hog"]
    assert calls[0]["require_best_guess"] is True
    assert calls[1]["possible_fault_types"] == []
    assert calls[1]["require_best_guess"] is False
    assert mcp_endpoints == [
        (api.DEFAULT_HISTORICAL_MCP_SSE_URL, 30),
        (api.DEFAULT_HISTORICAL_MCP_SSE_URL, 30),
    ]
    assert first["observability_source"] == second["observability_source"] == "history"
    assert calls[0]["phases"]["fault"]["start"] == run.timings["t_fault_start"]
    assert "chaos_profile" not in calls[0]
    assert storage.list_events(run.run_id)[0].ts == first["generated_at"]
    assert storage.get_artifact(run.run_id, "rca_investigation") is None
    assert "/api/runs/{run_id}/rca" in client.get("/openapi.json").json()["paths"]
    assert first["history_archive"]["status"] == "not_configured"


def test_legacy_rca_queries_history_instead_of_frozen_snapshot(platform, monkeypatch):
    client, storage, run = platform
    storage.save_artifact(run.run_id, "rca_evidence_snapshot", {"status": "ok"})
    clients = []
    results = []

    def create_client(url, timeout):
        client = object()
        clients.append((url, timeout, client))
        return client

    def investigate(**kwargs):
        results.append(kwargs)
        return {
            "answer": "Causa inconclusiva",
            "calls": [],
            "iterations": 1,
            "stop_reason": "completed",
        }

    monkeypatch.setattr(api, "MCPToolClient", create_client)
    monkeypatch.setattr(api, "answer_run_rca_mcp", investigate)

    response = client.post("/api/runs/run-test/rca")

    assert response.status_code == 200
    assert clients[0][:2] == (api.DEFAULT_HISTORICAL_MCP_SSE_URL, 30)
    assert results[0]["mcp_client"] is clients[0][2]
    assert response.json()["observability_source"] == "history"


def test_skill_rca_uses_historical_mcp_timeout_and_evidence_source(
    platform, monkeypatch
):
    from dataclasses import replace

    client, storage, run = platform
    spec = storage.get_experiment("test", 1).spec
    spec["historical_observability"] = {
        "mode": "dual_write",
        "required": True,
        "mcp_sse_url": "http://history-mcp/sse",
        "timeout_seconds": 90,
    }
    version = storage.create_experiment("test", spec)
    history_run = replace(
        run,
        run_id="run-history",
        experiment_version=version.version,
    )
    storage.create_run(history_run)
    clients = []
    calls = []

    class HistoricalClient:
        def __init__(self, url, timeout):
            clients.append((url, timeout))

    monkeypatch.setattr(api, "MCPToolClient", HistoricalClient)
    monkeypatch.setattr(
        api,
        "answer_skill_rca",
        lambda **kwargs: calls.append(kwargs) or result(),
    )

    response = client.post("/api/runs/run-history/skill-rca")

    assert response.status_code == 200, response.text
    assert clients == [("http://history-mcp/sse", 90)]
    assert calls[0]["evidence_source"] == "history"
    assert response.json()["observability_source"] == "history"


def test_api_reads_archive_by_run_id_and_allows_campaign_override(platform, monkeypatch):
    _, storage, _ = platform
    requested = []
    archive = {
        "status": "ok",
        "evidence_source": "archive",
        "campaign_id": "exploratory-rag-20261028T191703038725Z",
        "run_id": "run-test",
        "archive_key": (
            "exploratory-rag-20261028T191703038725Z/"
            "run-test/source-bundle.json.gz"
        ),
        "bundle_sha256": "a" * 64,
        "trace_count": 1,
        "span_count": 1,
        "phases": {"fault": {"span_count": 1, "trace_count": 1}},
    }

    class FakeArchive:
        def read_trace_evidence(self, **kwargs):
            requested.append(kwargs)
            return archive

    monkeypatch.setattr(
        api.HistoryArchiveReader,
        "from_environment",
        classmethod(lambda cls: FakeArchive()),
    )
    calls = []
    monkeypatch.setattr(
        api,
        "answer_skill_rca",
        lambda **kwargs: calls.append(kwargs) or result(),
    )
    client = TestClient(api.create_app())

    response = client.post(
        "/api/runs/run-test/skill-rca"
        "?history_campaign_id=exploratory-rag-20261028T191703038725Z",
        json={},
    )

    assert response.status_code == 200
    assert requested[0]["campaign_id"] == archive["campaign_id"]
    assert requested[0]["run_id"] == "run-test"
    assert requested[0]["phases"]["fault"]["start"] == "2026-10-02T10:02:00Z"
    assert calls[0]["archive_traces"] == archive
    assert response.json()["history_archive"] == {
        "status": "ok",
        "campaign_id": archive["campaign_id"],
        "run_id": "run-test",
        "archive_key": archive["archive_key"],
    }
    assert storage.get_artifact("run-test", response.json()["artifact_path"].split("/")[-1].removesuffix(".json"))


def test_dual_write_history_validation_reads_back_from_historical_mcp(platform, monkeypatch):
    from dataclasses import replace

    client, storage, run = platform
    spec = {
        "experiment": {"id": "dual-write"},
        "scope": {"namespace": "sock-shop"},
        "controlled_campaign": {"service": "payment"},
        "historical_observability": {
            "mode": "dual_write",
            "required": True,
            "mcp_sse_url": "http://history-mcp/sse",
            "timeout_seconds": 17,
        },
    }
    version = storage.create_experiment("dual-write", spec)
    run = replace(
        run,
        run_id="run-dual-write",
        experiment_id="dual-write",
        experiment_version=version.version,
    )
    run.summary = {
        "history_migration": {
            "status": "dual_write",
            "campaign_id": "dual-write-campaign",
        }
    }
    storage.create_run(run)
    phases = {
        "baseline": {"start": "2026-10-02T10:01:00Z", "end": "2026-10-02T10:02:00Z"},
        "fault": {"start": "2026-10-02T10:02:00Z", "end": "2026-10-02T10:03:00Z"},
    }
    monkeypatch.setattr(
        api,
        "_build_observation_window",
        lambda **kwargs: {"phases": phases},
    )
    client_arguments = []

    class HistoricalClient:
        def __init__(self, url, timeout):
            client_arguments.append((url, timeout))

    calls = []

    def validate(client, **kwargs):
        calls.append((client, kwargs))
        return {
            "status": "complete",
            "mode": "dual_write",
            "source_counts": {"metrics": 4, "logs": 1, "traces": 1},
        }

    monkeypatch.setattr(api, "MCPToolClient", HistoricalClient)
    monkeypatch.setattr(api, "validate_history_stream", validate)

    response = client.get("/api/runs/run-dual-write/history/validation")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "complete"
    assert response.json()["campaign_id"] == "dual-write"
    assert client_arguments == [("http://history-mcp/sse", 17)]
    assert calls[0][1] == {
        "phases": phases,
        "namespace": "sock-shop",
        "service": "payment",
    }


def test_api_persists_failed_attempt_and_returns_error(platform, monkeypatch):
    client, storage, run = platform
    monkeypatch.setattr(api, "answer_skill_rca", lambda **kwargs: result("error"))
    response = client.post("/api/runs/run-test/skill-rca")
    assert response.status_code == 502
    detail = response.json()["detail"]
    persisted = json.loads(Path(detail["artifact_path"]).read_text())
    assert persisted["status"] == "error" and persisted["evidence"]
    assert storage.list_events(run.run_id)[0].event_type == "skill_rca_failed"


def test_api_rejects_unknown_and_running_runs(platform):
    client, storage, run = platform
    assert client.post("/api/runs/missing/skill-rca").status_code == 404
    run.status = RunStatus.RUNNING
    storage.update_run(run)
    assert client.post("/api/runs/run-test/skill-rca").status_code == 409


def test_knowledge_api_enforces_pinned_split_and_rejects_injected_labels(platform):
    client, _, _ = platform
    body = {"corpus_id": "corpus", "mode": "annotate",
            "possible_fault_types": ["network-latency"]}
    assert client.post("/api/runs/run-test/skill-rag", json=body).status_code == 403
    headers = {"X-User-Role": "admin"}
    response = client.post("/api/runs/run-test/skill-rag", json=body, headers=headers)
    assert response.status_code == 409
    assert "pinned split" in response.json()["detail"]
    response = client.post(
        "/api/runs/run-test/skill-rag",
        json={**body, "labels": {"service": "orders"}}, headers=headers,
    )
    assert response.status_code == 422


def test_knowledge_api_training_freeze_test_and_idempotent_results(platform, monkeypatch):
    from dataclasses import replace

    from experiment.platform.tests.test_rca_knowledge import Model
    from experiment.platform.tests.test_rca_skill import FakeMCP

    client, storage, run = platform
    spec = storage.get_experiment("test", 1).spec
    spec.update(
        knowledge_campaign={"corpus_id": "corpus", "split": "train"},
        controlled_campaign={"service": "front-end", "fault": "network-latency"},
    )
    version = storage.create_experiment("test", spec)
    run = replace(run, run_id="run-train", experiment_version=version.version)
    run.summary = {"controlled_gates": {
        "intervention_confirmed_on_target": True, "mechanism_observed": {"passed": True},
    }}
    storage.create_run(run)
    model = Model(query=True)
    monkeypatch.setattr(api, "call_llm_tool_messages", model)
    mcp_clients = []

    def create_mcp_client(*args):
        mcp_clients.append(args)
        return FakeMCP()

    monkeypatch.setattr(api, "MCPToolClient", create_mcp_client)
    headers = {"X-User-Role": "admin"}
    body = {"corpus_id": "corpus", "mode": "annotate",
            "possible_fault_types": ["network-latency"]}
    response = client.post("/api/runs/run-train/skill-rag", json=body, headers=headers)
    assert response.status_code == 200, response.text
    training = response.json()
    assert training["pair"]["guide_status"] == "validated", training
    calls = len(model.payloads)
    cached = client.post("/api/runs/run-train/skill-rag", json=body, headers=headers)
    assert cached.json()["cached"] is True
    assert len(model.payloads) == calls
    freeze = client.post(
        "/api/skill-rag/corpus/freeze",
        json={"training_run_ids": ["run-train"]}, headers=headers,
    )
    assert freeze.status_code == 200, freeze.text
    assert freeze.json()["eligible_count"] == 1
    spec["experiment"]["id"] = "test-eval"
    spec["knowledge_campaign"]["split"] = "test"
    spec["historical_observability"] = {
        "mode": "dual_write",
        "required": True,
        "mcp_sse_url": "http://history-mcp/sse",
        "timeout_seconds": 90,
    }
    version = storage.create_experiment("test", spec)
    test = replace(
        run, run_id="run-eval", experiment_id="test-eval", experiment_version=version.version,
    )
    storage.create_run(test)
    test_model = Model(query=True)
    monkeypatch.setattr(api, "call_llm_tool_messages", test_model)
    response = client.post(
        "/api/runs/run-eval/skill-rag", json={**body, "mode": "rag"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "confirmed", result
    assert result["observability_source"] == "history"
    assert mcp_clients[-1] == ("http://history-mcp/sse", 90)
    assert {item["source"] for item in result["evidence"]} == {"history"}
    assert result["base_sha256"] == freeze.json()["sha256"]
    assert result["retrieved"][0]["source_run_id"] == "run-train"
    assert all(
        "training_annotation_labels" not in json.dumps(payload)
        and "must-not-reach-model" not in json.dumps(payload)
        for payload in test_model.payloads
    )
    assert Path(result["artifact_path"]).is_file()
    stale_result = dict(result)
    stale_result["evidence"] = [
        {**item, "source": "live"} for item in result["evidence"]
    ]
    storage.save_artifact(
        "run-eval", "skill_knowledge_corpus_rag", stale_result
    )
    mcp_call_count = len(mcp_clients)
    refreshed = client.post(
        "/api/runs/run-eval/skill-rag",
        json={**body, "mode": "rag"},
        headers=headers,
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json().get("cached") is not True
    assert {item["source"] for item in refreshed.json()["evidence"]} == {"history"}
    assert len(mcp_clients) > mcp_call_count
    assert client.post(
        "/api/runs/run-eval/skill-rag", json=body, headers=headers,
    ).status_code == 409


def test_failed_controlled_run_uses_persisted_baseline_and_fault_windows(platform, monkeypatch):
    client, storage, run = platform
    controlled_window = {
        "timezone": "UTC",
        "sampling_interval_seconds": 5,
        "window": {
            "start": "2026-10-02T10:00:00Z",
            "end": "2026-10-02T10:03:00Z",
        },
        "phases": {
            "baseline": {
                "start": "2026-10-02T10:01:00Z",
                "end": "2026-10-02T10:02:00Z",
            },
            "fault": {
                "start": "2026-10-02T10:02:00Z",
                "end": "2026-10-02T10:02:05Z",
            },
        },
        "phase_sources": {
            "baseline": "after_load_stabilization",
            "fault": "partial_observation_not_effective_intervention",
        },
    }
    run.status = RunStatus.FAILED
    run.timings = {
        "sampling_interval_seconds": 5,
        "controlled_observation_window": controlled_window,
        "post_observation_started": {"at": "2026-10-02T10:02:05Z"},
    }
    storage.update_run(run)
    calls = []
    monkeypatch.setattr(api, "MCPToolClient", lambda url, timeout: object())
    monkeypatch.setattr(
        api,
        "answer_skill_rca",
        lambda **kwargs: calls.append(kwargs) or result(),
    )

    response = client.post(
        "/api/runs/run-test/skill-rca",
        json={"possible_fault_types": ["container-kill"]},
    )

    assert response.status_code == 200, response.text
    assert calls[0]["phases"]["baseline"] == controlled_window["phases"]["baseline"]
    assert calls[0]["phases"]["fault"] == controlled_window["phases"]["fault"]
    assert calls[0]["phases"]["fault"] == {
        "start": "2026-10-02T10:02:00Z",
        "end": "2026-10-02T10:02:05Z",
    }
    assert calls[0]["phases"]["post"] == {
        "start": "2026-10-02T10:02:05Z",
        "end": controlled_window["window"]["end"],
    }


def test_multiagent_plan_is_read_only_and_excludes_ground_truth(platform, monkeypatch):
    client, storage, run = platform

    def forbidden(*args, **kwargs):
        pytest.fail("Planning must not call MCP, models or persist artifacts/events")

    monkeypatch.setattr(api, "MCPToolClient", forbidden)
    monkeypatch.setattr(api, "answer_skill_rca", forbidden)
    monkeypatch.setattr(storage, "save_artifact", forbidden)
    monkeypatch.setattr(storage, "append_event", forbidden)
    response = client.get("/api/runs/run-test/multiagent-rca/plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["execution_available"] is False and plan["mode"] == "planning_only"
    assert plan["context"]["run_id"] == run.run_id
    assert plan["context"]["phases"]["fault"]["start"] == run.timings["t_fault_start"]
    assert set(plan["context"]["phases"]) == {"baseline", "fault", "post"}
    assert plan["context"]["namespace"] == "sock-shop"
    assert "carts" in plan["context"]["known_services"]
    assert "must-not-reach-model" not in response.text
    assert "chaos_profile" not in response.text
    assert client.get("/api/runs/run-test/multiagent-rca/plan").json() == plan
    assert storage.list_events(run.run_id) == []
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/runs/{run_id}/multiagent-rca/plan" in paths
    assert "/api/runs/{run_id}/skill-rca" in paths
    assert "/api/runs/{run_id}/rca" in paths


def test_multiagent_plan_rejects_missing_running_and_unusable_runs(platform, monkeypatch):
    client, storage, run = platform
    assert client.get("/api/runs/missing/multiagent-rca/plan").status_code == 404
    monkeypatch.setattr(api, "load_system_card", lambda namespace: {})
    assert client.get("/api/runs/run-test/multiagent-rca/plan").status_code == 409
    run.status = RunStatus.RUNNING
    storage.update_run(run)
    assert client.get("/api/runs/run-test/multiagent-rca/plan").status_code == 409


def test_multiagent_strategy_endpoint_exposes_loaded_versions(platform):
    client, _storage, _run = platform
    response = client.get("/api/multiagent-rca/strategy")

    assert response.status_code == 200
    assert response.json() == {
        "strategy_version": api.MULTIAGENT_STRATEGY_VERSION,
        "prompt_version": api.MULTIAGENT_PROMPT_VERSION,
    }


def test_controlled_campaign_capabilities_endpoint_exposes_protocol(platform):
    client, _storage, _run = platform
    response = client.get("/api/controlled-campaign/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": api.CONTROLLED_CAMPAIGN_PROTOCOL_VERSION,
    }


def test_multiagent_execution_persists_immutable_attempt_and_excludes_ground_truth(platform, monkeypatch):
    client, storage, run = platform
    captured = []
    mcp_endpoints = []
    monkeypatch.setattr(
        api,
        "MCPToolClient",
        lambda url, timeout: mcp_endpoints.append((url, timeout)) or object(),
    )

    def investigate(**kwargs):
        captured.append(kwargs)
        return {
            "status": "inconclusive",
            "generated_at": "2026-10-13T12:00:00+00:00",
            "answer": "Causa inconclusiva",
            "diagnosis": {"status": "inconclusive"},
            "error": None,
            "evidence": [],
            "calls": [],
            "specialists": [],
            "budget": {"model_calls_used": 5},
            "followup_rounds": [{"round": 1, "action": "stop", "reason": "Sem novo check", "status": "stopped"}],
            "stop_reason": "coordinator_stop",
        }

    monkeypatch.setattr(api, "answer_multiagent_rca", investigate)
    first_response = client.post(
        "/api/runs/run-test/multiagent-rca",
        json={"possible_fault_types": ["container-kill", "cpu-hog"]},
    )
    second_response = client.post("/api/runs/run-test/multiagent-rca")
    assert first_response.status_code == second_response.status_code == 200
    first, second = first_response.json(), second_response.json()
    assert first["attempt_id"] != second["attempt_id"]
    assert mcp_endpoints == [
        (api.DEFAULT_HISTORICAL_MCP_SSE_URL, 30),
        (api.DEFAULT_HISTORICAL_MCP_SSE_URL, 30),
    ]
    assert first["observability_source"] == second["observability_source"] == "history"
    assert Path(first["artifact_path"]).exists()
    persisted = json.loads(Path(first["artifact_path"]).read_text())
    assert persisted["attempt_id"] == first["attempt_id"]
    assert persisted["status"] == "inconclusive"
    assert persisted["followup_rounds"] == first["followup_rounds"]
    assert persisted["stop_reason"] == first["stop_reason"] == "coordinator_stop"
    assert captured[0]["budget"].max_followup_rounds == 2
    assert captured[0]["context"].run_id == run.run_id
    assert captured[0]["context"].possible_fault_types == [
        "container-kill",
        "cpu-hog",
    ]
    assert captured[1]["context"].possible_fault_types == []
    assert "must-not-reach-model" not in repr(captured[0]["context"])
    assert "chaos_profile" not in repr(captured[0]["context"])
    assert len(storage.list_events(run.run_id)) == 2
    assert all(event.event_type == "multiagent_rca_completed" for event in storage.list_events(run.run_id))
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/runs/{run_id}/multiagent-rca" in paths


def test_multiagent_execution_persists_failure_and_returns_error(platform, monkeypatch):
    client, storage, run = platform
    monkeypatch.setattr(api, "MCPToolClient", lambda url, timeout: object())
    monkeypatch.setattr(api, "answer_multiagent_rca", lambda **kwargs: result("error"))
    response = client.post("/api/runs/run-test/multiagent-rca")
    assert response.status_code == 502
    detail = response.json()["detail"]
    persisted = json.loads(Path(detail["artifact_path"]).read_text())
    assert persisted["status"] == "error"
    assert storage.list_events(run.run_id)[0].event_type == "multiagent_rca_failed"


def test_multiagent_execution_uses_history_without_run_mcp_endpoint(
    platform, monkeypatch
):
    client, storage, _run = platform
    assert client.post("/api/runs/missing/multiagent-rca").status_code == 404
    storage.get_experiment = lambda *_args: SimpleNamespace(
        spec={"scope": {"namespace": "sock-shop"}, "observability": {}}
    )
    endpoints = []
    monkeypatch.setattr(
        api,
        "MCPToolClient",
        lambda url, timeout: endpoints.append(url) or object(),
    )
    monkeypatch.setattr(api, "answer_multiagent_rca", lambda **_kwargs: result())

    response = client.post("/api/runs/run-test/multiagent-rca")

    assert response.status_code == 200
    assert endpoints == [api.DEFAULT_HISTORICAL_MCP_SSE_URL]
    assert response.json()["observability_source"] == "history"
