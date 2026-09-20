#!/usr/bin/env python3
"""Avaliação dos baselines por split (S1–S3) com IC 95% via bootstrap.

Estabelece o **piso científico** antes do GAT: para cada split e cada
baseline, reporta top-3 (primária), top-1 e MRR com intervalo de confiança.
Um modelo só tem valor se superar B1 (max-anomaly) além dos ICs.

Saída: ``out/baselines_report.json`` + tabela no stdout.

Uso
---
    python -m experiment.eval.gat.evaluate --save
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.eval.gat.baselines import BASELINES  # noqa: E402
from experiment.eval.gat.metrics import bootstrap_ci, evaluate_scorer  # noqa: E402

SCHEMA_VERSION = "1.0"
SPLITS = ("S1", "S2", "S3")


def _load_dataset() -> List[Dict]:
    path = HERE / "out" / "dataset.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _split_test_graphs(graphs: List[Dict], split: str) -> List[Dict]:
    """Grafos marcados como teste no split dado."""
    return [g for g in graphs if (g.get("splits") or {}).get(split) == "test"]


def evaluate_baselines(graphs: List[Dict]) -> Dict:
    results: Dict = {"schema_version": SCHEMA_VERSION, "splits": {}}
    for split in SPLITS:
        test = _split_test_graphs(graphs, split)
        split_res: Dict = {"n_test": len(test), "baselines": {}}
        for name, scorer in BASELINES.items():
            point_metrics = evaluate_scorer(test, scorer)
            t3, t3_lo, t3_hi = bootstrap_ci(test, scorer, metric="top3")
            t1, t1_lo, t1_hi = bootstrap_ci(test, scorer, metric="top1")
            split_res["baselines"][name] = {
                "n": point_metrics["n"],
                "top3": {"point": t3, "ci95": [t3_lo, t3_hi]},
                "top1": {"point": t1, "ci95": [t1_lo, t1_hi]},
                "mrr": point_metrics["mrr"],
            }
        results["splits"][split] = split_res
    return results


def render(results: Dict) -> str:
    lines = ["=" * 78, "BASELINES — piso científico (top-3 primária, IC 95%)", "=" * 78]
    for split, sr in results["splits"].items():
        lines.append(f"\n{split} (n_test={sr['n_test']}):")
        lines.append(f"  {'baseline':<22} {'top3 [IC95]':<26} {'top1':<10} mrr")
        for name, m in sr["baselines"].items():
            t3 = m["top3"]
            t1 = m["top1"]
            lines.append(
                f"  {name:<22} "
                f"{t3['point']:.2f} [{t3['ci95'][0]:.2f},{t3['ci95'][1]:.2f}]   "
                f"{t1['point']:.2f}       {m['mrr']:.3f}"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Avalia baselines por split")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    graphs = _load_dataset()
    results = evaluate_baselines(graphs)
    print(render(results))

    if args.save:
        out = HERE / "out" / "baselines_report.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSalvo em {out}")


if __name__ == "__main__":
    main()
