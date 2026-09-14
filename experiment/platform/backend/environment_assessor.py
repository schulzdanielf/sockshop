"""Constrained environment-assessment agent.

This is a thin agent wrapper around the smoke-agent collectors. It keeps the
context small, limits the number of loops/tool calls, and exposes hooks for
observability so the operator can see what it asked, what came back, and why it
stopped.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .analysis.llm_client import call_llm_messages
from .metrics_smoke_agent import (
    PrometheusClient,
    build_assessment_messages,
    default_window,
    snapshot_health,
    _compact_payload,
    _emit,
    _max_metric_value,
    _top_service_names,
    check_http_error_rate_top,
    check_oom_kills,
    check_pod_restarts,
    check_request_latency_top,
    get_cpu_saturation_top,
    get_memory_saturation_top,
)


Hook = Callable[[Dict[str, Any]], None]


@dataclass
class AgentHooks:
    on_loop_start: Optional[Hook] = None
    on_tool_start: Optional[Hook] = None
    on_tool_end: Optional[Hook] = None
    on_assessment_start: Optional[Hook] = None
    on_assessment_end: Optional[Hook] = None
    on_loop_end: Optional[Hook] = None


@dataclass(frozen=True)
class ToolSpec:
    key: str
    fn: Callable[..., Dict[str, Any]]
    kwargs: Dict[str, Any]


_ALLOWED_STATUSES = {"healthy", "warning", "critical", "unknown", "unavailable"}


def _tool_specs_for_baseline(start: str, end: str, namespace: str) -> List[ToolSpec]:
    return [
        ToolSpec("http_error_rate", check_http_error_rate_top, {"start": start, "end": end, "step": "15s", "k": 3}),
        ToolSpec("request_latency_p95", check_request_latency_top, {"start": start, "end": end, "step": "15s", "k": 3}),
        ToolSpec("cpu_saturation", get_cpu_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 3}),
        ToolSpec("memory_saturation", get_memory_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 3}),
        ToolSpec("pod_restarts", check_pod_restarts, {"start": start, "end": end, "namespace": namespace, "step": "15s"}),
        ToolSpec("oom_kills", check_oom_kills, {"start": start, "end": end, "namespace": namespace, "step": "15s"}),
    ]


def _tool_specs_for_follow_up(snapshot: Dict[str, Any]) -> List[ToolSpec]:
    investigations = snapshot.get("investigations") or {}
    window = snapshot.get("window") or {}
    start = str(window.get("start") or "")
    end = str(window.get("end") or "")
    namespace = str(snapshot.get("namespace") or "sock-shop")

    if (investigations.get("oom_kills") or {}).get("found"):
        return [ToolSpec("oom_kills", check_oom_kills, {"start": start, "end": end, "namespace": namespace, "step": "15s"}), ToolSpec("memory_saturation", get_memory_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 5})]

    if (investigations.get("pod_restarts") or {}).get("found"):
        return [ToolSpec("pod_restarts", check_pod_restarts, {"start": start, "end": end, "namespace": namespace, "step": "15s"}), ToolSpec("cpu_saturation", get_cpu_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 5})]

    http_error = investigations.get("http_error_rate") or {}
    if http_error.get("found") and _max_metric_value(http_error, "error_rate_max_pct") >= 5.0:
        return [ToolSpec("http_error_rate", check_http_error_rate_top, {"start": start, "end": end, "step": "15s", "k": 5}), ToolSpec("request_latency_p95", check_request_latency_top, {"start": start, "end": end, "step": "15s", "k": 5})]

    latency = investigations.get("request_latency_p95") or {}
    if latency.get("found"):
        return [ToolSpec("request_latency_p95", check_request_latency_top, {"start": start, "end": end, "step": "15s", "k": 5}), ToolSpec("http_error_rate", check_http_error_rate_top, {"start": start, "end": end, "step": "15s", "k": 5})]

    cpu = investigations.get("cpu_saturation") or {}
    if cpu.get("found"):
        return [ToolSpec("cpu_saturation", get_cpu_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 5})]

    memory = investigations.get("memory_saturation") or {}
    if memory.get("found"):
        return [ToolSpec("memory_saturation", get_memory_saturation_top, {"start": start, "end": end, "namespace": namespace, "step": "15s", "k": 5})]

    return []


def _compact_assessment(assessment: Dict[str, Any]) -> Dict[str, Any]:
    parsed = assessment.get("parsed") or {}
    return {
        "status": parsed.get("status"),
        "services": parsed.get("services") or [],
        "summary": parsed.get("summary"),
        "evidence": parsed.get("evidence") or [],
        "actions": parsed.get("actions") or [],
        "source": assessment.get("source"),
    }


def _render_human_report(result: Dict[str, Any]) -> str:
    assessment = result.get("assessment") or {}
    parsed = assessment.get("parsed") or {}
    health = result.get("snapshot_health") or {}
    loops = result.get("loops") or []

    lines = [
        "Situação geral: {status}".format(status=parsed.get("status") or health.get("status") or "unknown"),
        "Resumo: {summary}".format(summary=parsed.get("summary") or "Sem resumo disponível."),
        "Sinais: {signals}".format(
            signals=", ".join(parsed.get("evidence") or []) or "nenhum sinal forte"
        ),
        "Serviços: {services}".format(
            services=", ".join(parsed.get("services") or []) or "nenhum serviço destacado"
        ),
        "Próximos passos: {actions}".format(
            actions=", ".join(parsed.get("actions") or []) or "continuar monitorando"
        ),
        "Fonte: {source}".format(source=assessment.get("source") or "baseline"),
        "Loops executados: {count}".format(count=len(loops)),
    ]
    if health.get("status") == "unavailable":
        errors = health.get("error_keys") or []
        if errors:
            lines.append("Falha de coleta: {errors}".format(errors=", ".join(errors)))
    return "\n".join(lines)


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


def _baseline_assessment(snapshot: Dict[str, Any]) -> Dict[str, Any]:
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
            "summary": "Memory saturation is elevated but stable.",
            "evidence": ["memory_saturation"],
            "actions": ["Inspect memory limits", "Review pod memory usage"],
        }

    latency = investigations.get("request_latency_p95") or {}
    if latency.get("found"):
        return {
            "status": "warning",
            "services": _top_service_names(latency),
            "summary": "Request latency is elevated.",
            "evidence": ["request_latency_p95"],
            "actions": ["Inspect slow paths", "Check downstream dependencies"],
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


def _merge_assessment(base: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    status = parsed.get("status")
    if isinstance(status, str) and status in _ALLOWED_STATUSES:
        merged["status"] = status
    services = parsed.get("services")
    if isinstance(services, list):
        merged["services"] = [str(service) for service in services if isinstance(service, str) and service][:2]
    summary = parsed.get("summary")
    if isinstance(summary, str) and summary.strip():
        merged["summary"] = summary.strip()
    evidence = parsed.get("evidence")
    if isinstance(evidence, list):
        merged["evidence"] = [str(item) for item in evidence if isinstance(item, str) and item][:4]
    actions = parsed.get("actions")
    if isinstance(actions, list):
        merged["actions"] = [str(item) for item in actions if isinstance(item, str) and item][:4]
    return merged


def _repair_messages(snapshot: Dict[str, Any], raw_response: str, base: Dict[str, Any]) -> List[Dict[str, str]]:
    system = (
        "You are a strict JSON repair step. Return exactly one JSON object and nothing else. "
        "Do not add markdown, explanation, or think tags. Keep the same schema."
    )
    payload = {
        "snapshot_health": snapshot_health(snapshot),
        "baseline": base,
        "invalid_response": raw_response,
        "required_schema": {
            "status": "healthy|warning|critical|unknown|unavailable",
            "services": ["service-a", "service-b"],
            "summary": "one short sentence",
            "evidence": ["http_error_rate"],
            "actions": ["short action", "short action"],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


@dataclass
class EnvironmentAssessorAgent:
    minutes: int = 10
    namespace: str = "sock-shop"
    max_loops: int = 2
    max_tool_calls: int = 8
    max_new_tokens: int = 180
    verbose: bool = False
    hooks: AgentHooks = field(default_factory=AgentHooks)
    llm: Callable[..., str] = call_llm_messages
    collector: Callable[..., Dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.collector is None:
            self.collector = self._default_collector

    def _default_collector(
        self, prometheus: Any, *, start: str, end: str, namespace: str, verbose: bool
    ) -> Dict[str, Any]:
        from .metrics_smoke_agent import collect_snapshot

        return collect_snapshot(
            prometheus,
            start=start,
            end=end,
            namespace=namespace,
            verbose=verbose,
        )

    def _log(self, message: str) -> None:
        _emit(message, verbose=self.verbose)

    def plan_follow_up_tools(self, snapshot: Dict[str, Any]) -> List[ToolSpec]:
        return _tool_specs_for_follow_up(snapshot)

    def _call_tool(self, prometheus: Any, spec: ToolSpec, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"tool": spec.key, "args": spec.kwargs}
        if self.hooks.on_tool_start:
            self.hooks.on_tool_start(payload)
        self._log(f"[assessor] tool={spec.key} args={json.dumps(spec.kwargs, ensure_ascii=False)}")
        result = spec.fn(prometheus, **spec.kwargs)
        if self.hooks.on_tool_end:
            self.hooks.on_tool_end({"tool": spec.key, "args": spec.kwargs, "result": _compact_payload(result)})
        self._log(f"[assessor] result={spec.key} {_compact_payload(result)}")
        snapshot.setdefault("investigations", {})[spec.key] = result
        snapshot.setdefault("tool_calls", []).append(
            {"tool": spec.key, "args": spec.kwargs, "result": _compact_payload(result)}
        )
        return result

    def _assess(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        if self.hooks.on_assessment_start:
            self.hooks.on_assessment_start({"snapshot_health": snapshot_health(snapshot)})
        self._log("[assessor] llm_assessment_start")
        base = _baseline_assessment(snapshot)
        assessment = {
            "parsed": base,
            "raw_response": None,
            "source": "baseline",
            "llm_valid": False,
        }
        try:
            assessment = self._run_llm(snapshot, base=base)
        finally:
            if self.hooks.on_assessment_end:
                self.hooks.on_assessment_end({"assessment": _compact_assessment(assessment)})
            self._log(f"[assessor] llm_assessment_end source={assessment.get('source')}")
        return assessment

    def _run_llm(self, snapshot: Dict[str, Any], *, base: Dict[str, Any]) -> Dict[str, Any]:
        messages = build_assessment_messages(snapshot)
        self._log("[assessor] llm_messages=")
        if self.verbose:
            print("\n\n".join(f"[{msg['role']}]\n{msg['content']}" for msg in messages), file=sys.stderr)

        raw = self.llm(messages, max_new_tokens=self.max_new_tokens)
        parsed = _extract_first_json_object(raw)
        if parsed is not None:
            merged = _merge_assessment(base, parsed)
            merged["source"] = "llm_json"
            merged["llm_valid"] = True
            return {
                "parsed": merged,
                "raw_response": raw,
                "source": "llm_json",
                "llm_valid": True,
            }

        repair = self.llm(
            _repair_messages(snapshot, raw, base),
            max_new_tokens=min(self.max_new_tokens, 120),
        )
        repaired = _extract_first_json_object(repair)
        if repaired is not None:
            merged = _merge_assessment(base, repaired)
            merged["source"] = "llm_repaired"
            merged["llm_valid"] = True
            return {
                "parsed": merged,
                "raw_response": raw,
                "repair_response": repair,
                "source": "llm_repaired",
                "llm_valid": True,
            }

        base = dict(base)
        base["source"] = "baseline"
        base["llm_valid"] = False
        return {
            "parsed": base,
            "raw_response": raw,
            "repair_response": repair,
            "source": "baseline",
            "llm_valid": False,
        }

    def run(self) -> Dict[str, Any]:
        start, end = default_window(self.minutes)
        prometheus = PrometheusClient()
        try:
            snapshot = self.collector(
                prometheus,
                start=start,
                end=end,
                namespace=self.namespace,
                verbose=self.verbose,
            )
            loops: List[Dict[str, Any]] = []
            tool_calls = len(snapshot.get("tool_calls") or [])
            latest_assessment: Optional[Dict[str, Any]] = None

            for loop_index in range(1, self.max_loops + 1):
                if self.hooks.on_loop_start:
                    self.hooks.on_loop_start({"loop": loop_index, "tool_calls": tool_calls})
                self._log(f"[assessor] loop={loop_index} start tool_calls={tool_calls}")

                assessment = self._assess(snapshot)
                latest_assessment = assessment
                loops.append(
                    {
                        "loop": loop_index,
                        "assessment": _compact_assessment(assessment),
                        "raw_response": assessment.get("raw_response"),
                        "source": assessment.get("source"),
                    }
                )

                parsed = assessment.get("parsed") or {}
                status = parsed.get("status")
                if status in {"healthy", "unavailable"}:
                    self._log(f"[assessor] loop={loop_index} stop status={status}")
                    break

                follow_ups = self.plan_follow_up_tools(snapshot)
                if not follow_ups:
                    self._log(f"[assessor] loop={loop_index} stop reason=no_follow_up")
                    break

                made_progress = False
                for spec in follow_ups:
                    if tool_calls >= self.max_tool_calls:
                        self._log("[assessor] stop reason=max_tool_calls")
                        break
                    self._call_tool(prometheus, spec, snapshot)
                    tool_calls += 1
                    made_progress = True

                if self.hooks.on_loop_end:
                    self.hooks.on_loop_end({"loop": loop_index, "assessment": _compact_assessment(assessment), "tool_calls": tool_calls})

                if not made_progress:
                    break

            final_assessment = latest_assessment or self._assess(snapshot)
            result = {
                "window": snapshot.get("window"),
                "namespace": self.namespace,
                "snapshot_health": snapshot_health(snapshot),
                "snapshot": snapshot,
                "loops": loops,
                "assessment": final_assessment,
            }
            result["report_text"] = _render_human_report(result)
            return result
        finally:
            prometheus.close()


def run_environment_assessor(
    *,
    minutes: int = 10,
    namespace: str = "sock-shop",
    max_loops: int = 2,
    max_tool_calls: int = 8,
    max_new_tokens: int = 180,
    verbose: bool = False,
    llm: Callable[..., str] = call_llm_messages,
    collector: Optional[Callable[..., Dict[str, Any]]] = None,
    hooks: Optional[AgentHooks] = None,
) -> Dict[str, Any]:
    return EnvironmentAssessorAgent(
        minutes=minutes,
        namespace=namespace,
        max_loops=max_loops,
        max_tool_calls=max_tool_calls,
        max_new_tokens=max_new_tokens,
        verbose=verbose,
        llm=llm,
        collector=collector,
        hooks=hooks or AgentHooks(),
    ).run()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--max-loops", type=int, default=2)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=180)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    result = run_environment_assessor(
        minutes=args.minutes,
        namespace=args.namespace,
        max_loops=args.max_loops,
        max_tool_calls=args.max_tool_calls,
        max_new_tokens=args.max_new_tokens,
        verbose=args.verbose,
    )
    print(result.get("report_text") or _render_human_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
