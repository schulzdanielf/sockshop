# Plano de treinamento e testes — GAT com edge features reais (Istio)

> **Criado em:** 2026-09-20 · **Branch:** `hibrido` · **Hipótese:** [H001](../hipoteses/H001-gat-localizacao-falhas.md)
> Plano cientificamente revisado: análise de poder, desenho do experimento, garantias e validações.

---

## 1. Análise de poder estatístico (a verdade incômoda, dita antes)

Cálculo de poder para detectar diferença de **+10 p.p. em top-3 accuracy** (o
critério de validação de H001), teste de duas proporções, α=0.05, poder alvo 0.8:

| Split | n_test atual | Poder para +10pp | n_test necessário (poder 0.8) |
|---|---|---|---|
| S1 (repetição) | 22 | **0.10** | 388 |
| S2 (held-out serviço) | 58 | **0.19** | 388 |
| S3 (held-out fault) | 50 | **0.17** | 388 |

**Conclusão dura e honesta:** com os n atuais, o experimento **não tem poder para
detectar +10 p.p.** — mesmo que o GAT melhore de verdade, não conseguiríamos
distinguir de ruído. E 388 runs/célula de teste é **inviável** (cada run ~5–10 min →
dezenas de horas de máquina, e o cluster local não aguenta essa escala com folga).

### O que isso muda no desenho (decisão científica)

Não vamos perseguir "+10 p.p. com poder 0.8" — é inalcançável no ambiente. Em vez
disso, três estratégias honestas:

1. **Critério de efeito maior.** Detectar +15 p.p. exige n=170/grupo; ainda grande,
   mas se o efeito real do GAT for grande (o que o MLP sugeriu no S3=0.86), um efeito
   grande é detectável com menos dados. Reportamos o **tamanho do efeito observado
   com IC**, não um sim/não binário.
2. **Repetições por célula (S1) como proxy de variância.** S1 mede reprodutibilidade;
   a variância entre réplicas da mesma célula informa o teto realista de acurácia.
3. **Estudo exploratório com IC reportado.** Aceitamos ICs largos, reportamos o efeito
   pontual + IC 95% e **não alegamos significância** que o n não sustenta. A conclusão
   forte vem da *direção e magnitude* + ablação, não de um teste de hipótese rígido.

> Esta é a postura correta: melhor um resultado exploratório honesto do que um teste
> de hipótese subalimentado que parece definitivo.

---

## 2. Desenho do experimento (treino/teste)

### 2.1 O que está sendo testado

**Pergunta:** com edge features reais (Istio), um GAT supera os baselines e o MLP na
localização do serviço raiz?

**Comparações (todas no mesmo split de teste, mesma régua):**

| Modelo | O que usa | Papel |
|---|---|---|
| **B0** random | nada | piso absoluto |
| **B1** max-anomaly | node features (max \|log-ratio\|) | heurística sem grafo — bar real |
| **B2** anomaly+difusão | node + topologia (difusão) | testa se estrutura ajuda (hoje: não) |
| **MLP** por-nó | node features, sem grafo/posição | ablação: prova se o grafo agrega |
| **GAT-static** | node + topologia estática, arestas neutras | a abordagem refutada (controle) |
| **GAT-full** | node + topologia + **edge features reais** | a hipótese testada |

A comparação-chave é **GAT-full vs GAT-static** (isola o efeito das edge features) e
**GAT-full vs MLP** (isola o efeito da estrutura do grafo).

### 2.2 Tipos de falha e nós (matriz)

Reaproveitamos o catálogo de chaos existente (7 fault types validados no Litmus):

| Categoria | Chaos type | Manifestação nas golden metrics |
|---|---|---|
| Recurso | `cpu-hog` | latência ↑ (CPU-bound), erro pode não subir |
| Recurso | `memory-hog` | latência ↑, possível OOM → erro ↑ |
| Disponibilidade | `pod-delete` | erro ↑ breve + latência spike (transiente) |
| Disponibilidade | `container-kill` | idem (transiente) |
| Rede | `network-latency` | latência ↑ na aresta afetada |
| Rede | `network-loss` | erro ↑ + latência ↑ na aresta |
| Aplicação | `http-status-code` | erro ↑ direto (5xx) |

**Nós-alvo:** os 8 core services (`front-end, catalogue, carts, orders, payment,
shipping, user, queue-master`). As falhas de rede são as que mais se beneficiam das
edge features reais — o efeito esperado do GAT-full é maior aí.

### 2.3 Splits (anti-leakage, já implementados)

| Split | Treino | Teste | Mede |
|---|---|---|---|
| **S1** repetição | réplicas antigas da célula | réplica mais recente | reprodutibilidade (teto) |
| **S2** held-out serviço | tudo exceto orders/payment | orders, payment | serviço novo, fault conhecido |
| **S3** held-out fault | tudo exceto pod-delete | pod-delete | fault novo, serviços conhecidos |
| ~~**S4** held-out intensity~~ | — | — | **removido** (decisão 2026-09-20) |

