#!/usr/bin/env python3
"""Treino e avaliação do GAT por split, comparado aos baselines.

Salvaguardas científicas (revisão de 2026-09-20):
* **Imputação + máscara** para dados faltantes (no ``model.graph_to_pyg``).
* **Seeds fixas**, múltiplas seeds → reporta média ± desvio (dataset pequeno).
* **Early stopping** em conjunto de validação separado do treino.
* **Mesmas métricas e bootstrap CI** dos baselines → comparação honesta.
* Artefatos versionados em ``out/gat/`` (config, métricas, comparação).

Uso
---
    python -m experiment.eval.gat.train --save
    python -m experiment.eval.gat.train --split S2 --seeds 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.eval.gat.baselines import BASELINES  # noqa: E402
from experiment.eval.gat.metrics import bootstrap_ci, evaluate_scorer  # noqa: E402
from experiment.eval.gat.model import (  # noqa: E402
    GATLocalizer,
    graph_to_pyg,
    set_seed,
)

SCHEMA_VERSION = "1.0"
SPLITS = ("S1", "S2", "S3")


def _load_dataset() -> List[Dict]:
    path = HERE / "out" / "dataset.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _split_graphs(graphs: List[Dict], split: str, which: str) -> List[Dict]:
    return [g for g in graphs if (g.get("splits") or {}).get(split) == which
            and (g.get("label") or {}).get("root_service_idx") is not None]


def _make_scorer(model: GATLocalizer):
    """Envolve o modelo treinado num scorer compatível com metrics.py."""
    model.eval()

    def scorer(graph: Dict) -> np.ndarray:
        data = graph_to_pyg(graph)
        with torch.no_grad():
            scores = model(data)
        return scores.cpu().numpy()

    return scorer


def _train_one(
    train_graphs: List[Dict],
    val_graphs: List[Dict],
    *,
    seed: int,
    epochs: int = 200,
    lr: float = 0.01,
    weight_decay: float = 5e-4,
    patience: int = 30,
) -> Tuple[GATLocalizer, Dict]:
    """Treina um GAT com early stopping na validação (top-3). Retorna (modelo, hist)."""
    set_seed(seed)
    model = GATLocalizer()
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)

    train_data = [graph_to_pyg(g) for g in train_graphs]

    best_val = -1.0
    best_state = None
    best_epoch = 0
    epochs_no_improve = 0
    history: List[Dict] = []

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        for data in train_data:
            if int(data.y) < 0:
                continue
            optimizer.zero_grad()
            scores = model(data).unsqueeze(0)  # [1, n_nodes]
            loss = F.cross_entropy(scores, data.y)
            loss.backward()
            optimizer.step()
            total_loss += float(loss)

        # validação: top-3
        val_scorer = _make_scorer(model)
        val_metrics = evaluate_scorer(val_graphs, val_scorer) if val_graphs else {"top3": float("nan")}
        val_top3 = val_metrics["top3"]
        history.append({"epoch": epoch, "train_loss": total_loss, "val_top3": val_top3})

        if val_graphs and val_top3 > best_val:
            best_val = val_top3
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            best_epoch = epoch
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    return model, {"best_epoch": best_epoch, "best_val_top3": best_val, "history": history}


def _stratified_val_split(train_graphs: List[Dict], val_frac: float, seed: int) -> Tuple[List[Dict], List[Dict]]:
    """Separa validação do treino, estratificando por serviço-alvo."""
    rng = np.random.default_rng(seed)
    by_label: Dict[int, List[Dict]] = {}
    for g in train_graphs:
        lbl = g["label"]["root_service_idx"]
        by_label.setdefault(lbl, []).append(g)
    train_out: List[Dict] = []
    val_out: List[Dict] = []
    for lbl, gs in by_label.items():
        gs = list(gs)
        rng.shuffle(gs)
        n_val = max(1, int(round(len(gs) * val_frac))) if len(gs) > 1 else 0
        val_out.extend(gs[:n_val])
        train_out.extend(gs[n_val:])
    return train_out, val_out


def train_split(
    graphs: List[Dict],
    split: str,
    *,
    seeds: List[int],
    val_frac: float = 0.2,
) -> Dict:
    """Treina o GAT no split (várias seeds) e avalia no teste vs baselines."""
    train_all = _split_graphs(graphs, split, "train")
    test = _split_graphs(graphs, split, "test")

    seed_results: List[Dict] = []
    for seed in seeds:
        tr, val = _stratified_val_split(train_all, val_frac, seed)
        model, hist = _train_one(tr, val, seed=seed)
        scorer = _make_scorer(model)
        t3, t3_lo, t3_hi = bootstrap_ci(test, scorer, metric="top3", seed=seed)
        t1, _, _ = bootstrap_ci(test, scorer, metric="top1", seed=seed)
        point = evaluate_scorer(test, scorer)
        seed_results.append({
            "seed": seed,
            "best_epoch": hist["best_epoch"],
            "best_val_top3": hist["best_val_top3"],
            "test_top3": t3,
            "test_top3_ci95": [t3_lo, t3_hi],
            "test_top1": point["top1"],
            "test_mrr": point["mrr"],
        })

    top3s = np.array([r["test_top3"] for r in seed_results])
    # baselines no mesmo teste
    baseline_cmp = {}
    for name, scorer in BASELINES.items():
        b3, b3_lo, b3_hi = bootstrap_ci(test, scorer, metric="top3")
        baseline_cmp[name] = {"top3": b3, "ci95": [b3_lo, b3_hi]}

    return {
        "split": split,
        "n_train": len(train_all),
        "n_test": len(test),
        "seeds": seeds,
        "gat_top3_mean": float(np.mean(top3s)),
        "gat_top3_std": float(np.std(top3s)),
        "gat_top3_per_seed": [float(x) for x in top3s],
        "seed_results": seed_results,
        "baselines": baseline_cmp,
    }


def render(results: Dict) -> str:
    lines = ["=" * 78, "GAT vs BASELINES — top-3 por split", "=" * 78]
    for split, r in results["splits"].items():
        lines.append(f"\n{split} (train={r['n_train']}, test={r['n_test']}):")
        lines.append(f"  GAT top-3: {r['gat_top3_mean']:.2f} ± {r['gat_top3_std']:.2f}  (seeds: {r['gat_top3_per_seed']})")
        for name, b in r["baselines"].items():
            lines.append(f"  {name:<22} top-3: {b['top3']:.2f} [{b['ci95'][0]:.2f},{b['ci95'][1]:.2f}]")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Treino do GAT por split")
    parser.add_argument("--split", default="all", choices=("all",) + SPLITS)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    graphs = _load_dataset()
    seeds = list(range(args.seeds))
    splits = SPLITS if args.split == "all" else (args.split,)

    results: Dict = {"schema_version": SCHEMA_VERSION, "seeds": seeds, "splits": {}}
    for split in splits:
        results["splits"][split] = train_split(graphs, split, seeds=seeds)

    print(render(results))

    if args.save:
        out_dir = HERE / "out" / "gat"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "gat_vs_baselines.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nSalvo em {out_dir}/gat_vs_baselines.json")


if __name__ == "__main__":
    main()
