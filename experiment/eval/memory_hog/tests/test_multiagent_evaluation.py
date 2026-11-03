import io
import json

import pytest

from experiment.eval.memory_hog import evaluate_multiagent_rca as evaluator
from experiment.eval.memory_hog.evaluate_multiagent_rca import (
    assess_run_quality,
    compare_evaluations,
    score_diagnosis,
)


def test_evaluation_preserves_all_replicas_of_the_same_service_fault(monkeypatch, tmp_path):
    config = {
        "experiment_campaign_id": "replicas",
        "test_experiments": [
            {"service": "orders", "chaos_type": "cpu-hog", "replica_idx": replica}
            for replica in range(5)
        ],
        "chaos_types": [{"name": "cpu-hog"}],
    }
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    (tmp_path / "runs.csv").write_text(
        "phase,chaos_type,service,replica_idx,run_id,status\n"
        + "".join(f"test,cpu-hog,orders,{i},run-{i},completed\n" for i in range(5))
    )
    requested = []
    requested_options = []

    def request(run_id, **kwargs):
        requested.append(run_id)
        requested_options.append(kwargs["possible_fault_types"])
        return {"status": "inconclusive", "diagnosis": {"primary_service": "orders", "category": "cpu-exhaustion"}}

    monkeypatch.setattr(evaluator, "request_multiagent_rca", request)
    monkeypatch.setattr(evaluator, "_get_strategy_info", lambda **kw: {
        "strategy_version": evaluator.STRATEGY_VERSION, "prompt_version": evaluator.PROMPT_VERSION,
    })
    _, path = evaluator.evaluate_campaign(tmp_path)
    report = json.loads(path.read_text())
    assert requested == [f"run-{i}" for i in range(5)]
    assert report["expected_runs"] == 5
    assert [r["replica_idx"] for r in report["per_run"]] == list(range(5))
    assert report["possible_fault_types"] == ["cpu-hog"]
    assert requested_options == [["cpu-hog"]] * 5


def test_scores_service_and_exact_fault_type_only():
    response = {
        "status": "confirmed",
        "diagnosis": {
            "primary_service": "payment",
            "category": "cpu-exhaustion",
            "fault_type": "cpu-hog",
        },
    }

    score = score_diagnosis(response, "payment", expected_fault_type="cpu-hog")

    assert score == {
        "predicted_service": "payment",
        "service_correct": True,
        "predicted_fault_type": "cpu-hog",
        "fault_type_correct": True,
    }


def test_missing_diagnosis_scores_incorrect():
    score = score_diagnosis(
        None, "catalogue", expected_fault_type="memory-hog"
    )

    assert score == {
        "predicted_service": None,
        "service_correct": False,
        "predicted_fault_type": None,
        "fault_type_correct": False,
    }


def test_multiagent_quality_preserves_category_separately_from_cause():
    quality = assess_run_quality(
        {
            "status": "inconclusive",
            "diagnosis": {
                "primary_service": "orders",
                "fault_type": "cpu-hog",
                "category": "cpu-exhaustion",
            },
        },
        "orders",
        effect_validated=None,
        expected_fault_type="cpu-hog",
        possible_fault_types=["cpu-hog", "memory-hog"],
    )

    assert quality["predicted_fault_type"] == "cpu-hog"
    assert quality["category"] == "cpu-exhaustion"
    assert quality["fault_type_correct"] is True


@pytest.mark.parametrize("fault_type", [None, "", "unknown", "outside-options"])
def test_multiagent_request_rejects_missing_or_unselectable_cause(
    monkeypatch, fault_type
):
    class JsonResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    response = {
        "status": "inconclusive",
        "diagnosis": {
            "primary_service": "orders",
            "category": "cpu-exhaustion",
            "fault_type": fault_type,
        },
        "strategy_version": evaluator.STRATEGY_VERSION,
        "prompt_version": evaluator.PROMPT_VERSION,
    }
    monkeypatch.setattr(
        evaluator,
        "urlopen",
        lambda *_args, **_kwargs: JsonResponse(json.dumps(response).encode("utf-8")),
    )

    result = evaluator.request_multiagent_rca(
        "run-1", possible_fault_types=["cpu-hog", "memory-hog"]
    )

    assert result["status"] == "error"
    assert "fault_type" in result["error"]
    assert result["response"] == response


