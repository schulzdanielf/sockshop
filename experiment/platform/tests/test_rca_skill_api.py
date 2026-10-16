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


def test_api_uses_pinned_run_config_and_preserves_attempts(platform, monkeypatch):
    client, storage, run = platform
    calls = []

    def investigate(**kwargs):
        calls.append(kwargs)
        return result()

    monkeypatch.setattr(api, "answer_skill_rca", investigate)
    first = client.post("/api/runs/run-test/skill-rca").json()
    second = client.post("/api/runs/run-test/skill-rca").json()
    assert first["attempt_id"] != second["attempt_id"]
    assert Path(first["artifact_path"]).exists()
    persisted = json.loads(Path(first["artifact_path"]).read_text())
    assert persisted["attempt_id"] == first["attempt_id"]
    assert persisted["model"] == api.DEFAULT_LLM_MODEL
    assert calls[0]["namespace"] == "sock-shop"
    assert calls[0]["phases"]["fault"]["start"] == run.timings["t_fault_start"]
    assert "chaos_profile" not in calls[0]
    assert storage.list_events(run.run_id)[0].ts == first["generated_at"]
    assert storage.get_artifact(run.run_id, "rca_investigation") is None
    assert "/api/runs/{run_id}/rca" in client.get("/openapi.json").json()["paths"]


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


def test_multiagent_execution_persists_immutable_attempt_and_excludes_ground_truth(platform, monkeypatch):
    client, storage, run = platform
    captured = []
    monkeypatch.setattr(api, "MCPToolClient", lambda url, timeout: object())

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
    first_response = client.post("/api/runs/run-test/multiagent-rca")
    second_response = client.post("/api/runs/run-test/multiagent-rca")
    assert first_response.status_code == second_response.status_code == 200
    first, second = first_response.json(), second_response.json()
    assert first["attempt_id"] != second["attempt_id"]
    assert Path(first["artifact_path"]).exists()
    persisted = json.loads(Path(first["artifact_path"]).read_text())
    assert persisted["attempt_id"] == first["attempt_id"]
    assert persisted["status"] == "inconclusive"
    assert persisted["followup_rounds"] == first["followup_rounds"]
    assert persisted["stop_reason"] == first["stop_reason"] == "coordinator_stop"
    assert captured[0]["budget"].max_followup_rounds == 2
    assert captured[0]["context"].run_id == run.run_id
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


def test_multiagent_execution_rejects_missing_run_and_missing_endpoint(platform):
    client, storage, _run = platform
    assert client.post("/api/runs/missing/multiagent-rca").status_code == 404
    storage.get_experiment = lambda *_args: SimpleNamespace(
        spec={"scope": {"namespace": "sock-shop"}, "observability": {}}
    )
    assert client.post("/api/runs/run-test/multiagent-rca").status_code == 409
