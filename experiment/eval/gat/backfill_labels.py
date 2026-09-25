#!/usr/bin/env python3
"""Backfill de labels (ground truth) para runs cujo gat_graph ficou sem label.

Contexto (ERRO 1 em experiment/docs/erros-experimentos.md): após o reset, os runs
foram gravados sem ``ground_truth_service``/``ground_truth_fault_category`` em
``run_features`` e, por consequência, sem ``label`` no ``gat_graph`` — embora as
tags do experimento (``target:<service>`` / ``fault_category:<cat>``) estejam
corretas por versão. Este script reconstrói o gabarito **sem recoletar**, lendo
as tags da versão exata de cada run.

Idempotente: só altera runs cujo label esteja ausente; preserva os demais.

Uso
---
    python -m experiment.eval.gat.backfill_labels          # dry-run (só mostra)
    python -m experiment.eval.gat.backfill_labels --apply  # grava
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.analysis.graph_builder import CORE_SERVICES  # noqa: E402

DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

# sorted(set(CORE_SERVICES)) — mesma ordem do graph_builder
NODE_INDEX = {svc: i for i, svc in enumerate(sorted(set(CORE_SERVICES)))}


def _tags_of(conn, experiment_id: str, version: int) -> list[str]:
    row = conn.execute(
        "SELECT spec_json FROM experiments WHERE experiment_id=? AND version=?",
        (experiment_id, version),
    ).fetchone()
    if not row:
        return []
    try:
        spec = json.loads(row["spec_json"])
    except (json.JSONDecodeError, TypeError):
        return []
    return (spec.get("experiment", {}) or {}).get("tags") or []


def _extract(tags: list[str]) -> tuple[str | None, str | None]:
    gt_service = gt_fault = None
    for tag in tags:
        if not isinstance(tag, str):
            continue
        if tag.startswith("target:") and gt_service is None:
            gt_service = tag.split(":", 1)[1].strip() or None
        elif tag.startswith("fault_category:") and gt_fault is None:
            gt_fault = tag.split(":", 1)[1].strip() or None
    return gt_service, gt_fault


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill de labels (ground truth)")
    p.add_argument("--apply", action="store_true", help="grava (default: dry-run)")
    args = p.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    runs = conn.execute("SELECT run_id, experiment_id, experiment_version FROM runs WHERE status='completed'").fetchall()

    stats = {"sem_graph": 0, "ja_ok": 0, "corrigidos": 0, "sem_target": 0, "sem_rf": 0}
    for r in runs:
        rid = r["run_id"]
        g_path = OBJ / rid / "gat_graph.json"
        if not g_path.exists():
            stats["sem_graph"] += 1
            continue
        graph = json.loads(g_path.read_text(encoding="utf-8"))
        label = graph.get("label") or {}
        if label.get("root_service_idx") is not None:
            stats["ja_ok"] += 1
            continue

        gt_service, gt_fault = _extract(_tags_of(conn, r["experiment_id"], r["experiment_version"]))
        if not gt_service:
            stats["sem_target"] += 1
            print(f"  [sem target] {rid}")
            continue

        # 1) gat_graph: preenche label
        graph["label"] = {
            "root_service_idx": NODE_INDEX.get(gt_service),
            "root_service": gt_service,
            "fault_category": gt_fault,
        }
        # 2) run_features: preenche ground_truth
        rf_path = OBJ / rid / "run_features.json"
        if rf_path.exists():
            rf = json.loads(rf_path.read_text(encoding="utf-8"))
            rf["ground_truth_service"] = gt_service
            if gt_fault:
                rf["ground_truth_fault_category"] = gt_fault
        else:
            stats["sem_rf"] += 1
            rf = None

        if args.apply:
            g_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            if rf is not None:
                rf_path.write_text(json.dumps(rf, ensure_ascii=False), encoding="utf-8")
        stats["corrigidos"] += 1

    print("\n=== BACKFILL DE LABELS ===")
    print(f"  sem gat_graph (pulados):      {stats['sem_graph']}")
    print(f"  já tinham label (intocados):  {stats['ja_ok']}")
    print(f"  sem tag target (não resolve): {stats['sem_target']}")
    print(f"  corrigidos{' (GRAVADO)' if args.apply else ' (dry-run)'}: {stats['corrigidos']}")
    if not args.apply:
        print("\nDry-run. Rode com --apply para gravar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
