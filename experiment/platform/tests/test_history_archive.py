import gzip
import hashlib
import io
import json
from datetime import datetime

import pytest

from experiment.platform.backend.history_archive import (
    HistoryArchiveError,
    HistoryArchiveReader,
)


CAMPAIGN_ID = "campaign-2026"
RUN_ID = "run-test"
PHASES = {
    "baseline": {
        "start": "2026-10-02T10:00:00Z",
        "end": "2026-10-02T10:01:00Z",
    },
    "fault": {
        "start": "2026-10-02T10:02:00Z",
        "end": "2026-10-02T10:03:00Z",
    },
}


class MemoryResponse:
    def __init__(self, value):
        self.stream = io.BytesIO(value)

    def read(self, size=-1):
        return self.stream.read(size)

    def close(self):
        self.stream.close()

    def release_conn(self):
        pass


class MemoryS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, bucket, key):
        return MemoryResponse(self.objects[key])


def _ns(value):
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return str(int(timestamp.timestamp() * 1_000_000_000))


def _span(trace_id, start, duration_ms, *, error=False):
    start_ns = int(_ns(start))
    end_ns = start_ns + duration_ms * 1_000_000
    return {
        "traceId": trace_id,
        "spanId": trace_id[:16],
        "name": "GET /orders",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": 2 if error else 1},
        "attributes": [
            {
                "key": "http.response.status_code",
                "value": {"intValue": 500 if error else 200},
            }
        ],
    }


def _trace(trace_id, span):
    return {
        "trace_id": trace_id,
        "trace": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "orders"},
                            }
                        ]
                    },
                    "scopeSpans": [{"spans": [span]}],
                }
            ]
        },
    }


def _reader(*, tamper=False):
    traces = [
        _trace(
            "1" * 32,
            _span("1" * 32, "2026-10-02T10:00:10Z", 100),
        ),
        _trace(
            "2" * 32,
            _span("2" * 32, "2026-10-02T10:02:10Z", 200, error=True),
        ),
    ]
    bundle = gzip.compress(
        json.dumps({"source": {"traces": traces}}, separators=(",", ":")).encode()
    )
    expected_hash = hashlib.sha256(bundle).hexdigest()
    if tamper:
        bundle += b" "
    manifest = {
        "campaign_id": CAMPAIGN_ID,
        "run_id": RUN_ID,
        "window": {
            "start": "2026-10-02T10:00:00Z",
            "end": "2026-10-02T10:03:00Z",
        },
        "bundle_sha256": expected_hash,
        "telemetry": {
            "traces": {
                "status": "partial",
                "search_index_status": "partial",
                "source_search_complete": True,
                "source_traces": 2,
                "source_spans": 2,
                "source_fetch_errors": [],
            }
        },
    }
    objects = {
        f"{CAMPAIGN_ID}/{RUN_ID}/manifest.json": json.dumps(manifest).encode(),
        f"{CAMPAIGN_ID}/{RUN_ID}/source-bundle.json.gz": bundle,
    }
    return HistoryArchiveReader(
        endpoint="unused",
        client=MemoryS3(objects),
    )


def test_archive_reader_verifies_bundle_and_summarizes_original_spans():
    reader = _reader()

    evidence = reader.read_trace_evidence(
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        phases=PHASES,
    )

    assert evidence["status"] == "ok"
    assert evidence["evidence_source"] == "archive"
    assert evidence["archive_key"] == (
        f"{CAMPAIGN_ID}/{RUN_ID}/source-bundle.json.gz"
    )
    assert evidence["trace_count"] == 2
    assert evidence["span_count"] == 2
    assert evidence["archive_span_count"] == 2
    assert evidence["trace_count_parity"] is True
    assert evidence["span_count_parity"] is True
    assert evidence["phases"]["baseline"]["trace_count"] == 1
    assert evidence["phases"]["fault"]["error_span_count"] == 1
    assert evidence["trace_details"][0]["trace_id"] == "2" * 32
    assert evidence["trace_details"][0]["http_status_code"] == 500
    assert evidence["trace_details"][0]["spans"][0]["span_id"] == "2" * 16
    assert evidence["search_index_status"] == "partial"


