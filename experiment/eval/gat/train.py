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
    FeatureStats,
    GATLocalizer,
    compute_feature_stats,
    get_device,
    graph_to_pyg,
    set_seed,
)
from experiment.eval.gat.simplified_model import SimplifiedGATLocalizer  # noqa: E402

SCHEMA_VERSION = "1.0"
SPLITS = ("S1", "S2", "S3")


def _load_dataset(path: Path | None = None) -> List[Dict]:
    path = path or HERE / "out" / "dataset.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _split_graphs(graphs: List[Dict], split: str, which: str) -> List[Dict]:
    return [g for g in graphs if (g.get("splits") or {}).get(split) == which
            and (g.get("label") or {}).get("root_service_idx") is not None]


def _make_scorer(
    model: torch.nn.Module,
    device: torch.device | None = None,
    cache: Dict[str, torch.Tensor] | None = None,
    stats: FeatureStats | None = None,
):
    """Envolve o modelo treinado num scorer compatível com metrics.py.

    Se ``cache`` for passado, os grafos convertidos para ``Data`` (já em
    ``device``) sao reaproveitados entre chamadas repetidas (val/test sao
    reavaliados a cada epoca/seed) em vez de reconverter+retransferir a
    cada chamada. ``stats`` (opcional): padronização z-score ajustada no
    treino (ver ``model.compute_feature_stats``), aplicada de forma
    consistente ao converter qualquer grafo (val/test incluídos).
    """
    model.eval()
    device = device or get_device()

    def scorer(graph: Dict) -> np.ndarray:
        if cache is not None:
            key = graph.get("run_id") or id(graph)
            data = cache.get(key)
            if data is None:
                data = graph_to_pyg(graph, stats=stats).to(device)
                cache[key] = data
        else:
            data = graph_to_pyg(graph, stats=stats).to(device)
        with torch.no_grad():
            scores = model(data)
        return scores.cpu().numpy()

    return scorer


def _val_loss(
    model: GATLocalizer,
    val_graphs: List[Dict],
    device: torch.device,
    cache: Dict[str, torch.Tensor],
    stats: FeatureStats | None,
) -> float:
    """Cross-entropy média nos grafos de validação — critério contínuo de
    early stopping. Com poucos grafos de validação (~15-30), o top-3 só
    assume múltiplos de ~1/n (ex.: 0.06 com n=17) — ruidoso demais para
    decidir quando parar. A loss é contínua e mais estável.
    """
    model.eval()
    total = 0.0
    n = 0
    with torch.no_grad():
        for g in val_graphs:
            key = g.get("run_id") or id(g)
            data = cache.get(key)
            if data is None:
                data = graph_to_pyg(g, stats=stats).to(device)
                cache[key] = data
            if int(data.y) < 0:
                continue
            scores = model(data).unsqueeze(0)
            loss = F.cross_entropy(scores, data.y)
            total += float(loss)
            n += 1
    return total / n if n else float("nan")


