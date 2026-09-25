# Relatório pós-correção GAT/MLP — métricas Istio, S3 corrigido e queue-master removido

Data: 2026-09-22

## 1. Contexto

Este relatório atualiza a análise depois das correções aplicadas no pipeline GAT:

- Métricas HTTP/latência/tráfego passaram a usar Istio/Envoy por `destination_workload`.
- Foi feito backfill histórico parcial via Prometheus PVC.
- `queue-master` foi removido como serviço-alvo do experimento.
- S3 deixou de usar `pod-delete` inexistente e passou a segurar `container-kill`.
- `http-status-code` vindo como path YAML foi normalizado para `http-status-code`.

Artefatos principais:

- Dataset atual: [experiment/eval/gat/out/dataset.jsonl](../../experiment/eval/gat/out/dataset.jsonl)
- Resultado GAT: [experiment/eval/gat/out/gat/gat_vs_baselines.json](../../experiment/eval/gat/out/gat/gat_vs_baselines.json)
- Resultado MLP: [experiment/eval/gat/out/gat/mlp_ablation.json](../../experiment/eval/gat/out/gat/mlp_ablation.json)
- Auditoria por run: [experiment/eval/gat/out/gat/gat_postfix_error_audit.json](../../experiment/eval/gat/out/gat/gat_postfix_error_audit.json)
- Tabela por run em CSV: [experiment/eval/gat/out/gat/gat_postfix_error_audit_runs.csv](../../experiment/eval/gat/out/gat/gat_postfix_error_audit_runs.csv)

Observação: a auditoria por run retreina o GAT para recuperar ranks por run. Como GATv2 em CUDA tem alguma não determinismo, os ranks por run devem ser lidos como diagnóstico local; os números oficiais agregados são os de `gat_vs_baselines.json`.

---

## 2. Estado do dataset corrigido

Dataset atual:

| Item | Valor |
|---|---:|
| Grafos totais | 105 |
| Serviços-alvo `queue-master` | 0 |
| `edge_source=full` | 105 |
| S1 | train=86, test=19 |
| S2 | train=75, test=30 |
| S3 | train=86, test=19 |
| S3 test | 19 `container-kill` |

Distribuição geral por falha:

| Falha | Runs |
|---|---:|
| network-latency | 28 |
| http-status-code | 22 |
| container-kill | 19 |
| memory-hog | 16 |
| network-loss | 10 |
| cpu-hog | 10 |

Distribuição geral por alvo:

| Serviço | Runs |
|---|---:|
| user | 25 |
| catalogue | 16 |
| carts | 15 |
| orders | 15 |
| payment | 15 |
| front-end | 10 |
| shipping | 9 |

Backfill Prometheus/Istio:

| Grupo | Runs |
|---|---:|
| Runs com métricas recuperadas via Istio no dataset atual | 59 |
| Runs preservados com métricas antigas | 46 |

Nos 59 recuperados que permaneceram após remover `queue-master`, as 3 node features principais (`traffic_lr`, `latency_p95_lr`, `error_rate_lr`) existem para todos os nós. As lacunas restantes vêm dos 46 runs antigos preservados porque o Prometheus atual não tinha séries nessas janelas.

---

## 3. Resultado agregado

### 3.1 GAT vs baselines

| Split | GAT top-3 | B0 random | B1 max anomaly | B2 diffused | Leitura |
|---|---:|---:|---:|---:|---|
| S1 | 0.57 ± 0.04 | 0.53 | 0.58 | 0.63 | GAT empata com B1 e perde de B2 |
| S2 | 0.36 ± 0.05 | 0.47 | 0.70 | 0.63 | GAT abaixo até do acaso; split zero-shot continua problemático |
| S3 | 0.49 ± 0.05 | 0.53 | 0.68 | 0.68 | GAT perde de heurísticas no held-out fault |

Conclusão: mesmo após corrigir cobertura de métricas, S3 e `queue-master`, o GAT **não supera os baselines simples**. Isso enfraquece bastante a hipótese de que a topologia/message passing está ajudando nesta formulação.

### 3.2 MLP vs GAT

