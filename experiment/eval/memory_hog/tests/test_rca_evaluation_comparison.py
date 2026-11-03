from __future__ import annotations

import pytest

from experiment.eval.memory_hog.rca_evaluation_common import (
    campaign_fault_types,
    compare_skill_and_multiagent,
)


def test_campaign_fault_types_are_derived_from_the_test_matrix():
    assert campaign_fault_types(
        {
            "test_experiments": [
                {"chaos_type": "network-latency"},
                {"chaos_type": "cpu-hog"},
                {"chaos_type": "network-latency"},
            ]
        },
        [],
    ) == ["cpu-hog", "network-latency"]


def test_partial_campaign_can_declare_the_full_benchmark_fault_options():
    options = [
        "container-kill",
        "cpu-hog",
        "http-status-code",
        "memory-hog",
        "network-latency",
    ]
    assert campaign_fault_types(
        {
            "possible_fault_types": options,
            "test_experiments": [{"chaos_type": "http-status-code"}],
        },
        [],
    ) == options


def test_declared_fault_options_must_cover_the_test_matrix():
    with pytest.raises(ValueError, match="outside possible_fault_types"):
        campaign_fault_types(
            {
                "possible_fault_types": ["http-status-code"],
                "test_experiments": [{"chaos_type": "memory-hog"}],
            },
            [],
        )


def test_paired_comparison_uses_shared_run_ids_and_counts_failures_as_wrong():
    skill = {
        "campaign_id": "campaign-a",
        "possible_fault_types": ["container-kill", "cpu-hog"],
        "results": [
            {
                "run_id": "run-1",
                "chaos_type": "cpu-hog",
                "ground_truth_service": "payment",
                "status": "confirmed",
                "fault_type": "cpu-hog",
                "fault_type_correct": True,
                "localization_correct": True,
            },
            {
                "run_id": "run-2",
                "chaos_type": "container-kill",
                "ground_truth_service": "orders",
                "status": "request_error",
            },
            {"run_id": "skill-only"},
        ],
    }
    multiagent = {
        "campaign_id": "campaign-a",
        "possible_fault_types": ["container-kill", "cpu-hog"],
        "per_run": [
            {
                "run_id": "run-1",
                "chaos_type": "cpu-hog",
                "expected_service": "payment",
                "status": "inconclusive",
                "predicted_fault_type": "cpu-hog",
                "fault_type_correct": True,
                "service_correct": False,
            },
            {
                "run_id": "run-2",
                "chaos_type": "container-kill",
                "expected_service": "orders",
                "status": "error",
                "predicted_fault_type": None,
                "fault_type_correct": False,
                "service_correct": False,
            },
            {"run_id": "multiagent-only"},
        ],
    }

    comparison = compare_skill_and_multiagent(skill, multiagent)

    assert comparison["paired_run_count"] == 2
    assert comparison["skill_only_run_count"] == 1
    assert comparison["multiagent_only_run_count"] == 1
    assert comparison["metrics"]["fault_type_correct"] == {
        "denominator": 2,
        "skill_correct": 1,
        "skill_accuracy": 0.5,
        "multiagent_correct": 1,
        "multiagent_accuracy": 0.5,
        "skill_only_correct": 0,
        "multiagent_only_correct": 0,
        "net_change_for_multiagent": 0,
    }
    assert comparison["metrics"]["service_correct"] == {
        "denominator": 2,
        "skill_correct": 1,
        "skill_accuracy": 0.5,
        "multiagent_correct": 0,
        "multiagent_accuracy": 0.0,
        "skill_only_correct": 1,
        "multiagent_only_correct": 0,
        "net_change_for_multiagent": -1,
    }
    assert set(comparison["metrics"]) == {"service_correct", "fault_type_correct"}
    assert "skill_category_correct" not in comparison["per_run"][0]
    assert comparison["failed_or_invalid"] == {"skill": 1, "multiagent": 1}


def test_paired_comparison_rejects_different_fault_option_sets():
    with pytest.raises(ValueError, match="different fault options"):
        compare_skill_and_multiagent(
            {
                "campaign_id": "campaign-a",
                "possible_fault_types": ["cpu-hog"],
                "results": [],
            },
            {
                "campaign_id": "campaign-a",
                "possible_fault_types": ["network-loss"],
                "per_run": [],
            },
        )


def test_paired_comparison_rejects_missing_or_mismatched_run_truth():
    skill = {
        "campaign_id": "campaign-a",
        "possible_fault_types": ["cpu-hog"],
        "results": [
            {
                "run_id": "run-1",
                "chaos_type": "cpu-hog",
                "ground_truth_service": "orders",
            }
        ],
    }
    multiagent = {
        "campaign_id": "campaign-a",
        "possible_fault_types": ["cpu-hog"],
        "per_run": [
            {
                "run_id": "run-1",
                "chaos_type": "memory-hog",
                "expected_service": "orders",
            }
        ],
    }

    with pytest.raises(ValueError, match="inconsistent chaos_type"):
        compare_skill_and_multiagent(skill, multiagent)

    with pytest.raises(ValueError, match="must record a campaign_id"):
        compare_skill_and_multiagent(
            {**skill, "campaign_id": None},
            {**multiagent, "campaign_id": None},
        )