def test_edge_latency_coverage_is_reported_in_campaign_quality():
    response = {
        "status": "inconclusive",
        "diagnosis": {"primary_service": "orders", "category": "unknown"},
        "calls": [
            {
                "tool": "prometheus_range_query",
                "status": "ok",
                "fault_observation_count": 4,
                "result": {
                    "metrics": {
                        "edge_latency_istio_mean_ms": {
                            "status": "ok",
                            "top_delta": [
                                {"service": "orders", "direction": "increase"}
                            ],
                        }
                    }
                },
            }
        ],
    }

    quality = assess_run_quality(
        response, "orders", effect_validated=None
    )
    quality["status"] = "inconclusive"
    summary = evaluator._summarize_runs([quality])

    assert quality["edge_latency_coverage"]["edge_latency_istio_mean_ms"] == {
        "status": "ok",
        "fault_observation_count": 4,
        "regressed_edges": 1,
    }
    assert (
        summary["edge_latency_fault_coverage_runs"]["edge_latency_istio_mean_ms"]
        == 1
    )
    assert summary["edge_latency_regression_runs"]["edge_latency_istio_mean_ms"] == 1


def test_assesses_metric_candidates_scope_revisions_and_independent_coverage():
    response = {
        "status": "confirmed",
        "diagnosis": {
            "status": "confirmed",
            "primary_service": "orders",
            "category": "memory-exhaustion",
            "supporting_evidence": ["E001", "E002"],
        },
        "investigation_scope": {
            "metric_candidates": ["catalogue", "orders"],
            "services": ["orders"],
            "preliminary_category": "memory-exhaustion",
            "history": [{"services": ["catalogue"]}, {"services": ["orders"]}],
            "revisions": [{"service": "orders"}],
        },
        "calls": [
            {
                "evidence_id": "E001",
                "family": "metrics",
                "service": "orders",
                "status": "ok",
                "fault_observation_count": 3,
            },
            {
                "evidence_id": "E002",
                "family": "logs",
                "service": "orders",
                "scope_services": ["orders"],
                "status": "ok",
                "fault_observation_count": 1,
            },
            {
                "evidence_id": "E003",
                "family": "traces",
                "scope_services": ["orders"],
                "status": "empty",
                "fault_observation_count": 0,
            },
        ],
        "evidence": [
            {"evidence_id": "E001", "family": "metrics", "status": "ok"},
            {"evidence_id": "E002", "family": "logs", "status": "ok"},
            {"evidence_id": "E003", "family": "traces", "status": "empty"},
        ],
        "model_attempts": [
            {
                "stage": "coordinator_followup",
                "status": "error",
                "repair_count": 1,
            }
        ],
        "specialists": [{"status": "error"}, {"status": "no_data"}],
        "budget": {
            "model_calls_used": 7,
            "tool_calls_used": 8,
            "tokens_used": 3000,
            "elapsed_seconds": 12.5,
            "validation_failures": 0,
        },
    }

    quality = assess_run_quality(
        response,
        "orders",
        effect_validated=True,
    )

    assert quality["target_in_metric_candidates"] is True
    assert quality["target_in_investigation_scope"] is True
    assert "category_correct" not in quality
    assert quality["scope_revision_count"] == 1
    assert quality["scope_violations"] == 0
    assert quality["fault_evidence_by_family"] == {"metrics": 1, "logs": 1}
    assert quality["confirmed_without_independent_family_support"] is False
    assert quality["repair_calls"] == 1
    assert quality["specialist_failures"] == 1
    assert quality["coordinator_failures"] == 1
    assert quality["model_attempt_failures"] == 1


