#!/usr/bin/env python3
"""Merge dos resultados de treino do GAT rodados em paralelo por split.

Quando ``train.py --split S1/S2/S3 --save`` roda em processos separados
(para paralelizar treino em CPU/GPU sem tocar na lógica de treino), cada
processo grava ``gat_vs_baselines.<split>.json`` em vez do arquivo final
compartilhado (evita que um processo sobrescreva o resultado do outro).
Este script combina os arquivos por split no ``gat_vs_baselines.json``
final, no mesmo schema que ``train.py --split all`` produziria.

Uso
---
    python -m experiment.eval.gat.merge_split_results
    python -m experiment.eval.gat.merge_split_results --splits S1 S2 S3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPLITS_DEFAULT = ("S1", "S2", "S3")


def merge(out_dir: Path, splits: Sequence[str] = SPLITS_DEFAULT) -> Dict:
    """Le um ``gat_vs_baselines.<split>.json`` por split e combina num único dict.

    Valida que todos os arquivos usam as mesmas seeds (senão a comparação
    entre splits não é honesta) e que cada arquivo de fato contém o split
    esperado.
    """
    merged: Dict | None = None
    for split in splits:
        path = out_dir / f"gat_vs_baselines.{split}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Falta {path} — rode `train.py --split {split} --save` antes de mergear."
            )
        data = json.loads(path.read_text(encoding="utf-8"))

        if split not in (data.get("splits") or {}):
            raise ValueError(
                f"{path} não contém o split {split} (splits presentes: {list((data.get('splits') or {}).keys())})"
            )

        if merged is None:
            merged = {
                "schema_version": data["schema_version"],
                "seeds": data["seeds"],
                "device": data["device"],
                "splits": {},
            }
        else:
            if data["seeds"] != merged["seeds"]:
                raise ValueError(
                    f"seeds inconsistentes entre splits: {split} tem {data['seeds']}, "
                    f"esperado {merged['seeds']} (comparação entre splits deixa de ser honesta)"
                )
            if data["device"] != merged["device"]:
                # Splits rodados em devices diferentes (ex.: um em cpu, outro
                # em cuda) — registra os dois em vez de mascarar a diferença.
                devices = set(str(merged["device"]).split("+")) | {str(data["device"])}
                merged["device"] = "+".join(sorted(devices))

        merged["splits"][split] = data["splits"][split]

    assert merged is not None
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge dos resultados de treino do GAT rodados em paralelo por split"
    )
    parser.add_argument("--splits", nargs="+", default=list(SPLITS_DEFAULT))
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else HERE / "out" / "gat"
    merged = merge(out_dir, tuple(args.splits))

    out_path = out_dir / "gat_vs_baselines.json"
    out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Merged {list(merged['splits'].keys())} -> {out_path}")

    from experiment.eval.gat.train import render  # noqa: E402 (reusa a formatação de train.py)

    print(render(merged))


if __name__ == "__main__":
    main()
