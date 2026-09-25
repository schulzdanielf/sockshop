"""Unit tests for the pure helpers in the MCP ``investigators`` module.

These cover the cluster-independent logic: pod→service normalisation,
range-query series iteration and per-service aggregation. Anything that
talks to Prometheus/Loki/Tempo is out of scope here.
"""
from __future__ import annotations

import pytest

import investigators as inv


class _FakePrometheus:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def query_range(self, query, start, end, step):
        self.calls.append(
            {"query": query, "start": start, "end": end, "step": step}
        )
        return self.payload


@pytest.mark.parametrize(
    "pod_name,expected",
    [
        ("catalogue-578f94df6c-sfk2r", "catalogue"),
        ("front-end-6f7c9d8b5d-abcde", "front-end"),
        ("carts-db-7d9c-x9k2z", "carts-db-7d9c-x9k2z"),  # 2nd part too short
        ("standalone", "standalone"),  # no hyphen
        ("", ""),
    ],
)
def test_pod_to_service(pod_name, expected):
    assert inv._pod_to_service(pod_name) == expected


def test_iter_series_yields_labels_and_points():
    payload = {
        "data": {
            "result": [
                {
                    "metric": {"pod": "carts-abc12-def34"},
                    "values": [[1.0, "2"], [2.0, "5"]],
                }
            ]
        }
    }
    series = list(inv._iter_series(payload))
    assert len(series) == 1
    labels, points = series[0]
    assert labels["pod"] == "carts-abc12-def34"
    assert points == [(1.0, 2.0), (2.0, 5.0)]


def test_iter_series_skips_malformed_points():
    payload = {
        "data": {
            "result": [
                {"metric": {"pod": "x"}, "values": [["bad", "value"], [3.0, "9"]]}
            ]
        }
    }
    _, points = next(inv._iter_series(payload))
    assert points == [(3.0, 9.0)]


def test_iter_series_on_non_dict_is_empty():
    assert list(inv._iter_series(None)) == []
    assert list(inv._iter_series([])) == []


def test_aggregate_counter_uses_max_minus_min():
    payload = {
        "data": {
            "result": [
                {
                    "metric": {"pod": "user-aaaaa-bbbbb"},
                    "values": [[1.0, "0"], [2.0, "3"]],
                }
            ]
        }
    }
    rollup = inv._aggregate_by_service(payload, mode="max_minus_min")
    assert rollup["user"]["value"] == 3.0


def test_counter_increments_handles_resets_inside_window():
    points = [(1.0, 10.0), (2.0, 12.0), (3.0, 1.0), (4.0, 3.0)]
    assert inv._counter_increments(points) == 5.0


def test_check_pod_restarts_ignores_duplicate_scrape_and_counts_window_deltas():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {
                            "pod": "rabbitmq-97cbcbdbc-2qd88",
                            "container": "rabbitmq",
                            "job": "kube-state-metrics",
                        },
                        "values": [[1.0, "60"], [2.0, "60"], [3.0, "60"]],
                    },
                    {
                        "metric": {
                            "pod": "rabbitmq-97cbcbdbc-2qd88",
                            "container": "rabbitmq",
                            "job": "kubernetes-nodes",
                        },
                        "values": [[1.0, "60"], [2.0, "179"], [3.0, "60"]],
                    },
                    {
                        "metric": {
                            "pod": "orders-aaaaa-bbbbb",
                            "container": "orders",
                            "job": "kube-state-metrics",
                        },
                        "values": [[1.0, "10"], [2.0, "12"], [3.0, "15"]],
                    },
                ]
            }
        }
    )

    out = inv.check_pod_restarts(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert 'job="kube-state-metrics"' in prom.calls[0]["query"]
    assert out["found"] is True
    assert out["count"] == 1
    rows = {row["service"]: row["restarts_in_window"] for row in out["top"]}
    assert rows["rabbitmq"] == 0
    assert rows["orders"] == 5


def test_check_pod_restarts_keeps_zero_count_service_after_top_five():
    services = ["carts", "catalogue", "front-end", "orders", "payment"]
    results = [
        {
            "metric": {
                "pod": f"{service}-aaaaa-bbbbb",
                "container": service,
                "job": "kube-state-metrics",
            },
            "values": [[1.0, "0"], [2.0, "0"]],
        }
        for service in services
    ]
    results.append(
        {
            "metric": {
                "pod": "rabbitmq-97cbcbdbc-2qd88",
                "container": "rabbitmq",
                "job": "kube-state-metrics",
            },
            "values": [[1.0, "60"], [2.0, "60"]],
        }
    )
    prom = _FakePrometheus({"data": {"result": results}})

    out = inv.check_pod_restarts(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    rabbitmq = next(row for row in out["top"] if row["service"] == "rabbitmq")
    assert rabbitmq["restarts_in_window"] == 0


def test_aggregate_gauge_uses_max():
    payload = {
        "data": {
            "result": [
                {
                    "metric": {"pod": "payment-aaaaa-bbbbb"},
                    "values": [[1.0, "0"], [2.0, "1"], [3.0, "0"]],
                }
            ]
        }
    }
    rollup = inv._aggregate_by_service(payload, mode="max")
    assert rollup["payment"]["value"] == 1.0


def test_aggregate_mean_averages_window():
    payload = {
        "data": {
            "result": [
                {
                    "metric": {"pod": "orders-aaaaa-bbbbb"},
                    "values": [[1.0, "10"], [2.0, "20"], [3.0, "30"]],
                }
            ]
        }
    }
    rollup = inv._aggregate_by_service(payload, mode="mean")
    assert rollup["orders"]["value"] == 20.0


def test_aggregate_mean_averages_pod_percentages_per_service():
    payload = {
        "data": {
            "result": [
                {
                    "metric": {"pod": "carts-aaaaa-bbbbb"},
                    "values": [[1.0, "10"], [2.0, "20"]],
                },
                {
                    "metric": {"pod": "carts-ccccc-ddddd"},
                    "values": [[1.0, "30"], [2.0, "50"]],
                },
            ]
        }
    }

    rollup = inv._aggregate_by_service(payload, mode="mean")

    assert rollup["carts"]["value"] == 27.5
    assert [pod["value"] for pod in rollup["carts"]["pods"]] == [15.0, 40.0]


def test_memory_saturation_uses_canonical_cadvisor_job_and_averages_replicas():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {"pod": "carts-aaaaa-bbbbb", "job": "kubernetes-cadvisor"},
                        "values": [[1.0, "50"], [2.0, "70"]],
                    },
                    {
                        "metric": {"pod": "carts-ccccc-ddddd", "job": "kubernetes-cadvisor"},
                        "values": [[1.0, "60"], [2.0, "80"]],
                    },
                ]
            }
        }
    )

    out = inv.get_memory_saturation_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    carts = out["top"][0]
    assert 'job="kubernetes-cadvisor"' in prom.calls[0]["query"]
    assert carts["service"] == "carts"
    assert carts["saturation_mean_pct"] == 65.0
    assert carts["saturation_max_pct"] == 80.0


