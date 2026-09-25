from unittest.mock import Mock

import pytest

from experiment.platform.backend.analysis.mcp_investigator import (
    investigate_metric_window,
)


def test_investigation_passes_explicit_window_and_returns_compact_evidence():
    client = Mock()
    client.call_tool_json.return_value = {
        "data": {
            "result": [
                {
                    "metric": {"service": "catalogue"},
                    "values": [[1, "2"], [2, "3"]],
                }
            ]
        }
    }

    result = investigate_metric_window(
        client,
        run_id="run-1",
        window_start="2026-09-25T10:00:00+00:00",
        window_end="2026-09-25T10:05:00+00:00",
        query="sum(rate(requests_total[1m]))",
        step_seconds=15,
    )

    client.call_tool_json.assert_called_once_with(
        "prometheus_range_query",
        {
            "query": "sum(rate(requests_total[1m]))",
            "start": "2026-09-25T10:00:00+00:00",
            "end": "2026-09-25T10:05:00+00:00",
            "step": "15s",
        },
    )
    assert result["evidence"]["series_count"] == 1
    assert result["evidence"]["sample_count"] == 2


def test_investigation_rejects_reversed_window():
    with pytest.raises(ValueError, match="invalid observation window"):
        investigate_metric_window(
            Mock(),
            run_id="run-1",
            window_start="2026-09-25T10:05:00+00:00",
            window_end="2026-09-25T10:00:00+00:00",
            query="up",
            step_seconds=15,
        )