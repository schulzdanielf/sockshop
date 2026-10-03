"""Bounded, phase-aware log evidence for run RCA."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from trace_analyzer import _classify_failure

_SERVICE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"),
    re.compile(
        r"(?i)(\b[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|credential)[\w.-]*\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
    ),
    re.compile(r"(?i)\b(cookie|set-cookie)(\s*:\s*)[^\r\n]+"),
)
_LOGS_PER_PHASE = 300
_MAX_GROUPS_PER_PHASE = 8
_MAX_EXAMPLES_PER_GROUP = 3
_MAX_LINE_CHARS = 1200


def _parse_time(value: str) -> Tuple[datetime, str]:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    unix_ns = calendar.timegm(utc.utctimetuple()) * 1_000_000_000 + utc.microsecond * 1000
    return utc, str(unix_ns)


def _sanitize_line(line: str) -> str:
    sanitized = line
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            sanitized = pattern.sub(r"\1\2[REDACTED]", sanitized)
        else:
            sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized[:_MAX_LINE_CHARS]


def _line_details(raw_line: Any) -> Tuple[str, Optional[str]]:
    if not isinstance(raw_line, str):
        raw_line = str(raw_line)
    try:
        record = json.loads(raw_line)
    except (TypeError, json.JSONDecodeError):
        record = None
    if isinstance(record, dict):
        message = next(
            (
                record[key]
                for key in ("message", "msg", "log", "body")
                if isinstance(record.get(key), str) and record[key]
            ),
            raw_line,
        )
        trace_id = next(
            (
                str(record[key])
                for key in ("trace_id", "traceID", "traceId")
                if record.get(key)
            ),
            None,
        )
        return _sanitize_line(message), trace_id
    return _sanitize_line(raw_line), None


def _iso_time(timestamp_ns: str) -> Optional[str]:
    try:
        value = int(timestamp_ns)
        seconds, remainder = divmod(value, 1_000_000_000)
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder // 1000
        )
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _signature(message: str) -> Optional[str]:
    signature = _classify_failure(message)
    if signature != "UNKNOWN_ERROR":
        return signature
    if re.search(r"\b(error|warn(?:ing)?|fatal|panic|exception|unhealthy|retry|timeout)\b", message, re.I):
        return "UNCLASSIFIED_WARNING_OR_ERROR"
    return None


def _phase_result(
    loki,
    *,
    query: str,
    phase: str,
    start: str,
    end: str,
    limit: int,
) -> Dict[str, Any]:
    raw = loki.query_range(
        query,
        start=start,
        end=end,
        limit=limit,
        direction="forward",
    )
    if not isinstance(raw, dict) or raw.get("status") == "error":
        return {
            "status": "tool_error",
            "error": raw.get("error") if isinstance(raw, dict) else "invalid response",
            "coverage": {"stream_count": 0, "lines_returned": 0, "truncated": False},
            "signatures": [],
            "sample_lines": [],
            "window": {"start": start, "end": end},
        }

    streams = (raw.get("data") or {}).get("result") or []
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    sample_lines: List[Dict[str, Any]] = []
    line_count = 0
    invalid_timestamps = 0
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") or {}
        pod = str(labels.get("pod") or "unknown")
        container = str(labels.get("container") or "unknown")
        for item in stream.get("values") or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            line_count += 1
            timestamp = _iso_time(item[0])
            if timestamp is None:
                invalid_timestamps += 1
                continue
            message, trace_id = _line_details(item[1])
            signature = _signature(message)
            if signature is None:
                if len(sample_lines) < 3:
                    sample_lines.append(
                        {
                            "timestamp": timestamp,
                            "pod": pod,
                            "container": container,
                            "message": message,
                        }
                    )
                continue
            key = (signature, pod, container)
            group = grouped.setdefault(
                key,
                {
                    "signature": signature,
                    "pod": pod,
                    "container": container,
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "count_observed": 0,
                    "examples": [],
                },
            )
            group["count_observed"] += 1
            group["last_seen"] = timestamp
            if trace_id:
                group["trace_id"] = trace_id[:128]
            if len(group["examples"]) < _MAX_EXAMPLES_PER_GROUP:
                evidence_seed = f"{phase}\0{pod}\0{container}\0{timestamp}\0{message}"
                group["examples"].append(
                    {
                        "evidence_id": hashlib.sha256(evidence_seed.encode()).hexdigest()[:16],
                        "timestamp": timestamp,
                        "message": message,
                    }
                )

    signatures = sorted(
        grouped.values(), key=lambda group: (-group["count_observed"], group["first_seen"])
    )
    return {
        "status": "ok" if streams else "no_data",
        "coverage": {
            "stream_count": len(streams),
            "lines_returned": line_count,
            "truncated": line_count >= limit,
            "invalid_timestamp_count": invalid_timestamps,
        },
        "signatures": signatures[:_MAX_GROUPS_PER_PHASE],
        "omitted_signature_group_count": max(0, len(signatures) - _MAX_GROUPS_PER_PHASE),
        "sample_lines": sample_lines,
        "window": {"start": start, "end": end},
    }


def check_service_logs(
    loki,
    *,
    service: str,
    namespace: str,
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    post_start: str = "",
    post_end: str = "",
    limit_per_phase: int = _LOGS_PER_PHASE,
) -> Dict[str, Any]:
    """Collect bounded, redacted service logs across run phases."""
    if not _SERVICE_RE.fullmatch(service or "") or not _NAMESPACE_RE.fullmatch(namespace or ""):
        return {"status": "invalid_input", "error": "invalid service or namespace"}
    if bool(post_start) != bool(post_end):
        return {"status": "invalid_input", "error": "post phase requires both bounds"}
    if (
        isinstance(limit_per_phase, bool)
        or not isinstance(limit_per_phase, int)
        or not 1 <= limit_per_phase <= _LOGS_PER_PHASE
    ):
        return {"status": "invalid_input", "error": "invalid per-phase line limit"}

    phase_bounds = {
        "baseline": (baseline_start, baseline_end),
        "fault": (fault_start, fault_end),
    }
    if post_start and post_end:
        phase_bounds["post"] = (post_start, post_end)
    normalized_bounds: Dict[str, Tuple[str, str]] = {}
    try:
        for phase, (start, end) in phase_bounds.items():
            start_dt, start_ns = _parse_time(start)
            end_dt, end_ns = _parse_time(end)
            if start_dt >= end_dt or (end_dt - start_dt).total_seconds() > 1800:
                raise ValueError(f"invalid {phase} window")
            normalized_bounds[phase] = (start_ns, end_ns)
    except (TypeError, ValueError, AttributeError) as exc:
        return {"status": "invalid_input", "error": str(exc)}

    escaped_service = re.escape(service).replace(r"\-", "-")
    query = (
        f'{{namespace="{namespace}",pod=~"^{escaped_service}(-[a-z0-9]+)*$",container!=""}}'
    )
    phases = {
        phase: _phase_result(
            loki,
            query=query,
            phase=phase,
            start=phase_bounds[phase][0],
            end=phase_bounds[phase][1],
            limit=limit_per_phase,
        )
        for phase in phase_bounds
    }
    statuses = [phase["status"] for phase in phases.values()]
    if all(status == "ok" for status in statuses):
        status = "ok"
    elif any(status == "ok" for status in statuses):
        status = "partial"
    elif any(status == "tool_error" for status in statuses):
        status = "tool_error"
    else:
        status = "no_data"
    return {
        "status": status,
        "service": service,
        "namespace": namespace,
        "query": query,
        "limit_per_phase": limit_per_phase,
        "phases": phases,
    }


def capture_run_log_snapshot(
    loki,
    *,
    services: List[str],
    namespace: str,
    baseline_start: str,
    baseline_end: str,
    fault_start: str,
    fault_end: str,
    post_start: str = "",
    post_end: str = "",
    max_total_lines: int = 1200,
) -> Dict[str, Any]:
    """Capture logs for all supplied services under one global line budget."""
    if (
        not isinstance(services, list)
        or not services
        or len(services) > 64
        or any(
            not isinstance(service, str) or not _SERVICE_RE.fullmatch(service)
            for service in services
        )
    ):
        return {"status": "invalid_input", "error": "invalid service list"}
    if not _NAMESPACE_RE.fullmatch(namespace or ""):
        return {"status": "invalid_input", "error": "invalid namespace"}
    if bool(post_start) != bool(post_end):
        return {"status": "invalid_input", "error": "post phase requires both bounds"}
    if (
        isinstance(max_total_lines, bool)
        or not isinstance(max_total_lines, int)
        or not 1 <= max_total_lines <= 1200
    ):
        return {"status": "invalid_input", "error": "invalid global line budget"}

    windows_to_validate = [
        (baseline_start, baseline_end),
        (fault_start, fault_end),
    ]
    if post_start and post_end:
        windows_to_validate.append((post_start, post_end))
    try:
        for start, end in windows_to_validate:
            start_dt, _ = _parse_time(start)
            end_dt, _ = _parse_time(end)
            if start_dt >= end_dt or (end_dt - start_dt).total_seconds() > 1800:
                raise ValueError("invalid phase window")
    except (TypeError, ValueError, AttributeError) as exc:
        return {"status": "invalid_input", "error": str(exc)}

    unique_services = sorted(set(services))
    phase_count = 3 if post_start and post_end else 2
    per_phase_limit = min(
        _LOGS_PER_PHASE,
        max_total_lines // (len(unique_services) * phase_count),
    )
    if per_phase_limit < 1:
        return {"status": "invalid_input", "error": "budget too small for requested services"}

    service_results = {}
    total_lines = 0
    statuses = []
    for service in unique_services:
        result = check_service_logs(
            loki,
            service=service,
            namespace=namespace,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            fault_start=fault_start,
            fault_end=fault_end,
            post_start=post_start,
            post_end=post_end,
            limit_per_phase=per_phase_limit,
        )
        service_results[service] = result
        statuses.append(result.get("status"))
        for phase in (result.get("phases") or {}).values():
            total_lines += int((phase.get("coverage") or {}).get("lines_returned") or 0)

    if statuses and all(status == "ok" for status in statuses):
        status = "ok"
    elif statuses and all(status == "no_data" for status in statuses):
        status = "no_data"
    elif any(status == "tool_error" for status in statuses):
        status = "partial" if any(status in {"ok", "partial"} for status in statuses) else "tool_error"
    else:
        status = "partial"

    return {
        "status": status,
        "namespace": namespace,
        "services": service_results,
        "budget": {
            "max_total_lines": max_total_lines,
            "lines_returned": total_lines,
            "per_service_phase_limit": per_phase_limit,
            "service_count": len(unique_services),
            "phase_count": phase_count,
        },
    }