| Split | GAT top-3 | MLP top-3 | B1 top-3 | Leitura |
|---|---:|---:|---:|---|
| S1 | 0.57 | 0.79 ± 0.04 | 0.58 | MLP domina GAT e B1; features por nó bastam melhor que grafo |
| S2 | 0.36 | 0.54 ± 0.04 | 0.70 | MLP melhora sobre GAT, mas B1 ainda domina |
| S3 | 0.49 | 0.61 ± 0.09 | 0.68 | MLP melhora sobre GAT, mas ainda perde para B1 |

Conclusão importante: o problema não é apenas falta de métrica. O controle MLP, que usa as mesmas node features sem message passing, supera o GAT em todos os splits. Isso aponta para **topologia/message passing prejudicando o sinal**, ou para o GAT estar aprendendo atalhos de posição em vez de regra causal.

---

## 4. Auditoria por split

### 4.1 S1 — repetição futura

S1 é o split mais favorável, pois o alvo e o tipo de falha aparecem no treino em outras réplicas.

GAT oficial: 0.57 top-3. Baselines: B1=0.58, B2=0.63. MLP=0.79.

Por tipo de falha, na auditoria por run:

| Falha | Runs teste | GAT top-3 | Top-1 | Rank médio |
|---|---:|---:|---:|---:|
| container-kill | 3 | 0.60 | 0.07 | 3.53 |
| cpu-hog | 2 | 0.80 | 0.20 | 2.60 |
| http-status-code | 4 | 0.40 | 0.05 | 3.90 |
| memory-hog | 3 | 0.67 | 0.40 | 2.73 |
| network-latency | 5 | 0.76 | 0.44 | 2.60 |
| network-loss | 2 | 0.40 | 0.30 | 4.30 |

Por alvo:

| Alvo | Runs teste | GAT top-3 | Top-1 | Rank médio |
|---|---:|---:|---:|---:|
| carts | 3 | 0.33 | 0.00 | 4.27 |
| catalogue | 3 | 0.40 | 0.40 | 3.33 |
| front-end | 2 | 0.70 | 0.00 | 3.40 |
| orders | 3 | 0.73 | 0.20 | 3.20 |
| payment | 3 | 0.87 | 0.53 | 2.13 |
| shipping | 2 | 0.70 | 0.10 | 3.30 |
| user | 3 | 0.60 | 0.40 | 3.00 |

Piores casos S1:

| Run | Falha | Alvo | GAT top-3 rate | B1 rank | B2 rank | Sanidade métrica |
|---|---|---|---:|---:|---:|---|
| run-56959244d120 | network-loss | carts | 0.00 | 8 | 7 | alvo não é principal anomalia |
| run-e49360e0bec8 | memory-hog | catalogue | 0.00 | 6 | 3 | alvo não é principal anomalia |
| run-d816a42827eb | network-latency | orders | 0.20 | 2 | 2 | B1/B2 acertariam, GAT não |
| run-a2eae36e3886 | http-status-code | user | 0.20 | 5 | 6 | sem flag forte, mas sinal fraco/moderado |
| run-9e41b685f9d4 | http-status-code | catalogue | 0.20 | 5 | 1 | erro HTTP sem sinal de erro no alvo |

Leitura S1: quando o sinal do alvo é forte, B1/B2 resolvem muitos casos. Quando o alvo não é a maior anomalia, o GAT frequentemente segue outro serviço sintomático. O MLP ter ficado em 0.79 sugere que a transformação por message passing está piorando, não ajudando.

### 4.2 S2 — serviço held-out (`orders`, `payment`)

S2 continua sendo o split mais artificial: `orders` e `payment` não aparecem como alvos no treino.

GAT oficial: 0.36 top-3. Baselines: B1=0.70, B2=0.63. MLP=0.54.

Por tipo de falha:

| Falha | Runs teste | GAT top-3 | Top-1 | Rank médio |
|---|---:|---:|---:|---:|
| cpu-hog | 5 | 0.08 | 0.00 | 6.48 |
| http-status-code | 5 | 0.56 | 0.12 | 3.52 |
| memory-hog | 10 | 0.42 | 0.30 | 4.42 |
| network-latency | 5 | 0.04 | 0.00 | 7.12 |
| network-loss | 5 | 0.72 | 0.48 | 3.20 |