def test_assesses_partial_data_separately_from_empty_trace_searches():
    response = {
        "status": "inconclusive",
        "diagnosis": {
            "status": "inconclusive",
            "primary_service": "orders",
            "category": "network-latency",
            "category_confidence": "low",
            "supporting_evidence": [],
        },
        "investigation_scope": {
            "metric_candidates": ["orders"],
            "services": ["orders"],
            "history": [{"services": ["orders"]}],
        },
        "calls": [
            {
                "evidence_id": "E001",
                "family": "metrics",
                "service": "orders",
                "status": "partial",
                "fault_observation_count": 2,
            },
            {
                "evidence_id": "E002",
                "family": "logs",
                "service": "orders",
                "status": "ok",
                "fault_observation_count": 1,
            },
            {
                "evidence_id": "E003",
                "family": "traces",
                "scope_services": ["orders"],
                "status": "no_data",
                "fault_observation_count": 0,
            },
        ],
        "evidence": [
            {"evidence_id": "E001", "family": "metrics", "status": "partial"},
            {"evidence_id": "E002", "family": "logs", "status": "ok"},
            {"evidence_id": "E003", "family": "traces", "status": "no_data"},
        ],
        "telemetry_coverage": {
            "families": {
                "metrics": {"statuses": {"partial": 1}},
                "logs": {"statuses": {"ok": 1}},
                "traces": {"statuses": {"no_data": 1}},
            },
            "trace_search_probes": {
                "attempted": 1,
                "traces_present": 0,
                "empty_windows": 1,
                "tool_errors": 0,
            },
        },
    }

    quality = assess_run_quality(
        response,
        "orders",
        effect_validated=None,
    )

    assert quality["fault_evidence_by_family"] == {"metrics": 1, "logs": 1}
    assert quality["partial_fault_evidence_by_family"] == {"metrics": 1}
    assert quality["complete_fault_evidence_by_family"] == {"logs": 1}
    assert quality["query_statuses_by_family"]["traces"] == {"no_data": 1}
    assert quality["trace_search_probes"]["empty_windows"] == 1


def test_scope_audit_detects_unauthorized_or_unverifiable_calls():
    response = {
        "status": "inconclusive",
        "diagnosis": {"primary_service": None, "category": "network-loss"},
        "investigation_scope": {
            "metric_candidates": [],
            "services": [],
            "history": [{"services": []}],
        },
        "calls": [
            {
                "family": "logs",
                "service": "orders",
                "scope_services": ["catalogue"],
                "status": "ok",
            },
            {"family": "traces", "service": "orders", "status": "ok"},
        ],
    }

    quality = assess_run_quality(
        response,
        "orders",
        effect_validated=None,
    )

    assert quality["scope_violations"] == 1
    assert quality["unverified_scope_calls"] == 1


def test_summary_stratifies_effect_validation_and_reports_pipeline_metrics():
    runs = [
        {
            "status": "confirmed",
            "valid_response": True,
            "effect_validated": True,
            "service_correct": True,
            "fault_type_correct": False,
            "target_in_metric_candidates": True,
            "target_in_investigation_scope": True,
            "scope_violations": 0,
            "unverified_scope_calls": 0,
            "scope_revision_count": 1,
            "specialist_failures": 0,
            "model_attempt_failures": 1,
            "coordinator_failures": 1,
            "repair_calls": 1,
            "validation_failures": 0,
            "confirmed_incorrect": True,
            "confirmed_without_independent_family_support": False,
            "fault_evidence_by_family": {"metrics": 1, "logs": 1},
            "model_calls": 6,
            "tool_calls": 5,
            "tokens": 1000,
            "elapsed_seconds": 10,
        },
        {
            "status": "error",
            "valid_response": False,
            "effect_validated": True,
            "service_correct": False,
            "fault_type_correct": False,
            "target_in_metric_candidates": None,
            "target_in_investigation_scope": None,
            "scope_violations": 0,
            "unverified_scope_calls": 0,
            "scope_revision_count": 0,
            "specialist_failures": 0,
            "model_attempt_failures": 0,
            "coordinator_failures": 0,
            "repair_calls": 0,
            "validation_failures": 0,
            "confirmed_incorrect": False,
            "confirmed_without_independent_family_support": False,
            "fault_evidence_by_family": {},
            "model_calls": None,
            "tool_calls": None,
            "tokens": None,
            "elapsed_seconds": None,
        },
    ]

    summary = evaluator._summarize_runs(runs)

    assert summary["errors_or_missing_runs"] == 1
    assert summary["metric_candidate_recall"] == 0.5
    assert summary["valid_response_metric_candidate_recall"] == 1.0
    assert summary["effect_validated_runs"] == 2
    assert summary["effect_validated_service_accuracy"] == 0.5
    assert summary["effect_validated_fault_type_accuracy"] == 0.0
    assert "categories_accuracy" not in summary
    assert summary["fault_family_coverage"] == {"metrics": 1, "logs": 1, "traces": 0}
    assert summary["repair_calls"] == 1


