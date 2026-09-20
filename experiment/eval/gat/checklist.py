#!/usr/bin/env python3
"""Checklist de experimento — auditoria offline de qualidade por run.

Aplica o catálogo de gates (``backend.quality``) retroativamente sobre
todos os runs do platform.db, sem subir o servidor. Para cada run produz:

* ``out/quality/<run_id>.json`` — resultado dos gates (run_quality)
* ``out/health/<run_id>.json`` — relatório consolidado (run_health)

E um agregado ``out/checklist_summary.json`` com as falhas mais comuns
— responde "quais etapas do pipeline estão quebrando, em quantos runs".

O mesmo catálogo roda online no engine; este CLI é a visão retroativa.

Uso
---
    python -m experiment.eval.gat.checklist            # relatório no stdout
    python -m experiment.eval.gat.checklist --save     # salva JSONs por run
    python -m experiment.eval.gat.checklist --run run-xxx   # um run só
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.quality import (  # noqa: E402
    RunBundle,
    build_run_health,
    run_gates,
)
from experiment.platform.backend.storage.sqlite_storage import (  # noqa: E402
    SqliteStorage,
)

DEFAULT_DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
DEFAULT_OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

SCHEMA_VERSION = "1.0"


def _make_bundle(storage: SqliteStorage, run: Any) -> RunBundle:
    """Monta o RunBundle a partir do storage + object store (offline)."""
    features = storage.get_run_features(run.run_id)
    events = [
        {"ts": e.ts, "event_type": e.event_type, "payload": e.payload}
        for e in storage.list_events(run.run_id)
    ]
    with storage._conn() as conn:
        artifact_names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM artifacts WHERE run_id = ?", (run.run_id,)
            ).fetchall()
        ]

    object_root = storage.object_store_root

    def load_artifact(name: str) -> Optional[Dict[str, Any]]:
        path = object_root / run.run_id / f"{name}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    return RunBundle(
        run_id=run.run_id,
        experiment_id=run.experiment_id,
        status=run.status.value,
        timings=run.timings or {},
        features=features,
        events=events,
        artifact_names=artifact_names,
        load_artifact=load_artifact,
    )


def run_checklist(
    storage: SqliteStorage, run_id: Optional[str] = None
) -> Dict[str, Any]:
    """Executa o checklist sobre todos os runs (ou um só) e agrega."""
    runs = storage.list_runs()
    if run_id:
        runs = [r for r in runs if r.run_id == run_id]
        if not runs:
            raise KeyError(f"Run não encontrado: {run_id}")

    per_run: List[Dict[str, Any]] = []
    health_by_run: Dict[str, Dict[str, Any]] = {}
    gap_counts: Counter = Counter()
    gate_fail_counts: Counter = Counter()
    usable = 0
    weak = 0

    for run in runs:
        bundle = _make_bundle(storage, run)
        quality = run_gates(bundle)
        health = build_run_health(bundle)
        health_by_run[run.run_id] = health
        per_run.append(quality)

        if quality["usable"]:
            usable += 1
        if quality["weak_signal"]:
            weak += 1
        for gap in quality["gaps"]:
            gap_counts[gap] += 1
        for g in quality["gates"]:
            if g["status"] == "fail":
                gate_fail_counts[g["gate"]] += 1

    summary = {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(storage.db_path),
        "total_runs": len(per_run),
        "usable": usable,
        "unusable": len(per_run) - usable,
        "weak_signal": weak,
        "gap_counts": dict(gap_counts.most_common()),
        "gate_fail_counts": dict(gate_fail_counts.most_common()),
        "runs": per_run,
    }
    return {"summary": summary, "health_by_run": health_by_run}


def render_report(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("CHECKLIST DE EXPERIMENTO — auditoria de qualidade")
    lines.append("=" * 78)
    lines.append(f"Total de runs: {summary['total_runs']}")
    lines.append(f"Utilizáveis: {summary['usable']}  |  Não utilizáveis: {summary['unusable']}")
    lines.append(f"Sinal fraco (informativo): {summary['weak_signal']}")
    lines.append("")

    lines.append("Falhas por gate (nº de runs):")
    if summary["gate_fail_counts"]:
        for gate, count in summary["gate_fail_counts"].items():
            lines.append(f"  {gate:<32} {count}")
    else:
        lines.append("  (nenhum gate falhou)")
    lines.append("")

    lines.append("Razões de falha (nº de runs):")
    if summary["gap_counts"]:
        for reason, count in summary["gap_counts"].items():
            lines.append(f"  {reason:<44} {count}")
    lines.append("")

    # Amostra de runs não utilizáveis para inspeção.
    unusable = [r["run_id"] for r in summary["runs"] if not r["usable"]]
    lines.append(f"Runs NÃO utilizáveis ({len(unusable)}):")
    for rid in unusable[:30]:
        lines.append(f"  {rid}")
    if len(unusable) > 30:
        lines.append(f"  … e mais {len(unusable) - 30}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Checklist de qualidade por run")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--object-store", default=str(DEFAULT_OBJ))
    parser.add_argument("--run", default=None, help="audita um único run_id")
    parser.add_argument("--save", action="store_true", help="salva JSONs por run")
    args = parser.parse_args()

    storage = SqliteStorage(
        db_path=Path(args.db), object_store_root=Path(args.object_store)
    )
    result = run_checklist(storage, run_id=args.run)

    print(render_report(result["summary"]))

    if args.save:
        out = HERE / "out"
        (out / "quality").mkdir(parents=True, exist_ok=True)
        (out / "health").mkdir(parents=True, exist_ok=True)
        for quality in result["summary"]["runs"]:
            rid = quality["run_id"]
            (out / "quality" / f"{rid}.json").write_text(
                json.dumps(quality, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            (out / "health" / f"{rid}.json").write_text(
                json.dumps(result["health_by_run"][rid], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        (out / "checklist_summary.json").write_text(
            json.dumps(result["summary"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nSalvo em {out}/ (quality/, health/, checklist_summary.json)")


if __name__ == "__main__":
    main()
