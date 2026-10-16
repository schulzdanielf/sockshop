from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiment.eval.memory_hog import evaluate_skill_rca as evaluation


def _baseline(run_id: str, *, status: str = "completed") -> dict:
    return {
        "run_id": run_id,
        "service": "carts",
        "chaos_type": "container-kill",
        "ground_truth_service": "carts",
        "ground_truth_fault_category": "pod-failure",
        "run_status": "completed",
        "rca_status": status,
        "localized_service": "carts",
        "localization_correct": True,
        "cause": "memory-exhaustion",
        "fault_category_correct": False,
    }


def test_load_cohort_uses_only_the_baseline_completed_runs(tmp_path):
    (tmp_path / "runs.csv").write_text(
        "run_id,status,service,chaos_type\n"
        "run-a,completed,carts,container-kill\n"
        "run-b,failed,carts,container-kill\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {"results": [_baseline("run-a"), _baseline("run-b", status="http_error")]}
        ),
        encoding="utf-8",
    )
    assert [
        row["run_id"] for row in evaluation.load_cohort(tmp_path, baseline_path)
    ] == ["run-a"]


def test_load_cohort_rejects_changed_run_truth(tmp_path):
    (tmp_path / "runs.csv").write_text(
        "run_id,status,service,chaos_type\nrun-a,completed,orders,cpu-hog\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"results": [_baseline("run-a")]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="disagree"):
        evaluation.load_cohort(tmp_path, baseline_path)


def test_skill_evaluation_records_metrics_paired_gains_and_never_overwrites(
    tmp_path, monkeypatch
):
    cohort = [_baseline("run-a"), _baseline("run-b")]
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        evaluation,
        "_call_skill_rca",
        lambda api, run_id, timeout: {
            "status": "confirmed",
            "diagnosis": {
                "primary_service": "carts",
                "category": "pod-failure" if run_id == "run-a" else "memory-exhaustion",
                "limitations": [],
            },
            "evidence": [
                {"family": "metrics", "status": "ok", "fault_observation_count": 5},
                {"family": "logs", "status": "ok", "fault_observation_count": 1},
            ],
            "tools_used": ["get_run_golden_signals", "loki_range_query"],
            "attempt_id": "attempt-" + run_id,
            "artifact_path": f"/objects/{run_id}.json",
            "skill": {"sha256": "abc"},
            "model": "qwen",
        },
    )
    report = evaluation.evaluate(
        api_base="http://localhost:8012",
        cohort=cohort,
        campaign_id="campaign",
        output=output,
    )
    assert report["summary"]["service_localization"] == {
        "correct": 2,
        "evaluated": 2,
        "accuracy": 1.0,
    }
    assert report["summary"]["fault_classification"] == {
        "correct": 1,
        "evaluated": 2,
        "accuracy": 0.5,
    }
    assert report["summary"]["paired_gains"] == {
        "localization": 0,
        "fault_category": 1,
        "both": 1,
    }
    assert report["results"][0]["fault_evidence_families"] == ["logs", "metrics"]
    assert json.loads(output.read_text())["summary"] == report["summary"]
    with pytest.raises(FileExistsError):
        evaluation.evaluate(
            api_base="http://localhost",
            cohort=cohort,
            campaign_id="campaign",
            output=output,
        )


def test_skill_http_error_keeps_attempt_artifact_reference(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    def fail(*args, **kwargs):
        raise HTTPError(
            "http://api",
            502,
            "Bad Gateway",
            {},
            BytesIO(
                b'{"detail":{"error":"Qwen offline","attempt_id":"att","artifact_path":"saved.json"}}'
            ),
        )

    monkeypatch.setattr(evaluation, "_call_skill_rca", fail)
    result = evaluation._attempt("http://api", _baseline("run-a"), 30)
    assert result["status"] == "http_error"
    assert result["attempt_id"] == "att"
    assert result["artifact_path"] == "saved.json"
