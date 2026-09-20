#!/usr/bin/env python3
"""Inventário do dataset para o GAT (Fase 0 da abordagem híbrida).

Responde, SEM subir o servidor e SEM chamar LLM:

1. **Quantos runs rotulados existem?** Contagem por célula
   (``chaos_type`` × ``ground_truth_service``), separando treino/teste
   (``is_training``) e status do run.
2. **Os runs antigos são reutilizáveis para o GAT?** Para cada run, mede a
   completude dos dados que o construtor de grafo precisa:
   ground truth (``target:``/``fault_category:``), timestamps das fases
   (``run.timings``), ``service_graph_delta``, ``propagation_graph``,
   hotspots por label, métricas por fase.

Saída: relatório no stdout + ``out/inventory.json`` (machine-readable,
schema versionado — consumível por agente futuro).

Uso
---
    python -m experiment.eval.gat.inventory
    python -m experiment.eval.gat.inventory --db outro/platform.db --save
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.storage.sqlite_storage import (  # noqa: E402
    SqliteStorage,
)

DEFAULT_DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
DEFAULT_OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

SCHEMA_VERSION = "1.0"

# Artefatos brutos que sinalizam se um run antigo pode ser reprocessado
# offline para extrair o service graph (C1/C2 do mapeamento evolutivo).
RAW_ARTIFACTS_FOR_GRAPH = ("traces_raw", "metrics_raw")


def _extract_ground_truth(features: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Ground truth extraído pelo engine das tags do spec (ver engine.py)."""
    return {
        "service": features.get("ground_truth_service"),
        "fault_category": features.get("ground_truth_fault_category"),
    }


def _timings_of(run_summary: Dict[str, Any], run_timings: Dict[str, Any]) -> Dict[str, Any]:
    """Fases do run conforme registradas em run.timings."""
    keys = (
        "t_start",
        "t_fault_start",
        "t_fault_end",
        "t_recovery_observed",
        "fault_injection_duration_seconds",
    )
    return {k: run_timings.get(k) for k in keys}


