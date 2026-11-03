from __future__ import annotations

from pathlib import Path

import pytest

from experiment.eval.memory_hog import evaluate_skill_rca


def test_campaign_report_tracks_edge_latency_fault_coverage_and_regressions():
    coverage = evaluate_skill_rca._edge_latency_coverage(
        {
            "evidence": [
                {
                    "status": "ok",
                    "fault_observation_count": 3,
                    "digest": {
                        "metrics": {
                            "edge_latency_service_graph_server_p90_s": {
                                "status": "ok",
                                "top_delta": [
                                    {"service": "shipping", "direction": "increase"}
                                ],
                            }
                        }
                    },
                }
            ]
        }
    )
    result = {
        "run_id": "run-1",
        "status": "confirmed",
        "tools_used": ["prometheus_range_query"],
        "fault_evidence_families": ["metrics"],
        "edge_latency_coverage": coverage,
        "localization_correct": True,
        "fault_type_correct": True,
    }

    summary = evaluate_skill_rca._summarize([result], [])

    assert summary["fault_type_classification"] == {
        "correct": 1,
        "evaluated": 1,
        "accuracy": 1.0,
    }
    assert set(summary).isdisjoint({"fault_classification", "both_correct"})
    assert coverage["edge_latency_service_graph_server_p90_s"] == {
        "status": "ok",
        "fault_observation_count": 3,
        "regressed_edges": 1,
    }
    assert (
        summary["edge_latency_fault_coverage_runs"][
            "edge_latency_service_graph_server_p90_s"
        ]
        == 1
    )
    assert (
        summary["edge_latency_regression_runs"][
            "edge_latency_service_graph_server_p90_s"
        ]
        == 1
    )


def test_campaign_summary_handles_legacy_reports_without_fault_type_scores():
    result = {
        "run_id": "run-1",
        "status": "inconclusive",
        "localization_correct": False,
        "fault_type_correct": False,
        "legacy": {
            "localization_correct": False,
        },
    }

    summary = evaluate_skill_rca._summarize([result], [{"run_id": "run-1"}])

    assert summary["paired_completed"] == 1
    assert summary["paired_gains"]["fault_type"] == 0
    assert summary["paired_regressions"]["fault_type"] == 0


def test_skill_cohort_can_be_loaded_from_campaign_manifest_without_legacy_report(
    tmp_path: Path,
):
    (tmp_path / "config.yaml").write_text(
        "experiment_campaign_id: latest\n"
        "chaos_types:\n"
        "  - name: network-latency\n",
        encoding="utf-8",
    )
    (tmp_path / "runs.csv").write_text(
        "phase,chaos_type,service,run_id,status\n"
        "test,network-latency,shipping,run-1,completed\n"
        "test,network-latency,orders,,not_run\n",
        encoding="utf-8",
    )

    cohort = evaluate_skill_rca.load_cohort(tmp_path)

    assert cohort == [
        {
            "run_id": "run-1",
            "run_status": "completed",
            "rca_status": None,
            "service": "shipping",
            "chaos_type": "network-latency",
            "ground_truth_service": "shipping",
        }
    ]


@pytest.mark.parametrize("status", ["confirmed", "inconclusive"])
def test_evaluation_preserves_and_logs_selected_cause_and_category(
    tmp_path, monkeypatch, capsys, status
):
    monkeypatch.setattr(
        evaluate_skill_rca,
        "_call_skill_rca",
        lambda *_args: {
            "status": status,
            "observability_source": "history",
            "diagnosis": {
                "primary_service": "orders",
                "fault_type": "cpu-hog",
                "category": "cpu-exhaustion",
            },
        },
    )
    report = evaluate_skill_rca.evaluate(
        api_base="http://unused",
        cohort=[{
            "run_id": "run-1",
            "service": "orders",
            "ground_truth_service": "orders",
            "chaos_type": "cpu-hog",
        }],
        campaign_id="campaign-1",
        output=tmp_path / "evaluation.json",
        possible_fault_types=["cpu-hog", "memory-hog"],
    )

    result = report["results"][0]
    assert result["status"] == status
    assert result["fault_type"] == "cpu-hog"
    assert result["category"] == "cpu-exhaustion"
    assert result["fault_type_correct"] is True
    output = capsys.readouterr().out
    assert "fault_type=cpu-hog" in output
    assert "category=cpu-exhaustion" in output
    assert "category=None" not in output


@pytest.mark.parametrize("fault_type", [None, "", "unknown", "outside-options"])
def test_skill_evaluation_rejects_missing_or_unselectable_cause(
    monkeypatch, fault_type
):
    monkeypatch.setattr(
        evaluate_skill_rca,
        "_call_skill_rca",
        lambda *_args: {
            "status": "inconclusive",
            "observability_source": "history",
            "diagnosis": {
                "primary_service": "orders",
                "category": "cpu-exhaustion",
                "fault_type": fault_type,
            },
        },
    )

    result = evaluate_skill_rca._attempt(
        "http://unused",
        {
            "run_id": "run-1",
            "service": "orders",
            "ground_truth_service": "orders",
            "chaos_type": "cpu-hog",
        },
        10,
        ["cpu-hog", "memory-hog"],
    )

    assert result["status"] == "invalid_response"
    assert "fault_type" in result["error"]
    assert result["valid_response"] is False
    assert result["localization_correct"] is False
    assert result["fault_type_correct"] is False


def test_skill_evaluation_rejects_response_without_historical_source(monkeypatch):
    monkeypatch.setattr(
        evaluate_skill_rca,
        "_call_skill_rca",
        lambda *_args: {
            "status": "confirmed",
            "diagnosis": {
                "primary_service": "orders",
                "fault_type": "cpu-hog",
                "category": "cpu-exhaustion",
            },
        },
    )

    result = evaluate_skill_rca._attempt(
        "http://unused",
        {
            "run_id": "run-1",
            "service": "orders",
            "ground_truth_service": "orders",
            "chaos_type": "cpu-hog",
        },
        10,
        ["cpu-hog"],
    )

    assert result["status"] == "invalid_response"
    assert result["valid_response"] is False
    assert "historical observability" in result["error"]