Por alvo:

| Alvo | Runs teste | GAT top-3 | Top-1 | Rank médio |
|---|---:|---:|---:|---:|
| orders | 15 | 0.09 | 0.01 | 6.59 |
| payment | 15 | 0.65 | 0.39 | 3.13 |

Piores células S2:

| Célula | Runs | GAT top-3 | Rank médio | Leitura |
|---|---:|---:|---:|---|
| network-latency × orders | 5 | 0.04 | 7.12 | GAT quase sempre erra apesar de B1/B2 fortes em vários runs |
| cpu-hog × orders | 5 | 0.08 | 6.48 | falha severa de generalização para orders |
| memory-hog × orders | 5 | 0.16 | 6.16 | S2 zero-shot pune o GAT muito mais que B1 |
| http-status-code × payment | 5 | 0.56 | 3.52 | melhor que antes, mas ainda com flags HTTP |
| memory-hog × payment | 5 | 0.68 | 2.68 | célula razoável |
| network-loss × payment | 5 | 0.72 | 3.20 | melhor célula S2 |

Confusões top-1 mais comuns em S2:

| Real | Predito top-1 | Votos |
|---|---|---:|
| orders | payment | 35 |
| payment | payment | 29 |
| orders | catalogue | 18 |
| payment | catalogue | 13 |
| payment | queue-master | 13 |
| orders | carts | 11 |

Leitura S2: o modelo praticamente não aprende `orders` em zero-shot. Ele colapsa para `payment`, `catalogue`, `carts` ou `front-end`. Isso não é só problema de métrica: em vários runs de `orders`, B1/B2 ranqueiam o alvo em 1 ou 2, enquanto o GAT põe o alvo perto do fim. A arquitetura/treino está penalizando exatamente o caso de generalização por serviço.

### 4.3 S3 — held-out fault (`container-kill`)

S3 agora está válido: 19 runs de teste, todos `container-kill`.

GAT oficial: 0.49 top-3. Baselines: B1=0.68, B2=0.68. MLP=0.61.

Por alvo:

| Alvo | Runs teste | GAT top-3 | Top-1 | Rank médio |
|---|---:|---:|---:|---:|
| carts | 5 | 0.56 | 0.44 | 3.80 |
| shipping | 4 | 0.30 | 0.10 | 4.75 |
| user | 10 | 0.66 | 0.22 | 3.50 |

Piores casos S3:

| Run | Alvo | GAT top-3 rate | B1 rank | B2 rank | Sanidade métrica |
|---|---|---:|---:|---:|---|
| run-1005a23bbce0 | carts | 0.00 | 5 | 6 | preservado, alvo sem sinal, 0/3 métricas observadas |
| run-c615050f8f70 | carts | 0.00 | 5 | 6 | preservado, alvo sem sinal, 0/3 métricas observadas |
| run-e5621b0d9ec2 | shipping | 0.00 | 2 | 2 | B1/B2 acertariam; GAT erra |
| run-bd3932cc78a6 | user | 0.20 | 2 | 5 | alvo forte, mas maior anomalia em orders |
| run-6f3546156bd0 | user | 0.20 | 4 | 3 | sinal existe, mas GAT erra |
| run-703faebf9b49 | shipping | 0.20 | 2 | 1 | B1/B2 acertariam; GAT erra |

Confusões top-1 mais comuns em S3:

| Real | Predito top-1 | Votos |
|---|---|---:|
| user | payment | 26 |
| carts | carts | 11 |
| user | user | 11 |
| user | carts | 8 |
| shipping | payment | 8 |
| shipping | catalogue | 6 |
| shipping | queue-master | 4 |

Leitura S3: parte do fracasso é dado ruim preservado (`carts` sem sinal em dois runs), mas não explica tudo. Mesmo em runs recuperados e com sinal forte, B1/B2 ranqueiam melhor que GAT. `shipping` é o alvo mais difícil e o modelo frequentemente desvia para `payment`/`catalogue`.

---

## 5. Sanidade das métricas coletadas vs rótulo

### 5.1 Visão geral

