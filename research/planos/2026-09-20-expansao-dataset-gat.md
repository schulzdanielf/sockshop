# 2026-09-20 — Plano: expansão do dataset GAT (mais runs full, sem vazamento)

**Motivação:** o re-teste da H001 com Istio ([diário](../diario/2026-09-20-gat-resultado-istio-full.md))
mostrou que o gargalo deixou de ser "edges estáticos" e virou **fome de dados
reais**: só 25/218 grafos são `edge_source: full` e o treino de S1 tinha **3
full**. S2 segue bloqueado (held-out orders/payment nunca visto como falha com
mesh). Esta campanha aumenta a base de grafos **full** sem misturar com o teste.

**Status:** planejado, não executado. Alvo: **≥ 120 grafos full adicionais**.

---

## 1. Por que a expansão NÃO mistura com o teste

Os splits são **recomputados por run** em
[build_dataset.py](../../../experiment/eval/gat/build_dataset.py) a partir do
`chaos_type` normalizado e do serviço — a atribuição não depende da config nem da
ordem de coleta:

- **S2 (held-out service):** qualquer run cujo `service ∈ {orders, payment}` é
  **sempre teste** em S2 (treino em S1/S3). Invariante imutável.
- **S3 (held-out fault):** qualquer run `pod-delete` é **sempre teste** em S3.
- **S1 (repetition):** dentro de cada célula (chaos × serviço), a réplica mais
  recente é teste; as demais, treino.

**Regra de ouro desta campanha:** toda nova célula usa `chaos_type ≠ pod-delete`
→ cai em **S3-train**; e expande principalmente serviços ≠ orders/payment →
**S2-train**. As novas células nunca são alvo de teste em S2/S3.

`is_training` (escrito pelo runner, backfill em
[backfill_is_training.py](../../../experiment/eval/memory_hog/backfill_is_training.py))
não afeta o split S1–S3 do GAT — serve só ao RAG. Não é fonte de vazamento aqui.

## 2. Expansão alvo por split (o que falta)

| split | hoje (train full) | gargalo | ação da campanha |
|---|---|---|---|
| **S1** | 3 full | quase nenhum full no treino | mais réplicas nas células expandidas (cada célula contribui réplicas de treino) |
| **S2** | 18 full (mas nunca orders/payment) | modelo nunca viu held-out falhar com mesh | **por desenho, não se treina held-out**; em vez disso, dar **diversidade** de (serviço, fault) full para o modelo aprender assinatura, não posição |
| **S3** | 18 full | pod-delete nunca no treino | adicionar faults não-pod-delete full em mais serviços |

## 3. Matriz de coleta proposta (train only)

Nova config [config_istio_expansion.yaml](../../../experiment/eval/gat/config_istio_expansion.yaml),
campanha `gat-istio-v2`, **`replicas: 5`**, `test_experiments` **vazio**
(train-only). **Todas as células usam faults ≠ pod-delete** (garantia S3-train)
e cobrem **todos os 8 serviços** em faults variados (diversidade S1/S2-train;
orders/payment aparecem aqui apenas como reforço de S1, pois continuam S2-test).

Faults selecionados (sem pod-delete, que é held-out): memory-hog, cpu-hog,
network-latency, network-loss, http-status-code, container-kill.

| serviço | faults (cada um × 5 réplicas) |
|---|---|
| catalogue | memory-hog, network-latency, http-status-code |
| carts | network-loss, http-status-code, container-kill |
| orders | cpu-hog, memory-hog, network-latency |
| payment | memory-hog, network-loss, http-status-code |
| user | http-status-code, container-kill, network-latency |
| shipping | network-latency, container-kill |
| queue-master | cpu-hog, memory-hog |
| front-end | cpu-hog, network-latency |

**Células:** 21 × 5 réplicas = **105 runs alvo** (decisão 2026-09-20: 95–105).
Com rendimento ~80%, espera-se **~84 grafos full** novos → total ~109 full.
A matriz enfatiza os faults mais fracos na auditoria (http-status-code,
container-kill, network-*) para dar mais exemplos onde o sinal é escasso.

> **Nota — não incluir pod-delete:** pod-delete é o held-out de S3; incluí-lo
> aqui o colocaria em S3-**test**, aumentando o teste e não o treino. Se quisermos
> mais S3-test full, podemos adicionar pod-delete como células separadas depois —
> decisão consciente, não acidental.

## 4. Salvaguardas anti-leakage (checklist de execução)

- [ ] **Antes de coletar:** `python -m experiment.eval.gat.validate_metrics --save` → READY.
- [ ] **`sch_netem` carregado** (`make chaos-enable-netem`) — network-* falha sem ele.
- [ ] **Mesh ativo:** sidecar istio-proxy 1/1 nos 8 pods de sock-shop.
- [ ] **Coletar com `is_training=true`** (fase train do runner) para não vazar no RAG.
- [ ] **Auditar cada run** (`audit_run`) antes de aceitar no corpus.
- [ ] **Depois de coletar:** `python -m experiment.eval.gat.build_dataset --save` e
  **conferir `dataset_report.json`**: os novos runs full devem cair em
  S1-train/S2-train/S3-train (nunca S2-test com serviço ≠ orders/payment, nunca
  S3-test com chaos ≠ pod-delete).
- [ ] Verificar que o **número de grafos de teste em S2/S3 não mudou** (teste
  permanece o mesmo conjunto held-out — a comparação com o resultado anterior
  continua válida).

## 5. Como evitar que static "engula" full no treino

Hoje o treino mistura 193 static + poucos full; as máscaras zeradas (static)
dominam e o modelo aprende a ignorar arestas. Opções (decidir após a expansão):

- **(a) Treino full-only:** treinar o GAT **só** nos grafos full. Com ~120 full
  dá para fazer train/val/test viável por split — **recomendado** como re-teste
  definitivo da H001.
- **(b) Oversample full:** pesar grafos full mais alto na loss.
- **(c) Curriculum:** pré-treinar em static (aprende node features), fine-tune em
  full (aprende a usar arestas).

A decisão (a/b/c) muda `train.py`; deixo para depois de termos o volume — sem
dados, qualquer uma é chute.

## 6. S2 — limitação honesta

S2 (held-out orders/payment) **por definição** nunca terá esses serviços como
falha no treino. Mais dados não removem isso; apenas dão **diversidade** para o
modelo talvez aprender a assinatura da falha em vez da posição. Se mesmo com
~120 full o S2-full continuar ≈ 0, o veredito "identidade de serviço held-out
quebra GAT" vira **conclusão estrutural da tese** (não falta de dado).

## 7. Comandos (resumo)

```bash
source .venv/bin/activate
# 1. gerar manifests + coletar (train only)
python -m experiment.eval.memory_hog.runner --config experiment/eval/gat/config_istio_expansion.yaml --phase train
# 2. rebuild do dataset + conferir splits
python -m experiment.eval.gat.build_dataset --save
# 3. re-treinar (GPU) e comparar
python -m experiment.eval.gat.train --seeds 5 --save
python -m experiment.eval.gat.mlp_ablation --save
```

## 8. Critérios de sucesso da expansão

1. **Volume:** ≥ 120 grafos full adicionais, com teste S2/S3 **inalterado**.
2. **Re-teste H001 (full-only):** GAT top-3 ≥ B1 + 10 p.p. em S2/S3 **ou**,
   se ainda < B1, H001 vira **refutada de forma definitiva** (não por falta de
   dado). Qualquer um dos dois é resultado cientificamente válido e publicável.
