import importlib
from unittest.mock import Mock

import httpx

PrometheusClient = importlib.import_module("prometheus_client").PrometheusClient


def test_scrape_targets_use_the_same_backend_as_queries():
    client = PrometheusClient(base_url="http://metrics")
    client.client.close()
    client.client = Mock()
    expected = {"status": "success", "data": {"activeTargets": [{"scrapeInterval": "15s"}]}}
    client.client.get.return_value.json.return_value = expected
    assert client.get_scrape_targets() == expected
    client.client.get.assert_called_once_with("http://metrics/api/v1/targets", params={"state": "active"})


def test_scrape_target_error_is_preserved_not_an_empty_success():
    client = PrometheusClient(base_url="http://metrics")
    client.client.close()
    client.client = Mock()
    client.client.get.side_effect = httpx.ReadTimeout("unavailable")
    assert client.get_scrape_targets() == {"status": "error", "error": "unavailable"}


def test_raw_range_retains_scrape_timestamps_with_bounded_historical_selector():
    client = PrometheusClient(base_url="http://metrics")
    client.client.close()
    client.client = Mock()
    expected = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    client.client.get.return_value.json.return_value = expected
    assert client.query_raw_range(
        'counter{job="kube-state-metrics"}',
        "2026-10-28T19:00:00Z",
        "2026-10-28T19:02:03.500Z",
    ) == expected
    client.client.get.assert_called_once_with(
        "http://metrics/api/v1/query",
        params={
            "query": 'counter{job="kube-state-metrics"}[123500ms]',
            "time": "2026-10-28T19:02:03.500Z",
        },
    )


def test_raw_range_preserves_backend_error():
    client = PrometheusClient(base_url="http://metrics")
    client.client.close()
    client.client = Mock()
    client.client.get.side_effect = httpx.ReadTimeout("unavailable")
    assert client.query_raw_range("counter", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z") == {
        "status": "error", "error": "unavailable"
    }
