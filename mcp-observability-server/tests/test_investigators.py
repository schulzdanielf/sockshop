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
    assert "status_code=~\"5..\"" in prom.calls[0]["query"]


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


def test_service_level_series_top_handles_prometheus_error():
    prom = _FakePrometheus({"status": "error", "error": "boom"})
    out = inv.check_request_latency_top(
        prom,
        start="2026-01-01T00:00:00Z",
        end="2026-01-01T00:05:00Z",
    )

    assert out["found"] is False
    assert out["error"] == "boom"
