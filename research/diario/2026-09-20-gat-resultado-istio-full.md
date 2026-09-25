# 2026-09-20 — Re-teste H001 com edge features reais (Istio): NÃO SUPORTADA

Depois da refutação com topologia estática (ver
[2026-09-20-gat-resultado.md](2026-09-20-gat-resultado.md)), ficou em aberto se
**edge features reais** (Istio mesh, arestas com telemetria A→B) mudariam o
resultado. Coletamos 24 runs válidos com Istio (`edge_source: full`), regeneramos
o dataset e re-treinamos o GAT (GPU, 3 seeds). **Resultado: H001 continua não
suportada.**

## Dataset

218 grafos totais: **193 static** (runs históricos sem mesh) + **25 full**
(runs novos com Istio). Distribuição por split — o gargalo ficou evidente:

| split | train | test | composição |
|---|---|---|---|
| S1 | 196 (193 static + **3 full**) | 22 (**22 full**) | teste 100% full, treino 98% static |
| S2 | 153 (135 static + 18 full) | 65 (58 static + 7 full) | treino nunca viu orders/payment |
| S3 | 160 (142 static + 18 full) | 58 (51 static + 7 full) | teste pod-delete mostly static |

## Resultado oficial (top-3, 3 seeds, GPU)

| Split | GAT (mean±std) | B0 | B1 max-anomaly | B2 difusão |
|---|---|---|---|---|
| S1 (22) | 0.48 ± 0.14 | 0.23 | 0.45 | 0.41 |
| S2 (65) | 0.24 ± 0.07 | 0.45 | **0.58** | 0.52 |
| S3 (58) | 0.40 ± 0.03 | 0.36 | **0.47** | 0.33 |

Abiação MLP (sem grafo, mesmas node features): **S1 0.61 / S2 0.56 / S3 0.63**
— MLP ≥ GAT em **todos** os splits. Message passing continua subtraindo valor.

## Decomposição por edge_source (seed 0, rerun) — a parte mais informativa

| split | subset | n | GAT | B1 |
|---|---|---|---|---|
| S1 | full (todo o teste) | 22 | **0.59** | 0.45 |
| S2 | full | 7 | 0.00 | 0.43 |
| S2 | static | 58 | 0.22 | 0.60 |
| S3 | full | 7 | 0.14 | 0.57 |
| S3 | static | 51 | 0.75 | 0.45 |

## Interpretação: 4 causas do resultado baixo

1. **Fome de dados reais (principal).** Só 25 grafos full; no treino de S1 há
   **3 full contra 193 static**. O GAT quase nunca vê edge features reais no
   aprendizado; as máscaras zeradas (static) dominam e o modelo aprende a ignorar
   as arestas. O re-teste exercitou edges reais no **teste**, não no **treino**.
2. **S2 segue estruturalmente bloqueado.** Held-out de orders/payment colapsa o
   GAT (0.24, abaixo do acaso 0.375) — **mesmo nos 7 grafos full (0.00)**. O
   mecanismo da refutação anterior persiste: o modelo aprende por identidade de
   serviço; serviços nunca falhos no treino quebram a generalização. Edge
   features reais **não** salvaram S2.
3. **MLP ≥ GAT em tudo.** Confirma que a estrutura (estática + esparsa de edges
   reais) é o problema, não o aprendizado.
4. **Amostras minúsculas + não-determinismo CUDA.** S2/S3-full têm n=7 (1 grafo =
   14 p.p.). GATv2 tem scatter não-determinístico em CUDA: o mesmo seed 0 deu
   0.95 (smoke), 0.68 (oficial), 0.59 (rerun) em S1 — jitter ±10 p.p. Tratar
   números individuais com cautela.

## Sinais positivos (honestos)

- **S1-full: GAT 0.59 > B1 0.45.** Nos grafos com edges reais, o GAT superou o
  melhor baseline — primeira evidência a favor, ainda que fraca (n=22, variância).
- **S3-static: GAT 0.75 > B1 0.45.** pod-delete em serviços vistos é aprendível.

## Veredito

**H001 NÃO SUPORTADA nesta configuração** — o gargalo mudou de "edges são
estáticos" para **(a) dados reais insuficientes (25)** + **(b) generalização S2
estruturalmente bloqueada**. A pergunta "edges reais ajudam?" tem resposta fraca
e positiva em S1 (0.59 vs 0.45), mas é indeterminada por falta de dados de treino
com mesh. O caminho para uma resposta definitiva está no plano de expansão:
[research/planos/2026-09-20-expansao-dataset-gat.md](../planos/2026-09-20-expansao-dataset-gat.md).

## Reprodutibilidade

```
source .venv/bin/activate
python -m experiment.eval.gat.build_dataset --save   # regenera dataset (218)
python -m experiment.eval.gat.train --seeds 3 --save # GAT (GPU, fallback CPU)
python -m experiment.eval.gat.mlp_ablation --save    # ablação MLP
python -m experiment.eval.gat.evaluate --save        # baselines
```

Artefatos: `experiment/eval/gat/out/gat/gat_vs_baselines.json`,
`mlp_ablation.json`, `out/baselines_report.json`.