> **Nota S4 — removido:** a tag `intensity` não mudava o `env` do chaos (era só
> rótulo; `memory-hog` rodava com os mesmos 500MB em todas as células), logo S4
> não mediria nada. O experimento foca em S1/S2/S3 (repetição, serviço novo,
> fault novo) — que já medem generalização de forma forte. Se no futuro a
> intensidade importar, os manifests de chaos precisam aceitar o parâmetro de
> verdade (ex.: cpu-hog cores, memory-hog MB).

---

## 3. Garantias, validações e verificações (o que não pode falhar em silêncio)

Esta é a seção que atende ao seu pedido de "não ficar no escuro". Cada item tem um
mecanismo concreto.

### 3.1 Antes de coletar (gates de entrada)

| Garantia | Mecanismo | Falha se |
|---|---|---|
| Métricas disponíveis | `validate_metrics.py` → `validation_check.json` (exit 1 se faltar) | node <8/8 ou edge <7/7 |
| Mesh saudável | `istiod` 1/1 + pods com sidecar Running | algum pod sem istio-proxy |
| Ground truth presente | tags `target:`/`fault_category:` no spec | run sem label |

### 3.2 Durante a coleta (checklist por run — já implementado)

Gate runner (`backend/quality/`) roda por run e produz `run_quality.json` +
`run_health.json`. Gates que bloqueiam runs ruins:

| Gate | O que valida |
|---|---|
| `collection_completeness` | artefatos existem E têm conteúdo (ex.: traces não vazios) |
| `window_sanity` | fases com timestamps coerentes, pontos suficientes |
| `label_consistency` | ground truth existe e o serviço-alvo é observado |
| `graph_connectivity` | grafo tem nós/arestas mínimos e contém o alvo |
| `feature_sanity` | sem NaN/Inf nas features |
| `symptom_presence` | (informativo) sinal mensurável presente |

**Novo gate necessário para o mesh:** `edge_features_present` — para runs com
`edge_source` esperado = full, validar que ao menos 1 aresta tem feature não-nula.
*A adicionar antes de coletar.*

### 3.3 No dataset (integridade)

| Garantia | Mecanismo |
|---|---|
| Anti-leakage S2/S3 | validado: 0 runs held-out no treino |
| Normalização chaos_type | `orders-container-kill` → `container-kill` |
| Janela efetiva (C3) | transientes recortadas (cap 180s) |
| Sem IDs nos tensores | features sem nome de serviço (S2 honesto) |
| Imputação + máscara | None→0 + máscara binária (distingue "sem dado" de "sem mudança") |

### 3.4 No treino (rigor estatístico)

- **Seeds fixas + múltiplas seeds** (≥3) → reporta média ± desvio
- **Early stopping** em validação estratificada (separada do treino)
- **Bootstrap IC 95%** em todas as métricas (obrigatório com n pequeno)
- **Reportar todos os splits** — sem cherry-picking
- **Análise de erros estruturada** (`errors.jsonl`): cada erro com run_id, top-3 + scores, label — padrões consultáveis, não anedóticos
- **Explainability:** pesos de atenção do GAT exportados por run de teste

### 3.5 Critérios de sucesso/fracasso (definidos antes)

- **H001 validada (exploratório):** GAT-full > GAT-static E > B1, com efeito
  consistente nas seeds e ICs que não cruzam B1 — **sem alegação de significância
  estatística rígida** (n não sustenta).
- **H001 refutada:** GAT-full ≈ GAT-static (edge features não ajudam) ou < B1.
- **Mecanismo entendido:** se falhar, a análise de erros + atenção dizem por quê.

---

## 4. Passo a passo executável

```bash
source .venv/bin/activate

# 0) gate de entrada — só prossiga se READY
python -m experiment.eval.gat.validate_metrics --window-minutes 5 --save

# 1) coletar runs novos com mesh ativo (registrar intensity no chaos_profile!)
#    → cada run vira edge_source: full

# 2) regenerar dataset (mistura static antigos + full novos; edge_source por amostra)
python -m experiment.eval.gat.build_dataset --save

# 3) treinar e avaliar (seeds, vs baselines)
python -m experiment.eval.gat.train --seeds 3 --save
python -m experiment.eval.gat.mlp_ablation --save
python -m experiment.eval.gat.evaluate --save

# 4) comparar GAT-full vs GAT-static vs MLP vs B1 por split, com IC 95%
```

## 5. Riscos registrados (honestidade)

1. **Poder estatístico baixo** — mitigado pela postura exploratória (efeito + IC, não sim/não).
2. **Vazamento topológico** — topologia ainda é a mesma estrutura; as edge features variam, mas a estrutura não. S2 continua o teste crítico.
3. **queue-master assíncrono** — golden metrics HTTP não se aplicam; com Istio, a aresta shipping→queue-master existe, mas o nó em si pode continuar cego.
4. **Runs novos custam tempo** — priorizar células fracas e fault types de rede (onde edge features mais ajudam).
