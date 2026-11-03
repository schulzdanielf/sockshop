import base64
import copy
import struct
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

import experiment.platform.tools.migrate_observability_history as history

from experiment.platform.tools.migrate_observability_history import (
    _family_status,
    _fetch_tempo_traces,
    _length_field,
    _log_coverage_status,
    _loki_entries_until_match,
    _namespace_log_query,
    _encode_remote_write_series,
    _prom_samples,
    _sample_digest,
    _snapshot_log_entries,
    _tempo_ingest_batch,
    _tempo_trace_span_count,
    _tempo_to_otlp,
    _tempo_search,
)


def test_prometheus_matrix_samples_are_canonicalized():
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "up", "service": "carts"},
                    "values": [[10.5, "1"], [25.5, "0"]],
                }
            ],
        },
    }

    samples, unsupported = _prom_samples(payload)

    assert unsupported == 0
    assert len(samples) == 2
    assert samples[0]["timestamp_ms"] == 10500
    assert samples[0]["value"] == 1.0
    assert _sample_digest(samples) == _sample_digest(list(reversed(samples)))


def test_prometheus_histograms_are_counted_as_unsupported():
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"__name__": "request_duration"},
                    "values": [],
                    "histograms": [{"timestamp": 10, "histogram": {}}],
                }
            ],
        },
    }

    samples, unsupported = _prom_samples(payload)

    assert samples == []
    assert unsupported == 1


def test_remote_write_protobuf_compresses_as_snappy_block():
    import snappy

    series = _encode_remote_write_series(
        {"__name__": "up", "service": "carts"},
        [{"timestamp_ms": 1000, "value": 1.0}],
    )
    request = _length_field(1, series)

    assert snappy.decompress(snappy.compress(request)) == request
    assert request.startswith(b"\x0a")
    sample_message = b"\x09" + struct.pack("<d", 1.0) + b"\x10\xe8\x07"
    assert series.endswith(_length_field(2, sample_message))


def test_namespace_log_query_excludes_bounded_snapshot_streams():
    assert _namespace_log_query(
        "sock-shop", exclude_snapshots=True
    ) == '{namespace="sock-shop",capture_quality=""}'


def test_namespace_log_query_rejects_non_kubernetes_names():
    try:
        _namespace_log_query("sock-shop\"} |= \"injected")
    except ValueError as exc:
        assert "namespace" in str(exc)
    else:
        raise AssertionError("invalid namespace should be rejected")


def test_snapshot_log_entries_preserve_provenance_and_timestamps():
    run = {"run_id": "run-abc123", "namespace": "sock-shop"}
    snapshot = {
        "services": {
            "carts": {
                "phases": {
                    "fault": {
                        "sample_lines": [
                            {
                                "timestamp": "2026-10-28T19:35:35.386Z",
                                "pod": "carts-abc",
                                "container": "carts",
                                "message": "request failed",
                            }
                        ]
                    }
                }
            }
        }
    }

    entries = _snapshot_log_entries(snapshot, run)

    assert len(entries) == 1
    assert entries[0]["labels"]["run_id"] == "run-abc123"
    assert entries[0]["labels"]["phase"] == "fault"
    assert entries[0]["labels"]["capture_quality"] == "bounded_snapshot"
    assert entries[0]["line"] == "request failed"


def test_tempo_batched_trace_is_wrapped_as_otlp_resource_spans():
    payload = {
        "batches": [
            {
                "resource": {"attributes": []},
                "instrumentationLibrary": {"name": "sockshop"},
                "spans": [{"traceId": "a" * 32, "spanId": "b" * 16}],
            }
        ]
    }

    result = _tempo_to_otlp(payload)

    assert result == {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "sockshop"},
                        "spans": [{"traceId": "a" * 32, "spanId": "b" * 16}],
                    }
                ],
            }
        ]
    }


