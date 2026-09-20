"""Unit tests for window-scoped service graph summaries and deltas."""

from __future__ import annotations

from service_graph import diff_graph_summaries, summarize_trace_graph


def _span(
    span_id: str,
    service: str,
    name: str,
    start_ms: int,
    duration_ms: int,
    parent_id: str | None = None,
    status: int = 0,
    attrs: list | None = None,
) -> dict:
    status_payload = {"code": status}
    return {
        "spanId": span_id,
        "parentSpanId": parent_id,
        "name": name,
        "startTimeUnixNano": str(start_ms * 1_000_000),
        "endTimeUnixNano": str((start_ms + duration_ms) * 1_000_000),
        "status": status_payload,
        "attributes": attrs or [],
    }


def _batch(service: str, spans: list) -> dict:
    return {
        "resource": {
            "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
        },
        "scopeSpans": [{"spans": spans}],
    }


def _trace(trace_id: str, root_service: str, batches: list, start_ms: int) -> dict:
    return {
        "trace_id": trace_id,
        "root_service": root_service,
        "start_time_unix_nano": str(start_ms * 1_000_000),
        "features": {"batches": batches},
    }


def test_summarize_trace_graph_extracts_nodes_edges_and_cascade():
    healthy = _trace(
        "t1",
        "front-end",
        [
            _batch(
                "front-end",
                [
                    _span("root", "front-end", "GET /", 1000, 100),
                ],
            ),
            _batch(
                "orders",
                [
                    _span("child", "orders", "GET /orders", 1010, 80, parent_id="root"),
                ],
            ),
        ],
        1000,
    )
    faulty = _trace(
        "t2",
        "front-end",
        [
            _batch(
                "front-end",
                [
                    _span("root", "front-end", "GET /", 2000, 2600),
                ],
            ),
            _batch(
                "orders",
                [
                    _span(
                        "child", "orders", "GET /orders", 2010, 2500, parent_id="root"
                    ),
                ],
            ),
            _batch(
                "payment",
                [
                    _span(
                        "pay",
                        "payment",
                        "POST /pay",
                        2020,
                        2400,
                        parent_id="child",
                        status=2,
                        attrs=[
                            {
                                "key": "peer.service",
                                "value": {
                                    "stringValue": "payment.sock-shop.svc.cluster.local"
                                },
                            }
                        ],
                    )
                ],
            ),
        ],
        2000,
    )

    summary = summarize_trace_graph(
        [healthy, faulty],
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-01T00:01:00Z",
        phase="fault",
        top_k=5,
    )

    assert summary["phase"] == "fault"
    assert summary["trace_count"] == 2
    assert summary["node_count"] == 3
    assert summary["edge_count"] >= 2
    assert summary["error_trace_count"] == 1
    assert summary["error_edge_count"] >= 1
    assert summary["top_nodes"][0]["service"] == "payment"
    assert summary["top_nodes"][0]["error_span_count"] == 1
    assert any(
        edge["from"] == "orders" and edge["to"] == "payment"
        for edge in summary["top_edges"]
    )
    assert summary["cascade_order"][0]["service"] == "front-end"
    assert summary["cascade_order"][0]["signal"] == "latency"


def test_diff_graph_summaries_ranks_latency_and_error_changes():
    baseline = {
        "window": {"start": "b0", "end": "b1"},
        "top_nodes": [
            {
                "service": "payment",
                "span_count": 10,
                "error_rate": 0.0,
                "latency_p95_ms": 100.0,
            },
            {
                "service": "orders",
                "span_count": 10,
                "error_rate": 0.0,
                "latency_p95_ms": 90.0,
            },
        ],
        "top_edges": [
            {
                "from": "orders",
                "to": "payment",
                "span_count": 8,
                "error_rate": 0.0,
                "latency_p95_ms": 120.0,
                "latency_mean_ms": 80.0,
            }
        ],
    }
    fault = {
        "window": {"start": "f0", "end": "f1"},
        "cascade_order": [{"service": "payment", "signal": "latency"}],
        "suspected_propagation_path": ["orders", "payment"],
        "top_nodes": [
            {
                "service": "payment",
                "span_count": 12,
                "error_rate": 0.25,
                "latency_p95_ms": 2300.0,
            },
            {
                "service": "orders",
                "span_count": 12,
                "error_rate": 0.0,
                "latency_p95_ms": 1800.0,
            },
        ],
        "top_edges": [
            {
                "from": "orders",
                "to": "payment",
                "span_count": 10,
                "error_rate": 0.2,
                "latency_p95_ms": 2200.0,
                "latency_mean_ms": 1700.0,
            },
            {
                "from": "front-end",
                "to": "orders",
                "span_count": 10,
                "error_rate": 0.0,
                "latency_p95_ms": 1500.0,
                "latency_mean_ms": 1200.0,
            },
        ],
    }
    post = {
        "window": {"start": "p0", "end": "p1"},
        "top_edges": [
            {
                "from": "orders",
                "to": "payment",
                "span_count": 9,
                "error_rate": 0.0,
                "latency_p95_ms": 110.0,
            }
        ],
    }

    delta = diff_graph_summaries(baseline, fault, recovery=post, top_k=5)

    assert delta["top_edge_deltas"][0]["from"] == "orders"
    assert delta["top_edge_deltas"][0]["to"] == "payment"
    assert delta["top_edge_deltas"][0]["latency_p95_ms_delta"] == 2080.0
    assert delta["top_edge_deltas"][0]["error_rate_delta"] == 0.2
    assert delta["first_degraded_services"] == [
        {"service": "payment", "signal": "latency"}
    ]
    assert delta["suspected_propagation_path"] == ["orders", "payment"]
    assert delta["recovery"]["recovered_edges"] == [{"from": "orders", "to": "payment"}]
