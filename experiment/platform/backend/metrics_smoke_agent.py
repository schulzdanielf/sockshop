"""Lightweight metrics smoke agent for the current environment.

This agent is intentionally narrow: it collects a small set of high-level,
window-scoped observability findings and asks the configured LLM for a short
operational assessment. It is meant as a smoke test for the new MCP-style
investigator contracts and the model interface, not as a full RCA pipeline.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pprint import pformat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .analysis.llm_client import LLMClientError, call_llm_messages

ROOT = Path(__file__).resolve().parents[3]
MCP_SERVER_DIR = ROOT / "mcp-observability-server"
if str(MCP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER_DIR))

_investigators = importlib.import_module("investigators")
check_http_error_rate_top = _investigators.check_http_error_rate_top
check_oom_kills = _investigators.check_oom_kills
check_pod_restarts = _investigators.check_pod_restarts
check_request_latency_top = _investigators.check_request_latency_top
get_cpu_saturation_top = _investigators.get_cpu_saturation_top
get_memory_saturation_top = _investigators.get_memory_saturation_top

PrometheusClient = importlib.import_module("prometheus_client").PrometheusClient


@dataclass(frozen=True)
class InvestigationSpec:
    key: str
    fn: Callable[..., Dict[str, Any]]
    kwargs: Dict[str, Any]


def _emit(message: str, *, verbose: bool) -> None:
    if verbose:
        print(message, file=sys.stderr)


def _compact_for_log(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    compact: Dict[str, Any] = {
        "found": payload.get("found"),
        "count": payload.get("count"),
    }
    if payload.get("error"):
        compact["error"] = payload.get("error")
        return compact
    top = payload.get("top") or []
    compact["top"] = top[:2]
    compact["query"] = payload.get("query")
    return compact


def default_window(minutes: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace(
        "+00:00", "Z"
    )


def collect_snapshot(
    prometheus: Any,
    *,
    start: str,
    end: str,
    namespace: str = "sock-shop",
    verbose: bool = False,
) -> Dict[str, Any]:
    specs = [
        InvestigationSpec(
            key="http_error_rate",
            fn=check_http_error_rate_top,
            kwargs={"start": start, "end": end, "step": "15s", "k": 3},
        ),
        InvestigationSpec(
            key="request_latency_p95",
            fn=check_request_latency_top,
            kwargs={"start": start, "end": end, "step": "15s", "k": 3},
        ),
        InvestigationSpec(
            key="cpu_saturation",
            fn=get_cpu_saturation_top,
            kwargs={
                "start": start,
                "end": end,
                "namespace": namespace,
                "step": "15s",
                "k": 3,
            },
        ),
        InvestigationSpec(
            key="memory_saturation",
            fn=get_memory_saturation_top,
            kwargs={
                "start": start,
                "end": end,
                "namespace": namespace,
                "step": "15s",
                "k": 3,
            },
        ),
        InvestigationSpec(
            key="pod_restarts",
            fn=check_pod_restarts,
            kwargs={
                "start": start,
                "end": end,
                "namespace": namespace,
                "step": "15s",
            },
        ),
        InvestigationSpec(
            key="oom_kills",
            fn=check_oom_kills,
            kwargs={
                "start": start,
                "end": end,
                "namespace": namespace,
                "step": "15s",
            },
        ),
    ]
    snapshot = {
        "window": {"start": start, "end": end},
        "namespace": namespace,
        "investigations": {},
        "tool_calls": [],
    }
    for spec in specs:
        _emit(f"[agent] tool={spec.key} args={json.dumps(spec.kwargs, ensure_ascii=False)}", verbose=verbose)
        result = spec.fn(prometheus, **spec.kwargs)
        snapshot["investigations"][spec.key] = result
        call_record = {
            "tool": spec.key,
            "args": spec.kwargs,
            "result": _compact_for_log(result),
        }
        snapshot["tool_calls"].append(call_record)
        _emit(f"[agent] result={spec.key} {pformat(call_record['result'])}", verbose=verbose)
    return snapshot


def snapshot_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    investigations = snapshot.get("investigations") or {}
    error_keys = [
        key
        for key, payload in investigations.items()
        if isinstance(payload, dict) and payload.get("error")
    ]
    active_keys = [
        key
        for key, payload in investigations.items()
        if isinstance(payload, dict) and payload.get("found")
    ]
    status = "unavailable" if error_keys else ("signal" if active_keys else "quiet")
    return {
        "status": status,
        "error_keys": error_keys,
        "active_keys": active_keys,
    }


def _compact_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    compact: Dict[str, Any] = {
        "found": payload.get("found"),
        "count": payload.get("count"),
    }
    if payload.get("error"):
        compact["error"] = payload.get("error")
        return compact
    top = payload.get("top") or []
    compact["top"] = top[:2]
    return compact


def build_assessment_messages(snapshot: Dict[str, Any]) -> List[Dict[str, str]]:
    health = snapshot_health(snapshot)
    compact = {
        key: _compact_payload(value)
        for key, value in (snapshot.get("investigations") or {}).items()
    }
    system = (
        "You are a concise SRE triage agent. Use only the provided metrics JSON. "
        "Do not invent missing data. If the metrics backend is unavailable, say so. "
        "Return exactly one short JSON object."
    )
    user = {
        "task": "Assess the current operational situation from the live metrics snapshot.",
        "required_schema": {
            "status": "healthy|warning|critical|unknown|unavailable",
            "services": ["service-a", "service-b"],
            "summary": "one short sentence",
            "evidence": ["http_error_rate", "request_latency_p95"],
            "actions": ["short action", "short action"]
        },
        "response_rules": [
            "Keep the summary to one short sentence.",
            "Return at most two services.",
            "Return at most two actions.",
        ],
        "snapshot_health": health,
        "snapshot": compact,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_assessment_prompt(snapshot: Dict[str, Any]) -> str:
    messages = build_assessment_messages(snapshot)
    return "\n\n".join(f"[{msg['role']}]\n{msg['content']}" for msg in messages)


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    depth = 0
    start = -1
    in_str = False
    escape = False
    for idx, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _top_service_names(payload: Any, limit: int = 2) -> List[str]:
    if not isinstance(payload, dict):
        return []
    names: List[str] = []
    for row in payload.get("top") or []:
        if not isinstance(row, dict):
            continue
        service = row.get("service")
        if isinstance(service, str) and service and service not in names:
            names.append(service)
        if len(names) >= limit:
            break
    return names


def _max_metric_value(payload: Any, key: str) -> float:
    if not isinstance(payload, dict):
        return 0.0
    top = payload.get("top") or []
    best = 0.0
    for row in top:
        if not isinstance(row, dict):
            continue
        try:
            value = float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > best:
            best = value
    return best


def fallback_assessment(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic triage when the small LLM does not return valid JSON."""
    investigations = snapshot.get("investigations") or {}
    health = snapshot_health(snapshot)
    if health.get("status") == "unavailable":
        return {
            "status": "unavailable",
            "services": [],
            "summary": "Metrics backend unavailable for live triage.",
            "evidence": health.get("error_keys") or [],
            "actions": ["Check Prometheus connectivity", "Retry the smoke agent"],
        }

    if (investigations.get("oom_kills") or {}).get("found"):
        return {
            "status": "critical",
            "services": _top_service_names(investigations.get("oom_kills")),
            "summary": "OOM kills detected in the current window.",
            "evidence": ["oom_kills"],
            "actions": ["Inspect memory limits", "Check recent pod events"],
        }

    if (investigations.get("pod_restarts") or {}).get("found"):
        return {
            "status": "critical",
            "services": _top_service_names(investigations.get("pod_restarts")),
            "summary": "Pod restarts detected in the current window.",
            "evidence": ["pod_restarts"],
            "actions": ["Inspect pod events", "Check container logs"],
        }

    http_error = investigations.get("http_error_rate") or {}
    if http_error.get("found") and _max_metric_value(http_error, "error_rate_max_pct") >= 5.0:
        return {
            "status": "critical",
            "services": _top_service_names(http_error),
            "summary": "Elevated HTTP 5xx rate detected.",
            "evidence": ["http_error_rate"],
            "actions": ["Inspect failing endpoints", "Review service logs"],
        }

    memory = investigations.get("memory_saturation") or {}
    if memory.get("found"):
        return {
            "status": "warning",
            "services": _top_service_names(memory),
            "summary": "Memory saturation is elevated but no failures are visible.",
            "evidence": ["memory_saturation"],
            "actions": ["Inspect memory-heavy services", "Review resource limits"],
        }

    latency = investigations.get("request_latency_p95") or {}
    if latency.get("found"):
        return {
            "status": "warning",
            "services": _top_service_names(latency),
            "summary": "Request latency is elevated in the current window.",
            "evidence": ["request_latency_p95"],
            "actions": ["Inspect slow endpoints", "Correlate with traces"],
        }

    cpu = investigations.get("cpu_saturation") or {}
    if cpu.get("found"):
        return {
            "status": "warning",
            "services": _top_service_names(cpu),
            "summary": "CPU saturation is elevated but stable.",
            "evidence": ["cpu_saturation"],
            "actions": ["Inspect CPU-heavy services", "Review CPU limits"],
        }

    return {
        "status": "healthy",
        "services": [],
        "summary": "No strong error, restart, or saturation signal was detected.",
        "evidence": [],
        "actions": ["Keep monitoring", "Run a trace-focused probe if needed"],
    }