Auditoria sobre os 105 grafos:

| Métrica de qualidade | Valor |
|---|---:|
| Runs com métricas recuperadas por Istio | 59 |
| Média de métricas observadas no alvo (0-3) | 2.54 |
| Alvo sem sinal | 2 |
| Alvo com sinal fraco | 5 |
| Alvo não é principal anomalia | 38 |
| `http-status-code` sem sinal de erro no alvo | 17 |
| Mediana da anomalia no alvo | 0.906 |

Interpretação: a cobertura melhorou muito, mas o corpus ainda mistura dois grupos:

1. **Runs recuperados por Istio**: métricas completas e mais confiáveis.
2. **Runs preservados antigos**: ainda podem ter lacunas, especialmente `error_rate_lr` e alguns targets com 0/3 ou 2/3 métricas observadas.

### 5.2 Qualidade por tipo de falha

| Falha | Runs | Recuperados | Métricas alvo médias | Sem sinal | Sinal fraco | Alvo não é principal anomalia | HTTP sem erro no alvo | Mediana anomalia alvo |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| container-kill | 19 | 11 | 2.42 | 2 | 2 | 6 | 0 | 1.341 |
| cpu-hog | 10 | 8 | 2.70 | 0 | 0 | 4 | 0 | 0.387 |
| http-status-code | 22 | 14 | 2.64 | 0 | 3 | 13 | 17 | 0.182 |
| memory-hog | 16 | 5 | 2.38 | 0 | 0 | 6 | 0 | 0.930 |
| network-latency | 28 | 17 | 2.64 | 0 | 0 | 5 | 0 | 2.215 |
| network-loss | 10 | 4 | 2.40 | 0 | 0 | 4 | 0 | 0.790 |

Problemas claros:

- **`http-status-code` continua suspeito**: 17/22 runs não mostram aumento relevante de `error_rate_lr` no alvo. Isso indica que mesmo usando Istio, o efeito pode estar aparecendo em outro ponto do caminho, com label `source_workload`, `destination_workload=unknown`, ou como erro percebido no cliente. Esses runs não devem ser usados como evidência principal até a semântica da injeção ser confirmada.
- **`container-kill` tem 2 runs sem sinal total no alvo** (`carts` preservado antigo): estes dois devem ser excluídos da avaliação principal, pois não há dado para localizar.
- **38/105 runs têm alvo diferente da maior anomalia**: isso não é necessariamente erro do experimento, mas torna a tarefa mais difícil. Para alguns tipos de falha, o sintoma principal aparece downstream/upstream, enquanto o label é a causa injetada.

### 5.3 Qualidade por serviço

| Serviço | Runs | Recuperados | Métricas alvo médias | Sem sinal | Sinal fraco | Alvo não é principal anomalia | Mediana anomalia alvo |
|---|---:|---:|---:|---:|---:|---:|---:|
| carts | 15 | 6 | 2.13 | 2 | 2 | 10 | 0.196 |
| catalogue | 16 | 3 | 2.19 | 0 | 0 | 2 | 0.792 |
| front-end | 10 | 10 | 3.00 | 0 | 0 | 8 | 0.387 |
| orders | 15 | 5 | 2.27 | 0 | 0 | 3 | 2.182 |
| payment | 15 | 9 | 2.67 | 0 | 2 | 8 | 0.547 |
| shipping | 9 | 9 | 3.00 | 0 | 0 | 1 | 3.623 |
| user | 25 | 17 | 2.76 | 0 | 1 | 6 | 1.319 |

Problemas por serviço:

- **carts**: pior qualidade de sinal; 2 runs sem sinal, 2 fracos, 10/15 com alvo não sendo principal anomalia. Precisa de recoleta/filtragem antes de concluir sobre o modelo.
- **front-end**: métricas completas, mas 8/10 vezes o alvo não é a principal anomalia; provavelmente front-end reflete sintomas espalhados ou fluxo de entrada, não causa isolada.
- **payment**: razoável em algumas células, mas muito confundido com outros nós e afetado por `http-status-code`.
- **shipping**: métricas completas e alvo forte, mas GAT vai mal em S3; aqui o problema parece mais de modelo/topologia do que de coleta.

