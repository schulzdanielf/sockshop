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


def critical_item(identifier):
    return item(
        identifier,
        {"critical_path": [{"service": "shipping", "observation": "x" * 1500}]},
    )


def decode_columnar_rows(table):
    rows = []
    for values in table["rows"]:
        row = {}
        for column, value in zip(table["columns"], values):
            segments = [
                segment.replace("~1", "/").replace("~0", "~")
                for segment in column.split("/")
            ]
            parent = row
            for segment in segments[:-1]:
                parent = parent.setdefault(segment, {})
            parent[segments[-1]] = value
        rows.append(row)
    return rows


def summary_llm(calls, summary="Observations retained without causal inference."):
    def invoke(messages, tools, **kwargs):
        calls.append(messages)
        payload = next(
            json.loads(message["content"])
            for message in reversed(messages)
            if message["role"] == "user"
            and message["content"].startswith('{"evidence"')
        )
        return {
            "content": json.dumps(
                {
                    "items": [
                        {
                            "evidence_id": evidence["id"],
                            "summary": summary,
                        }
                        for evidence in payload["evidence"]
                    ]
                }
            )
        }

    return invoke


def test_explicit_budget_keeps_protected_facts_and_records_omissions():
    evidence = [
        item(
            "N001",
            {
                "top_delta": [
                    {"service": "catalogue", "baseline": 15.07, "fault": 54.73}
                ],
                "traces": [
                    {"traceID": str(index), "details": "span-" * 40}
                    for index in range(40)
                ],
            },
        )
    ]
    original = copy.deepcopy(evidence)
    compact = projection.compact_evidence_for_model(evidence, char_budget=2000)
    text = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
    assert len(text) <= 2000
    assert "54.73" in text and "15.07" in text
    assert "projection_omitted_traces_count" in text
    assert evidence == original
    with pytest.raises(ValueError, match="must be positive"):
        projection.compact_evidence_for_model(evidence, char_budget=0)


def test_projection_preserves_evidence_provenance_and_query_scope():
    source = item("N002", {"purpose": "http_status_rates"})
    source.update(
        {
            "kind": "http",
            "service": "shipping",
            "phase": "fault",
            "source_provenance": "prometheus_istio",
        }
    )

    projected = projection.compact_evidence_for_model([source])

    assert projected[0]["kind"] == "http"
    assert projected[0]["service"] == "shipping"
    assert projected[0]["phase"] == "fault"
    assert projected[0]["source_provenance"] == "prometheus_istio"


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
    raw = item("E001", {"observations": ["generic latency"] * 8000, **facts})
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


def test_large_nested_metric_series_are_losslessly_columnar_compacted():
    label_keys = (
        "destination_service",
        "destination_service_name",
        "destination_workload",
        "destination_workload_namespace",
        "reporter",
        "response_code",
        "response_flags",
        "source_workload",
        "source_workload_namespace",
        "extra/test~label",
    )
    phases = {}
    source_series = {}
    for phase_index, phase in enumerate(("baseline", "fault", "post", "warmup")):
        rows = []
        for index in range(83):
            label_values = {
                "destination_service": f"svc-{index % 17}",
                "destination_service_name": f"svc-{index % 17}",
                "destination_workload": f"work-{index % 17}",
                "destination_workload_namespace": "sock-shop",
                "reporter": "destination",
                "response_code": str(200 + index % 4),
                "response_flags": "-",
                "source_workload": f"client-{index % 17}",
                "source_workload_namespace": "sock-shop",
                "extra/test~label": f"v-{index % 17}",
            }
            labels = {key: label_values[key] for key in label_keys}
            rows.append(
                {
                    "destination_identity_source": "labels",
                    "labels": labels,
                    "max": float(index + phase_index + 2),
                    "mean": float(index + phase_index + 1),
                    "min": float(index + phase_index),
                    "resolved_destination_service": labels["destination_service"],
                    "sample_count": index % 20 + 1,
                    "service": labels["destination_service"],
                }
            )
        source_series[phase] = rows
        phases[phase] = {
            "coverage": {"sample_count": sum(row["sample_count"] for row in rows)},
            "series": rows,
            "status": "ok",
        }
    evidence = item(
        "E006",
        {
            "limitations": [],
            "phases": phases,
            "purpose": "edge_latency_istio",
            "service": None,
            "status": "ok",
            "unit": "ms",
            "windows": {},
        },
    )
    assert len(json.dumps([evidence], ensure_ascii=False)) > (
        projection.MODEL_EVIDENCE_CONTEXT_CHARS
    )

    compacted = projection.compact_evidence_for_model([evidence])
    encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True)

    assert len(encoded) <= projection.MODEL_EVIDENCE_CONTEXT_CHARS
    for phase, expected_rows in source_series.items():
        table = compacted[0]["digest_summary"]["phases"][phase]["series"]
        assert table["column_encoding"] == "json_pointer"
        assert table["source_pointer"] == f"/digest/phases/{phase}/series"
        assert "labels/extra~1test~0label" in table["columns"]
        assert decode_columnar_rows(table) == expected_rows


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


