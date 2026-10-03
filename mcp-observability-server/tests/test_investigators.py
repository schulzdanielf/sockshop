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


def test_check_pod_restarts_reports_container_increment_interval_not_exact_time():
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    {
                        "metric": {
                            "pod": "user-abcde-12345",
                            "container": "user",
                            "job": "kube-state-metrics",
                        },
                        "values": [[1760000000, "3"], [1760000015, "4"]],
                    }
                ]
            }
        }
    )

    result = inv.check_pod_restarts(
        prom, start="2025-10-09T08:53:20Z", end="2025-10-09T08:53:35Z"
    )
    pod = result["top"][0]["pods"][0]

    assert pod["container"] == "user"
    assert pod["restart_observations"] == [
        {
            "observed_from": "2025-10-09T08:53:20Z",
            "observed_to": "2025-10-09T08:53:35Z",
            "restart_count_delta": 1.0,
            "counter_reset": False,
            "timestamp_precision": "between_scrapes",
        }
    ]


def test_check_pod_restarts_keeps_containers_separate_and_marks_counter_reset():
    duplicate_app_series = {
        "metric": {
            "pod": "user-abcde-12345",
            "container": "user",
            "job": "kube-state-metrics",
        },
        "values": [[1760000000, "0"], [1760000015, "2"]],
    }
    prom = _FakePrometheus(
        {
            "data": {
                "result": [
                    duplicate_app_series,
                    {**duplicate_app_series, "metric": dict(duplicate_app_series["metric"])},
                    {
                        "metric": {
                            "pod": "user-abcde-12345",
                            "container": "istio-proxy",
                            "job": "kube-state-metrics",
                        },
                        "values": [[1760000000, "5"], [1760000015, "1"]],
                    },
                ]
            }
        }
    )

    result = inv.check_pod_restarts(
        prom, start="2025-10-09T08:53:20Z", end="2025-10-09T08:53:35Z"
    )
    service = result["top"][0]
    pods = {pod["container"]: pod for pod in service["pods"]}

    assert service["restarts_in_window"] == 3
    assert pods["user"]["value"] == 2
    assert pods["user"]["restart_observations"][0]["restart_count_delta"] == 2
    assert pods["istio-proxy"]["value"] == 1
    assert pods["istio-proxy"]["restart_observations"][0]["counter_reset"] is True


def test_check_service_readiness_reports_only_observed_transitions():
    class _ReadinessPrometheus:
        def query_range(self, _query, start, _end, _step):
            values = [[1760000000, "1"], [1760000015, "0"], [1760000030, "0"]]
            if start == "baseline":
                values = [[1760000000, "0"], [1760000015, "0"]]
            return {
                "data": {
                    "result": [
                        {
                            "metric": {"pod": "user-abcde-12345"},
                            "values": values,
                        }
                    ]
                }
            }

    result = inv.check_service_readiness(
        _ReadinessPrometheus(),
        baseline_start="baseline",
        baseline_end="baseline-end",
        fault_start="fault",
        fault_end="fault-end",
    )
    baseline_pod = result["phases"]["baseline"]["services"][0]["pods"][0]
    fault_pod = result["phases"]["fault"]["services"][0]["pods"][0]

    assert baseline_pod["transitions"] == []
    assert fault_pod["transitions"] == [
        {
            "transition": "ready_to_not_ready",
            "observed_from": "2025-10-09T08:53:20Z",
            "observed_to": "2025-10-09T08:53:35Z",
            "timestamp_precision": "between_scrapes",
        }
    ]


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


@pytest.mark.parametrize(
    "payload,expected_status",
    [
        ({"data": {"result": []}}, "no_data"),
        ({"status": "error", "error": "backend unavailable"}, "tool_error"),
    ],
)
def test_oom_and_restart_tools_report_coverage_state(payload, expected_status):
    for investigator in (inv.check_oom_kills, inv.check_pod_restarts):
        result = investigator(
            _FakePrometheus(payload),
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T00:05:00Z",
        )

        assert result["status"] == expected_status
        assert result["coverage"] == {"series_count": 0, "sample_count": 0}


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


