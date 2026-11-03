"""Recheck historical training evidence and annotate B without changing prior revisions."""

from __future__ import annotations

import argparse
import copy
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from experiment.eval.memory_hog.retroactive_b import (
    _atomic_json,
    _file_sha256,
    _generate_retro_b,
    _source_annotation,
    _training_cohort,
)
from experiment.platform.backend.analysis.llm_client import call_llm_tool_messages, llm_health
from experiment.platform.backend.analysis.system_card import load_system_card
from experiment.platform.backend.metrics_smoke_agent import (
    get_cpu_saturation_top,
    get_memory_saturation_top,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.rca_knowledge import (
    Check,
    canonical,
    digest,
    execute_check,
    write_once,
)
from experiment.platform.backend.rca_skill import (
    TOOL_ERRORS,
    Diagnosis,
    validate_diagnosis,
)

VERSION = "retro-b-recheck-v2"


class HistoricalClient:
    """Run corrected local saturation helpers over the same read-only MCP transport."""

    def __init__(self, client: MCPToolClient):
        self.client = client
        self.tools = client.list_tools()
        self.requests: list[dict[str, Any]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tools

    def query_range(self, query: str, start: str, end: str, step: str) -> dict[str, Any]:
        return self.call_tool_json(
            "prometheus_range_query",
            {"query": query, "start": start, "end": end, "step": step},
        )

    def call_tool_json(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_memory_saturation_top":
            return get_memory_saturation_top(self, **arguments)
        if name == "get_cpu_saturation_top":
            return get_cpu_saturation_top(self, **arguments)
        started = time.monotonic()
        request = {"tool": name, "arguments": arguments}
        self.requests.append(request)
        try:
            result = self.client.call_tool_json(name, arguments)
            if not isinstance(result, dict):
                raise ValueError("MCP returned a non-object")
        except TOOL_ERRORS as exc:
            request.update(status="tool_error", error=str(exc))
            raise
        finally:
            request["duration_ms"] = round((time.monotonic() - started) * 1000)
        request.update(status=result.get("status", "returned"), result=result)
        return result


def focused_evidence(evidence: list[dict[str, Any]], service: str) -> list[dict[str, Any]]:
    """Keep target edges and resource comparisons before general context truncation."""
    focused = []
    resources = {"get_memory_saturation_top", "get_cpu_saturation_top"}
    ordered = sorted(evidence, key=lambda item: (
        0 if item["tool"] in {"tempo_analyze_trace", "tempo_get_trace"} else
        1 if item["tool"] in resources else 2
    ))
    for item in ordered:
        if item.get("status") != "ok" or (
            item.get("fault_observation_count", 0) <= 0 and item["tool"] not in resources
        ):
            continue
        value = item.get("digest") or {}
        tool = item["tool"]
        details: dict[str, Any] = {}
        if tool in {"tempo_analyze_trace", "tempo_get_trace"}:
            edges = value.get("dependency_map") or []
            details = {
                "phase": value.get("phase"),
                "trace_id": value.get("trace_id"),
                "dependency_map": [
                    edge for edge in edges if service in {edge.get("from"), edge.get("to")}
                ],
                "error_spans": (value.get("error_spans") or [])[:5],
            }
        elif tool == "get_run_golden_signals":
            details = {
                "metrics": {
                    name: {
                        "unit": metric.get("unit"),
                        "target_rows": [
                            row for row in metric.get("top_delta") or []
                            if row.get("service") == service
                        ],
                    }
                    for name, metric in (value.get("metrics") or {}).items()
                }
            }
        elif tool in {"get_memory_saturation_top", "get_cpu_saturation_top"}:
            details = {
                **value,
                "top": [row for row in value.get("top") or [] if row.get("service") == service],
            }
        elif tool in {"check_service_logs", "loki_range_query"} and value.get("service") == service:
            details = value
        if details:
            row = {
                "id": item["id"],
                "family": item["family"],
                "arguments": item.get("arguments"),
                "details": details,
            }
            if len(canonical(focused + [row])) <= 20000:
                focused.append(row)
    return focused


def _check(kind: str, service: str, phase: str = "fault") -> Check:
    return Check(
        kind=kind,
        service=service,
        phase=phase,
        hypothesis="Confrontar o rotulo de treino com dados da mesma janela historica.",
        expected_support="Sinal discriminativo relativo ao baseline e ao servico.",
        expected_refutation="Dados positivos que contradigam o mecanismo proposto.",
        historical_pair_ids=[],
        patterns=[],
    )


def collect(
    source: dict[str, Any],
    client: HistoricalClient,
    known: set[str],
    callers: list[str],
) -> dict[str, Any]:
    response = source["response"]
    evidence = copy.deepcopy(response["evidence"])
    calls = copy.deepcopy(response.get("calls") or [])
    original_checks = response.get("additional_checks") or []
    target = source["labels"]["service"]
    windows = {key: value for key, value in source["pair"]["phases"].items()
               if key in {"baseline", "fault", "post"}}
    checks = [
        Check.model_validate(row["check"])
        for row in original_checks if row.get("status") != "ok"
    ]
    checks.append(_check("logs", target))
    for kind, fault in (("memory", "memory-hog"), ("cpu", "cpu-hog")):
        if source["labels"]["fault_type"] == fault:
            checks.extend(_check(kind, target, phase) for phase in ("baseline", "fault"))
    if any(check.kind == "trace_detail" for check in checks):
        checks.insert(0, _check("traces", target))
    if source["labels"]["fault_type"] == "http-status-code":
        checks.extend(_check("logs", service) for service in callers[:2])
    outcomes = []
    seen = set()
    request_start = len(client.requests)
    for check in checks:
        key = canonical(check.model_dump(include={"kind", "service", "phase", "patterns"}))
        if key in seen:
            continue
        seen.add(key)
        if check.service not in known:
            outcomes.append({"check": check.model_dump(), "status": "rejected",
                             "error": "Service is absent from the system card"})
            continue
        # A reviewed retry is a new query attempt; old calls remain in the audit.
        new_calls = copy.deepcopy(calls) if check.kind == "trace_detail" else []
        previous_count = len(new_calls)
        try:
            outcome = execute_check(
                check, client, "sock-shop", windows, known, evidence, new_calls,
            )
        except TOOL_ERRORS as exc:
            outcome = {"check": check.model_dump(), "status": "tool_error",
                       "error": f"{type(exc).__name__}: {exc}"}
        calls.extend(new_calls[previous_count:])
        outcomes.append(outcome)
        if check.kind == "logs" and check.patterns and outcome["status"] == "no_data":
            fallback = check.model_copy(update={"patterns": []})
            fallback_calls: list[dict[str, Any]] = []
            try:
                result = execute_check(
                    fallback, client, "sock-shop", windows, known, evidence, fallback_calls
                )
            except TOOL_ERRORS as exc:
                result = {"check": fallback.model_dump(), "status": "tool_error",
                          "error": f"{type(exc).__name__}: {exc}"}
            result["reason"] = "Unfiltered fallback: literal patterns returned no logs"
            outcomes.append(result)
            calls.extend(fallback_calls)
    return {
        "version": VERSION,
        "source_annotation_sha256": _file_sha256(source["path"]),
        "evidence": evidence,
        "calls": calls,
        "original_additional_checks": original_checks,
        "rechecks": outcomes,
        "transport_requests": client.requests[request_start:],
        "phases": windows,
    }


def _contract_retry_path(output: Path, cell_id: str) -> Path:
    previous_path = output / "contract_repairs" / f"{cell_id}.json"
    if not previous_path.is_file():
        raise ValueError(f"No prior contract-repair record for {cell_id}")
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    if previous.get("status") != "error":
        raise ValueError(f"Only failed contract repairs can be retried: {cell_id}")
    retry_path = output / "contract_revalidations" / f"{cell_id}.json"
    if retry_path.exists():
        raise FileExistsError(f"Contract revalidation already exists for {cell_id}")
    return retry_path


def run(
    campaign: Path,
    *,
    annotate: bool,
    repair_contracts: bool = False,
    retry_failed_contracts: bool = False,
    cell_ids: set[str] | None = None,
) -> dict[str, Any]:
    if retry_failed_contracts and (
        not annotate or not repair_contracts or not cell_ids
    ):
        raise ValueError(
            "Retrying failed contracts requires annotation, contract repair, and explicit cell IDs"
        )
    config, cells, cohort = _training_cohort(campaign)
    output = campaign / "retro_b_recheck_v2"
    manifest = {"version": VERSION, "source_hashes": cohort["source_hashes"]}
    write_once(output / "manifest.json", manifest)
    card = load_system_card("sock-shop")
    known = set(card["services"])
    categories = set(card["allowed_fault_categories"])
    possible_fault_types = sorted({cell["chaos_type"] for cell in cells})
    client = None
    results = []
    if annotate and llm_health(timeout=5).get("ok") is not True:
        raise RuntimeError("The configured model is unavailable")
    known_cell_ids = {cell["cell_id"] for cell in cells}
    if cell_ids and not cell_ids.issubset(known_cell_ids):
        raise ValueError(f"Unknown campaign cell IDs: {sorted(cell_ids - known_cell_ids)}")
    if retry_failed_contracts:
        for cell_id in cell_ids or set():
            _contract_retry_path(output, cell_id)
    for cell in cells:
        if cell_ids is not None and cell["cell_id"] not in cell_ids:
            continue
        entry = cohort["latest"][cell["cell_id"]]
        if entry["status"] != "completed":
            continue
        source = _source_annotation(campaign, cell, entry, config)
        path = output / "evidence" / f"{cell['cell_id']}.json"
        if path.exists():
            bundle = json.loads(path.read_text(encoding="utf-8"))
            if bundle["source_annotation_sha256"] != _file_sha256(source["path"]):
                raise ValueError("Recheck provenance mismatch")
        else:
            spec = json.loads((campaign / "attempts" / cell["cell_id"] / "spec.json").read_text())
            endpoint = spec["observability"]["metrics"]["mcp_sse_url"]
            if client is None:
                client = HistoricalClient(MCPToolClient(endpoint, 60))
            callers = [
                name for name, details in card["services"].items()
                if cell["service"] in details.get("downstream", [])
            ]
            bundle = collect(source, client, known, callers)
            bundle.update(run_id=entry["run_id"], cell_id=cell["cell_id"])
            bundle["sha256"] = digest(bundle)
            write_once(path, bundle)
        if bundle["sha256"] != digest({key: value for key, value in bundle.items() if key != "sha256"}):
            raise ValueError("Recheck bundle hash mismatch")
        result = {
            "cell_id": cell["cell_id"], "run_id": entry["run_id"],
            "rechecks": dict(Counter(row["status"] for row in bundle["rechecks"])),
            "effect_validated": source["effect_validated"],
        }
        if annotate:
            guide_path = output / "annotations" / f"{cell['cell_id']}.json"
            original_guide_path = guide_path
            if retry_failed_contracts:
                guide_path = _contract_retry_path(output, cell["cell_id"])
            elif repair_contracts and guide_path.exists():
                previous = json.loads(guide_path.read_text(encoding="utf-8"))
                if previous["status"] == "error":
                    guide_path = output / "contract_repairs" / f"{cell['cell_id']}.json"
            if guide_path.exists():
                annotation = json.loads(guide_path.read_text(encoding="utf-8"))
                if annotation["bundle_sha256"] != bundle["sha256"]:
                    raise ValueError("B reannotation bundle mismatch")
            else:
                restarted = [
                    row["service"] for row in source["response"]["initial_diagnosis"]["restart_analysis"]
                ]
                model_evidence = bundle["evidence"]
                details_sha256 = None
                details_path = output / "detailed_trace_evidence" / f"{cell['cell_id']}.json"
                if repair_contracts and details_path.exists():
                    details = json.loads(details_path.read_text(encoding="utf-8"))
                    if (
                        details["source_bundle_sha256"] != bundle["sha256"]
                        or details["run_id"] != entry["run_id"]
                        or details["sha256"] != digest(
                            {key: value for key, value in details.items() if key != "sha256"}
                        )
                    ):
                        raise ValueError("Detailed trace evidence provenance mismatch")
                    model_evidence = model_evidence + [
                        row for row in details["evidence"] if row["status"] == "ok"
                    ]
                    details_sha256 = details["sha256"]
                try:
                    guide, attempts, tokens = _generate_retro_b(
                        llm=call_llm_tool_messages,
                        labels=source["labels"],
                        document_a=source["response"]["a"],
                        evidence=model_evidence,
                        original_diagnosis=source["response"]["initial_diagnosis"],
                        executed_checks=bundle["rechecks"],
                        known_services=known, categories=categories,
                        possible_fault_types=possible_fault_types,
                        max_attempts=2,
                        focused_evidence=focused_evidence(model_evidence, cell["service"]),
                        restarted_services=restarted,
                    )
                except TOOL_ERRORS as exc:
                    annotation = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                else:
                    diagnosis = validate_diagnosis(
                        Diagnosis.model_validate(guide["diagnosis"]), model_evidence,
                        known, categories, restarted, possible_fault_types,
                    )
                    guide["diagnosis"] = diagnosis.model_dump()
                    annotation = {
                        "status": diagnosis.status,
                        "eligible": source["effect_validated"] and diagnosis.status == "confirmed",
                        "b": guide, "model_attempts": attempts, "total_tokens": tokens,
                    }
                annotation.update(
                    run_id=entry["run_id"], bundle_sha256=bundle["sha256"],
                    source_annotation_sha256=_file_sha256(source["path"]),
                    detailed_trace_evidence_sha256=details_sha256,
                    repaired_from=(
                        str(
                            (output / "contract_repairs" / f"{cell['cell_id']}.json")
                            .relative_to(output)
                            if retry_failed_contracts
                            else original_guide_path.relative_to(output)
                        )
                        if guide_path != original_guide_path else None
                    ),
                )
                write_once(guide_path, annotation)
            result.update(status=annotation["status"], eligible=annotation.get("eligible", False))
        results.append(result)
        print(json.dumps(result), flush=True)
    summary = {
        **manifest,
        "completed_train_count": len(results),
        "test_runs_started": 0,
        "recheck_status": dict(sum((Counter(row["rechecks"]) for row in results), Counter())),
        "diagnosis_status": dict(Counter(row.get("status", "not_requested") for row in results)),
        "eligible_count": sum(row.get("eligible", False) for row in results),
        "eligibility_validation": "automated structural gates; not causal content review",
        "per_run": results,
        "base_frozen": False,
    }
    summary_path = (
        output / "contract_revalidations" / "summary.json"
        if retry_failed_contracts
        else output / "summary.json"
    )
    _atomic_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--annotate", action="store_true")
    parser.add_argument("--repair-contracts", action="store_true")
    parser.add_argument("--retry-failed-contracts", action="store_true")
    parser.add_argument("--cell-id", action="append", dest="cell_ids")
    args = parser.parse_args()
    if args.repair_contracts and not args.annotate:
        parser.error("--repair-contracts requires --annotate")
    if args.retry_failed_contracts and (
        not args.repair_contracts or not args.annotate or not args.cell_ids
    ):
        parser.error(
            "--retry-failed-contracts requires --annotate, --repair-contracts, and --cell-id"
        )
    run(
        args.campaign.resolve(),
        annotate=args.annotate,
        repair_contracts=args.repair_contracts,
        retry_failed_contracts=args.retry_failed_contracts,
        cell_ids=set(args.cell_ids) if args.cell_ids else None,
    )


if __name__ == "__main__":
    main()