def assess_run(run: Any, features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Avalia a completude de um run para o dataset do GAT.

    Retorna um dict com flags booleanas e a lista de lacunas. É a versão
    "pré-gates" do que o gate runner fará online (ver mapeamento evolutivo,
    seção 2.3): aqui medimos runs históricos; lá validaremos runs novos.
    """
    f = features or {}
    gt = _extract_ground_truth(f)
    timings = run.timings or {}

    has_gt_service = bool(gt["service"])
    has_gt_fault = bool(gt["fault_category"])
    has_phase_timings = all(
        timings.get(k) for k in ("t_start", "t_fault_start", "t_fault_end")
    )
    has_service_graph_delta = bool(f.get("service_graph_delta"))
    has_propagation_graph = bool(f.get("propagation_graph"))
    has_metric_hotspots = bool(f.get("metric_hotspots"))
    has_phase_metrics = bool(f.get("phase_metrics"))
    has_summary_text = bool(f.get("summary_text"))

    # O que o GAT precisa minimamente: label + janelas + alguma forma de grafo.
    # service_graph_delta é a evidência mais próxima do que C1/C2 produzirão
    # completos; propagation_graph é a alternativa mais crua.
    gaps: List[str] = []
    if not has_gt_service:
        gaps.append("missing_ground_truth_service")
    if not has_gt_fault:
        gaps.append("missing_ground_truth_fault_category")
    if not has_phase_timings:
        gaps.append("missing_phase_timings")
    if not has_service_graph_delta:
        gaps.append("missing_service_graph_delta")
    if not has_propagation_graph:
        gaps.append("missing_propagation_graph")
    if not has_metric_hotspots:
        gaps.append("missing_metric_hotspots")

    # Reutilizável "como está" para o grafo v0: label + timings + algum grafo.
    # Runs sem service_graph_delta ainda podem ser reprocessados offline se os
    # artefatos brutos existirem (verificado separadamente em ``artifacts``).
    reusable_as_is = (
        has_gt_service
        and has_gt_fault
        and has_phase_timings
        and (has_service_graph_delta or has_propagation_graph)
    )

    return {
        "run_id": run.run_id,
        "experiment_id": run.experiment_id,
        "status": run.status.value,
        "is_training": bool(f.get("is_training", True)),
        "chaos_type": f.get("chaos_type"),
        "verdict": f.get("verdict"),
        "ground_truth": gt,
        "timings": _timings_of(run.summary or {}, timings),
        "completeness": {
            "ground_truth_service": has_gt_service,
            "ground_truth_fault_category": has_gt_fault,
            "phase_timings": has_phase_timings,
            "service_graph_delta": has_service_graph_delta,
            "propagation_graph": has_propagation_graph,
            "metric_hotspots": has_metric_hotspots,
            "phase_metrics": has_phase_metrics,
            "summary_text": has_summary_text,
        },
        "reusable_as_is": reusable_as_is,
        "gaps": gaps,
    }


def _artifacts_for(storage: SqliteStorage, run_id: str) -> List[str]:
    """Nomes dos artefatos persistidos para o run (object store)."""
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT name FROM artifacts WHERE run_id = ?", (run_id,)
        ).fetchall()
    return [r["name"] for r in rows]


def build_inventory(storage: SqliteStorage) -> Dict[str, Any]:
    runs = storage.list_runs()

    per_run: List[Dict[str, Any]] = []
    cells: Counter = Counter()
    gap_counts: Counter = Counter()
    status_counts: Counter = Counter()
    reusable = 0
    reusable_with_reprocess = 0

    for run in runs:
        features = storage.get_run_features(run.run_id)
        row = assess_run(run, features)
        row["artifacts"] = _artifacts_for(storage, run.run_id)

        # Reprocessável offline: não é reusável como está, mas tem os brutos.
        has_raw = all(a in row["artifacts"] for a in RAW_ARTIFACTS_FOR_GRAPH)
        row["reprocessable_offline"] = (not row["reusable_as_is"]) and has_raw

        per_run.append(row)
        status_counts[row["status"]] += 1
        if row["reusable_as_is"]:
            reusable += 1
        if row["reprocessable_offline"]:
            reusable_with_reprocess += 1
        for gap in row["gaps"]:
            gap_counts[gap] += 1

        if row["ground_truth"]["service"] and row["chaos_type"]:
            split = "train" if row["is_training"] else "test"
            cells[
                f'{row["chaos_type"]} × {row["ground_truth"]["service"]} [{split}]'
            ] += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(storage.db_path),
        "total_runs": len(runs),
        "by_status": dict(status_counts),
        "reusable_as_is": reusable,
        "reusable_with_offline_reprocess": reusable_with_reprocess,
        "gap_counts": dict(gap_counts.most_common()),
        "cells": dict(sorted(cells.items())),
        "runs": per_run,
    }


def render_report(inv: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("INVENTÁRIO DO DATASET GAT (Fase 0)")
    lines.append("=" * 78)
    lines.append(f"DB: {inv['db_path']}")
    lines.append(f"Total de runs: {inv['total_runs']}")
    lines.append(f"Por status: {inv['by_status']}")
    lines.append("")
    lines.append(
        f"Reutilizáveis como estão (label + timings + grafo): "
        f"{inv['reusable_as_is']}"
    )
    lines.append(
        f"Reprocessáveis offline (têm traces_raw+metrics_raw, mas sem grafo pronto): "
        f"{inv['reusable_with_offline_reprocess']}"
    )
    lines.append("")

    lines.append("Lacunas mais comuns (nº de runs afetados):")
    if inv["gap_counts"]:
        for gap, count in inv["gap_counts"].items():
            lines.append(f"  {gap:<40} {count}")
    else:
        lines.append("  (nenhuma lacuna — todos os runs completos)")
    lines.append("")

    lines.append("Células rotuladas (chaos_type × serviço [split]):")
    if inv["cells"]:
        for cell, count in inv["cells"].items():
            lines.append(f"  {cell:<55} {count}")
    else:
        lines.append("  (nenhuma célula rotulada)")
    lines.append("")

    # Runs reutilizáveis, para inspeção rápida.
    reusable_ids = [r["run_id"] for r in inv["runs"] if r["reusable_as_is"]]
    lines.append(f"Runs reutilizáveis ({len(reusable_ids)}):")
    for rid in reusable_ids[:40]:
        lines.append(f"  {rid}")
    if len(reusable_ids) > 40:
        lines.append(f"  … e mais {len(reusable_ids) - 40}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventário do dataset GAT")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="caminho do platform.db")
    parser.add_argument(
        "--object-store",
        default=str(DEFAULT_OBJ),
        help="raiz do object store (artefatos)",
    )
    parser.add_argument("--save", action="store_true", help="salva out/inventory.json")
    args = parser.parse_args()

    storage = SqliteStorage(
        db_path=Path(args.db), object_store_root=Path(args.object_store)
    )
    inv = build_inventory(storage)

    print(render_report(inv))

    if args.save:
        out_dir = HERE / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "inventory.json"
        path.write_text(json.dumps(inv, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSalvo em {path}")


if __name__ == "__main__":
    main()
