from __future__ import annotations

from experiment.eval.gat.quality_filter import classify_graph, filter_dataset


def _graph(target: str = "orders", values=None, fault: str = "network-latency"):
    values = values or {"traffic_lr": 1.0, "latency_p95_lr": 0.5, "error_rate_lr": 0.2}
    return {
        "run_id": "test-run",
        "chaos_type_normalized": fault,
        "edge_source": "full",
        "nodes": [
            {"idx": 0, "service": target, "features": {**values, "span_count": 1.0}},
        ],
        "label": {"root_service": target, "root_service_idx": 0},
    }


def test_clean_graph_has_full_target_coverage():
    status, quality = classify_graph(_graph())
    assert status == "clean"
    assert quality["target_metric_observed_count"] == 3


def test_partial_graph_is_warning():
    status, quality = classify_graph(_graph(values={"traffic_lr": 1.0, "latency_p95_lr": None, "error_rate_lr": None}))
    assert status == "warning"
    assert "target_metric_coverage:1/3" in quality["warnings"]


def test_no_signal_and_http_fault_are_excluded():
    no_signal, _ = classify_graph(_graph(values={"traffic_lr": None, "latency_p95_lr": None, "error_rate_lr": None}))
    http_fault, _ = classify_graph(_graph(fault="http-status-code"))
    assert no_signal == "excluded"
    assert http_fault == "excluded"


def test_label_fault_is_used_when_top_level_fault_is_missing():
    graph = _graph(fault=None)
    graph["label"]["fault_category"] = "http-status-code"
    status, quality = classify_graph(graph)
    assert status == "excluded"
    assert "excluded_fault:http-status-code" in quality["reasons"]


def test_filter_returns_audit_counts():
    groups, report = filter_dataset([_graph(), _graph(fault="http-status-code")])
    assert len(groups["clean"]) == 1
    assert len(groups["excluded"]) == 1
    assert report["counts"]["clean"] == 1