def assess_snapshot(
    snapshot: Dict[str, Any],
    *,
    llm: Callable[..., str] = call_llm_messages,
    max_new_tokens: int = 220,
    verbose: bool = False,
) -> Dict[str, Any]:
    messages = build_assessment_messages(snapshot)
    _emit("[agent] llm_messages=", verbose=verbose)
    if verbose:
        print(build_assessment_prompt(snapshot), file=sys.stderr)
    raw = llm(messages, max_new_tokens=max_new_tokens)
    parsed = _extract_first_json_object(raw)
    source = "llm_json"
    if parsed is None:
        parsed = fallback_assessment(snapshot)
        source = "fallback"
    return {
        "parsed": parsed,
        "raw_response": raw,
        "source": source,
    }


def run_smoke_agent(
    *,
    minutes: int,
    namespace: str,
    use_llm: bool,
    max_new_tokens: int,
    verbose: bool = False,
) -> Dict[str, Any]:
    start, end = default_window(minutes)
    prometheus = PrometheusClient()
    try:
        snapshot = collect_snapshot(
            prometheus,
            start=start,
            end=end,
            namespace=namespace,
            verbose=verbose,
        )
    finally:
        prometheus.close()

    result: Dict[str, Any] = {
        "snapshot_health": snapshot_health(snapshot),
        "snapshot": snapshot,
    }
    if not use_llm:
        return result

    try:
        result["assessment"] = assess_snapshot(
            snapshot,
            max_new_tokens=max_new_tokens,
            verbose=verbose,
        )
    except LLMClientError as exc:
        result["assessment"] = {
            "parsed": None,
            "raw_response": None,
            "error": str(exc),
        }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=220)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    result = run_smoke_agent(
        minutes=args.minutes,
        namespace=args.namespace,
        use_llm=not args.no_llm,
        max_new_tokens=args.max_new_tokens,
        verbose=args.verbose,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    health = result.get("snapshot_health") or {}
    if health.get("status") == "unavailable":
        return 2
    assessment = result.get("assessment") or {}
    if assessment.get("error"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
