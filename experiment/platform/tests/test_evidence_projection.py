import copy
import json

import pytest

from experiment.platform.backend import evidence_projection as projection


def item(identifier, digest):
    return {
        "id": identifier,
        "tool": "get_run_golden_signals",
        "family": "metrics",
        "status": "ok",
        "source": "live",
        "observation_count": 4,
        "fault_observation_count": 2,
        "arguments": {"start": "2026-10-29T10:00:00Z", "end": "2026-10-29T10:01:00Z"},
        "digest": digest,
    }


def test_preserves_metric_edge_and_event_facts_even_when_last_in_large_payload():
    facts = {
        "metrics": {
            "cpu_saturation_pct": {
                "unit": "%",
                "phases": {"fault": {"sample_count": 2}},
                "top_delta": [
                    {
                        "service": "catalogue",
                        "baseline": 15.07,
                        "fault": 54.73,
                        "delta": 39.66,
                    }
                ],
            },
            "edge_latency_ms": {
                "unit": "ms",
                "top_delta": [
                    {
                        "source_workload": "orders",
                        "destination_workload": "payment",
                        "baseline": 5.0,
                        "fault": 800.0,
                        "delta": 795.0,
                    }
                ],
            },
        },
        "phases": {
            "fault": {
                "coverage": {"sample_count": 2},
                "termination_events": [
                    {
                        "pod": "payment-abc",
                        "container": "payment",
                        "uid": "uid",
                        "terminated_at": 1793268001.0,
                        "reason": "Error",
                        "exit_code": 137,
                    }
                ],
            }
        },
    }
    raw = item("E001", {"observations": ["generic latency"] * 5000, **facts})
    original = copy.deepcopy(raw)
    compacted = projection.compact_evidence_for_model([raw])
    digest = compacted[0]["digest_summary"]
    assert digest["metrics"] == facts["metrics"]
    assert digest["phases"] == facts["phases"]
    assert digest["projection_omitted_observations_count"] > 0
    assert raw == original
    assert len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= (
        projection.MODEL_EVIDENCE_CONTEXT_CHARS
    )


def test_preserves_trace_errors_and_lifecycle_reason_lists():
    raw = item(
        "E001",
        {
            "critical_path": [
                {"service": "payment", "duration_ms": 800, "is_error": True}
            ],
            "error_spans": [
                {"service": "payment", "name": "authorize", "status_code": 503}
            ],
            "termination_events": [
                {"container": "payment", "reasons": ["Error"], "exit_codes": [137]}
            ],
            "observations": ["noise"] * 10000,
        },
    )
    compacted = projection.compact_evidence_for_model([raw])
    digest = compacted[0]["digest_summary"]
    for key in ("critical_path", "error_spans", "termination_events"):
        assert digest[key] == raw["digest"][key]


def test_large_critical_payload_is_an_explicit_error_not_empty_success():
    raw = item(
        "E001",
        {
            "top_delta": [
                {"service": f"service-{index}", "baseline": 1, "fault": 2}
                for index in range(10000)
            ]
        },
    )
    with pytest.raises(ValueError, match="critical evidence facts exceed"):
        projection.compact_evidence_for_model([raw])


def test_projection_is_idempotent_and_rejects_lost_legacy_facts():
    compacted = projection.compact_evidence_for_model(
        [item("E001", {"observations": ["useful"]})]
    )
    assert projection.compact_evidence_for_model(compacted) == compacted
    with pytest.raises(ValueError, match="legacy compacted"):
        projection.compact_evidence_for_model(
            [{"id": "E001", "digest_summary": '{"metrics"...[truncated]'}]
        )


def test_query_text_omission_keeps_valid_json_and_explicit_count():
    raw = item("E001", {"status": "no_data", "coverage": {"sample_count": 0}})
    raw["arguments"]["query"] = "x" * 1000
    compacted = projection.compact_evidence_for_model([raw])
    arguments = compacted[0]["arguments_summary"]
    assert arguments["start"] == raw["arguments"]["start"]
    assert arguments["omitted_text_characters"]["query"] == 840
    assert compacted[0]["digest_summary"]["coverage"]["sample_count"] == 0


def test_columnar_metric_rows_keep_exact_values_and_source_pointer():
    rows = [
        {"service": f"service-{index}", "baseline": 1, "fault": 2, "delta": 1}
        for index in range(8)
    ]
    raw = item("E001", {"metrics": {"latency/ms": {"top_delta": rows}}})
    compacted = projection.compact_evidence_for_model([raw])
    table = compacted[0]["digest_summary"]["metrics"]["latency/ms"]["top_delta"]
    assert table["source_pointer"] == "/digest/metrics/latency~1ms/top_delta"
    assert [dict(zip(table["columns"], row)) for row in table["rows"]] == rows
