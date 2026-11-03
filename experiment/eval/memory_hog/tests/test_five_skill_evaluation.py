import json
import subprocess
from pathlib import Path

from experiment.eval.memory_hog import run_five_skill_evaluation as five
from experiment.eval.memory_hog import run_targeted_training as targeted
from experiment.eval.memory_hog.run_five_skill_evaluation import (
    FIVE_RUN_CELLS,
    build_quality_summary,
)


def test_five_run_matrix_covers_controlled_application_faults():
    assert [cell["chaos_type"] for cell in FIVE_RUN_CELLS] == [
        "cpu-hog",
        "memory-hog",
        "network-latency",
        "container-kill",
        "http-status-code",
    ]
    assert len({cell["service"] for cell in FIVE_RUN_CELLS}) == 5
    assert not any(cell["service"] == "queue-master" for cell in FIVE_RUN_CELLS)


def test_json_safe_serializes_non_finite_metrics_as_null():
    report = targeted.json_safe({"fault": [float("nan"), float("inf"), 0.5]})

    assert report == {"fault": [None, None, 0.5]}
    assert json.loads(json.dumps(report, allow_nan=False)) == report


def test_quality_summary_scores_service_and_category_and_keeps_abstentions():
    rows = [
        {
            "phase": "test",
            "status": "completed",
            "run_id": "run-1",
            "chaos_type": "memory-hog",
            "service": "catalogue",
        },
        {
            "phase": "test",
            "status": "completed",
            "run_id": "run-2",
            "chaos_type": "container-kill",
            "service": "user",
        },
    ]
    responses = {
        "run-1": {
            "status": "confirmed",
            "diagnosis": {
                "primary_service": "catalogue",
                "category": "memory-exhaustion",
                "fault_type": "memory-hog",
            },
        },
        "run-2": {
            "status": "inconclusive",
            "diagnosis": {
                "primary_service": None,
                "category": "unknown",
                "fault_type": "container-kill",
            },
        },
    }

    summary = build_quality_summary(
        campaign_id="test",
        expected_runs=5,
        run_rows=rows,
        effect_reports=[{"passed": True}, {"passed": False}],
        responses=responses,
        failed_skill_run_ids=[],
        skill_failure_reasons={},
        fault_categories={
            "memory-hog": "memory-exhaustion",
            "container-kill": "pod-failure",
        },
    )

    assert summary["completed_runs"] == 2
    assert summary["effect_checks_passed"] == 1
    assert summary["confirmed"] == 1
    assert summary["inconclusive"] == 1
    assert summary["service_accuracy"] == 0.5
    assert summary["fault_category_accuracy"] == 0.5
    assert summary["fault_type_accuracy"] == 1.0
    assert summary["joint_accuracy"] == 0.5
    assert summary["score_denominator"] == 2


def test_skill_rca_logs_failures_without_dumping_full_response(
    monkeypatch, tmp_path, capsys
):
    valid_response = {
        "status": "confirmed",
        "diagnosis": {"primary_service": "catalogue", "category": "memory-exhaustion"},
    }

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        if "run-ok" in command:
            output.write_text(json.dumps(valid_response), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, json.dumps(valid_response), "")
        return subprocess.CompletedProcess(
            command, 1, "", "Skill RCA HTTP 502: invalid diagnosis JSON"
        )

    monkeypatch.setattr(five.subprocess, "run", fake_run)
    responses, failures, reasons = five._run_skill_rca(
        [{"run_id": "run-ok"}, {"run_id": "run-error"}],
        tmp_path,
    )

    assert responses == {"run-ok": valid_response}
    assert failures == ["run-error"]
    assert reasons == {"run-error": "Skill RCA HTTP 502: invalid diagnosis JSON"}
    assert (tmp_path / "run-ok.log").exists()
    assert "invalid diagnosis JSON" in (tmp_path / "run-error.log").read_text(
        encoding="utf-8"
    )
    assert len(capsys.readouterr().out) < 100
