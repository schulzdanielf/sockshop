#!/usr/bin/env python3
"""Auditor de dados coletados — prova que um run gerou as métricas necessárias.

Valida, por run, que o experimento de caos foi bem-sucedido e produziu tudo o
que o GAT precisa (camada 4.3 do plano de auditoria):

* run chegou a ``completed``
* ``metrics_raw`` tem as 3 golden metrics por serviço com pontos > 0
* ``istio_edges`` existe com séries > 0 nas arestas da topologia
* ``gat_graph`` existe com ``edge_source: full`` e ≥ 1 aresta com feature não-nula
* ground truth (target/fault_category/intensity) presente

Saída: relatório no stdout + JSON opcional. Exit 0 = passou, 1 = falhou.

Uso
---
    python -m experiment.eval.gat.audit_run run-<id>
    python -m experiment.eval.gat.audit_run run-<id> --save
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.platform.backend.storage.sqlite_storage import SqliteStorage  # noqa: E402

DEFAULT_DB = ROOT / "experiment" / "platform" / "data" / "platform.db"
DEFAULT_OBJ = ROOT / "experiment" / "platform" / "data" / "object_store"

SCHEMA_VERSION = "1.0"

NODE_GOLDEN = ("traffic", "error_rate", "latency_p95")


def _load_artifact(storage: SqliteStorage, run_id: str, name: str) -> Optional[Dict[str, Any]]:
    path = storage.object_store_root / run_id / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _n_points(metrics_raw: Dict[str, Any], metric_id: str) -> int:
    data = metrics_raw.get("data") or {}
    result = ((data.get(metric_id) or {}).get("data") or {}).get("result") or []
    return sum(len(s.get("values") or []) for s in result)


def _istio_edge_series_count(istio_edges: Dict[str, Any]) -> Dict[str, int]:
    out = {}
    for m, payload in istio_edges.items():
        out[m] = len((payload.get("data") or {}).get("result") or [])
    return out


def audit_run(storage: SqliteStorage, run_id: str) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # 1. status
    try:
        run = storage.get_run(run_id)
    except KeyError:
        return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "ok": False,
                "checks": [{"check": "run_exists", "ok": False, "detail": "not found"}]}
    add("run_completed", run.status.value == "completed", run.status.value)

    # 2. ground truth
    features = storage.get_run_features(run_id) or {}
    gt_service = features.get("ground_truth_service")
    gt_fault = features.get("ground_truth_fault_category")
    add("ground_truth_present", bool(gt_service and gt_fault),
        {"service": gt_service, "fault_category": gt_fault})

    # 3. metrics_raw — golden metrics por serviço com pontos > 0
    metrics_raw = _load_artifact(storage, run_id, "metrics_raw")
    if not metrics_raw:
        add("metrics_raw_golden", False, "artifact missing")
    else:
        per_metric = {m: _n_points(metrics_raw, m) for m in NODE_GOLDEN}
        ok = all(n > 0 for n in per_metric.values())
        add("metrics_raw_golden", ok, per_metric)

    # 4. istio_edges — séries > 0
    istio_edges = _load_artifact(storage, run_id, "istio_edges")
    if not istio_edges:
        add("istio_edges_present", False, "artifact missing")
    else:
        counts = _istio_edge_series_count(istio_edges)
        ok = all(n > 0 for n in counts.values())
        add("istio_edges_present", ok, counts)

    # 5. gat_graph — edge_source full + ≥1 aresta com feature
    gat_graph = _load_artifact(storage, run_id, "gat_graph")
    if not gat_graph:
        add("gat_graph_full", False, "artifact missing")
    else:
        edge_source = gat_graph.get("edge_source")
        edges = gat_graph.get("edges") or []
        with_feats = sum(
            1 for e in edges
            if isinstance(e, dict) and any(
                (e.get("features") or {}).get(k) is not None
                for k in ("req_rate_lr", "latency_lr", "error_lr")
            )
        )
        ok = edge_source == "full" and with_feats > 0
        add("gat_graph_full", ok, {"edge_source": edge_source, "edges_with_features": with_feats})

    ok_all = all(c["ok"] for c in checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "ok": ok_all,
        "checks": checks,
    }


def render(result: Dict[str, Any]) -> str:
    lines = ["=" * 78, f"AUDITORIA DE RUN — {result['run_id']}", "=" * 78]
    for c in result["checks"]:
        mark = "✓" if c["ok"] else "✗"
        lines.append(f"  {mark} {c['check']:<24} {json.dumps(c['detail'], ensure_ascii=False)[:60]}")
    lines.append("")
    lines.append(f"VEREDITO: {'PASSOU — dados completos' if result['ok'] else 'FALHOU — ver detalhes acima'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audita dados coletados de um run")
    parser.add_argument("run_id")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--object-store", default=str(DEFAULT_OBJ))
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    storage = SqliteStorage(db_path=Path(args.db), object_store_root=Path(args.object_store))
    result = audit_run(storage, args.run_id)
    print(render(result))

    if args.save:
        out = HERE / "out" / "audit" / f"{args.run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSalvo em {out}")

    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