def test_preserves_large_critical_evidence_within_expanded_context_budget():
    rows = [{"service": f"service-{index}", "fact": "x" * 80} for index in range(800)]
    compacted = projection.compact_evidence_for_model(
        [item("E001", {"critical_path": rows})]
    )
    encoded = json.dumps(compacted, ensure_ascii=False, sort_keys=True)
    critical_path = compacted[0]["digest_summary"]["critical_path"]

    assert 56_000 < len(encoded) <= projection.MODEL_EVIDENCE_CONTEXT_CHARS
    assert critical_path["source_pointer"] == "/digest/critical_path"
    assert [
        dict(zip(critical_path["columns"], row)) for row in critical_path["rows"]
    ] == rows


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


def test_large_evidence_is_summarized_before_the_context_limit(monkeypatch):
    monkeypatch.setattr(
        projection, "MODEL_EVIDENCE_SUMMARY_THRESHOLD_CHARS", 2500
    )
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []
    session = projection.EvidenceProjectionSession(summary_llm(calls))

    projected = session.model_view(evidence)

    audit = session.audit[0]
    assert len(calls) == 1
    assert audit["summary_trigger"] == "reuse_threshold"
    assert audit["summarized"] is True
    assert audit["split"] is False
    assert audit["partition_count"] == 1
    assert audit["summary_model_calls"] == 1
    assert [entry["id"] for entry in projected] == [
        entry["id"] for entry in evidence
    ]
    assert all(
        entry["digest_summary"]["bounded_summary_version"]
        == projection.SPLIT_PROJECTION_VERSION
        for entry in projected
    )
    assert session.model_view(evidence) == projected
    assert len(calls) == 1


def test_large_evidence_is_partitioned_summarized_and_cached_by_source():
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []
    session = projection.EvidenceProjectionSession(summary_llm(calls), char_budget=2500)

    projected = session.model_view(evidence)

    assert len(json.dumps(projected, ensure_ascii=False, sort_keys=True)) <= 2500
    assert [entry["id"] for entry in projected] == [entry["id"] for entry in evidence]
    assert all(
        entry["digest_summary"]["bounded_summary_version"]
        == projection.SPLIT_PROJECTION_VERSION
        for entry in projected
    )
    assert all(entry["digest_summary"]["source_sha256"] for entry in projected)
    assert len(calls) == 3
    assert session.audit[0]["split"] is True
    assert session.audit[0]["partition_count"] == 3
    assert session.audit[0]["projected_evidence"] == projected

    assert session.model_view(evidence) == projected
    assert len(calls) == 3
    assert (
        projection.compact_evidence_for_model(projected, char_budget=2500) == projected
    )