def test_paired_comparison_uses_only_shared_run_ids():
    baseline = {
        "campaign_id": "campaign-1",
        "possible_fault_types": ["cpu-hog", "memory-hog"],
        "strategy_version": "v1",
        "per_run": [
            {
                "run_id": "run-a",
                "chaos_type": "cpu-hog",
                "expected_service": "orders",
                "service_correct": False,
                "fault_type_correct": True,
                "target_in_metric_candidates": False,
                "scope_violations": 1,
                "scope_revision_count": 0,
                "fault_evidence_by_family": {"metrics": 1, "logs": 0, "traces": 0},
            },
            {
                "run_id": "run-b",
                "chaos_type": "memory-hog",
                "expected_service": "payment",
                "service_correct": True,
                "fault_type_correct": False,
                "target_in_metric_candidates": True,
                "scope_violations": 0,
                "scope_revision_count": 0,
                "fault_evidence_by_family": {"metrics": 1, "logs": 1, "traces": 0},
            },
            {
                "run_id": "run-only-baseline",
                "service_correct": True,
                "fault_type_correct": True,
            },
        ],
    }
    current = {
        "campaign_id": "campaign-1",
        "possible_fault_types": ["cpu-hog", "memory-hog"],
        "strategy_version": "v2",
        "per_run": [
            {
                "run_id": "run-a",
                "chaos_type": "cpu-hog",
                "expected_service": "orders",
                "service_correct": True,
                "fault_type_correct": False,
                "target_in_metric_candidates": True,
                "scope_violations": 0,
                "scope_revision_count": 1,
                "fault_evidence_by_family": {"metrics": 1, "logs": 1, "traces": 1},
            },
            {
                "run_id": "run-b",
                "chaos_type": "memory-hog",
                "expected_service": "payment",
                "service_correct": False,
                "fault_type_correct": True,
                "target_in_metric_candidates": True,
                "scope_violations": 0,
                "scope_revision_count": 0,
                "fault_evidence_by_family": {"metrics": 1, "logs": 0, "traces": 0},
            },
            {
                "run_id": "run-only-current",
                "service_correct": True,
                "fault_type_correct": True,
            },
        ],
    }

    comparison = compare_evaluations(current, baseline)

    assert comparison["paired_run_count"] == 2
    assert comparison["baseline_strategy_version"] == "v1"
    assert comparison["deltas"]["service_correct"] == {
        "improved": 1,
        "regressed": 1,
        "net_change": 0,
        "paired_observations": 2,
    }
    assert comparison["deltas"]["fault_type_correct"]["net_change"] == 0
    assert "category_correct" not in comparison["deltas"]
    assert comparison["deltas"]["target_in_metric_candidates"]["improved"] == 1
    assert comparison["deltas"]["fault_traces_covered"]["improved"] == 1
    assert comparison["count_deltas"]["scope_violations"]["net_change"] == -1


