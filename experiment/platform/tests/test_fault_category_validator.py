"""Unit tests for ``fault_category_validator.validate_fault_category``.

The validator overrides the LLM's ``fault_category`` only when exactly
one evidence rule fires unambiguously. These tests pin that contract:

* no evidence            → no override
* single OOM signal      → memory-exhaustion
* single CPU saturation  → cpu-exhaustion
* single restart counter → pod-failure
* two competing signals  → conflict, no override
* re-running is a no-op  → idempotent, original answer preserved
"""
from __future__ import annotations

from typing import Any, Dict, List

from experiment.platform.backend.analysis.fault_category_validator import (
    validate_fault_category,
)


def _hotspots(**metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a ``metric_hotspots`` dict from ``metric_id=[rows]`` kwargs."""
    return {metric_id: {"top": rows} for metric_id, rows in metrics.items()}


def _row(label: str, baseline: float, fault: float) -> Dict[str, Any]:
    return {
        "label": label,
        "baseline_mean": baseline,
        "fault_mean": fault,
        "delta_abs": fault - baseline,
    }


def test_no_signal_keeps_llm_verdict():
    analysis = {"fault_category": "cpu-exhaustion"}
    out = validate_fault_category(analysis, {"metric_hotspots": {}})

    assert out["fault_category"] == "cpu-exhaustion"
    assert out["validator_meta"]["fired"] is False
    assert out["validator_meta"]["rule"] is None


def test_oom_signal_overrides_to_memory():
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 0.0, 1.0)],
        )
    }
    analysis = {"fault_category": "pod-failure"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "memory-exhaustion"
    meta = out["validator_meta"]
    assert meta["fired"] is True
    assert meta["rule"] == "memory-exhaustion"
    assert meta["original_fault_category"] == "pod-failure"


def test_cpu_saturation_overrides_to_cpu():
    features = {
        "metric_hotspots": _hotspots(
            cpu_saturation_pct=[_row("orders", 10.0, 95.0)],
        )
    }
    analysis = {"fault_category": "memory-exhaustion"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "cpu-exhaustion"
    assert out["validator_meta"]["rule"] == "cpu-exhaustion"


def test_restart_counter_overrides_to_pod_failure():
    features = {
        "metric_hotspots": _hotspots(
            pod_restarts_total=[_row("user", 0.0, 2.0)],
        )
    }
    analysis = {"fault_category": "unknown"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "pod-failure"
    assert out["validator_meta"]["rule"] == "pod-failure"


def test_conflicting_signals_keep_llm_verdict():
    # Memory saturation (>90 %) and CPU saturation (>80 %) both fire and
    # neither suppresses the other → ambiguous, so we keep the LLM guess.
    features = {
        "metric_hotspots": _hotspots(
            memory_saturation_pct=[_row("carts", 10.0, 95.0)],
            cpu_saturation_pct=[_row("carts", 10.0, 95.0)],
        )
    }
    analysis = {"fault_category": "pod-failure"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "pod-failure"  # unchanged
    meta = out["validator_meta"]
    assert meta["fired"] is False
    assert sorted(meta["conflict"]) == ["cpu-exhaustion", "memory-exhaustion"]


def test_network_latency_signal_overrides_to_network_latency():
    features = {
        "metric_hotspots": _hotspots(
            latency_p95=[_row("orders", 0.1, 0.9)],
            error_rate=[_row("orders", 0.0, 0.02)],
        )
    }
    analysis = {"fault_category": "cpu-exhaustion"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "network-latency"
    assert out["validator_meta"]["rule"] == "network-latency"


def test_network_drop_signal_overrides_to_network_loss():
    features = {
        "metric_hotspots": _hotspots(
            network_receive_dropped_rate=[_row("shipping", 0.0, 0.25)],
        )
    }
    analysis = {"fault_category": "memory-exhaustion"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "network-loss"
    assert out["validator_meta"]["rule"] == "network-loss"


def test_http_error_signal_overrides_to_http_error():
    features = {
        "metric_hotspots": _hotspots(
            error_rate=[_row("orders", 0.0, 0.5)],
            latency_p95=[_row("orders", 0.1, 0.15)],
        )
    }
    analysis = {"fault_category": "network-latency"}
    out = validate_fault_category(analysis, features)

    assert out["fault_category"] == "http-error"
    assert out["validator_meta"]["rule"] == "http-error"


def test_validator_is_idempotent():
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 0.0, 1.0)],
        )
    }
    analysis = {"fault_category": "pod-failure"}
    first = validate_fault_category(analysis, features)
    second = validate_fault_category(first, features)

    assert second["fault_category"] == "memory-exhaustion"
    # The original LLM answer is preserved across repeated calls.
    assert second["validator_meta"]["original_fault_category"] == "pod-failure"


def test_missing_features_is_safe():
    analysis = {"fault_category": "cpu-exhaustion"}
    out = validate_fault_category(analysis, None)

    assert out["fault_category"] == "cpu-exhaustion"
    assert out["validator_meta"]["fired"] is False