def test_expanded_evidence_reuses_summaries_by_source_id():
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []
    session = projection.EvidenceProjectionSession(summary_llm(calls), char_budget=2500)

    session.model_view(evidence)
    expanded = evidence + [critical_item("E004")]
    projected = session.model_view(expanded)

    assert len(calls) == 4
    assert [entry["id"] for entry in projected] == [
        "E001",
        "E002",
        "E003",
        "E004",
    ]
    assert session.audit[-1]["reused_summary_ids"] == ["E001", "E002", "E003"]
    assert session.audit[-1]["summary_model_calls"] == 1


def test_split_projection_redacts_unsupported_numeric_claims_after_one_retry():
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []
    session = projection.EvidenceProjectionSession(
        summary_llm(calls, "The metric increased by 999 units."),
        char_budget=2500,
    )

    projected = session.model_view(evidence)

    assert len(calls) == 6
    assert session.audit[-1]["summary_model_calls"] == 6
    assert session.audit[-1]["partition_count"] == 3
    assert all(
        partition["numeric_claim_redactions"]
        for partition in session.audit[-1]["partitions"]
    )
    assert all(
        entry["digest_summary"]["summary_validation"] == "numeric_claims_redacted"
        for entry in projected
    )
    assert all(
        entry["digest_summary"]["redacted_numeric_claims_count"] == 1
        and "?" in entry["digest_summary"]["summary"]
        and "999" not in entry["digest_summary"]["summary"]
        for entry in projected
    )


def test_numeric_redaction_keeps_a_max_length_summary_within_schema(monkeypatch):
    monkeypatch.setattr(
        projection, "MODEL_EVIDENCE_SUMMARY_THRESHOLD_CHARS", 1
    )
    evidence = [critical_item("E001")]
    calls = []
    prefix = "The metric increased by 999 units. "
    session = projection.EvidenceProjectionSession(
        summary_llm(calls, prefix + "x" * (700 - len(prefix))),
        char_budget=2500,
    )

    projected = session.model_view(evidence)

    summary = projected[0]["digest_summary"]["summary"]
    assert len(summary) <= 700
    assert "999" not in summary
    assert "?" in summary
    assert projected[0]["digest_summary"]["summary_validation"] == (
        "numeric_claims_redacted"
    )


def test_split_projection_repairs_one_unsupported_numeric_response():
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []

    def invoke(messages, tools, **kwargs):
        calls.append(messages)
        payload = next(
            json.loads(message["content"])
            for message in reversed(messages)
            if message["role"] == "user"
            and message["content"].startswith('{"evidence"')
        )
        summary = (
            "The metric increased by 999 units."
            if len(calls) == 1
            else "Observed changes are retained without invented quantities."
        )
        return {
            "content": json.dumps(
                {
                    "items": [
                        {
                            "evidence_id": item["id"],
                            "summary": summary,
                        }
                        for item in payload["evidence"]
                    ]
                }
            )
        }

    session = projection.EvidenceProjectionSession(invoke, char_budget=2500)

    projected = session.model_view(evidence)

    assert len(projected) == 3
    assert len(calls) == 4
    assert "999" in calls[1][-1]["content"]


def test_split_projection_accepts_equivalent_decimal_formatting():
    evidence = [critical_item(f"E{index:03d}") for index in range(1, 4)]
    calls = []
    session = projection.EvidenceProjectionSession(
        summary_llm(calls, "Observed count is 4.0."),
        char_budget=2500,
    )

    projected = session.model_view(evidence)

    assert len(projected) == 3
    assert len(calls) == 3


def test_single_unpartitionable_evidence_fails_without_summarizing():
    evidence = [critical_item("E001")]
    calls = []
    session = projection.EvidenceProjectionSession(summary_llm(calls), char_budget=100)

    with pytest.raises(projection.EvidenceContextOverflow):
        session.model_view(evidence)
    assert calls == []