def test_archive_reader_derives_cross_service_dependency_edges():
    reader = _reader()
    trace_id = "3" * 32
    parent = _span(trace_id, "2026-10-02T10:02:10Z", 300)
    parent["spanId"] = "a" * 16
    child = _span(trace_id, "2026-10-02T10:02:11Z", 200, error=True)
    child["spanId"] = "b" * 16
    child["parentSpanId"] = parent["spanId"]
    trace = {
        "trace_id": trace_id,
        "trace": {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "frontend"},
                            }
                        ]
                    },
                    "scopeSpans": [{"spans": [parent]}],
                },
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "orders"},
                            }
                        ]
                    },
                    "scopeSpans": [{"spans": [child]}],
                },
            ]
        },
    }
    bundle = gzip.compress(
        json.dumps({"source": {"traces": [trace]}}, separators=(",", ":")).encode()
    )
    manifest_key = f"{CAMPAIGN_ID}/{RUN_ID}/manifest.json"
    manifest = json.loads(reader.client.objects[manifest_key])
    manifest["bundle_sha256"] = hashlib.sha256(bundle).hexdigest()
    manifest["telemetry"]["traces"]["source_traces"] = 1
    manifest["telemetry"]["traces"]["source_spans"] = 2
    reader.client.objects[manifest_key] = json.dumps(manifest).encode()
    reader.client.objects[f"{CAMPAIGN_ID}/{RUN_ID}/source-bundle.json.gz"] = bundle

    evidence = reader.read_trace_evidence(
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        phases=PHASES,
    )

    assert evidence["status"] == "ok"
    assert evidence["trace_details"][0]["dependency_edges"] == [
        {
            "from_service": "frontend",
            "to_service": "orders",
            "operation": "GET /orders",
            "duration_ms": 200.0,
            "is_error": True,
        }
    ]


def test_archive_reader_reads_run_manifest_without_downloading_bundle():
    reader = _reader()

    manifest = reader.read_manifest(campaign_id=CAMPAIGN_ID, run_id=RUN_ID)

    assert manifest["campaign_id"] == CAMPAIGN_ID
    assert manifest["run_id"] == RUN_ID
    assert manifest["telemetry"]["traces"]["source_spans"] == 2


def test_archive_reader_rejects_bundle_checksum_mismatch():
    with pytest.raises(HistoryArchiveError, match="SHA-256"):
        _reader(tamper=True).read_trace_evidence(
            campaign_id=CAMPAIGN_ID,
            run_id=RUN_ID,
            phases=PHASES,
        )


def test_archive_reader_marks_phase_outside_archived_window_partial():
    phases = {
        **PHASES,
        "post": {
            "start": "2026-10-02T10:03:00Z",
            "end": "2026-10-02T10:04:00Z",
        },
    }

    evidence = _reader().read_trace_evidence(
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        phases=phases,
    )

    assert evidence["status"] == "partial"
    assert evidence["window_coverage"]["baseline"]["covered"] is True
    assert evidence["window_coverage"]["post"]["covered"] is False


def test_archive_reader_marks_manifest_volume_mismatch_partial():
    reader = _reader()
    manifest_key = f"{CAMPAIGN_ID}/{RUN_ID}/manifest.json"
    manifest = json.loads(reader.client.objects[manifest_key])
    manifest["telemetry"]["traces"]["source_spans"] = 3
    reader.client.objects[manifest_key] = json.dumps(manifest).encode()

    evidence = reader.read_trace_evidence(
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
        phases=PHASES,
    )

    assert evidence["status"] == "partial"
    assert evidence["span_count_parity"] is False
    assert evidence["source_span_count_expected"] == 3


def test_archive_reader_rejects_unsafe_object_key_components():
    with pytest.raises(HistoryArchiveError, match="campaign_id"):
        _reader().read_trace_evidence(
            campaign_id="../campaign",
            run_id=RUN_ID,
            phases=PHASES,
        )


def test_archive_reader_is_opt_in(monkeypatch):
    monkeypatch.delenv("HISTORY_ARCHIVE_ENABLED", raising=False)
    assert HistoryArchiveReader.from_environment() is None

    monkeypatch.setenv("HISTORY_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("HISTORY_ARCHIVE_ENDPOINT", "127.0.0.1:8333")
    reader = HistoryArchiveReader.from_environment()
    assert reader is not None
    assert reader.endpoint == "127.0.0.1:8333"