def test_get_run_golden_signals_compares_baseline_and_fault_with_coverage():
    class _PhasePrometheus:
        def __init__(self):
            self.calls = []

        def query_range(self, query, start, end, step):
            self.calls.append((query, start, end, step))
            value = "10" if start == "2026-01-01T00:00:00Z" else "20"
            return {
                "data": {
                    "result": [
                        {
                            "metric": {"destination_workload": "orders"},
                            "values": [[1.0, value], [2.0, value]],
                        }
                    ]
                }
            }

    prom = _PhasePrometheus()
    out = inv.get_run_golden_signals(
        prom,
        baseline_start="2026-01-01T00:00:00Z",
        baseline_end="2026-01-01T00:01:00Z",
        fault_start="2026-01-01T00:02:00Z",
        fault_end="2026-01-01T00:03:00Z",
    )

    assert len(prom.calls) == 10
    assert {(call[1], call[2]) for call in prom.calls} == {
        ("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
        ("2026-01-01T00:02:00Z", "2026-01-01T00:03:00Z"),
    }
    assert out["status"] == "ok"
    row = out["metrics"]["request_rate"]["top"][0]
    assert row["service"] == "orders"
    assert row["baseline_mean"] == 10.0
    assert row["fault_mean"] == 20.0
    assert row["delta_abs"] == 10.0
    assert out["metrics"]["request_rate"]["phases"]["fault"]["sample_count"] == 2
    assert out["metrics"]["cpu_saturation_pct"]["signal_detected"] is True


def test_check_network_packet_drops_compares_service_rates_and_coverage():
    class _PacketPrometheus:
        def __init__(self):
            self.calls = []

        def query_range(self, query, start, end, step):
            self.calls.append({"query": query, "start": start, "end": end, "step": step})
            baseline = start == "2026-01-01T00:00:00Z"
            is_receive = "receive_packets_dropped" in query
            value = "0" if baseline or not is_receive else "0.5"
            return {
                "data": {
                    "result": [
                        {
                            "metric": {"interface": "eth0"},
                            "values": [[1.0, value], [2.0, value]],
                        }
                    ]
                }
            }

    prom = _PacketPrometheus()
    out = inv.check_network_packet_drops(
        prom,
        baseline_start="2026-01-01T00:00:00Z",
        baseline_end="2026-01-01T00:01:00Z",
        fault_start="2026-01-01T00:02:00Z",
        fault_end="2026-01-01T00:03:00Z",
    )

    assert len(prom.calls) == 4
    assert all('job="kubernetes-cadvisor"' in call["query"] for call in prom.calls)
    assert all("sum by (interface)" in call["query"] for call in prom.calls)
    assert all(call["step"] == "15s" for call in prom.calls)
    assert out["status"] == "ok"
    assert out["found"] is True
    receive = out["metrics"]["network_receive_dropped_rate"]
    assert receive["signal_detected"] is True
    assert receive["top"][0]["interface"] == "eth0"
    assert receive["top"][0]["delta_abs"] == 0.5
    assert out["metrics"]["network_transmit_dropped_rate"]["signal_detected"] is False


def test_get_run_golden_signals_distinguishes_no_data_from_tool_error():
    prom = _FakePrometheus({"data": {"result": []}})
    out = inv.get_run_golden_signals(
        prom,
        baseline_start="2026-01-01T00:00:00Z",
        baseline_end="2026-01-01T00:01:00Z",
        fault_start="2026-01-01T00:02:00Z",
        fault_end="2026-01-01T00:03:00Z",
    )

    assert out["status"] == "no_data"
    assert all(metric["status"] == "no_data" for metric in out["metrics"].values())


def test_check_service_readiness_compares_per_service_across_phases():
    class _ReadinessPrometheus:
        def __init__(self):
            self.calls = []

        def query_range(self, query, start, end, step):
            self.calls.append((query, start, end, step))
            user_ready = "0" if start == "fault" else "1"
            return {
                "data": {
                    "result": [
                        {
                            "metric": {"pod": "user-abcde-12345"},
                            "values": [[1.0, user_ready], [2.0, user_ready]],
                        },
                        {
                            "metric": {"pod": "catalogue-fghij-67890"},
                            "values": [[1.0, "1"], [2.0, "1"]],
                        },
                    ]
                }
            }

    prom = _ReadinessPrometheus()
    out = inv.check_service_readiness(
        prom,
        baseline_start="baseline",
        baseline_end="baseline-end",
        fault_start="fault",
        fault_end="fault-end",
        post_start="post",
        post_end="post-end",
    )

    assert len(prom.calls) == 3
    assert all('condition="true"' in call[0] for call in prom.calls)
    assert all('job="kube-state-metrics"' in call[0] for call in prom.calls)
    assert out["status"] == "ok"
    fault_services = {
        row["service"]: row for row in out["phases"]["fault"]["services"]
    }
    assert fault_services["user"]["ready_mean_pct"] == 0.0
    assert fault_services["catalogue"]["ready_mean_pct"] == 100.0
