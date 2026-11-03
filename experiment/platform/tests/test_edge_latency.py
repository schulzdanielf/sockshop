from __future__ import annotations

import time

from experiment.platform.backend.analysis.edge_latency import (
    build_edge_latency_queries,
    normalize_edge_latency_result,
)
from experiment.platform.backend.multiagent_rca import (
    InvestigationBudget,
    _EvidenceRegistry,
    _metric_candidate_fallback,
    _metric_triage_profiles,
    _query_edge_latency_profiles,
)

PHASES = {
    "baseline": {
        "start": "1970-01-01T00:01:40Z",
        "end": "1970-01-01T00:02:09Z",
    },
    "fault": {
        "start": "1970-01-01T00:02:10Z",
        "end": "1970-01-01T00:02:39Z",
    },
    "post": {
        "start": "1970-01-01T00:02:40Z",
        "end": "1970-01-01T00:03:09Z",
    },
}


def test_edge_latency_queries_are_fixed_window_and_keep_edge_labels():
    queries = build_edge_latency_queries(
        "sock-shop",
        ["orders", "shipping", "front-end"],
    )
    by_name = {query["metric_name"]: query["query"] for query in queries}

    istio_query = by_name["edge_latency_istio_mean_ms"]
    assert 'reporter="source"' in istio_query
    assert 'source_workload!="unknown"' in istio_query
    assert "sum by (source_workload, destination_workload)" in istio_query
    assert "rate(istio_request_duration_milliseconds_sum" in istio_query
    assert "rate(istio_request_duration_milliseconds_count" in istio_query

    client_query = by_name["edge_latency_service_graph_client_p90_s"]
    server_query = by_name["edge_latency_service_graph_server_p90_s"]
    assert "histogram_quantile(0.90" in client_query
    assert "traces_service_graph_request_client_seconds_bucket" in client_query
    assert "histogram_quantile(0.90" in server_query
    assert "traces_service_graph_request_server_seconds_bucket" in server_query
    assert "by (le, client, server)" in server_query
    assert "$__rate_interval" not in server_query
    assert "[1m]" in server_query


class EdgeLatencyMCP:
    def __init__(self):
        self.calls = []

    def call_tool_json(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        query = arguments["query"]
        if "istio_request_duration_milliseconds" in query:
            labels = {
                "source_workload": "orders",
                "destination_workload": "shipping",
            }
            baseline, fault = "11", "155"
        elif "request_client_seconds_bucket" in query:
            labels = {"client": "orders", "server": "shipping"}
            baseline, fault = "0.11", "0.45"
        else:
            labels = {"client": "orders", "server": "shipping"}
            baseline, fault = "0.055", "0.06"
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": labels,
                        "values": [[110, baseline], [140, fault]],
                    }
                ],
            },
        }


def test_edge_latency_queries_feed_fault_comparisons_into_candidate_ranking():
    client = EdgeLatencyMCP()
    registry = _EvidenceRegistry(
        client=client,
        tools={"prometheus_range_query": {"inputSchema": {}}},
        phases=PHASES,
        namespace="sock-shop",
        budget=InvestigationBudget(),
        started=time.monotonic(),
    )

    results = _query_edge_latency_profiles(
        registry,
        ["orders", "shipping"],
    )

    assert len(results) == 3
    assert len(client.calls) == 3
    assert all(call[0] == "prometheus_range_query" for call in client.calls)
    assert all(call[1]["step"] == "15s" for call in client.calls)
    assert all(call[1]["start"] == PHASES["baseline"]["start"] for call in client.calls)
    assert all(call[1]["end"] == PHASES["post"]["end"] for call in client.calls)

    istio_evidence = next(
        call
        for call in registry.calls
        if "istio_request_duration_milliseconds_sum" in call["arguments"]["query"]
    )
    istio_metric = istio_evidence["digest"]["metrics"]["edge_latency_istio_mean_ms"]
    istio_edge = istio_metric["top_delta"][0]
    assert istio_evidence["fault_observation_count"] == 1
    assert istio_edge["service"] == istio_edge["destination_workload"] == "shipping"
    assert istio_edge["source_workload"] == "orders"
    assert istio_edge["baseline"] == 11
    assert istio_edge["fault"] == 155
    assert istio_edge["delta"] == 144

    ranking = _metric_candidate_fallback(
        registry.calls,
        ["orders", "shipping"],
        limit=2,
    )
    assert ranking[0] == "shipping"

    profile = next(
        row
        for row in _metric_triage_profiles(
            registry.calls, ["orders", "shipping"], ranking
        )
        if row["service"] == "shipping"
    )
    assert "edge_latency" in profile["signal_groups"]
    assert any(
        signal.get("source_workload") == "orders" for signal in profile["signals"]
    )


def test_edge_latency_without_a_comparable_baseline_is_not_ranked():
    spec = build_edge_latency_queries("sock-shop", ["orders", "shipping"])[0]
    result = normalize_edge_latency_result(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "source_workload": "orders",
                            "destination_workload": "shipping",
                        },
                        "values": [[140, "155"]],
                    }
                ],
            },
        },
        spec=spec,
        phases=PHASES,
        known_services=["orders", "shipping"],
    )

    assert result["status"] == "partial"
    metric = result["metrics"][spec["metric_name"]]
    assert metric["top_delta"] == []
    assert any("Sem aresta" in limitation for limitation in metric["limitations"])