---

## 6. Revisão de cada grupo experimental falha × alvo

A tabela completa por run está em [gat_postfix_error_audit_runs.csv](../../experiment/eval/gat/out/gat/gat_postfix_error_audit_runs.csv). Abaixo está a leitura por grupo experimental mais relevante.

### Grupos com maior confiança operacional

| Grupo | Evidência | Recomendação |
|---|---|---|
| `network-latency × orders` | B1/B2 frequentemente bons; GAT péssimo em S2 | usar como prova de que GAT não generaliza para `orders` |
| `memory-hog × payment` | boa performance relativa; alvo geralmente forte | manter como caso positivo/controle |
| `network-loss × payment` | B1/B2 e GAT melhores que média | manter como célula confiável, mas conferir runs preservados |
| `container-kill × shipping` | métricas completas; GAT ruim; B1/B2 bons em alguns runs | ótimo caso para investigar erro do GAT/message passing |
| `network-latency × catalogue/user` | bons sinais e S1 razoável | manter como núcleo confiável |

### Grupos suspeitos ou contaminados

| Grupo | Problema | Ação |
|---|---|---|
| `http-status-code × *` | 17/22 sem sinal de erro no alvo | não usar na métrica principal até validar source/destination do fault |
| `container-kill × carts` | 2 runs com 0/3 métricas no alvo | excluir esses runs ou recoletar |
| `network-loss × carts` | alvo quase não é anomalia principal; GAT e B1 falham | revisar se o rótulo reflete causa observável |
| `memory-hog × catalogue` | S1 falha total em run com alvo não principal | revisar se efeito aparece em front-end antes de catalogue |
| `cpu-hog × orders` | S2 muito ruim; parte por zero-shot, parte por sinal variável | precisa de S2' antes de concluir |

---

## 7. Hipóteses atualizadas

### H1 — GAT está prejudicado pela topologia/message passing

Status: **fortemente suportada**.

Evidência:

- MLP supera GAT em todos os splits.
- B1/B2 superam GAT em S2/S3.
- Há casos onde B1/B2 ranqueiam o alvo em 1 ou 2, mas GAT coloca o alvo em rank médio 5-8.
- Em S3, `container-kill × shipping` tem métricas completas, B1/B2 bons em alguns runs, mas GAT ruim.

Interpretação: a propagação do GAT parece diluir o sinal local ou induzir viés de posição. A topologia estática não está ajudando a localizar a causa raiz; está misturando sintomas.

### H2 — S2 continua inadequado como critério principal

Status: **confirmada**.

Evidência:

- `orders` tem top-3 0.09 e rank médio 6.59.
- `payment` tem top-3 0.65 e rank médio 3.13.
- Ambos são held-out do treino, mas o colapso é assimétrico.
- B1 resolve muito melhor sem precisar aprender classe.

Interpretação: S2 ainda é útil como diagnóstico de viés/generalização zero-shot, mas não deve ser critério primário da hipótese científica. Trocar para S2' é prioridade: serviço aparece no treino, mas combinação falha × serviço fica held-out.

### H3 — `http-status-code` ainda está conceitualmente mal definido

Status: **suportada**.

Evidência:

- 17/22 runs `http-status-code` têm flag `http_error_without_target_error_signal`.
- O alvo raramente é a maior anomalia.
- A correção de Istio melhorou cobertura, mas não resolveu a semântica da injeção.

Interpretação: `VirtualService fault.abort` pode manifestar erro no cliente/origem, no gateway, em `destination_workload=unknown`, ou em arestas, não necessariamente no workload-alvo. O rótulo “serviço alvo” pode estar errado para essa falha.

### H4 — ainda há contaminação por runs preservados antigos

Status: **suportada parcialmente**.

Evidência:

- 46/105 runs ainda usam métricas antigas preservadas.
- Em S3, 2 runs `container-kill × carts` têm 0/3 métricas observadas no alvo e GAT/B1/B2 falham.
- Muitos piores casos S2 são preservados com apenas 1/3 ou 2/3 métricas no alvo.

Interpretação: o backfill resolveu a parte recuperável, mas a avaliação ainda mistura dados bons e antigos incompletos. Isso reduz a confiabilidade do número final.

