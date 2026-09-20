#!/usr/bin/env python3
"""Ablação: MLP por-nó (sem grafo) vs GAT — isola o efeito da topologia.

Pergunta científica: a topologia estática ajuda ou atrapalha? O MLP usa as
**mesmas node features** do GAT, mas **sem message passing e sem noção de
posição** (pesos compartilhados entre nós). Se o MLP ≥ GAT, a estrutura
estática é o problema, não o aprendizado.

Resultado (2026-09-20): MLP >> GAT em S2 e S3 → topologia estática é
prejudicial (injeta viés de identidade de nó que quebra a generalização).

Uso
---
    python -m experiment.eval.gat.mlp_ablation --save
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.eval.gat.baselines import BASELINES  # noqa: E402
from experiment.eval.gat.metrics import bootstrap_ci, evaluate_scorer  # noqa: E402
from experiment.eval.gat.model import NODE_DIM, graph_to_pyg, set_seed  # noqa: E402

SCHEMA_VERSION = "1.0"
SPLITS = ("S1", "S2", "S3")


class NodeMLP(nn.Module):
    """MLP por-nó: pesos compartilhados, sem topologia nem posição."""

    def __init__(self, d: int = NODE_DIM, h: int = 16, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d, h), nn.ReLU(), nn.Dropout(dropout), nn.Linear(h, 1)
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def _load_dataset() -> List[Dict]:
    path = HERE / "out" / "dataset.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _split_graphs(graphs: List[Dict], split: str, which: str) -> List[Dict]:
    return [g for g in graphs if (g.get("splits") or {}).get(split) == which
            and (g.get("label") or {}).get("root_service_idx") is not None]


def _make_scorer(model: NodeMLP):
    model.eval()

    def scorer(graph: Dict) -> np.ndarray:
        data = graph_to_pyg(graph)
        with torch.no_grad():
            return model(data.x).cpu().numpy()

    return scorer


def _train_mlp(train_graphs: List[Dict], *, seed: int, epochs: int = 150, lr: float = 0.01, weight_decay: float = 5e-4) -> NodeMLP:
    set_seed(seed)
    model = NodeMLP()
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    data = [graph_to_pyg(g) for g in train_graphs]
    for _ in range(epochs):
        model.train()
        for d in data:
            if int(d.y) < 0:
                continue
            opt.zero_grad()
            scores = model(d.x).unsqueeze(0)
            loss = F.cross_entropy(scores, d.y)
            loss.backward()
            opt.step()
    return model


def run_ablation(graphs: List[Dict], seeds: List[int]) -> Dict:
    results: Dict = {"schema_version": SCHEMA_VERSION, "seeds": seeds, "splits": {}}
    for split in SPLITS:
        tr = _split_graphs(graphs, split, "train")
        te = _split_graphs(graphs, split, "test")
        top3s = []
        for seed in seeds:
            model = _train_mlp(tr, seed=seed)
            top3s.append(evaluate_scorer(te, _make_scorer(model))["top3"])
        top3s = np.array(top3s)
        b1 = bootstrap_ci(te, BASELINES["B1_max_anomaly"], metric="top3")
        results["splits"][split] = {
            "n_train": len(tr),
            "n_test": len(te),
            "mlp_top3_mean": float(np.mean(top3s)),
            "mlp_top3_std": float(np.std(top3s)),
            "mlp_top3_per_seed": [float(x) for x in top3s],
            "b1_top3": {"point": b1[0], "ci95": [b1[1], b1[2]]},
        }
    return results


def render(results: Dict) -> str:
    lines = ["=" * 78, "ABLAÇÃO — MLP por-nó (sem grafo) vs B1", "=" * 78]
    for split, r in results["splits"].items():
        lines.append(
            f"\n{split} (train={r['n_train']}, test={r['n_test']}): "
            f"MLP top-3 = {r['mlp_top3_mean']:.2f} ± {r['mlp_top3_std']:.2f}  |  "
            f"B1 = {r['b1_top3']['point']:.2f} "
            f"[{r['b1_top3']['ci95'][0]:.2f},{r['b1_top3']['ci95'][1]:.2f}]"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ablação MLP por-nó")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    graphs = _load_dataset()
    results = run_ablation(graphs, list(range(args.seeds)))
    print(render(results))

    if args.save:
        out = HERE / "out" / "gat" / "mlp_ablation.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSalvo em {out}")


if __name__ == "__main__":
    main()