def test_aggregate_unknown_mode_raises():
    payload = {
        "data": {
            "result": [{"metric": {"pod": "x-aaaaa-bbbbb"}, "values": [[1.0, "1"]]}]
        }
    }
    with pytest.raises(ValueError):
        inv._aggregate_by_service(payload, mode="nope")


def test_check_http_error_rate_top_returns_service_level_rows():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {"name": "orders"},
                        "values": [[1.0, "2.0"], [2.0, "4.0"]],
                    },
                    {
                        "metric": {"name": "front-end"},
                        "values": [[1.0, "0.0"], [2.0, "0.2"]],
                    },
                ]
            }
        }
    )
    out = inv.check_http_error_rate_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert out["found"] is True
    assert out["count"] == 1
    assert out["top"][0]["service"] == "orders"
    assert out["top"][0]["error_rate_mean_pct"] == 3.0
    assert "istio_requests_total" in prom.calls[0]["query"]
    assert "response_code=~\"5..\"" in prom.calls[0]["query"]
    assert "destination_workload" in prom.calls[0]["query"]
    assert prom.calls[0]["query"].count("(") == prom.calls[0]["query"].count(")")


def test_check_http_error_rate_top_accepts_destination_workload_label():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {"destination_workload": "orders"},
                        "values": [[1.0, "2.0"], [2.0, "4.0"]],
                    },
                    {
                        "metric": {"destination_workload": "unknown"},
                        "values": [[1.0, "100.0"], [2.0, "100.0"]],
                    },
                ]
            }
        }
    )

    out = inv.check_http_error_rate_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert out["found"] is True
    assert out["count"] == 1
    assert out["top"][0]["service"] == "orders"
    assert out["top"][0]["error_rate_max_pct"] == 4.0


def test_check_request_latency_top_returns_ordered_rows():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {"name": "shipping"},
                        "values": [[1.0, "0.5"], [2.0, "0.8"]],
                    },
                    {
                        "metric": {"name": "payment"},
                        "values": [[1.0, "0.1"], [2.0, "0.2"]],
                    },
                ]
            }
        }
    )
    out = inv.check_request_latency_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert out["found"] is True
    assert out["top"][0]["service"] == "shipping"
    assert out["top"][0]["latency_p95_max_seconds"] == 0.8
    assert "histogram_quantile(0.95" in prom.calls[0]["query"]
    assert "istio_request_duration_milliseconds_bucket" in prom.calls[0]["query"]
    assert "destination_workload" in prom.calls[0]["query"]


def test_service_level_series_top_handles_prometheus_error():
    prom = _FakePrometheus({"status": "error", "error": "boom"})
    out = inv.check_request_latency_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert out["found"] is False
    assert out["error"] == "boom"
