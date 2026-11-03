"""Read-only, run-scoped trace evidence from the S3 history archive."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import ijson
from minio.error import S3Error
from urllib3.exceptions import HTTPError as UrllibHTTPError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_TRACE_ID = re.compile(r"^[A-Za-z0-9+/=_-]{1,128}$")
_MANIFEST_LIMIT = 1024 * 1024
_SPOOL_MEMORY_LIMIT = 16 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class HistoryArchiveError(RuntimeError):
    """A safe, user-facing archive lookup or validation error."""


class HistoryArchiveNotFound(HistoryArchiveError):
    """A requested archive object has not been written yet."""


class HistoryArchiveReader:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str = "history-runs",
        secure: bool = False,
        namespace: str = "monitoring-history",
        secret_name: str = "seaweedfs-history-credentials",
        kubectl: str = "kubectl",
        access_key: str | None = None,
        secret_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.secure = secure
        self.namespace = namespace
        self.secret_name = secret_name
        self.kubectl = kubectl
        self.access_key = access_key
        self.secret_key = secret_key
        self.client = client

    @classmethod
    def from_environment(cls) -> HistoryArchiveReader | None:
        enabled = os.getenv("HISTORY_ARCHIVE_ENABLED", "false").lower()
        if enabled not in {"true", "false", "1", "0"}:
            raise ValueError("HISTORY_ARCHIVE_ENABLED must be true, false, 1, or 0")
        if enabled not in {"true", "1"}:
            return None

        endpoint = os.getenv("HISTORY_ARCHIVE_ENDPOINT", "").strip()
        if not endpoint or "://" in endpoint:
            raise ValueError(
                "HISTORY_ARCHIVE_ENDPOINT must be a host:port reachable by the platform"
            )
        secure_value = os.getenv("HISTORY_ARCHIVE_SECURE", "false").lower()
        if secure_value not in {"true", "false", "1", "0"}:
            raise ValueError("HISTORY_ARCHIVE_SECURE must be true, false, 1, or 0")
        return cls(
            endpoint=endpoint,
            bucket=os.getenv("HISTORY_ARCHIVE_BUCKET", "history-runs"),
            secure=secure_value in {"true", "1"},
            namespace=os.getenv("HISTORY_ARCHIVE_NAMESPACE", "monitoring-history"),
            secret_name=os.getenv(
                "HISTORY_ARCHIVE_SECRET_NAME", "seaweedfs-history-credentials"
            ),
            kubectl=os.getenv("KUBECTL_BIN", "kubectl"),
            access_key=os.getenv("HISTORY_ARCHIVE_ACCESS_KEY"),
            secret_key=os.getenv("HISTORY_ARCHIVE_SECRET_KEY"),
        )

    def read_trace_evidence(
        self,
        *,
        campaign_id: str,
        run_id: str,
        phases: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        _validate_identifier(campaign_id, "campaign_id")
        _validate_identifier(run_id, "run_id")
        phase_bounds = _phase_bounds(phases)
        prefix = f"{campaign_id}/{run_id}"
        manifest = self.read_manifest(campaign_id=campaign_id, run_id=run_id)
        bundle_key = f"{prefix}/source-bundle.json.gz"
        expected_hash = manifest.get("bundle_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(
            r"[a-f0-9]{64}", expected_hash
        ):
            raise HistoryArchiveError("Archive manifest has no valid bundle checksum")
        window_coverage = _archive_window_coverage(
            manifest.get("window"), phase_bounds
        )

        with self._download_bundle(bundle_key, expected_hash) as bundle_stream:
            try:
                with gzip.GzipFile(fileobj=bundle_stream, mode="rb") as decoded:
                    summary = _summarize_trace_stream(
                        ijson.items(decoded, "source.traces.item"),
                        phase_bounds,
                    )
            except (OSError, EOFError, ValueError, ijson.JSONError, zlib.error) as exc:
                raise HistoryArchiveError(
                    "Archived source bundle is not a readable gzip JSON document"
                ) from exc

        telemetry_all = manifest.get("telemetry")
        telemetry = (
            telemetry_all.get("traces") or {}
            if isinstance(telemetry_all, dict)
            else {}
        )
        source_errors = telemetry.get("source_fetch_errors") or []
        source_error_count = (
            len(source_errors)
            if isinstance(source_errors, list)
            else int(source_errors)
            if (
                isinstance(source_errors, int)
                and not isinstance(source_errors, bool)
                and source_errors >= 0
            )
            else None
        )
        source_search_complete = telemetry.get("source_search_complete") is True
        expected_trace_count = _nonnegative_int(telemetry.get("source_traces"))
        expected_span_count = _nonnegative_int(telemetry.get("source_spans"))
        trace_count_parity = (
            expected_trace_count is not None
            and expected_trace_count == summary["trace_count"]
        )
        span_count_parity = (
            expected_span_count is not None
            and expected_span_count == summary["archive_span_count"]
        )
        trace_data_complete = (
            source_search_complete
            and source_error_count == 0
            and summary["invalid_span_count"] == 0
            and summary["unidentified_span_count"] == 0
            and trace_count_parity
            and span_count_parity
            and all(item["covered"] for item in window_coverage.values())
        )
        summary_status = (
            "no_data"
            if summary["span_count"] == 0 and trace_data_complete
            else "partial"
            if summary["span_count"] == 0 or not trace_data_complete
            else "ok"
        )
        summary.update(
            {
                "status": summary_status,
                "evidence_source": "archive",
                "campaign_id": campaign_id,
                "run_id": run_id,
                "archive_key": bundle_key,
                "bundle_sha256": expected_hash,
                "source_search_complete": source_search_complete,
                "source_fetch_error_count": source_error_count,
                "manifest_trace_status": telemetry.get("status"),
                "search_index_status": telemetry.get("search_index_status"),
                "window_coverage": window_coverage,
                "manifest_overall_status": manifest.get("overall_status"),
                "source_trace_count_expected": expected_trace_count,
                "source_span_count_expected": expected_span_count,
                "trace_count_parity": trace_count_parity,
                "span_count_parity": span_count_parity,
            }
        )
        return summary

    def read_manifest(self, *, campaign_id: str, run_id: str) -> dict[str, Any]:
        _validate_identifier(campaign_id, "campaign_id")
        _validate_identifier(run_id, "run_id")
        manifest = self._read_manifest(f"{campaign_id}/{run_id}/manifest.json")
        if (
            manifest.get("campaign_id") != campaign_id
            or manifest.get("run_id") != run_id
        ):
            raise HistoryArchiveError(
                "Archive manifest does not match the requested run"
            )
        return manifest

    def _read_manifest(self, key: str) -> dict[str, Any]:
        response = self._get_object(key)
        try:
            payload = response.read(_MANIFEST_LIMIT + 1)
            if len(payload) > _MANIFEST_LIMIT or response.read(1):
                raise HistoryArchiveError("Archive manifest exceeds the size limit")
        except (OSError, UrllibHTTPError, TimeoutError) as exc:
            raise HistoryArchiveError("Could not read the archive manifest") from exc
        finally:
            _close_response(response)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HistoryArchiveError("Archive manifest is not valid JSON") from exc
        if not isinstance(value, dict):
            raise HistoryArchiveError("Archive manifest is not a JSON object")
        return value

    def _download_bundle(self, key: str, expected_hash: str):
        response = self._get_object(key)
        stream = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_LIMIT, mode="w+b")
        digest = hashlib.sha256()
        try:
            while chunk := response.read(_CHUNK_SIZE):
                digest.update(chunk)
                stream.write(chunk)
        except (OSError, UrllibHTTPError, TimeoutError) as exc:
            stream.close()
            raise HistoryArchiveError("Could not read the archived source bundle") from exc
        finally:
            _close_response(response)
        if digest.hexdigest() != expected_hash:
            stream.close()
            raise HistoryArchiveError("Archived source bundle failed SHA-256 verification")
        stream.seek(0)
        return stream

    def _get_object(self, key: str):
        try:
            return self._get_client().get_object(self.bucket, key)
        except S3Error as exc:
            code = exc.code
            if code in {"NoSuchKey", "NoSuchObject"}:
                raise HistoryArchiveNotFound(
                    "Requested archive object has not been written"
                ) from exc
            raise HistoryArchiveError("S3 archive request failed") from exc
        except (UrllibHTTPError, OSError, TimeoutError) as exc:
            raise HistoryArchiveError("S3 archive request failed") from exc

    def _get_client(self):
        if self.client is not None:
            return self.client
        access_key, secret_key = self._credentials()
        try:
            from minio import Minio
        except ImportError as exc:
            raise HistoryArchiveError(
                "The MinIO client is not installed in the platform environment"
            ) from exc
        try:
            self.client = Minio(
                self.endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=self.secure,
            )
        except ValueError as exc:
            raise HistoryArchiveError("Invalid history archive endpoint") from exc
        return self.client

    def _credentials(self) -> tuple[str, str]:
        if self.access_key and self.secret_key:
            return self.access_key, self.secret_key
        try:
            result = subprocess.run(
                [
                    self.kubectl,
                    "get",
                    "secret",
                    self.secret_name,
                    "--namespace",
                    self.namespace,
                    "-o",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            secret = json.loads(result.stdout)
            values = secret["data"]
            access_key = base64.b64decode(
                values["history-access-key"], validate=True
            ).decode()
            secret_key = base64.b64decode(
                values["history-secret-key"], validate=True
            ).decode()
        except (
            OSError,
            subprocess.SubprocessError,
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
        ) as exc:
            raise HistoryArchiveError(
                "Could not read the history S3 service credential from Kubernetes"
            ) from exc
        if not access_key or not secret_key:
            raise HistoryArchiveError("History S3 service credential is empty")
        return access_key, secret_key


def _validate_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not _IDENTIFIER.fullmatch(value)
    ):
        raise HistoryArchiveError(
            f"{name} contains characters unsafe for an archive key"
        )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HistoryArchiveError("RCA phase has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise HistoryArchiveError("RCA phase timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _epoch_ns(value: datetime) -> int:
    delta = value - _EPOCH
    return (
        (delta.days * 86400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1000
    )


def _phase_bounds(
    phases: dict[str, dict[str, str]],
) -> dict[str, tuple[int, int]]:
    result = {}
    for name in ("baseline", "fault", "post"):
        phase = phases.get(name)
        if phase is None:
            continue
        if not isinstance(phase, dict):
            raise HistoryArchiveError(f"RCA phase {name} has an invalid shape")
        start = _parse_time(phase.get("start"))
        end = _parse_time(phase.get("end"))
        if start >= end:
            raise HistoryArchiveError(f"RCA phase {name} has unordered timestamps")
        result[name] = (_epoch_ns(start), _epoch_ns(end))
    if not {"baseline", "fault"}.issubset(result):
        raise HistoryArchiveError(
            "Archive trace reading requires baseline and fault phases"
        )
    return result


def _archive_window_coverage(
    window: Any,
    phases: dict[str, tuple[int, int]],
) -> dict[str, dict[str, bool]]:
    if not isinstance(window, dict):
        return {phase: {"covered": False} for phase in phases}
    try:
        archive_start = _epoch_ns(_parse_time(window.get("start")))
        archive_end = _epoch_ns(_parse_time(window.get("end")))
    except HistoryArchiveError:
        return {phase: {"covered": False} for phase in phases}
    return {
        phase: {
            "covered": start >= archive_start and end <= archive_end,
        }
        for phase, (start, end) in phases.items()
    }


def _attribute_map(attributes: Any) -> dict[str, Any]:
    if isinstance(attributes, dict):
        return attributes
    result = {}
    if not isinstance(attributes, list):
        return result
    for item in attributes:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        value = item.get("value")
        if isinstance(value, dict):
            value = next(
                (
                    value[key]
                    for key in (
                        "stringValue",
                        "intValue",
                        "doubleValue",
                        "boolValue",
                    )
                    if key in value
                ),
                None,
            )
        result[item["key"]] = value
    return result


def _resource_scopes(
    payload: dict[str, Any],
) -> Iterable[tuple[dict[str, Any], list[Any]]]:
    resource_spans = payload.get("resourceSpans")
    if isinstance(resource_spans, list):
        for resource_span in resource_spans:
            if isinstance(resource_span, dict):
                resource = resource_span.get("resource")
                yield (
                    resource if isinstance(resource, dict) else {},
                    resource_span.get("scopeSpans") or [],
                )
        return
    batches = payload.get("batches")
    if isinstance(batches, list):
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            scopes = batch.get("scopeSpans")
            if not isinstance(scopes, list):
                spans = batch.get("spans")
                scopes = [{"spans": spans if isinstance(spans, list) else []}]
            resource = batch.get("resource")
            yield (resource if isinstance(resource, dict) else {}, scopes)
        return
    nested = payload.get("trace")
    if isinstance(nested, dict):
        yield from _resource_scopes(nested)


def _safe_label(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    return "".join(char for char in value if char.isprintable()).strip()[:160] or default


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _safe_span_identifier(value: Any) -> str | None:
    if isinstance(value, str) and _TRACE_ID.fullmatch(value):
        return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _span_records(trace_item: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(trace_item, dict):
        yield {"_invalid": True}
        return
    trace_id = trace_item.get("trace_id")
    trace = trace_item.get("trace")
    if not isinstance(trace, dict):
        yield {"_invalid": True}
        return
    trace_id = (
        trace_id
        if isinstance(trace_id, str) and _TRACE_ID.fullmatch(trace_id)
        else None
    )
    seen_span = False
    for resource, scopes in _resource_scopes(trace):
        resource_attributes = _attribute_map(resource.get("attributes"))
        service = _safe_label(
            resource_attributes.get("service.name")
            or resource.get("serviceName")
            or resource.get("service.name"),
            "unknown",
        )
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            spans = scope.get("spans")
            if not isinstance(spans, list):
                seen_span = True
                yield {"_invalid": True}
                continue
            for span in spans:
                if not isinstance(span, dict):
                    seen_span = True
                    yield {"_invalid": True}
                    continue
                seen_span = True
                start_ns = _integer(span.get("startTimeUnixNano"))
                end_ns = _integer(span.get("endTimeUnixNano"))
                if start_ns is None or end_ns is None or end_ns < start_ns:
                    yield {"_invalid": True}
                    continue
                attributes = _attribute_map(span.get("attributes"))
                status = span.get("status") or {}
                status_code = (
                    status.get("code")
                    if isinstance(status, dict)
                    else span.get("statusCode") or status
                )
                http_status = _integer(
                    attributes.get("http.response.status_code")
                    or attributes.get("http.status_code")
                )
                status_text = str(status_code).upper()
                is_error = status_text in {"2", "ERROR", "STATUS_CODE_ERROR"}
                if http_status is not None and http_status >= 500:
                    is_error = True
                yield {
                    "trace_id": trace_id,
                    "span_id": _safe_span_identifier(span.get("spanId")),
                    "parent_span_id": _safe_span_identifier(
                        span.get("parentSpanId")
                    ),
                    "service": _safe_label(
                        attributes.get("service.name"), service
                    ),
                    "operation": _safe_label(span.get("name"), "unknown"),
                    "kind": (
                        span.get("kind")
                        if isinstance(span.get("kind"), (str, int))
                        else None
                    ),
                    "start_time_unix_nano": start_ns,
                    "end_time_unix_nano": end_ns,
                    "duration_ms": (end_ns - start_ns) / 1_000_000,
                    "http_status_code": http_status,
                    "status_code": status_code,
                    "is_error": is_error,
                }
    if not seen_span:
        yield {"_invalid": True}


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _trace_start_iso(timestamp_ns: int) -> str:
    timestamp = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, timezone.utc)
    return timestamp.isoformat().replace("+00:00", "Z")


def _summarize_trace_stream(
    traces: Iterable[Any],
    phase_bounds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    phase_counts = {
        phase: {
            "trace_count": 0,
            "span_count": 0,
            "error_span_count": 0,
            "service_span_counts": Counter(),
        }
        for phase in phase_bounds
    }
    baseline_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    fault_spans: list[dict[str, Any]] = []
    total_trace_count = 0
    total_span_count = 0
    archive_span_count = 0
    invalid_span_count = 0
    unidentified_span_count = 0

    for trace_item in traces:
        total_trace_count += 1
        trace_phases: set[str] = set()
        for span in _span_records(trace_item):
            if span.get("_invalid"):
                invalid_span_count += 1
                continue
            if span.get("trace_id") is None:
                unidentified_span_count += 1
            archive_span_count += 1
            start_ns = span["start_time_unix_nano"]
            end_ns = span["end_time_unix_nano"]
            matched = [
                phase
                for phase, (phase_start, phase_end) in phase_bounds.items()
                if start_ns < phase_end and end_ns >= phase_start
            ]
            if not matched:
                continue
            total_span_count += 1
            for phase in matched:
                trace_phases.add(phase)
                counts = phase_counts[phase]
                counts["span_count"] += 1
                counts["service_span_counts"][span["service"]] += 1
                if span["is_error"]:
                    counts["error_span_count"] += 1
            duration_key = (span["service"], span["operation"])
            if "baseline" in matched:
                baseline_durations[duration_key].append(span["duration_ms"])
            if "fault" in matched:
                fault_spans.append(span)
        for phase in trace_phases:
            phase_counts[phase]["trace_count"] += 1

    baseline_p95 = {
        key: _percentile_95(values)
        for key, values in baseline_durations.items()
        if values
    }
    fault_errors = [span for span in fault_spans if span["is_error"]]
    latency_candidates = []
    for span in fault_spans:
        p95 = baseline_p95.get((span["service"], span["operation"]))
        if p95 is None or span["duration_ms"] <= p95:
            continue
        latency_candidates.append(
            {
                **span,
                "baseline_p95_ms": round(p95, 3),
                "latency_delta_ms": round(span["duration_ms"] - p95, 3),
                "baseline_sample_count": len(
                    baseline_durations[(span["service"], span["operation"])]
                ),
            }
        )
    selected_spans = fault_errors
    if not selected_spans:
        selected_spans = sorted(
            latency_candidates,
            key=lambda item: item["latency_delta_ms"],
            reverse=True,
        )
    else:
        selected_spans = sorted(
            selected_spans,
            key=lambda item: item["duration_ms"],
            reverse=True,
        )

    trace_details = []
    selected_ids = set()
    for span in selected_spans:
        trace_id = span.get("trace_id")
        if not trace_id or trace_id in selected_ids:
            continue
        selected_ids.add(trace_id)
        trace_details.append(
            {
                "trace_id": trace_id,
                "phase": "fault",
                "service": span["service"],
                "operation": span["operation"],
                "start_time": _trace_start_iso(span["start_time_unix_nano"]),
                "start_time_unix_nano": str(span["start_time_unix_nano"]),
                "duration_ms": round(span["duration_ms"], 3),
                "http_status_code": span["http_status_code"],
                "status_code": span["status_code"],
                "is_error": span["is_error"],
                "baseline_p95_ms": span.get("baseline_p95_ms"),
                "latency_delta_ms": span.get("latency_delta_ms"),
                "baseline_sample_count": span.get("baseline_sample_count"),
                "_selected_span": span,
            }
        )
        if len(trace_details) == 2:
            break

    for detail in trace_details:
        trace_spans = [
            item
            for item in fault_spans
            if item.get("trace_id") == detail["trace_id"]
        ]
        trace_spans.sort(key=lambda item: item["start_time_unix_nano"])
        selected_span = detail.pop("_selected_span")
        selected_identity = (
            selected_span["start_time_unix_nano"],
            selected_span.get("span_id"),
            selected_span["service"],
            selected_span["operation"],
        )
        visible_spans = trace_spans[:32]
        if not any(
            (
                item["start_time_unix_nano"],
                item.get("span_id"),
                item["service"],
                item["operation"],
            )
            == selected_identity
            for item in visible_spans
        ):
            visible_spans = visible_spans[:31] + [selected_span]
            visible_spans.sort(key=lambda item: item["start_time_unix_nano"])
        detail["spans"] = [
            {
                "span_id": item["span_id"],
                "parent_span_id": item["parent_span_id"],
                "service": item["service"],
                "operation": item["operation"],
                "kind": item["kind"],
                "start_time": _trace_start_iso(item["start_time_unix_nano"]),
                "start_time_unix_nano": str(item["start_time_unix_nano"]),
                "end_time_unix_nano": str(item["end_time_unix_nano"]),
                "duration_ms": round(item["duration_ms"], 3),
                "http_status_code": item["http_status_code"],
                "status_code": item["status_code"],
                "is_error": item["is_error"],
            }
            for item in visible_spans
        ]
        spans_by_id = {
            item["span_id"]: item for item in trace_spans if item.get("span_id")
        }
        edges = []
        seen_edges = set()
        for item in trace_spans:
            parent = spans_by_id.get(item.get("parent_span_id"))
            if not parent or parent["service"] == item["service"]:
                continue
            edge = (parent["service"], item["service"], item["operation"])
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            edges.append(
                {
                    "from_service": edge[0],
                    "to_service": edge[1],
                    "operation": edge[2],
                    "duration_ms": round(item["duration_ms"], 3),
                    "is_error": item["is_error"],
                }
            )
            if len(edges) == 20:
                break
        detail["dependency_edges"] = edges
        detail["omitted_span_count"] = max(
            0, len(trace_spans) - len(detail["spans"])
        )

    return {
        "trace_count": total_trace_count,
        "span_count": total_span_count,
        "archive_span_count": archive_span_count,
        "invalid_span_count": invalid_span_count,
        "unidentified_span_count": unidentified_span_count,
        "phases": {
            phase: {
                "trace_count": counts["trace_count"],
                "span_count": counts["span_count"],
                "error_span_count": counts["error_span_count"],
                "service_span_counts": dict(
                    sorted(counts["service_span_counts"].items())
                ),
            }
            for phase, counts in phase_counts.items()
        },
        "trace_details": trace_details,
    }


def _close_response(response: Any) -> None:
    response.close()
    response.release_conn()
