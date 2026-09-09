"""Unit tests for ``service_localizer.localize_service``.

The localizer overrides the LLM's ``rca`` (the *where*) using the one
metric anchor that is causally tied to each fault category, but only
when a single service dominates. These tests pin that contract:

* memory-exhaustion → top ``oom_killed`` service
* cpu-exhaustion    → top ``cpu_throttled`` service
* pod-failure       → top ``|pod_restarts_total|`` service
* unknown category  → no anchor, no override
* tied signals      → ambiguous, no override
"""
from __future__ import annotations

from typing import Any, Dict, List

from experiment.platform.backend.analysis.service_localizer import localize_service


def _hotspots(**metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {metric_id: {"top": rows} for metric_id, rows in metrics.items()}


def _row(label: str, delta: float, fault_mean: float = 0.0) -> Dict[str, Any]:
    return {"label": label, "delta_abs": delta, "fault_mean": fault_mean}


def test_memory_anchor_overrides_rca():
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 1.0), _row("carts", 0.0)],
        )
    }
    analysis = {"fault_category": "memory-exhaustion", "rca": "carts"}
    out = localize_service(analysis, features)

    assert out["rca"] == "payment"
    loc = out["validator_meta"]["localizer"]
    assert loc["fired"] is True
    assert loc["original_rca"] == "carts"


def test_cpu_anchor_uses_throttled_metric():
    features = {
        "metric_hotspots": _hotspots(
            cpu_throttled=[_row("orders", 0.8), _row("user", 0.0)],
        )
    }
    analysis = {"fault_category": "cpu-exhaustion", "rca": "user"}
    out = localize_service(analysis, features)

    assert out["rca"] == "orders"


def test_pod_failure_anchor_uses_restart_counter():
    features = {
        "metric_hotspots": _hotspots(
            pod_restarts_total=[_row("user", 3.0), _row("orders", 0.0)],
        )
    }
    analysis = {"fault_category": "pod-failure", "rca": "orders"}
    out = localize_service(analysis, features)

    assert out["rca"] == "user"


def test_unknown_category_does_not_override():
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 1.0)],
        )
    }
    analysis = {"fault_category": "network-latency", "rca": "front-end"}
    out = localize_service(analysis, features)

    assert out["rca"] == "front-end"  # unchanged
    loc = out["validator_meta"]["localizer"]
    assert loc["fired"] is False
    assert "no anchor" in loc["reason"]


def test_tied_signals_are_ambiguous():
    # Runner-up within the 25 % tie tolerance of the leader → no override.
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 1.0), _row("carts", 0.95)],
        )
    }
    analysis = {"fault_category": "memory-exhaustion", "rca": "carts"}
    out = localize_service(analysis, features)

    assert out["rca"] == "carts"  # unchanged
    assert out["validator_meta"]["localizer"]["fired"] is False


def test_anchor_agreeing_with_llm_is_no_change():
    features = {
        "metric_hotspots": _hotspots(
            oom_killed=[_row("payment", 1.0), _row("carts", 0.0)],
        )
    }
    analysis = {"fault_category": "memory-exhaustion", "rca": "payment"}
    out = localize_service(analysis, features)

    assert out["rca"] == "payment"
    loc = out["validator_meta"]["localizer"]
    assert loc["fired"] is True
    assert "agrees" in loc["reason"]
