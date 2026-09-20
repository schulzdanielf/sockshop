"""Prompt and summary safety tests for phase-scoped service graph deltas."""

from __future__ import annotations

from experiment.platform.backend.analysis.prompt import assemble_prompt
from experiment.platform.backend.analysis.summary import build_run_summary_l2


def _features_with_graph_delta() -> dict:
    return {
        "verdict": "degraded_recoverable",
        "chaos_type": "network-latency",
        "slo_thresholds": {"error_rate": 0.05, "latency_p95_ms": 800},
        "phase_metrics": {},
        "metric_hotspots": {},
        "slo_violations": [],
        "affected_services": [],
        "trace_summary": {"trace_count": 50, "error_trace_count": 5},
        "propagation_graph": {
            "cascade_order": [
                {"service": "front-end", "t_offset_s": 0.0},
                {"service": "orders", "t_offset_s": 1.0},
            ],
            "edges": [
                {
                    "from": "front-end",
                    "to": "orders",
                    "trace_count": 10,
                    "error_count": 2,
                }
            ],
        },
        "service_graph_delta": {
            "delta": {
                "first_degraded_services": [
                    {"service": "orders", "signal": "error", "t_offset_s": 1.2}
                ],
                "suspected_propagation_path": ["front-end", "orders", "payment"],
                "top_edge_deltas": [
                    {
                        "from": "orders",
                        "to": "payment",
                        "error_rate_delta": 0.6,
                        "latency_p95_ms_delta": 2000.0,
                        "latency_mean_ms_delta": 1200.0,
                    }
                ],
                "recovery": {
                    "recovered_edges": [],
                    "persistent_edges": [{"from": "orders", "to": "payment"}],
                },
            }
        },
    }


def test_l2_summary_includes_compact_service_graph_delta():
    summary, _tags = build_run_summary_l2(
        "run-graph", "exp-graph", _features_with_graph_delta(), {}
    )

    assert "## Service graph delta" in summary
    assert "orders → payment" in summary
    assert "Δp95=2000ms" in summary
    assert "Propagation path: front-end -> orders -> payment" in summary


def test_target_prompt_masks_legacy_graph_but_keeps_service_graph_delta():
    summary, tags = build_run_summary_l2(
        "run-graph", "exp-graph", _features_with_graph_delta(), {}
    )
    prompt, _meta = assemble_prompt(
        {
            "target": {
                "run_id": "run-graph",
                "summary_text": summary,
                "tags": tags,
            },
            "neighbours": [],
        },
        system_id=None,
    )

    assert "## Target run" in prompt
    assert "## Propagation graph" not in prompt
    assert "## Service graph delta" in prompt
    assert "orders → payment" in prompt
    assert "front-end -> orders -> payment" in prompt
