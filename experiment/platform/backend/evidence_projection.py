"""Bound model context without cutting JSON or discarding discriminative facts."""

from __future__ import annotations

import copy
import json
from typing import Any

MODEL_EVIDENCE_CONTEXT_CHARS = 56000
PROJECTION_VERSION = "structured-evidence-v2"
EVIDENCE_FORMAT_NOTICE = (
    "Evidencias compactadas sao objetos JSON. Tabelas columns/rows preservam os "
    "valores: a coluna N nomeia o valor N de cada linha; source_pointer liga a "
    "lista ao digest original. Contagens projection_omitted_* e "
    "omitted_text_characters indicam dados omitidos, nao ausencia de falha. "
    "Historico continua sendo hipotese, nao evidencia atual."
)
_PROTECTED_LISTS = {
    "top_delta",
    "affected",
    "termination_events",
    "restart_observations",
    "critical_path",
    "error_spans",
    "failure_signatures",
    "transitions",
    "reasons",
    "exit_codes",
    "operations",
    "services",
    "pod_observations",
    "top",
    "top_edge_deltas",
    "first_degraded_services",
    "missing_edges_during_fault",
    "new_error_edges",
    "persistent_edges",
    "recovered_edges",
    "pattern_counts",
    "signatures",
    "series",
    "reason",
    "missing_metrics",
    "collected_metrics",
    "observed_services",
}
_TEXT_LIMITS = {"query": 160, "message": 400, "error": 600}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _bound_text(value: Any) -> Any:
    if isinstance(value, list):
        return [_bound_text(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    omitted = {}
    for key, item in value.items():
        limit = _TEXT_LIMITS.get(key)
        if limit and isinstance(item, str) and len(item) > limit:
            result[key] = item[:limit]
            omitted[key] = len(item) - limit
        else:
            result[key] = _bound_text(item)
    if omitted:
        result["omitted_text_characters"] = omitted
    return result


def _reducible_lists(value: Any) -> list[tuple[int, dict[str, Any], str, list[Any]]]:
    candidates = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, list) and item and key not in _PROTECTED_LISTS:
                minimum = (
                    1
                    if key in {"sample_lines", "signatures", "top_nodes", "top_edges"}
                    else 0
                )
                if len(item) > minimum:
                    candidates.append((len(_json(item)), value, key, item))
            # Protected rows may have optional examples, but their facts stay intact.
            candidates.extend(_reducible_lists(item))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_reducible_lists(item))
    return candidates


def columnar_projection(value: Any, pointer: str) -> Any:
    if isinstance(value, dict):
        return {
            key: columnar_projection(
                item, pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            )
            for key, item in value.items()
        }
    if not isinstance(value, list):
        return value
    rows = [
        columnar_projection(item, f"{pointer}/{index}")
        for index, item in enumerate(value)
    ]
    if (
        len(rows) >= 2
        and all(isinstance(row, dict) for row in rows)
        and all(row.keys() == rows[0].keys() for row in rows)
    ):
        columns = sorted(rows[0])
        table = {
            "source_pointer": pointer,
            "columns": columns,
            "rows": [[row[column] for column in columns] for row in rows],
        }
        if len(_json(table)) < len(_json(rows)):
            return table
    return rows


def compact_evidence_for_model(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not evidence:
        return []
    if all(item.get("projection_version") == PROJECTION_VERSION for item in evidence):
        if len(_json(evidence)) > MODEL_EVIDENCE_CONTEXT_CHARS:
            raise ValueError("structured evidence exceeds the model context budget")
        for item in evidence:
            if not isinstance(item["arguments_summary"], dict) or not isinstance(
                item["digest_summary"], dict
            ):
                raise ValueError("structured evidence summaries must be JSON objects")
        return copy.deepcopy(evidence)

    projections = []
    for item in evidence:
        if "digest_summary" in item and "digest" not in item:
            raise ValueError(
                "cannot recover facts from a legacy compacted payload; use raw evidence"
            )
        entry = {
            key: item.get(key)
            for key in (
                "id",
                "tool",
                "family",
                "source",
                "status",
                "observation_count",
                "fault_observation_count",
            )
        }
        entry["projection_version"] = PROJECTION_VERSION
        arguments = _bound_text(copy.deepcopy(item.get("arguments") or {}))
        digest = _bound_text(copy.deepcopy(item.get("digest") or {}))
        projections.append((entry, arguments, digest))

    def encode() -> list[dict[str, Any]]:
        return [
            {
                **entry,
                "arguments_summary": copy.deepcopy(arguments),
                "digest_summary": columnar_projection(digest, "/digest"),
            }
            for entry, arguments, digest in projections
        ]

    encoded = encode()
    while len(_json(encoded)) > MODEL_EVIDENCE_CONTEXT_CHARS:
        candidates = [
            candidate
            for _, _, digest in projections
            for candidate in _reducible_lists(digest)
        ]
        if not candidates:
            raise ValueError(
                "critical evidence facts exceed the model context budget; "
                "increase the explicit budget or split the analysis"
            )
        _, parent, key, items = max(candidates, key=lambda candidate: candidate[0])
        minimum = (
            1 if key in {"sample_lines", "signatures", "top_nodes", "top_edges"} else 0
        )
        keep = max(minimum, len(items) // 2)
        omitted_count = len(items) - keep
        del items[keep:]
        omission_key = f"projection_omitted_{key}_count"
        parent[omission_key] = parent.get(omission_key, 0) + omitted_count
        encoded = encode()
    return encoded
