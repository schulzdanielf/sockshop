#!/usr/bin/env python3
"""Dataset builder do GAT — reprocessa platform.db → dataset.jsonl + splits.

Reconstrói, de forma **versionada e reproduzível**, o dataset de grafos a
partir dos runs históricos (Opção D: topologia estática + node features do
``metrics_raw``). Para cada run reutilizável:

1. **C3 — janela efetiva de fault:** recorte curto para falhas transientes
   (container-kill, pod-delete), fase inteira para persistentes.
2. **Normalização de chaos_type:** remove prefixo de serviço
   (``orders-container-kill`` → ``container-kill``) para as células não
   ficarem artificialmente esparsas.
3. **Grafo:** via :func:`graph_builder.build_gat_graph`.
4. **Splits S1–S4 anti-leakage** (definidos em
   ``experiment/docs/experimento_validacao_phase_f1.md``), persistidos por
   run em ``split_assignment.json`` com a regra usada (decisão como dado).

Saídas (em ``out/``): ``dataset.jsonl``, ``split_assignment.json``,
``dataset_report.json``. Tudo JSON schema-versionado (consumível por agente).

Uso
---
    python -m experiment.eval.gat.build_dataset --save
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.analysis.graph_builder import (  # noqa: E402
    build_gat_graph,
)
from experiment.platform.backend.storage.sqlite_storage import (  # noqa: E402
    SqliteStorage,
)

DEFAULT_DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
DEFAULT_OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

SCHEMA_VERSION = "1.0"

# ── C3: falhas transientes (estímulo breve) vs persistentes ─────────────
# Transientes: o sintoma é breve dentro de uma fase de fault longa; usar a
# fase inteira dilui o pico. Cap curto ancorado no disparo.
TRANSIENT_FAULTS = {"container-kill", "pod-delete"}
TRANSIENT_CAP_SECONDS = 180  # 3 min pós-disparo (cap no fim real da fase)

# ── Splits anti-leakage (seção 3 de experimento_validacao_phase_f1.md) ──
S2_HELD_OUT_SERVICES = ("orders", "payment")
S3_HELD_OUT_FAULT = "pod-delete"
# S4 (intensidade) não é aplicável retroativamente: os runs históricos não
# registram intensidade de forma consistente. Marcado como None.


def normalize_chaos_type(raw: Optional[str]) -> Optional[str]:
    """Remove prefixo de serviço do chaos_type do workflow.

    ``orders-container-kill`` → ``container-kill``;
    ``payment-network-latency`` → ``network-latency``; ``cpu-hog`` → ``cpu-hog``.
    """
    if not raw:
        return None
    known = (
        "container-kill",
        "http-status-code",
        "network-latency",
        "network-loss",
        "memory-hog",
        "pod-delete",
        "cpu-hog",
    )
    for k in known:
        if raw == k:
            return k
        if raw.endswith("-" + k):
            return k
    return raw


def effective_fault_window(
    chaos_type: Optional[str],
    fault_start: str,
    fault_end: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """C3 — retorna (start, end, meta) da janela efetiva de fault.

    Transiente: recorta a janela para ``TRANSIENT_CAP_SECONDS`` após o
    disparo (cap no fim real da fase). Persistente: fase inteira. A regra
    escolhida vai para ``meta`` (decisão como dado).
    """
    from datetime import datetime, timedelta

    fs = datetime.fromisoformat(fault_start.replace("Z", "+00:00"))
    fe = datetime.fromisoformat(fault_end.replace("Z", "+00:00"))
    phase_seconds = (fe - fs).total_seconds()

    is_transient = chaos_type in TRANSIENT_FAULTS
    if is_transient:
        cap_end = fs + timedelta(seconds=TRANSIENT_CAP_SECONDS)
        eff_end = min(cap_end, fe)
        rule = "transient_recorte"
    else:
        eff_end = fe
        rule = "persistent_full_phase"

    meta = {
        "rule": rule,
        "chaos_type_raw": chaos_type,
        "is_transient": is_transient,
        "phase_duration_seconds": phase_seconds,
        "effective_window_seconds": (eff_end - fs).total_seconds(),
        "cap_seconds": TRANSIENT_CAP_SECONDS if is_transient else None,
    }
    return fault_start, eff_end.isoformat(), meta


def assign_splits(
    run_id: str,
    chaos_type: str,
    service: str,
    is_training: bool,
    replica_rank_in_cell: int,
    total_in_cell: int,
) -> Dict[str, Optional[str]]:
    """Atribui o run aos splits S1–S4 (treino/teste), com a regra usada.

    Um run pode ser treino num split e teste noutro. Retorna
    ``{split: "train"|"test"|None}`` + ``rule`` descritiva.
    """
    splits: Dict[str, Optional[str]] = {"S1": None, "S2": None, "S3": None, "S4": None}

    # S1 — Repetition: as últimas réplicas de cada célula são teste.
    # time-split: réplicas mais antigas no treino, a mais recente no teste.
    if total_in_cell >= 2:
        splits["S1"] = "test" if replica_rank_in_cell == total_in_cell - 1 else "train"

    # S2 — Held-out service: runs em orders/payment são teste.
    splits["S2"] = "test" if service in S2_HELD_OUT_SERVICES else "train"

    # S3 — Held-out fault: runs de pod-delete são teste.
    splits["S3"] = "test" if chaos_type == S3_HELD_OUT_FAULT else "train"

    # S4 — Held-out intensity: não recuperável retroativamente.
    splits["S4"] = None

    return splits


def _split_rule_description(splits: Dict[str, Optional[str]]) -> str:
    parts = []
    if splits["S1"]:
        parts.append("S1:repetition")
    if splits["S2"]:
        parts.append(f"S2:held_out_service={S2_HELD_OUT_SERVICES}")
    if splits["S3"]:
        parts.append(f"S3:held_out_fault={S3_HELD_OUT_FAULT}")
    return ";".join(parts) if parts else "none"


def build_dataset(storage: SqliteStorage) -> Dict[str, Any]:
    """Reprocessa todos os runs reusáveis em grafos + splits."""
    runs = storage.list_runs()

    # Ordenar por started_at para o time-split de S1 ser determinístico.
    runs = sorted(runs, key=lambda r: r.started_at or "")

    # rank da réplica dentro da célula (chaos × serviço), para S1.
    cell_counter: Dict[Tuple[str, str], int] = defaultdict(int)
    cell_totals: Dict[Tuple[str, str], int] = defaultdict(int)
    # primeira passada: totais por célula
    cell_of_run: Dict[str, Tuple[str, str]] = {}
    for run in runs:
        feats = storage.get_run_features(run.run_id) or {}
        ct = normalize_chaos_type(feats.get("chaos_type"))
        svc = feats.get("ground_truth_service")
        if ct and svc:
            cell_of_run[run.run_id] = (ct, svc)
            cell_totals[(ct, svc)] += 1

    graphs: List[Dict[str, Any]] = []
    split_rows: List[Dict[str, Any]] = []
    excluded: Counter = Counter()

    for run in runs:
        if run.status.value != "completed":
            excluded[f"status_{run.status.value}"] += 1
            continue

        feats = storage.get_run_features(run.run_id)
        if not feats:
            excluded["no_features"] += 1
            continue

        gt_service = feats.get("ground_truth_service")
        gt_fault = feats.get("ground_truth_fault_category")
        if not gt_service or not gt_fault:
            excluded["missing_ground_truth"] += 1
            continue

        metrics_raw = _load_artifact(storage, run.run_id, "metrics_raw")
        if not metrics_raw:
            excluded["no_metrics_raw"] += 1
            continue

        timings = run.timings or {}
        fault_start = timings.get("t_fault_start")
        fault_end = timings.get("t_fault_end")
        base_start = timings.get("t_start")
        if not (fault_start and fault_end and base_start):
            excluded["missing_timings"] += 1
            continue

        chaos_norm = normalize_chaos_type(feats.get("chaos_type"))

        # C3 — janela efetiva
        eff_start, eff_end, window_meta = effective_fault_window(
            chaos_norm, fault_start, fault_end
        )

        # Grafo (Opção D: sem edge_metrics → edge_source=static)
        graph = build_gat_graph(
            run_id=run.run_id,
            metrics_raw=metrics_raw,
            baseline_window=(base_start, fault_start),
            fault_window=(eff_start, eff_end),
            ground_truth_service=gt_service,
            ground_truth_fault_category=gt_fault,
            is_training=bool(feats.get("is_training", True)),
            edge_metrics=None,
        )
        graph["chaos_type_raw"] = feats.get("chaos_type")
        graph["chaos_type_normalized"] = chaos_norm
        graph["fault_window_meta"] = window_meta

        # Splits S1–S4
        cell = (chaos_norm, gt_service)
        rank = cell_counter[cell]
        cell_counter[cell] += 1
        total_in_cell = cell_totals[cell]
        splits = assign_splits(
            run.run_id,
            chaos_norm,
            gt_service,
            bool(feats.get("is_training", True)),
            rank,
            total_in_cell,
        )
        graph["splits"] = splits

        graphs.append(graph)
        split_rows.append(
            {
                "run_id": run.run_id,
                "is_training": bool(feats.get("is_training", True)),
                "chaos_type": chaos_norm,
                "service": gt_service,
                "replica_rank_in_cell": rank,
                "total_in_cell": total_in_cell,
                "splits": splits,
                "rule": _split_rule_description(splits),
            }
        )

    # Relatório
    cells_report: Counter = Counter()
    edge_source_counts: Counter = Counter()
    for g in graphs:
        split_flag = "train" if g.get("is_training") else "test"
        cells_report[f"{g['chaos_type_normalized']} × {g['label']['root_service']} [{split_flag}]"] += 1
        edge_source_counts[g.get("edge_source", "?")] += 1

    report = {
        "schema_version": SCHEMA_VERSION,
        "total_runs": len(runs),
        "graphs_built": len(graphs),
        "excluded": dict(excluded),
        "cells": dict(sorted(cells_report.items())),
        "edge_source_counts": dict(edge_source_counts),
    }
    return {"graphs": graphs, "splits": split_rows, "report": report}


def _load_artifact(storage: SqliteStorage, run_id: str, name: str) -> Optional[Dict[str, Any]]:
    path = storage.object_store_root / run_id / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def render_report(report: Dict[str, Any]) -> str:
    lines = ["=" * 78, "DATASET BUILDER (GAT, Opção D)", "=" * 78]
    lines.append(f"Total de runs: {report['total_runs']}")
    lines.append(f"Grafos construídos: {report['graphs_built']}")
    if report["excluded"]:
        lines.append(f"Excluídos: {report['excluded']}")
    lines.append("")
    lines.append(f"Células ({len(report['cells'])}):")
    for cell, n in report["cells"].items():
        lines.append(f"  {cell:<55} {n}")
    lines.append("")
    lines.append(f"edge_source: {report['edge_source_counts']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dataset builder do GAT")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--object-store", default=str(DEFAULT_OBJ))
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    storage = SqliteStorage(
        db_path=Path(args.db), object_store_root=Path(args.object_store)
    )
    result = build_dataset(storage)
    print(render_report(result["report"]))

    if args.save:
        out = HERE / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "dataset.jsonl").write_text(
            "\n".join(json.dumps(g, ensure_ascii=False) for g in result["graphs"]),
            encoding="utf-8",
        )
        (out / "split_assignment.json").write_text(
            json.dumps(result["splits"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out / "dataset_report.json").write_text(
            json.dumps(result["report"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nSalvo em {out}/ (dataset.jsonl, split_assignment.json, dataset_report.json)")


if __name__ == "__main__":
    main()