def test_tempo_trace_ids_are_normalized_without_mutating_source():
    trace_id = base64.b64encode(bytes(range(16))).decode("ascii")
    span_id = base64.b64encode(bytes(range(8))).decode("ascii")
    parent_span_id = base64.b64encode(bytes(range(8, 16))).decode("ascii")
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "parentSpanId": parent_span_id,
                                "links": [
                                    {"traceId": trace_id, "spanId": span_id}
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    original = copy.deepcopy(payload)

    result = _tempo_to_otlp(payload)
    span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    assert span["traceId"] == bytes(range(16)).hex()
    assert span["spanId"] == bytes(range(8)).hex()
    assert span["parentSpanId"] == bytes(range(8, 16)).hex()
    assert span["links"][0]["traceId"] == bytes(range(16)).hex()
    assert payload == original


def test_tempo_search_splits_capped_windows_and_deduplicates_results():
    traces = {
        (100, 103): [{"traceID": "a" * 32}, {"traceID": "b" * 32}],
        (100, 101): [{"traceID": "a" * 32}],
        (102, 103): [{"traceID": "b" * 32}],
    }

    def search(_client, _method, _url, params):
        bounds = (int(params["start"]), int(params["end"]))
        return {"traces": traces[bounds]}

    with patch.object(history, "TEMPO_SEARCH_LIMIT", 2), patch.object(
        history, "_json_request", side_effect=search
    ):
        result = _tempo_search(
            None,
            "http://tempo",
            datetime.fromtimestamp(100, timezone.utc),
            datetime.fromtimestamp(103, timezone.utc),
        )

    assert result["_migration_complete"] is True
    assert [trace["traceID"] for trace in result["_migration_traces"]] == [
        "a" * 32,
        "b" * 32,
    ]
    assert len(result["_migration_search_chunks"]) == 3


def test_loki_verification_retries_until_entries_are_queryable():
    entries = [
        {
            "timestamp_ns": 123,
            "labels": {"namespace": "sock-shop"},
            "line": "ready",
        }
    ]
    with patch.object(
        history, "_loki_entries", side_effect=[([], True), (entries, True)]
    ), patch.object(history.time, "sleep") as sleep:
        actual, complete, matches = _loki_entries_until_match(
            None, "http://loki", "{}", 0, 1000, 10, entries
        )

    assert actual == entries
    assert complete is True
    assert matches is True
    sleep.assert_called_once_with(1)


def test_log_coverage_accepts_target_supersets_but_rejects_missing_source_entries():
    source = Counter({"source-log": 1})
    target_superset = Counter({"source-log": 1, "other-log": 1})
    target_missing = Counter({"other-log": 1})

    assert _log_coverage_status(source, target_superset, True, True) == "complete"
    assert _log_coverage_status(source, target_missing, True, True) == "partial"
    assert _log_coverage_status(Counter(), target_superset, True, True) == "missing"


def test_tempo_source_trace_fetch_retries_not_found_until_available():
    trace_id = "a" * 32
    request = history.httpx.Request("GET", "http://tempo/api/traces/" + trace_id)
    response = history.httpx.Response(404, request=request)
    not_found = history.httpx.HTTPStatusError(
        "not found", request=request, response=response
    )
    payload = {"batches": [{"spans": []}]}

    with patch.object(
        history, "_tempo_trace", side_effect=[not_found, payload]
    ), patch.object(history.time, "sleep") as sleep:
        traces, errors = _fetch_tempo_traces(None, "http://tempo", [trace_id])

    assert traces == [{"trace_id": trace_id, "trace": payload}]
    assert errors == []
    sleep.assert_called_once_with(1)


def test_tempo_source_trace_fetch_bounds_retries_for_persistent_not_found():
    trace_id = "b" * 32
    request = history.httpx.Request("GET", "http://tempo/api/traces/" + trace_id)
    response = history.httpx.Response(404, request=request)
    not_found = history.httpx.HTTPStatusError(
        "not found", request=request, response=response
    )

    with patch.object(history, "_tempo_trace", side_effect=not_found) as fetch, patch.object(
        history.time, "sleep"
    ) as sleep:
        traces, errors = _fetch_tempo_traces(None, "http://tempo", [trace_id])

    assert traces == []
    assert errors[0]["trace_id"] == trace_id
    assert errors[0]["error"]["type"] == "HTTPStatusError"
    assert fetch.call_count == 7
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4, 8, 16, 32]


def test_target_tempo_trace_lookup_counts_spans_and_treats_404_as_missing():
    payload = {
        "batches": [
            {
                "scopeSpans": [
                    {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16}]}
                ]
            }
        ]
    }
    with patch.object(history, "_tempo_trace", return_value=payload):
        assert _tempo_trace_span_count(None, "http://tempo", "a" * 32) == 1

    request = history.httpx.Request("GET", "http://tempo/api/traces/" + "a" * 32)
    response = history.httpx.Response(404, request=request)
    not_found = history.httpx.HTTPStatusError(
        "not found", request=request, response=response
    )
    with patch.object(history, "_tempo_trace", side_effect=not_found):
        assert _tempo_trace_span_count(None, "http://tempo", "a" * 32) is None


def test_tempo_batch_ingest_rejects_partial_success():
    class Response:
        content = b'{"partialSuccess":{}}'

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "partialSuccess": {
                    "rejectedSpans": 2,
                    "errorMessage": "invalid span",
                }
            }

    class Client:
        def post(self, *_args, **_kwargs):
            return Response()

    trace = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {"spans": [{"traceId": "a" * 32, "spanId": "b" * 16}]}
                ]
            }
        ]
    }
    try:
        _tempo_ingest_batch(Client(), "http://tempo", [trace])
    except history.TempoPartialSuccessError as exc:
        assert "rejected 2 spans" in str(exc)
    else:
        raise AssertionError("partial OTLP success should not be accepted")


def test_missing_or_different_sample_volumes_never_claim_complete():
    assert _family_status(0, 0, True, None, None) == "missing"
    assert _family_status(10, 9, True, "source", "target") == "partial"
    assert _family_status(10, 10, False, "same", "same") == "partial"
    assert _family_status(10, 10, True, "same", "same") == "complete"
