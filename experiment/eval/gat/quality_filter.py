#!/usr/bin/env python3
"""Classifica a qualidade dos grafos GAT sem modificar o dataset original.

Uso:
    python -m experiment.eval.gat.quality_filter
    python -m experiment.eval.gat.quality_filter --save

Por padrão, apenas o relatório é gerado. Com ``--save``, os grafos são
separados em ``clean``, ``warning`` e ``excluded``; nenhum arquivo de entrada
é sobrescrito.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "out" / "dataset.jsonl"
DEFAULT_OUTPUT_DIR = HERE / "out" / "quality_filter"

SCHEMA_VERSION = "1.0"
NODE_METRICS = ("traffic_lr", "latency_p95_lr", "error_rate_lr")
DEFAULT_EXCLUDED_TARGETS = {"queue-master"}
DEFAULT_EXCLUDED_FAULTS = {"http-status-code", "http-error"}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _nodes_by_service(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(node.get("service")): node
        for node in graph.get("nodes", [])
        if node.get("service")
    }


def _target_metrics(graph: Dict[str, Any], target: str) -> Dict[str, Any]:
    node = _nodes_by_service(graph).get(target) or {}
    features = node.get("features") or {}
    return {key: features.get(key) for key in NODE_METRICS}


def _anomaly(value: Any) -> float:
    return abs(float(value)) if _finite(value) else 0.0


def classify_graph(
    graph: Dict[str, Any],
    *,
    min_observed: int = 3,
    min_target_anomaly: float = 0.0,
    excluded_targets: Optional[Iterable[str]] = None,
    excluded_faults: Optional[Iterable[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Retorna ``(status, quality)`` sem mutar o grafo.

    ``clean`` exige sinal observável nas três métricas do alvo. ``warning``
    mantém exemplos com sinal parcial para análise secundária. Exemplos sem
    sinal, sem rótulo ou com dado não finito são excluídos.
    """
    excluded_targets = set(excluded_targets or DEFAULT_EXCLUDED_TARGETS)
    excluded_faults = set(excluded_faults or DEFAULT_EXCLUDED_FAULTS)
    warnings: List[str] = []
    reasons: List[str] = []
    target = (graph.get("label") or {}).get("root_service")
    target_idx = (graph.get("label") or {}).get("root_service_idx")
    fault = (
        graph.get("chaos_type_normalized")
        or graph.get("chaos_type_raw")
        or (graph.get("label") or {}).get("fault_category")
    )
    nodes = _nodes_by_service(graph)

    if not target or target_idx is None:
        reasons.append("missing_ground_truth")
    if target in excluded_targets:
        reasons.append(f"excluded_target:{target}")
    if target not in nodes:
        reasons.append("target_node_missing")
    if fault in excluded_faults:
        reasons.append(f"excluded_fault:{fault}")

    metrics = _target_metrics(graph, target) if target else {}
    observed = sum(_finite(value) for value in metrics.values())
    target_anomaly = max((_anomaly(value) for value in metrics.values()), default=0.0)

    all_values = [
        value
        for node in graph.get("nodes", [])
        for value in (node.get("features") or {}).values()
        if value is not None
    ]
    if any(not _finite(value) for value in all_values):
        reasons.append("non_finite_feature")
    if observed == 0:
        reasons.append("target_without_observable_signal")
    elif target_anomaly <= min_target_anomaly:
        reasons.append("target_anomaly_below_threshold")

    if observed < min_observed and observed > 0:
        warnings.append(f"target_metric_coverage:{observed}/{len(NODE_METRICS)}")
    if target and target_anomaly > 0:
        node_anomalies = {
            str(node.get("service")): max(
                (_anomaly((node.get("features") or {}).get(key)) for key in NODE_METRICS),
                default=0.0,
            )
            for node in graph.get("nodes", [])
        }
        if node_anomalies and target_anomaly < max(node_anomalies.values()):
            warnings.append("target_not_largest_anomaly")
    if graph.get("edge_source") != "full":
        warnings.append(f"edge_source:{graph.get('edge_source', 'missing')}")

    if reasons:
        status = "excluded"
    elif observed < min_observed:
        status = "warning"
    else:
        status = "clean"

    quality = {
        "status": status,
        "target_service": target,
        "fault_category": fault,
        "target_metric_observed_count": observed,
        "target_metric_total": len(NODE_METRICS),
        "target_anomaly": target_anomaly,
        "warnings": sorted(set(warnings)),
        "reasons": sorted(set(reasons)),
    }
    return status, quality


def load_graphs(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def filter_dataset(
    graphs: Iterable[Dict[str, Any]],
    *,
    min_observed: int = 3,
    min_target_anomaly: float = 0.0,
    excluded_targets: Optional[Iterable[str]] = None,
    excluded_faults: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    excluded_targets = set(excluded_targets or DEFAULT_EXCLUDED_TARGETS)
    excluded_faults = set(excluded_faults or DEFAULT_EXCLUDED_FAULTS)
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    reason_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    by_fault: Dict[str, Counter[str]] = defaultdict(Counter)
    by_target: Dict[str, Counter[str]] = defaultdict(Counter)

    for graph in graphs:
        status, quality = classify_graph(
            graph,
            min_observed=min_observed,
            min_target_anomaly=min_target_anomaly,
            excluded_targets=excluded_targets,
            excluded_faults=excluded_faults,
        )
        annotated = dict(graph)
        annotated["quality"] = quality
        groups[status].append(annotated)
        for reason in quality["reasons"]:
            reason_counts[reason] += 1
        for warning in quality["warnings"]:
            warning_counts[warning] += 1
        by_fault[str(quality["fault_category"])][status] += 1
        by_target[str(quality["target_service"])][status] += 1

    report = {
        "schema_version": SCHEMA_VERSION,
        "input_graphs": sum(len(items) for items in groups.values()),
        "counts": {status: len(groups.get(status, [])) for status in ("clean", "warning", "excluded")},
        "parameters": {
            "min_observed": min_observed,
            "min_target_anomaly": min_target_anomaly,
            "excluded_targets": sorted(excluded_targets),
            "excluded_faults": sorted(excluded_faults),
        },
        "exclude_reasons": dict(sorted(reason_counts.items())),
        "warnings": dict(sorted(warning_counts.items())),
        "by_fault": {key: dict(sorted(value.items())) for key, value in sorted(by_fault.items())},
        "by_target": {key: dict(sorted(value.items())) for key, value in sorted(by_target.items())},
    }
    return dict(groups), report


def main() -> None:
    parser = argparse.ArgumentParser(description="Classifica a qualidade do dataset GAT")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-observed", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--min-target-anomaly", type=float, default=0.0)
    parser.add_argument("--include-http-status-code", action="store_true")
    parser.add_argument("--save", action="store_true", help="Grava subconjuntos e relatório")
    args = parser.parse_args()

    excluded_faults = set() if args.include_http_status_code else DEFAULT_EXCLUDED_FAULTS
    groups, report = filter_dataset(
        load_graphs(args.input),
        min_observed=args.min_observed,
        min_target_anomaly=args.min_target_anomaly,
        excluded_faults=excluded_faults,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.save:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for status, graphs in groups.items():
            path = args.output_dir / f"dataset.{status}.jsonl"
            path.write_text(
                "".join(json.dumps(graph, ensure_ascii=False) + "\n" for graph in graphs),
                encoding="utf-8",
            )
        (args.output_dir / "quality_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nArtefatos salvos em {args.output_dir}")


if __name__ == "__main__":
    main()