### H5 — a tarefa “causa injetada” diverge da tarefa “maior sintoma”

Status: **fortemente suportada**.

Evidência:

- 38/105 runs têm alvo diferente da principal anomalia.
- Em `front-end`, 8/10 runs têm outro nó como maior anomalia apesar de métricas completas.
- Em muitos casos, B1 acerta quando o alvo é maior anomalia; erra quando o sintoma principal está em outro serviço.

Interpretação: o dataset rotula a causa injetada, mas as features capturam sintomas observados. Isso é correto para RCA real, mas exige um modelo causal mais cuidadoso; uma heurística de maior anomalia mede “onde doeu mais”, não necessariamente “onde foi injetado”.

---

## 8. Sugestões práticas

### 8.1 Antes de treinar novamente como resultado final

1. Criar um `quality_filter` oficial e reportar dois resultados:
   - **dataset limpo**: apenas runs com métricas completas e alvo com sinal mínimo;
   - **dataset bruto**: todos os runs, para medir robustez.

2. Excluir da métrica principal:
   - `http-status-code`, por enquanto;
   - runs com `target_metric_observed_count < 3`;
   - runs com `target_anomaly == 0`;
   - opcionalmente runs preservados antigos se o objetivo for avaliar o pipeline corrigido.

3. Recalcular GAT/MLP/B1/B2 no dataset limpo.

### 8.2 Para o modelo

1. Usar MLP como baseline forte obrigatório.
   - Hoje o MLP é melhor que GAT; qualquer novo GNN precisa bater MLP, não só B0/B1.

2. Testar ablações rápidas:
   - GAT sem edge features;
   - GAT com arestas embaralhadas;
   - GAT com residual/self-feature forte;
   - MLP + agregados de vizinhança como baseline intermediário.

3. Revisar loss/arquitetura:
   - Cross-entropy por nó pode ser sensível a sintomas espalhados.
   - Considerar loss de ranking/margem ou treino que penalize menos quando o sintoma principal é vizinho causal.

4. Salvar predições por run no treino oficial.
   - A auditoria atual precisou retreinar para recuperar ranks; isso deve virar saída padrão (`predictions.<split>.jsonl`).

### 8.3 Para o experimento

1. Substituir S2 por S2'.
   - Held-out de combinação falha × serviço, não serviço inteiro.
   - Exemplo: `orders` aparece no treino com algumas falhas, mas `network-latency × orders` fica teste.

2. Recoletar células críticas:
   - `container-kill × carts` (corrigir os dois runs sem sinal);
   - `container-kill × shipping` (caso forte para investigar GAT);
   - `network-latency × orders` (B1 bom, GAT ruim);
   - `cpu-hog × orders` (S2 colapso).

3. Validar `http-status-code` separadamente.
   - Medir erro por `source_workload`, `destination_workload`, `destination_service`, `response_code` e arestas.
   - Só recolocar no dataset se o erro aparecer no caminho esperado e o rótulo for semanticamente correto.

4. Diferenciar no relatório:
   - “localizar causa injetada”;
   - “localizar maior sintoma”;
   - “localizar caminho causal”.

---

## 9. Veredito atualizado

O resultado continua ruim, mas agora o diagnóstico é mais claro.

- A correção de métricas resolveu a cobertura HTTP/latência/tráfego para os runs recuperáveis.
- S3 agora é válido e mostra que o GAT não generaliza bem para `container-kill`.
- Remover `queue-master` tirou uma fonte óbvia de rótulo irrecuperável, mas o GAT ainda perde para baselines.
- O MLP supera o GAT, indicando que o problema está na forma como o grafo/message passing está sendo usado.
- `http-status-code` e runs preservados antigos ainda contaminam o dataset.

Portanto, a hipótese atual deve ser:

> Com as features atuais, o sinal de localização está principalmente nas node features. A topologia estática/message passing do GAT tende a diluir ou deslocar esse sinal, e a avaliação ainda mistura casos causalmente ambíguos ou metricamente incompletos. O próximo teste decisivo é rodar GAT, MLP e baselines em um dataset limpo e em S2' composicional.
