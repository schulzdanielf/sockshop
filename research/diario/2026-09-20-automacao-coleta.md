# 2026-09-20 — Automação da coleta de runs (harness GAT + Istio)

A etapa manual de "coletar runs novos" foi automatizada reutilizando o harness
de avaliação existente (`experiment/eval/memory_hog/runner.py`), que já injeta
chaos, coleta via plataforma e grava ground truth.

## O que foi criado

- **`experiment/eval/gat/config_istio.yaml`** — config da campanha `gat-istio-v1`:
  - mesmas células da avaliação base (13 train + 10 test)
  - **prioriza fault types de rede** (onde edge features mais ajudam)
  - `strategies: []` — esta campanha é só para construir o corpus de grafos (o GAT é avaliado offline)
  - saída separada (`out/runs_istio.csv`) para não misturar com a avaliação LLM

- **`runner.py` estendido** — `build_spec` grava a tag `intensity:<valor>` no spec
  (função `cell_intensity`). **[REVERTIDO 2026-09-20]** — a tag não mudava o env do
  chaos, logo o split S4 não mediria nada. Removido por simplicidade.

## Validação

- **Dry-run** confirmou: tag `intensity:high` presente no spec.
- **Suíte: 149 testes passando** (incluindo os do harness memory_hog).

## Como executar a coleta (comandos)

```bash
source .venv/bin/activate

# 0) pré-requisitos: mesh ativo + netem + métricas OK
make chaos-enable-netem                                        # sch_netem p/ network-*
python -m experiment.eval.gat.validate_metrics --save          # deve dar READY

# 1) dry-run (inspecionar o primeiro spec)
python -m experiment.eval.memory_hog.runner --config experiment/eval/gat/config_istio.yaml --dry-run

# 2) coleta (fase chaos — injeta todas as células train+test)
python -m experiment.eval.memory_hog.runner --config experiment/eval/gat/config_istio.yaml --phase chaos

# 3) depois de coletar: regenerar dataset (mistura static antigos + full novos)
python -m experiment.eval.gat.build_dataset --save

# 4) treinar e comparar
python -m experiment.eval.gat.train --seeds 3 --save
python -m experiment.eval.gat.mlp_ablation --save
python -m experiment.eval.gat.evaluate --save
```

## Observações importantes

- **O engine já coleta Istio automaticamente** ao fim de cada run (integração de
  2026-09-20) — cada run novo terá `edge_source: full` sem passo extra.
- **Custo estimado:** 23 células × ~5–10 min/run ≈ 2–4 horas de máquina (podendo
  rodar à noite). `--retry-failed` retoma de onde parou.
- **Runs históricos** permanecem `edge_source: static` — o dataset mistura os dois
  e o `edge_source` por amostra permite medir o delta das edge features.
