#!/usr/bin/env python3
"""Migrate one or more run windows into the isolated historical telemetry stack."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import binascii
import copy
import gzip
import hashlib
import io
import json
import math
import os
import re
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx


LOKI_SOURCE_LIMIT = 2000
LOKI_HISTORY_LIMIT = 10000
TEMPO_SEARCH_LIMIT = 5000
TEMPO_TRACE_WORKERS = 8
TEMPO_INGEST_BATCH_TRACES = 50
REMOTE_WRITE_BATCH_SERIES = 100


def _iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _window_times(run: Dict[str, Any]) -> Tuple[datetime, datetime]:
    window = run.get("observation_window")
    if not isinstance(window, dict):
        raise ValueError(f"{run.get('run_id', 'run')}: observation window is missing")
    bounds = window.get("window")
    if not isinstance(bounds, dict) or not bounds.get("start") or not bounds.get("end"):
        raise ValueError(f"{run.get('run_id', 'run')}: observation window has no bounds")
    start = _iso_datetime(str(bounds["start"]))
    end = _iso_datetime(str(bounds["end"]))
    if end <= start:
        raise ValueError(f"{run.get('run_id', 'run')}: observation window is invalid")
    return start, end


def _epoch_seconds(value: datetime) -> float:
    return value.timestamp()


def _epoch_ns(value: datetime) -> int:
    return int(round(value.timestamp() * 1_000_000_000))


def _json_request(
    client: httpx.Client, method: str, url: str, **kwargs: Any
) -> Dict[str, Any]:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{method} {url} returned a non-object JSON response")
    return payload


def _prom_range(
    client: httpx.Client,
    base_url: str,
    start: datetime,
    end: datetime,
    namespace: str,
) -> Dict[str, Any]:
    namespace_value = json.dumps(namespace)
    query = (
        f'{{kubernetes_namespace={namespace_value}}} or '
        f'{{namespace={namespace_value}}}'
    )
    payload = _json_request(
        client,
        "GET",
        f"{base_url.rstrip('/')}/api/v1/query_range",
        params={
            "query": query,
            "start": _epoch_seconds(start),
            "end": _epoch_seconds(end),
            "step": "15s",
        },
    )
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus range query failed: {payload.get('error', 'unknown error')}")
    return payload


def _namespace_log_query(namespace: str, *, exclude_snapshots: bool = False) -> str:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError("namespace is not a valid Kubernetes namespace")
    query = f"{{namespace={json.dumps(namespace)}"
    if exclude_snapshots:
        query += ',capture_quality=""'
    return query + "}"


def _prom_samples(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    data = payload.get("data") or {}
    if data.get("resultType") != "matrix":
        raise ValueError("Prometheus range query did not return a matrix")
    samples: List[Dict[str, Any]] = []
    unsupported_histograms = 0
    for series in data.get("result") or []:
        labels = series.get("metric") or {}
        if not isinstance(labels, dict) or not labels.get("__name__"):
            unsupported_histograms += len(series.get("values") or [])
            continue
        for timestamp, value in series.get("values") or []:
            samples.append(
                {
                    "labels": {str(name): str(label) for name, label in labels.items()},
                    "timestamp_ms": int(round(float(timestamp) * 1000)),
                    "value": float(value),
                }
            )
        unsupported_histograms += len(series.get("histograms") or [])
    return samples, unsupported_histograms


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint value cannot be negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _length_field(field_number: int, value: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(value)) + value


def _encode_remote_write_series(
    labels: Dict[str, str], samples: Sequence[Dict[str, Any]]
) -> bytes:
    encoded = bytearray()
    for name, value in sorted(labels.items()):
        label = _length_field(1, name.encode()) + _length_field(2, value.encode())
        encoded.extend(_length_field(1, label))
    for sample in sorted(samples, key=lambda item: item["timestamp_ms"]):
        sample_message = b"\x09"
        sample_message += struct.pack("<d", sample["value"])
        sample_message += b"\x10"
        sample_message += _varint(sample["timestamp_ms"])
        encoded.extend(_length_field(2, sample_message))
    return bytes(encoded)


def _sample_digest(samples: Iterable[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    canonical = sorted(
        (
            tuple(sorted(sample["labels"].items())),
            int(sample["timestamp_ms"]),
            float(sample["value"]).hex(),
        )
        for sample in samples
    )
    for labels, timestamp_ms, value in canonical:
        row = json.dumps(
            [labels, timestamp_ms, value], separators=(",", ":"), ensure_ascii=False
        )
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _remote_write(
    client: httpx.Client, target_url: str, samples: Sequence[Dict[str, Any]]
) -> int:
    import snappy

    grouped: Dict[Tuple[Tuple[str, str], ...], List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[tuple(sorted(sample["labels"].items()))].append(sample)
    series_items = list(grouped.items())
    written = 0
    for offset in range(0, len(series_items), REMOTE_WRITE_BATCH_SERIES):
        request = bytearray()
        batch = series_items[offset : offset + REMOTE_WRITE_BATCH_SERIES]
        for label_pairs, series_samples in batch:
            labels = dict(label_pairs)
            series_message = _encode_remote_write_series(labels, series_samples)
            request.extend(_length_field(1, series_message))
        response = client.post(
            f"{target_url.rstrip('/')}/api/v1/write",
            content=snappy.compress(bytes(request)),
            headers={
                "Content-Type": "application/x-protobuf",
                "Content-Encoding": "snappy",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
            },
        )
        response.raise_for_status()
        written += sum(len(items) for _, items in batch)
    return written


def _loki_chunk(
    client: httpx.Client,
    base_url: str,
    query: str,
    start_ns: int,
    end_ns: int,
    limit: int,
    depth: int = 0,
) -> Tuple[List[Dict[str, Any]], bool]:
    payload = _json_request(
        client,
        "GET",
        f"{base_url.rstrip('/')}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "forward",
        },
    )
    if payload.get("status") != "success":
        raise ValueError(f"Loki range query failed: {payload.get('error', 'unknown error')}")
    entries = []
    for stream in (payload.get("data") or {}).get("result") or []:
        labels = stream.get("stream") or {}
        for timestamp, line in stream.get("values") or []:
            entries.append(
                {
                    "timestamp_ns": int(timestamp),
                    "labels": {str(name): str(value) for name, value in labels.items()},
                    "line": str(line),
                }
            )
    if len(entries) < limit:
        return entries, True
    if depth >= 32 or end_ns - start_ns <= 1:
        return entries, False
    midpoint = (start_ns + end_ns) // 2
    left, left_complete = _loki_chunk(
        client, base_url, query, start_ns, midpoint, limit, depth + 1
    )
    right, right_complete = _loki_chunk(
        client, base_url, query, midpoint + 1, end_ns, limit, depth + 1
    )
    return _unique_log_entries(left + right), left_complete and right_complete


def _unique_log_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique = {}
    for entry in entries:
        key = (
            entry["timestamp_ns"],
            tuple(sorted(entry["labels"].items())),
            entry["line"],
        )
        unique[key] = entry
    return [unique[key] for key in sorted(unique)]


def _loki_entries(
    client: httpx.Client,
    base_url: str,
    query: str,
    start_ns: int,
    end_ns: int,
    limit: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    return _loki_chunk(client, base_url, query, start_ns, end_ns, limit)


def _log_counter(entries: Iterable[Dict[str, Any]]) -> Counter:
    return Counter(
        (
            int(entry["timestamp_ns"]),
            tuple(sorted(entry["labels"].items())),
            str(entry["line"]),
        )
        for entry in entries
    )


def _log_digest(entries: Iterable[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(_log_counter(entries).items()):
        row = json.dumps(item, separators=(",", ":"), ensure_ascii=False)
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _push_loki(
    client: httpx.Client, target_url: str, entries: Sequence[Dict[str, Any]]
) -> None:
    grouped: Dict[Tuple[Tuple[str, str], ...], List[List[str]]] = defaultdict(list)
    for entry in entries:
        labels = tuple(sorted(entry["labels"].items()))
        grouped[labels].append([str(entry["timestamp_ns"]), entry["line"]])
    streams = [
        {"stream": dict(labels), "values": sorted(values, key=lambda item: int(item[0]))}
        for labels, values in grouped.items()
    ]
    for offset in range(0, len(streams), 100):
        response = client.post(
            f"{target_url.rstrip('/')}/loki/api/v1/push",
            json={"streams": streams[offset : offset + 100]},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()


def _loki_entries_until_match(
    client: httpx.Client,
    base_url: str,
    query: str,
    start_ns: int,
    end_ns: int,
    limit: int,
    expected: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], bool, bool]:
    expected_digest = _log_digest(expected)
    latest: List[Dict[str, Any]] = []
    complete = False
    for delay in (0, 1, 2, 4, 8):
        if delay:
            time.sleep(delay)
        latest, complete = _loki_entries(
            client, base_url, query, start_ns, end_ns, limit
        )
        if (
            complete
            and len(latest) == len(expected)
            and _log_digest(latest) == expected_digest
        ):
            return latest, complete, True
    return latest, complete, False


def _snapshot_log_entries(snapshot: Dict[str, Any], run: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = []
    run_id = str(run["run_id"])
    services = snapshot.get("services") or {}
    if not isinstance(services, dict):
        return entries
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        for phase_name, phase in (service.get("phases") or {}).items():
            if not isinstance(phase, dict):
                continue
            for line in phase.get("sample_lines") or []:
                if not isinstance(line, dict) or not line.get("timestamp"):
                    continue
                timestamp_ns = _epoch_ns(_iso_datetime(str(line["timestamp"])))
                labels = {
                    "namespace": str(line.get("namespace") or run.get("namespace") or ""),
                    "service": str(line.get("service") or service_name),
                    "pod": str(line.get("pod") or ""),
                    "container": str(line.get("container") or ""),
                    "run_id": run_id,
                    "phase": str(phase_name),
                    "capture_quality": "bounded_snapshot",
                }
                entries.append(
                    {
                        "timestamp_ns": timestamp_ns,
                        "labels": labels,
                        "line": str(line.get("message") or ""),
                    }
                )
    return _unique_log_entries(entries)


def _trace_ids(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    data = payload.get("_migration_traces")
    if not isinstance(data, list):
        data = payload.get("traces")
    if not isinstance(data, list):
        data = (payload.get("data") or {}).get("traces")
    if not isinstance(data, list):
        raise ValueError("Tempo search response has no traces list")
    complete = payload.get("_migration_complete")
    if isinstance(complete, bool):
        return data, complete
    return data, len(data) < TEMPO_SEARCH_LIMIT


def _tempo_search(
    client: httpx.Client, base_url: str, start: datetime, end: datetime
) -> Dict[str, Any]:
    query_chunks: List[Dict[str, Any]] = []

    def search_range(
        range_start: int, range_end: int, depth: int
    ) -> Tuple[List[Dict[str, Any]], bool]:
        payload = _json_request(
            client,
            "GET",
            f"{base_url.rstrip('/')}/api/search",
            params={
                "start": str(range_start),
                "end": str(range_end),
                "limit": str(TEMPO_SEARCH_LIMIT),
            },
        )
        traces, complete = _trace_ids(payload)
        metrics = payload.get("metrics")
        query_chunks.append(
            {
                "start": range_start,
                "end": range_end,
                "returned_traces": len(traces),
                "complete": complete,
                "metrics": metrics if isinstance(metrics, dict) else {},
            }
        )
        if complete or depth >= 32 or range_end - range_start <= 1:
            return traces, complete

        midpoint = (range_start + range_end) // 2
        left, left_complete = search_range(range_start, midpoint, depth + 1)
        right, right_complete = search_range(midpoint + 1, range_end, depth + 1)
        unique = {_tempo_trace_id(trace): trace for trace in left + right}
        return [unique[trace_id] for trace_id in sorted(unique)], (
            left_complete and right_complete
        )

    range_start = int(start.timestamp())
    range_end = math.ceil(end.timestamp())
    if range_end < range_start:
        raise ValueError("Tempo search window has invalid bounds")
    traces, complete = search_range(range_start, range_end, 0)
    query_chunks.sort(key=lambda item: (item["start"], item["end"]))
    return {
        "_migration_complete": complete,
        "_migration_traces": traces,
        "_migration_search_chunks": query_chunks,
    }


def _tempo_trace_id(trace: Dict[str, Any]) -> str:
    value = trace.get("traceID") or trace.get("traceId") or trace.get("trace_id")
    if not isinstance(value, str) or not value:
        raise ValueError("Tempo search result is missing a trace ID")
    return value


def _tempo_id_to_otlp(value: Any, byte_length: int, field: str) -> Any:
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(f"Tempo trace has an invalid {field}")
    if re.fullmatch(rf"[0-9a-fA-F]{{{byte_length * 2}}}", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Tempo trace has an invalid {field}") from exc
    if len(decoded) != byte_length:
        raise ValueError(f"Tempo trace has an invalid {field} length")
    return decoded.hex()


def _normalize_span_ids(span: Dict[str, Any]) -> None:
    for field, byte_length in (
        ("traceId", 16),
        ("spanId", 8),
        ("parentSpanId", 8),
    ):
        if field in span:
            span[field] = _tempo_id_to_otlp(span[field], byte_length, field)
    for link in span.get("links") or []:
        if not isinstance(link, dict):
            continue
        for field, byte_length in (("traceId", 16), ("spanId", 8)):
            if field in link:
                link[field] = _tempo_id_to_otlp(link[field], byte_length, field)


def _tempo_to_otlp(payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("resourceSpans"), list):
        otlp = copy.deepcopy(payload)
    else:
        batches = payload.get("batches")
        if not isinstance(batches, list):
            nested = payload.get("trace")
            if isinstance(nested, dict):
                return _tempo_to_otlp(nested)
            raise ValueError("Tempo trace response cannot be converted to OTLP JSON")
        resource_spans = []
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            scopes = batch.get("scopeSpans")
            if not isinstance(scopes, list):
                scopes = [
                    {
                        "scope": batch.get("instrumentationScope")
                        or batch.get("instrumentationLibrary")
                        or {},
                        "spans": batch.get("spans") or [],
                    }
                ]
            resource_spans.append(
                {
                    "resource": copy.deepcopy(batch.get("resource") or {}),
                    "scopeSpans": copy.deepcopy(scopes),
                }
            )
        otlp = {"resourceSpans": resource_spans}
    if not otlp["resourceSpans"]:
        raise ValueError("Tempo trace response contains no OTLP resource spans")
    for resource in otlp["resourceSpans"]:
        if not isinstance(resource, dict):
            raise ValueError("Tempo trace contains an invalid resource")
        for scope in resource.get("scopeSpans") or []:
            if not isinstance(scope, dict):
                raise ValueError("Tempo trace contains an invalid scope")
            for span in scope.get("spans") or []:
                if not isinstance(span, dict):
                    raise ValueError("Tempo trace contains an invalid span")
                _normalize_span_ids(span)
    return otlp


def _trace_span_count(payload: Dict[str, Any]) -> int:
    otlp = _tempo_to_otlp(payload)
    return sum(
        len(scope.get("spans") or [])
        for resource in otlp["resourceSpans"]
        for scope in (resource.get("scopeSpans") or [])
    )


def _tempo_trace(
    client: httpx.Client, base_url: str, trace_id: str
) -> Dict[str, Any]:
    return _json_request(
        client,
        "GET",
        f"{base_url.rstrip('/')}/api/traces/{trace_id}",
    )


def _fetch_tempo_traces(
    client: httpx.Client, base_url: str, trace_ids: Sequence[str]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    payloads: Dict[str, Dict[str, Any]] = {}
    failures: Dict[str, Exception] = {}
    pending: Dict[str, Exception] = {}

    def fetch(trace_id: str):
        try:
            return trace_id, _tempo_trace(client, base_url, trace_id), None
        except (httpx.HTTPError, ValueError) as exc:
            return trace_id, None, exc

    def collect(results) -> None:
        for trace_id, payload, error in results:
            if error is None:
                payloads[trace_id] = payload
            elif (
                isinstance(error, httpx.HTTPStatusError)
                and error.response.status_code == 404
            ):
                pending[trace_id] = error
            else:
                failures[trace_id] = error

    with ThreadPoolExecutor(max_workers=TEMPO_TRACE_WORKERS) as executor:
        collect(executor.map(fetch, trace_ids))
        for delay in (1, 2, 4, 8, 16, 32):
            if not pending:
                break
            time.sleep(delay)
            retry_ids = list(pending)
            pending.clear()
            collect(executor.map(fetch, retry_ids))

    failures.update(pending)
    traces = [
        {"trace_id": trace_id, "trace": payloads[trace_id]}
        for trace_id in sorted(payloads)
    ]
    errors = [
        {"trace_id": trace_id, "error": _record_error(failures[trace_id])}
        for trace_id in sorted(failures)
    ]
    return traces, errors


def _tempo_trace_span_counts(
    client: httpx.Client, base_url: str, trace_ids: Sequence[str]
) -> List[Tuple[str, Optional[int], Exception | None]]:
    def lookup(trace_id: str) -> Tuple[str, Optional[int], Exception | None]:
        try:
            return trace_id, _tempo_trace_span_count(client, base_url, trace_id), None
        except (httpx.HTTPError, ValueError) as exc:
            return trace_id, None, exc

    with ThreadPoolExecutor(max_workers=TEMPO_TRACE_WORKERS) as executor:
        return list(executor.map(lookup, trace_ids))


def _tempo_trace_span_count(
    client: httpx.Client, base_url: str, trace_id: str
) -> Optional[int]:
    try:
        return _trace_span_count(_tempo_trace(client, base_url, trace_id))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


class TempoPartialSuccessError(ValueError):
    pass


def _tempo_ingest(
    client: httpx.Client, otlp_url: str, trace: Dict[str, Any]
) -> None:
    _tempo_ingest_batch(client, otlp_url, [trace])


def _tempo_ingest_batch(
    client: httpx.Client, otlp_url: str, traces: Sequence[Dict[str, Any]]
) -> None:
    resource_spans = []
    for trace in traces:
        resource_spans.extend(_tempo_to_otlp(trace)["resourceSpans"])
    if not resource_spans:
        raise ValueError("Tempo ingest batch contains no resource spans")
    response = client.post(
        f"{otlp_url.rstrip('/')}/v1/traces",
        json={"resourceSpans": resource_spans},
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()
    if response.content:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Tempo ingest returned a non-object response")
        partial_success = result.get("partialSuccess")
        if isinstance(partial_success, dict):
            rejected_spans = int(partial_success.get("rejectedSpans") or 0)
            if rejected_spans:
                detail = partial_success.get("errorMessage") or ""
                raise TempoPartialSuccessError(
                    f"Tempo rejected {rejected_spans} spans: {detail}"
                )


def _family_status(
    source_count: int,
    target_count: int,
    source_complete: bool,
    source_digest: str | None,
    target_digest: str | None,
    had_error: bool = False,
) -> str:
    if source_count == 0:
        return "missing"
    if (
        not had_error
        and source_complete
        and source_count == target_count
        and source_digest == target_digest
    ):
        return "complete"
    return "partial"


def _log_coverage_status(
    source_entries: Counter,
    target_entries: Counter,
    source_complete: bool,
    target_complete: bool,
) -> str:
    if not source_entries:
        return "missing"
    if (
        source_complete
        and target_complete
        and not (source_entries - target_entries)
    ):
        return "complete"
    return "partial"


def _record_error(exc: Exception) -> Dict[str, str]:
    message = str(exc)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.text:
        message = f"{message}; response={exc.response.text[:300]}"
    return {"type": type(exc).__name__, "message": message[:500]}


def _s3_client():
    from minio import Minio

    endpoint = os.environ["HISTORY_S3_ENDPOINT"]
    access_key = os.environ["HISTORY_S3_ACCESS_KEY"]
    secret_key = os.environ["HISTORY_S3_SECRET_KEY"]
    secure = os.getenv("HISTORY_S3_SECURE", "false").lower() == "true"
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


def _upload_json(client: Any, bucket: str, key: str, payload: Dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    client.put_object(
        bucket,
        key,
        io.BytesIO(compressed),
        len(compressed),
        content_type="application/gzip",
    )
    return hashlib.sha256(compressed).hexdigest()


def _save_run_archive(
    object_client: Any,
    bucket: str,
    prefix: str,
    bundle: Dict[str, Any],
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    bundle_hash = _upload_json(
        object_client, bucket, f"{prefix}/source-bundle.json.gz", bundle
    )
    manifest["bundle_sha256"] = bundle_hash
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, indent=2, ensure_ascii=False
    ).encode("utf-8")
    object_client.put_object(
        bucket,
        f"{prefix}/manifest.json",
        io.BytesIO(manifest_bytes),
        length=len(manifest_bytes),
        content_type="application/json",
    )
    return manifest


def migrate_run(
    client: httpx.Client,
    object_client: Any,
    bucket: str,
    source_prometheus: str,
    source_loki: str,
    source_tempo: str,
    target_prometheus: str,
    target_loki: str,
    target_tempo: str,
    target_tempo_otlp: str,
    campaign_id: str,
    run: Dict[str, Any],
) -> Dict[str, Any]:
    run_id = str(run.get("run_id") or "")
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,63}", run_id):
        raise ValueError("run_id contains characters unsafe for object keys")
    namespace = str(run.get("namespace") or "sock-shop")
    _namespace_log_query(namespace)
    prefix = f"{campaign_id}/{run_id}"
    try:
        start, end = _window_times(run)
    except ValueError as exc:
        bundle = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "namespace": namespace,
            "status": run.get("status", "unknown"),
            "window": None,
            "source": {
                "bounded_log_snapshot": run.get("log_snapshot"),
                "trace_collection": run.get("trace_collection"),
                "window_error": str(exc),
            },
        }
        manifest = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "namespace": namespace,
            "run_status": run.get("status", "unknown"),
            "window": None,
            "overall_status": "missing",
            "telemetry": {
                family: {"status": "missing", "reason": str(exc)}
                for family in ("metrics", "logs", "traces")
            },
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return _save_run_archive(object_client, bucket, prefix, bundle, manifest)
    start_ns, end_ns = _epoch_ns(start), _epoch_ns(end)
    bundle: Dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "namespace": namespace,
        "status": run.get("status", "unknown"),
        "window": {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
        "source": {},
    }
    results: Dict[str, Any] = {}

    try:
        source_metrics = _prom_range(
            client, source_prometheus, start, end, namespace
        )
        source_samples, unsupported_histograms = _prom_samples(source_metrics)
        bundle["source"]["metrics"] = source_metrics
        target_samples, _ = _prom_samples(
            _prom_range(client, target_prometheus, start, end, namespace)
        )
        source_digest = _sample_digest(source_samples)
        target_digest = _sample_digest(target_samples)
        if source_samples and (
            len(source_samples) != len(target_samples)
            or source_digest != target_digest
        ):
            target_index = {
                (tuple(sorted(item["labels"].items())), item["timestamp_ms"])
                for item in target_samples
            }
            missing = [
                item
                for item in source_samples
                if (tuple(sorted(item["labels"].items())), item["timestamp_ms"])
                not in target_index
            ]
            if missing:
                _remote_write(client, target_prometheus, missing)
                target_samples, _ = _prom_samples(
                    _prom_range(client, target_prometheus, start, end, namespace)
                )
                target_digest = _sample_digest(target_samples)
        metrics_status = _family_status(
            len(source_samples),
            len(target_samples),
            unsupported_histograms == 0
            and not source_metrics.get("warnings")
            and not source_metrics.get("infos"),
            source_digest,
            target_digest,
        )
        if unsupported_histograms and metrics_status == "complete":
            metrics_status = "partial"
        results["metrics"] = {
            "status": metrics_status,
            "source_samples": len(source_samples),
            "target_samples": len(target_samples),
            "unsupported_histograms": unsupported_histograms,
            "source_sha256": source_digest,
            "target_sha256": target_digest,
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        results["metrics"] = {"status": "partial", "error": _record_error(exc)}
        bundle["source"]["metrics_error"] = _record_error(exc)

    source_logs: List[Dict[str, Any]] = []
    source_logs_complete = False
    source_log_count = 0
    source_log_digest = _log_digest(source_logs)
    snapshot_entries: List[Dict[str, Any]] = []
    snapshot_query: Optional[str] = None
    snapshot_parity: Optional[bool] = None
    source_log_query = _namespace_log_query(namespace)
    target_log_query = _namespace_log_query(namespace, exclude_snapshots=True)
    try:
        source_logs, source_logs_complete = _loki_entries(
            client,
            source_loki,
            source_log_query,
            start_ns,
            end_ns,
            LOKI_SOURCE_LIMIT,
        )
        bundle["source"]["logs"] = source_logs
        target_logs, target_logs_complete = _loki_entries(
            client,
            target_loki,
            target_log_query,
            start_ns,
            end_ns,
            LOKI_HISTORY_LIMIT,
        )
        source_log_counter = _log_counter(source_logs)
        target_log_counter = _log_counter(target_logs)
        missing_entries = []
        remaining = target_log_counter.copy()
        for entry in source_logs:
            key = (
                entry["timestamp_ns"],
                tuple(sorted(entry["labels"].items())),
                entry["line"],
            )
            if remaining[key]:
                remaining[key] -= 1
            else:
                missing_entries.append(entry)
        if missing_entries:
            _push_loki(client, target_loki, missing_entries)
            target_logs, target_logs_complete = _loki_entries(
                client,
                target_loki,
                target_log_query,
                start_ns,
                end_ns,
                LOKI_HISTORY_LIMIT,
            )
            target_log_counter = _log_counter(target_logs)
        source_log_count = sum(source_log_counter.values())
        target_log_count = sum(target_log_counter.values())
        source_log_digest = _log_digest(source_logs)
        target_log_digest = _log_digest(target_logs)
        logs_status = _log_coverage_status(
            source_log_counter,
            target_log_counter,
            source_logs_complete,
            target_logs_complete,
        )
        missing_source_entries = sum(
            (source_log_counter - target_log_counter).values()
        )
        extra_target_entries = sum(
            (target_log_counter - source_log_counter).values()
        )
        log_snapshot = run.get("log_snapshot")
        if logs_status == "missing" and isinstance(log_snapshot, dict):
            snapshot_entries = _snapshot_log_entries(log_snapshot, run)
            if snapshot_entries:
                snapshot_query = (
                    f'{{run_id="{run_id}",capture_quality="bounded_snapshot"}}'
                )
                existing, existing_complete = _loki_entries(
                    client,
                    target_loki,
                    snapshot_query,
                    start_ns,
                    end_ns,
                    LOKI_HISTORY_LIMIT,
                )
                existing_keys = _log_counter(existing)
                to_push = []
                for entry in snapshot_entries:
                    key = (
                        entry["timestamp_ns"],
                        tuple(sorted(entry["labels"].items())),
                        entry["line"],
                    )
                    if existing_keys[key]:
                        existing_keys[key] -= 1
                    else:
                        to_push.append(entry)
                if to_push:
                    _push_loki(client, target_loki, to_push)
                imported, imported_complete = _loki_entries(
                    client,
                    target_loki,
                    snapshot_query,
                    start_ns,
                    end_ns,
                    LOKI_HISTORY_LIMIT,
                )
                snapshot_parity = (
                    len(imported) == len(snapshot_entries)
                    and existing_complete
                    and imported_complete
                    and _log_digest(imported) == _log_digest(snapshot_entries)
                )
                logs_status = "partial"
                target_log_count = len(imported)
                target_log_digest = _log_digest(imported)
                missing_source_entries = None
                extra_target_entries = None
        bundle["source"]["bounded_log_snapshot"] = log_snapshot
        bundle["source"]["snapshot_log_entries"] = snapshot_entries
        results["logs"] = {
            "status": logs_status,
            "source_entries": source_log_count,
            "target_entries": target_log_count,
            "source_sha256": source_log_digest,
            "target_sha256": target_log_digest,
            "source_complete": source_logs_complete,
            "target_complete": target_logs_complete,
            "missing_source_entries": missing_source_entries,
            "extra_target_entries": extra_target_entries,
            "bounded_snapshot_entries": len(snapshot_entries),
            "bounded_snapshot_sha256": _log_digest(snapshot_entries),
            "snapshot_parity": snapshot_parity,
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        results["logs"] = {"status": "partial", "error": _record_error(exc)}
        bundle["source"]["logs_error"] = _record_error(exc)

    try:
        source_search = _tempo_search(client, source_tempo, start, end)
        source_traces = source_search.pop("_migration_traces")
        source_search_complete = bool(source_search.pop("_migration_complete"))
        source_search_chunks = source_search.pop("_migration_search_chunks")
        source_trace_ids = sorted({_tempo_trace_id(trace) for trace in source_traces})
        bundle["source"]["trace_search"] = {
            "complete": source_search_complete,
            "trace_count": len(source_trace_ids),
            "chunks": source_search_chunks,
        }
        source_trace_payloads, source_trace_errors = _fetch_tempo_traces(
            client, source_tempo, source_trace_ids
        )
        source_span_count = 0
        source_span_counts: Dict[str, int] = {}
        for item in source_trace_payloads:
            trace_id = item["trace_id"]
            trace_spans = _trace_span_count(item["trace"])
            source_span_counts[trace_id] = trace_spans
            source_span_count += trace_spans
        bundle["source"]["traces"] = source_trace_payloads
        bundle["source"]["trace_errors"] = source_trace_errors

        target_verified_ids = set()
        preexisting_verified_ids = set()
        target_span_count = 0
        missing_traces = []
        verification_errors = []
        source_trace_by_id = {
            item["trace_id"]: item for item in source_trace_payloads
        }
        for trace_id, existing_span_count, error in _tempo_trace_span_counts(
            client,
            target_tempo,
            [item["trace_id"] for item in source_trace_payloads],
        ):
            expected_span_count = source_span_counts[trace_id]
            if error is not None:
                verification_errors.append(
                    {"trace_id": trace_id, "error": _record_error(error)}
                )
                continue
            if existing_span_count is None or existing_span_count < expected_span_count:
                missing_traces.append(source_trace_by_id[trace_id])
            elif (
                existing_span_count is not None
                and existing_span_count == expected_span_count
            ):
                target_verified_ids.add(trace_id)
                preexisting_verified_ids.add(trace_id)
                target_span_count += existing_span_count
            else:
                verification_errors.append(
                    {
                        "trace_id": trace_id,
                        "source_spans": expected_span_count,
                        "target_spans": existing_span_count,
                    }
                )

        imported_ids = set()
        span_count = 0
        ingest_errors = []
        for offset in range(0, len(missing_traces), TEMPO_INGEST_BATCH_TRACES):
            batch = missing_traces[offset : offset + TEMPO_INGEST_BATCH_TRACES]
            try:
                batch_spans = sum(
                    source_span_counts[item["trace_id"]] for item in batch
                )
                _tempo_ingest_batch(
                    client,
                    target_tempo_otlp,
                    [item["trace"] for item in batch],
                )
            except TempoPartialSuccessError as exc:
                ingest_errors.append(
                    {
                        "trace_ids": [item["trace_id"] for item in batch],
                        "error": _record_error(exc),
                    }
                )
            except (httpx.HTTPError, ValueError):
                for item in batch:
                    trace_id = item["trace_id"]
                    try:
                        _tempo_ingest(client, target_tempo_otlp, item["trace"])
                        span_count += source_span_counts[trace_id]
                        imported_ids.add(trace_id)
                    except (httpx.HTTPError, ValueError) as item_exc:
                        ingest_errors.append(
                            {
                                "trace_id": trace_id,
                                "error": _record_error(item_exc),
                            }
                        )
            else:
                span_count += batch_spans
                imported_ids.update(item["trace_id"] for item in batch)
        for trace_id, verified_span_count, error in _tempo_trace_span_counts(
            client,
            target_tempo,
            [item["trace_id"] for item in missing_traces],
        ):
            if error is not None:
                verification_errors.append(
                    {"trace_id": trace_id, "error": _record_error(error)}
                )
                continue
            expected_span_count = source_span_counts[trace_id]
            if (
                verified_span_count is not None
                and verified_span_count == expected_span_count
            ):
                target_verified_ids.add(trace_id)
                target_span_count += verified_span_count
            else:
                verification_errors.append(
                    {
                        "trace_id": trace_id,
                        "source_spans": expected_span_count,
                        "target_spans": verified_span_count,
                    }
                )

        if imported_ids:
            time.sleep(2)
        target_search = _tempo_search(client, target_tempo, start, end)
        target_search_traces, target_search_complete = _trace_ids(target_search)
        target_search_chunks = target_search.pop("_migration_search_chunks")
        target_search_ids = {_tempo_trace_id(trace) for trace in target_search_traces}
        bundle["target_trace_search"] = {
            "complete": target_search_complete,
            "trace_count": len(target_search_ids),
            "chunks": target_search_chunks,
        }
        source_trace_count = len(source_trace_ids)
        target_trace_count = len(target_verified_ids)
        source_trace_digest = hashlib.sha256(
            "\n".join(source_trace_ids).encode("utf-8")
        ).hexdigest()
        target_trace_digest = hashlib.sha256(
            "\n".join(sorted(target_verified_ids)).encode("utf-8")
        ).hexdigest()
        target_search_digest = hashlib.sha256(
            "\n".join(sorted(target_search_ids)).encode("utf-8")
        ).hexdigest()
        data_status = _family_status(
            source_trace_count,
            target_trace_count,
            source_search_complete
            and not source_trace_errors
            and not ingest_errors
            and not verification_errors
            and source_span_count == target_span_count,
            source_trace_digest,
            target_trace_digest,
        )
        search_status = _family_status(
            source_trace_count,
            len(target_search_ids),
            source_search_complete and target_search_complete,
            source_trace_digest,
            target_search_digest,
        )
        traces_status = (
            "missing"
            if data_status == "missing"
            else "complete"
            if data_status == "complete" and search_status == "complete"
            else "partial"
        )
        verified_imported_spans = sum(
            source_span_counts[trace_id]
            for trace_id in target_verified_ids - preexisting_verified_ids
        )
        results["traces"] = {
            "status": traces_status,
            "data_status": data_status,
            "search_index_status": search_status,
            "source_traces": source_trace_count,
            "target_traces": target_trace_count,
            "imported_spans": verified_imported_spans,
            "otlp_accepted_spans": span_count,
            "source_spans": source_span_count,
            "target_spans": target_span_count,
            "target_search_traces": len(target_search_ids),
            "source_sha256": source_trace_digest,
            "target_sha256": target_trace_digest,
            "source_search_complete": source_search_complete,
            "target_search_complete": target_search_complete,
            "source_fetch_errors": source_trace_errors,
            "target_ingest_errors": ingest_errors,
            "target_verification_errors": verification_errors,
        }
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        results["traces"] = {"status": "partial", "error": _record_error(exc)}
        bundle["source"]["traces_error"] = _record_error(exc)

    logs_result = results.get("logs")
    if isinstance(logs_result, dict) and logs_result.get("status") == "partial":
        try:
            if snapshot_entries and snapshot_query:
                final_entries, final_complete, snapshot_parity = (
                    _loki_entries_until_match(
                        client,
                        target_loki,
                        snapshot_query,
                        start_ns,
                        end_ns,
                        LOKI_HISTORY_LIMIT,
                        snapshot_entries,
                    )
                )
                target_log_count = len(final_entries)
                target_log_digest = _log_digest(final_entries)
                logs_result.update(
                    {
                        "target_entries": target_log_count,
                        "target_sha256": target_log_digest,
                        "target_complete": final_complete,
                        "snapshot_parity": snapshot_parity,
                    }
                )
            elif source_log_count:
                final_entries, final_complete, _ = _loki_entries_until_match(
                    client,
                    target_loki,
                    target_log_query,
                    start_ns,
                    end_ns,
                    LOKI_HISTORY_LIMIT,
                    source_logs,
                )
                target_log_count = len(final_entries)
                target_log_digest = _log_digest(final_entries)
                final_log_counter = _log_counter(final_entries)
                logs_result.update(
                    {
                        "target_entries": target_log_count,
                        "target_sha256": target_log_digest,
                        "target_complete": final_complete,
                        "missing_source_entries": sum(
                            (source_log_counter - final_log_counter).values()
                        ),
                        "extra_target_entries": sum(
                            (final_log_counter - source_log_counter).values()
                        ),
                        "status": _log_coverage_status(
                            source_log_counter,
                            final_log_counter,
                            source_logs_complete,
                            final_complete,
                        ),
                    }
                )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logs_result["verification_error"] = _record_error(exc)
            logs_result["status"] = "partial"

    statuses = [result.get("status") for result in results.values()]
    if statuses and all(status == "complete" for status in statuses):
        overall = "complete"
    elif any(status in {"complete", "partial"} for status in statuses):
        overall = "partial"
    else:
        overall = "missing"
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "namespace": namespace,
        "run_status": run.get("status", "unknown"),
        "window": bundle["window"],
        "overall_status": overall,
        "telemetry": results,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return _save_run_archive(object_client, bucket, prefix, bundle, manifest)


def run_migration(input_path: Path) -> Dict[str, Any]:
    raw = input_path.read_bytes()
    payload_bytes = gzip.decompress(raw) if input_path.suffix == ".gz" else raw
    payload = json.loads(payload_bytes)
    campaign_id = str(payload.get("campaign_id") or "campaign")
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", campaign_id):
        raise ValueError("campaign_id contains characters unsafe for object keys")
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("migration input must contain at least one run")

    settings = {
        "source_prometheus": os.environ["SOURCE_PROMETHEUS_URL"],
        "source_loki": os.environ["SOURCE_LOKI_URL"],
        "source_tempo": os.environ["SOURCE_TEMPO_URL"],
        "target_prometheus": os.environ["TARGET_PROMETHEUS_URL"],
        "target_loki": os.environ["TARGET_LOKI_URL"],
        "target_tempo": os.environ["TARGET_TEMPO_URL"],
        "target_tempo_otlp": os.environ["TARGET_TEMPO_OTLP_URL"],
        "bucket": os.getenv("HISTORY_S3_BUCKET", "history-runs"),
    }
    object_client = _s3_client()
    results = []
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError("each migration run entry must be an object")
            results.append(
                migrate_run(
                    client,
                    object_client,
                    settings["bucket"],
                    settings["source_prometheus"],
                    settings["source_loki"],
                    settings["source_tempo"],
                    settings["target_prometheus"],
                    settings["target_loki"],
                    settings["target_tempo"],
                    settings["target_tempo_otlp"],
                    campaign_id,
                    run,
                )
            )
    return {
        "campaign_id": campaign_id,
        "run_count": len(results),
        "status_counts": dict(Counter(item["overall_status"] for item in results)),
        "runs": [
            {
                "run_id": item["run_id"],
                "status": item["overall_status"],
                "telemetry": {
                    family: result["status"]
                    for family, result in item["telemetry"].items()
                },
            }
            for item in results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    summary = run_migration(args.input)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, httpx.HTTPError, KeyError) as exc:
        print(f"telemetry history migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
