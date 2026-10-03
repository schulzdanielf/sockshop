"""Tests for transient Tempo request recovery."""

from unittest.mock import Mock
import importlib

import httpx
import pytest

TempoClient = importlib.import_module("tempo_client").TempoClient


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"traces": [{"traceID": "trace-1"}]}


@pytest.mark.parametrize("operation", ["search", "get_trace"])
def test_tempo_client_retries_transient_transport_errors(monkeypatch, operation):
    monkeypatch.setattr("tempo_client.time.sleep", lambda _seconds: None)
    http_client = Mock()
    http_client.get.side_effect = [
        httpx.ReadTimeout("temporary timeout"),
        _Response(),
    ]
    client = TempoClient(base_url="http://tempo")
    client.client = http_client

    if operation == "search":
        result = client.search_traces(start="2026-09-30T10:00:00Z", end="2026-09-30T10:01:00Z")
    else:
        result = client.get_trace("trace-1")

    assert result == {"traces": [{"traceID": "trace-1"}]}
    assert http_client.get.call_count == 2