def _train_one(
    train_graphs: List[Dict],
    val_graphs: List[Dict],
    *,
    seed: int,
    device: torch.device | None = None,
    stats: FeatureStats | None = None,
    epochs: int = 200,
    lr: float = 0.01,
    weight_decay: float = 5e-4,
    patience: int = 30,
    model_class: type[torch.nn.Module] = GATLocalizer,
) -> Tuple[torch.nn.Module, Dict]:
    """Treina um GAT com early stopping na validação (val_loss). Retorna (modelo, hist)."""
    set_seed(seed)
    device = device or get_device()
    model = model_class().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)

    train_data = [graph_to_pyg(g, stats=stats).to(device) for g in train_graphs]
    # Cache dos grafos de validação convertidos: o mesmo val_graphs é
    # reavaliado a cada época (até `epochs` vezes) — sem cache, cada
    # chamada reconverte JSON->Data e retransfere para o device à toa.
    val_cache: Dict[str, torch.Tensor] = {}

    best_val_loss = float("inf")
    best_state = None
    best_epoch = 0
    best_val_top1 = float("nan")
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

        # Top-1 é a métrica principal; loss e top-3 permanecem no histórico.
        val_loss = _val_loss(model, val_graphs, device, val_cache, stats) if val_graphs else float("nan")
        val_scorer = _make_scorer(model, device, cache=val_cache, stats=stats)
        val_metrics = evaluate_scorer(val_graphs, val_scorer) if val_graphs else {
            "top1": float("nan"),
            "top3": float("nan"),
        }
        val_top1 = val_metrics["top1"]
        val_top3 = val_metrics["top3"]
        history.append({
            "epoch": epoch,
            "train_loss": total_loss,
            "val_loss": val_loss,
            "val_top1": val_top1,
            "val_top3": val_top3,
        })

        if val_graphs and val_top1 > best_val_top1:
            best_val_loss = val_loss
            best_val_top1 = val_top1
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            best_epoch = epoch
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    return model, {
        "best_epoch": best_epoch,
        "best_val_loss": best_val_loss,
        "best_val_top1": best_val_top1,
        "history": history,
    }


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
    device: torch.device | None = None,
    model_class: type[torch.nn.Module] = GATLocalizer,
    edge_direction: str = "bidirectional",
) -> Dict:
    """Treina o GAT no split (várias seeds) e avalia no teste vs baselines."""
    device = device or get_device()
    train_all = _split_graphs(graphs, split, "train")
    test = _split_graphs(graphs, split, "test")
    train_all = [_with_edge_direction(g, edge_direction) for g in train_all]
    test = [_with_edge_direction(g, edge_direction) for g in test]
    # Padronização (z-score) ajustada só no pool de treino do split (nunca
    # no teste) — reaplicada de forma idêntica em val/test/treino.
    stats = compute_feature_stats(train_all)
    # Cache dos grafos de teste: reused por todas as seeds (mesmo test set).
    test_cache: Dict[str, torch.Tensor] = {}

    seed_results: List[Dict] = []
    for seed in seeds:
        tr, val = _stratified_val_split(train_all, val_frac, seed)
        model, hist = _train_one(
            tr,
            val,
            seed=seed,
            device=device,
            stats=stats,
            model_class=model_class,
        )
        scorer = _make_scorer(model, device, cache=test_cache, stats=stats)
        t1, t1_lo, t1_hi = bootstrap_ci(test, scorer, metric="top1", seed=seed)
        t3, t3_lo, t3_hi = bootstrap_ci(test, scorer, metric="top3", seed=seed)
        point = evaluate_scorer(test, scorer)
        seed_results.append({
            "seed": seed,
            "best_epoch": hist["best_epoch"],
            "best_val_top1": hist["best_val_top1"],
            "test_top1": t1,
            "test_top1_ci95": [t1_lo, t1_hi],
            "test_top3": t3,
            "test_top3_ci95": [t3_lo, t3_hi],
            "test_top1_point": point["top1"],
            "test_mrr": point["mrr"],
        })

    top1s = np.array([r["test_top1"] for r in seed_results])
    top3s = np.array([r["test_top3"] for r in seed_results])
    # baselines no mesmo teste
    baseline_cmp = {}
    for name, scorer in BASELINES.items():
        b1, b1_lo, b1_hi = bootstrap_ci(test, scorer, metric="top1")
        b3, b3_lo, b3_hi = bootstrap_ci(test, scorer, metric="top3")
        baseline_cmp[name] = {
            "top1": b1,
            "top1_ci95": [b1_lo, b1_hi],
            "top3": b3,
            "top3_ci95": [b3_lo, b3_hi],
        }

    return {
        "split": split,
        "n_train": len(train_all),
        "n_test": len(test),
        "seeds": seeds,
        "gat_top1_mean": float(np.mean(top1s)),
        "gat_top1_std": float(np.std(top1s)),
        "gat_top1_per_seed": [float(x) for x in top1s],
        "gat_top3_mean": float(np.mean(top3s)),
        "gat_top3_std": float(np.std(top3s)),
        "gat_top3_per_seed": [float(x) for x in top3s],
        "seed_results": seed_results,
        "baselines": baseline_cmp,
    }