def test_paired_comparison_rejects_incompatible_campaigns_options_and_labels():
    report = {
        "campaign_id": "campaign-1",
        "possible_fault_types": ["cpu-hog"],
        "per_run": [
            {
                "run_id": "run-a",
                "chaos_type": "cpu-hog",
                "expected_service": "orders",
            }
        ],
    }

    with pytest.raises(ValueError, match="different campaigns"):
        compare_evaluations(
            report,
            {**report, "campaign_id": "campaign-2"},
        )
    with pytest.raises(ValueError, match="different fault options"):
        compare_evaluations(
            report,
            {**report, "possible_fault_types": ["memory-hog"]},
        )
    with pytest.raises(ValueError, match="inconsistent service labels"):
        compare_evaluations(
            report,
            {
                **report,
                "per_run": [
                    {
                        **report["per_run"][0],
                        "expected_service": "payment",
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    "strategy_version,prompt_version",
    [
        ("stale", evaluator.PROMPT_VERSION),
        (evaluator.STRATEGY_VERSION, "stale"),
    ],
)
def test_evaluator_preflight_rejects_version_mismatch_before_creating_output(
    tmp_path,
    monkeypatch,
    strategy_version,
    prompt_version,
):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    (campaign_dir / "config.yaml").write_text(
        "test_experiments: []\nchaos_types: []\n",
        encoding="utf-8",
    )
    (campaign_dir / "runs.csv").write_text("phase,run_id,status\n", encoding="utf-8")
    monkeypatch.setattr(
        evaluator,
        "_get_strategy_info",
        lambda **_kwargs: {
            "strategy_version": strategy_version,
            "prompt_version": prompt_version,
        },
    )

    with pytest.raises(RuntimeError, match="does not match"):
        evaluator.evaluate_campaign(campaign_dir)

    assert not (campaign_dir / "multiagent_rca").exists()


def test_request_rejects_unverified_prompt_version(monkeypatch):
    class JsonResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    response = {
        "status": "inconclusive",
        "diagnosis": {},
        "strategy_version": evaluator.STRATEGY_VERSION,
        "prompt_version": "stale-prompt",
    }
    monkeypatch.setattr(
        evaluator,
        "urlopen",
        lambda *_args, **_kwargs: JsonResponse(json.dumps(response).encode("utf-8")),
    )

    result = evaluator.request_multiagent_rca("run-1")

    assert result["status"] == "error"
    assert result["expected_prompt_version"] == evaluator.PROMPT_VERSION
    assert result["response_prompt_version"] == "stale-prompt"


def test_campaign_evaluation_writes_quality_report_and_paired_comparison(
    tmp_path,
    monkeypatch,
):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    (campaign_dir / "config.yaml").write_text(
        "experiment_campaign_id: campaign-1\n"
        "test_experiments:\n"
        "  - chaos_type: cpu-hog\n"
        "    service: orders\n",
        encoding="utf-8",
    )
    (campaign_dir / "runs.csv").write_text(
        "phase,chaos_type,service,run_id,status\n"
        "test,cpu-hog,orders,run-1,completed\n",
        encoding="utf-8",
    )
    (campaign_dir / "effect_validation.json").write_text(
        json.dumps([{"run_id": "run-1", "passed": True}]),
        encoding="utf-8",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "campaign_id": "campaign-1",
                "possible_fault_types": ["cpu-hog"],
                "strategy_version": "baseline-v1",
                "per_run": [
                    {
                        "run_id": "run-1",
                        "chaos_type": "cpu-hog",
                        "expected_service": "orders",
                        "service_correct": False,
                        "fault_type_correct": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    response = {
        "status": "inconclusive",
        "diagnosis": {
            "status": "inconclusive",
            "primary_service": "orders",
            "category": "cpu-exhaustion",
            "fault_type": "cpu-hog",
            "supporting_evidence": [],
        },
        "investigation_scope": {
            "metric_candidates": ["orders"],
            "services": ["orders"],
            "preliminary_category": "cpu-exhaustion",
            "history": [{"services": ["orders"]}],
            "revisions": [],
        },
        "calls": [],
        "evidence": [],
        "specialists": [],
        "model_attempts": [],
        "budget": {},
    }
    monkeypatch.setattr(
        evaluator,
        "_get_strategy_info",
        lambda **_kwargs: {
            "strategy_version": evaluator.STRATEGY_VERSION,
            "prompt_version": evaluator.PROMPT_VERSION,
        },
    )
    monkeypatch.setattr(
        evaluator,
        "request_multiagent_rca",
        lambda *_args, **_kwargs: response,
    )
    skill_path = tmp_path / "skill-evaluation.json"
    skill_path.write_text(
        json.dumps(
            {
                "campaign_id": "campaign-1",
                "possible_fault_types": ["cpu-hog"],
                "results": [
                    {
                        "run_id": "run-1",
                        "chaos_type": "cpu-hog",
                        "ground_truth_service": "orders",
                        "status": "inconclusive",
                        "fault_type": "cpu-hog",
                        "fault_type_correct": True,
                        "localization_correct": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    markdown_path, json_path = evaluator.evaluate_campaign(
        campaign_dir,
        compare_evaluation_path=baseline_path,
        compare_skill_evaluation_path=skill_path,
    )

    report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert report["strategy_version"] == evaluator.STRATEGY_VERSION
    assert report["effect_validated_runs"] == 1
    assert report["effect_validated_service_accuracy"] == 1.0
    assert report["effect_validated_fault_type_accuracy"] == 1.0
    assert "categories_accuracy" not in report
    assert "category_correct" not in report["per_run"][0]
    assert report["paired_comparison"]["paired_run_count"] == 1
    assert report["paired_comparison"]["deltas"]["service_correct"]["improved"] == 1
    assert "category_correct" not in report["paired_comparison"]["deltas"]
    assert set(report["skill_vs_multiagent_comparison"]["metrics"]) == {
        "service_correct",
        "fault_type_correct",
    }
    comparison = report["skill_vs_multiagent_comparison"]
    assert comparison["paired_run_count"] == 1
    assert comparison["metrics"]["fault_type_correct"]["multiagent_accuracy"] == 1.0
    assert "recall do alvo no ranking métrico" in markdown
    assert "Comparação pareada" in markdown
    assert "Skill RCA vs multiagentes" in markdown
    assert "categoria" not in markdown.casefold()