def render(results: Dict) -> str:
    lines = ["=" * 78, "GAT vs BASELINES — top-1 por split", "=" * 78]
    for split, r in results["splits"].items():
        lines.append(f"\n{split} (train={r['n_train']}, test={r['n_test']}):")
        lines.append(f"  GAT top-1: {r['gat_top1_mean']:.2f} ± {r['gat_top1_std']:.2f}  (seeds: {r['gat_top1_per_seed']})")
        for name, b in r["baselines"].items():
            lines.append(f"  {name:<22} top-1: {b['top1']:.2f} [{b['top1_ci95'][0]:.2f},{b['top1_ci95'][1]:.2f}]")
    return "\n".join(lines)


def _with_edge_direction(graph: Dict, edge_direction: str) -> Dict:
    """Retorna uma cópia com a orientação escolhida, sem mutar o dataset."""
    if edge_direction == "bidirectional":
        return graph
    expected = "real" if edge_direction == "caller-to-callee" else "reverse"
    copy = dict(graph)
    copy["edges"] = [
        edge for edge in graph.get("edges", []) if edge.get("direction") == expected
    ]
    return copy


def main() -> None:
    parser = argparse.ArgumentParser(description="Treino do GAT por split")
    parser.add_argument("--split", default="all", choices=("all",) + SPLITS)
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--save", action="store_true")
    parser.add_argument(
        "--device", default=None, choices=("cpu", "cuda"),
        help="Forca o device de treino. Default: auto (CUDA se disponivel).",
    )
    parser.add_argument(
        "--model", default="legacy", choices=("legacy", "simplified"),
        help="Arquitetura GAT: legacy (2 camadas) ou simplified (1 camada residual).",
    )
    parser.add_argument(
        "--edge-direction", default="bidirectional",
        choices=("bidirectional", "caller-to-callee", "callee-to-caller"),
        help="Orientação das arestas usada na avaliação.",
    )
    parser.add_argument(
        "--dataset", type=Path, default=None,
        help="JSONL alternativo, por exemplo dataset.clean.jsonl do quality_filter.",
    )
    args = parser.parse_args()

    graphs = _load_dataset(args.dataset)
    seeds = list(range(args.seeds))
    splits = SPLITS if args.split == "all" else (args.split,)

    device = torch.device(args.device) if args.device else get_device()
    print(f"Device: {device}")
    model_class = SimplifiedGATLocalizer if args.model == "simplified" else GATLocalizer

    results: Dict = {
        "schema_version": SCHEMA_VERSION,
        "seeds": seeds,
        "device": str(device),
        "model": args.model,
        "edge_direction": args.edge_direction,
        "splits": {},
    }
    for split in splits:
        results["splits"][split] = train_split(
            graphs,
            split,
            seeds=seeds,
            device=device,
            model_class=model_class,
            edge_direction=args.edge_direction,
        )

    print(render(results))

    if args.save:
        out_dir = HERE / "out" / "gat"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Splits individuais (para rodar em paralelo, um processo por split)
        # gravam num arquivo próprio — evita que processos concorrentes
        # sobrescrevam o gat_vs_baselines.json final uns dos outros. Use
        # `merge_split_results.py` para combinar depois.
        suffix = "" if args.model == "legacy" and args.edge_direction == "bidirectional" else f".{args.model}.{args.edge_direction}"
        if args.split == "all":
            out_path = out_dir / f"gat_vs_baselines{suffix}.json"
        else:
            out_path = out_dir / f"gat_vs_baselines{suffix}.{args.split}.json"
        out_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nSalvo em {out_path}")


if __name__ == "__main__":
    main()
