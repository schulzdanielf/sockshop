# Plano de correção do experimento GAT e mapa de acertos/erros (2026-09-22)

## 1. Objetivo

Este relatório responde a três perguntas práticas após o resultado ruim do
GAT pós-reset:

1. Como corrigir o experimento e a avaliação aproveitando o que já foi feito?
2. O que precisa ser removido, corrigido ou recoletado?
3. Onde o modelo acerta/erra por tipo de falha e por serviço, para decidir
   onde investir.

Base usada:
- Dataset atual: `experiment/eval/gat/out/dataset.jsonl`, 115 grafos.
- Resultado GAT: `experiment/eval/gat/out/gat/gat_vs_baselines.json`.
- Resultado MLP regenerado: `experiment/eval/gat/out/gat/mlp_ablation.json`.
- Auditoria por run/seed feita nesta sessão com 5 seeds do GAT para S1/S2.

S3 não entra no mapa de erros do modelo porque está vazio (`n_test=0`). Isso é
um bug de configuração/dataset, não erro de predição.

---

## 2. Hipóteses de correção aproveitando os dados existentes

### H1 — limpar o dataset atual já melhora o piso da avaliação

**Hipótese:** remover os casos sabidamente inválidos (`http-status-code` sem
sinal sob Istio e `queue-master` sem observabilidade) produzirá uma avaliação
mais honesta sem jogar fora todo o trabalho de coleta.

Evidência:
- Dataset bruto: B1 top-3 = 0.52 em 115 grafos.
- Dataset sem `http-status-code` e sem `queue-master`: B1 top-3 = 0.60 em 83 grafos.
- O ganho vem só de limpeza, sem mudar modelo.

Correção proposta:
- Criar uma etapa explícita de `quality_filter` antes do treino, com motivos de
  exclusão versionados por run.
- Excluir de treino/teste, por enquanto:
  - `http-status-code` sob Istio, até confirmar medição correta do efeito.
  - `queue-master` como alvo, se a task continuar baseada em métricas Istio.
  - qualquer run cujo alvo tenha `target_anomaly == 0` e não exista outra
    feature observável que justifique manter.

O que reaproveitar:
- Reaproveitar os 83 grafos limpos.
- Manter os grafos excluídos em relatório separado, não apagar fisicamente.
  Eles são úteis para documentar limitações da instrumentação.

### H2 — S3 precisa ser redefinido antes de qualquer nova conclusão

**Hipótese:** a métrica `NaN` de S3 vem exclusivamente do held-out fault errado.
`S3_HELD_OUT_FAULT = "pod-delete"`, mas a campanha atual usa
`container-kill`; portanto nenhum run vira teste.

Correção proposta:
- Opção rápida: alterar S3 para `container-kill` na avaliação atual e
  reconstruir dataset/resultados.
- Opção mais limpa: manter S3 como "falha nova" e definir explicitamente a
  campanha S3 antes da coleta: ou recoletar `pod-delete`, ou documentar que
  `container-kill` é o transiente held-out da versão pós-reset.

O que reaproveitar:
- Os 19 runs atuais de `container-kill` podem virar teste S3 imediatamente.
- Os demais continuam como treino S3.

Risco:
- Se `container-kill` já influenciou decisões de hiperparâmetro/limpeza, usar
  como S3 agora é menos puro do que recoletar, mas ainda é muito melhor que
  `n_test=0`.

### H3 — `http-status-code` precisa de uma correção de mensuração, não só de injeção

**Hipótese:** mesmo com `VirtualService fault.abort`, o erro pode aparecer no
cliente/origem, não no `destination_workload` do serviço-alvo. Por isso o alvo
continua sem `error_rate_lr` e a avaliação vê label falso.

Correção proposta:
- Não usar `http-status-code` no treino atual.
- Antes de recoletar em massa, fazer uma coleta piloto por serviço com checagem
  automática:
  - erro HTTP visível no cliente esperado;
  - métrica por `source_workload` e/ou `destination_service`;
  - correlação entre label e campo usado na feature.
- Se o efeito aparece no cliente, criar features específicas para HTTP abort:
  - erro em edges de entrada/saída relacionados ao alvo;
  - score de erro por origem cliente;
  - talvez label semântico: "falha percebida no caminho" vs. "serviço raiz".

O que reaproveitar:
- Os 22 runs atuais servem como evidência negativa e teste de regressão: uma
  futura versão não deve classificar esses casos como válidos se o alvo segue
  sem sinal.

### H4 — `queue-master` deve virar classe especial ou sair da task WHERE

**Hipótese:** `queue-master` não é observável via Istio porque a comunicação é
AMQP/RabbitMQ; exigir localização por métricas HTTP/trace do mesh é injusto.

Correção proposta:
- Opção A: excluir `queue-master` da task GAT atual e reportar como limitação
  de cobertura.
- Opção B: adicionar instrumentação RabbitMQ/AMQP e features específicas
  antes de incluí-lo.
- Opção C: manter como categoria "não-localizável" e avaliar separadamente,
  nunca misturado no top-3 geral.

O que reaproveitar:
- Os 10 runs de `queue-master` documentam a lacuna de observabilidade.

### H5 — S2 deve deixar de ser critério principal

**Hipótese:** S2 mede uma tarefa artificial: classe de serviço completamente
não vista no treino (`orders`/`payment`). Isso é útil para detectar viés de
posição, mas não deve ser usado como veredito principal da hipótese H001.

Correção proposta:
- Manter S2 como split diagnóstico.
- Criar S2' mais realista: held-out combinação falha × serviço, garantindo que
  o serviço aparece no treino com outras falhas e a falha aparece no treino em
  outros serviços.
- Critério principal deve ser:
  - S1 limpo: generalização por réplica futura.
  - S3 corrigido: generalização para tipo de falha novo.
  - S2' limpo: generalização composicional.

O que reaproveitar:
- O dataset atual já permite simular S2' em parte, mas ficará melhor após
  completar células faltantes.

---

## 3. O que remover, corrigir e rodar de novo

### Remover/excluir da avaliação atual

1. `http-status-code` atual: 22 grafos.
   - Motivo: label falso ou métrica no lugar errado.
   - Status: excluir de treino/teste até piloto confirmar feature correta.

2. `queue-master` como alvo: 10 grafos.
   - Motivo: alvo sem sinal em 10/10 casos.
   - Status: excluir da métrica principal ou avaliar como categoria separada.

3. Runs com alvo sem qualquer sinal (`target_anomaly == 0`).
   - Motivo: impossível distinguir por features atuais.
   - Status: no mínimo reportar como `unobservable`, idealmente excluir da
     métrica principal.

### Corrigir no código de avaliação/dataset

1. Corrigir `S3_HELD_OUT_FAULT`.
   - Atual: `pod-delete`.
   - Proposto para dataset atual: `container-kill`, ou recoleta explícita de
     `pod-delete`.

2. Corrigir `normalize_chaos_type`.
   - Hoje, quando `chaos_type` é um path YAML como
     `.../payment-http-status-code.yaml`, ele fica como path inteiro.
   - Deve extrair `http-status-code` mesmo quando vier como path.

3. Adicionar relatório de qualidade por run antes do treino.
   - Campos mínimos: `run_id`, `chaos_type`, `service`, `usable_for_training`,
     `exclusion_reason`, `target_anomaly`, `edge_source`, `split`.

4. Amarrar MLP ao mesmo pipeline do GAT.
   - Hoje `mlp_ablation.json` pode ficar desatualizado.
   - Todo resultado GAT deveria sempre sair junto com MLP e baselines no mesmo
     dataset e nos mesmos splits.

### Rodar novos experimentos faltantes

Prioridade 1 — fechar S3:
- Rodar/coletar `container-kill` em serviços sem cobertura suficiente, se S3
  for redefinido para `container-kill`.
- Alternativamente recoletar `pod-delete` em todas as classes relevantes.

Prioridade 2 — balancear células difíceis:
- `shipping`: hoje muito difícil e pouco representado.
- `front-end`: sinal existe, mas o modelo erra muito.
- `carts`: aparece pouco no teste e falha forte no S1.
- `orders`/`payment`: precisam aparecer no treino se quisermos avaliar S2'
  em vez de zero-shot puro.

Prioridade 3 — corrigir HTTP:
- Fazer piloto `http-status-code` com `VirtualService fault.abort` e validação
  automática do local onde o erro aparece.
- Só depois rodar matriz completa por serviço.

---

## 4. Revisão do processo de treinamento

### Problemas observados

1. O treino atual mistura exemplos bons e exemplos sem sinal. O modelo recebe
   labels impossíveis e tenta otimizar contra ruído.
2. O early stopping usa validação estratificada por serviço, mas se o dataset
   contém labels falsos/sem sinal, a validação também fica contaminada.
3. S2 força predição em classes nunca vistas. Isso penaliza modelos
   paramétricos e favorece heurísticas não treináveis, como B1.
4. O artefato salvo guarda métricas agregadas, mas não guarda predições por
   run. Isso impede auditoria rápida de "onde errou" depois do treino.
5. MLP, GAT e baselines não estão integrados em um único relatório versionado.

### Correções recomendadas

1. Adicionar `quality_filter` antes de split/treino.
   - O treino deve receber somente runs `usable_for_training=true`.
   - Runs excluídos devem aparecer em relatório, não sumir silenciosamente.

2. Salvar predições por run para cada seed.
   - Campos: `split`, `seed`, `run_id`, `actual_service`, `actual_chaos`,
     `rank`, `top1`, `top3`, `scores_by_service`, `flags`.
   - Isso torna a análise de acerto/erro reprodutível sem retreinar.

3. Treinar e reportar sempre GAT + MLP + B0/B1/B2 juntos.
   - MLP é o controle que separa "as features bastam" de "o grafo ajuda".
   - B1 é o controle mínimo: se o modelo não bate B1 no split limpo, não há
     evidência de aprendizado útil.

4. Separar resultados em duas camadas.
   - `main`: dataset limpo, hipótese científica.
   - `diagnostic`: todos os dados, incluindo casos unobservable/bugados, para
     medir robustez e lacunas de instrumentação.

5. Revisar a função objetivo se o objetivo real for ranking robusto.
   - Cross-entropy força probabilidade concentrada em um nó.
   - Considerar perdas de ranking/top-k ou margem contra negativos difíceis,
     especialmente quando anomalias se espalham para vizinhos.
   - Só vale testar isso depois de limpar dataset/splits; antes disso só
     otimiza ruído.

6. Reavaliar se edge features ajudam.
   - Se o GAT continuar pior que MLP no dataset limpo, testar:
     - GAT sem edge features;
     - GAT com arestas embaralhadas;
     - MLP com features agregadas de vizinhos;
     - full-only vs. static-only.

---

## 5. Mapa de acertos e erros do GAT atual

Importante: os números abaixo são do modelo atual no dataset bruto. Eles servem
para orientar correção, não para conclusão científica final.

### 5.1 S1 — generalização por réplica futura

S1 tem 11 runs de teste. Por seed, o GAT acertou top-3 em:

- seed 0: 4/11
- seed 1: 4/11
- seed 2: 5/11
- seed 3: 6/11
- seed 4: 6/11

#### S1 por serviço

| Serviço alvo | Runs | Top-3 | Top-1 | Rank médio | Interpretação |
|---|---:|---:|---:|---:|---|
| carts | 1 | 0.00 | 0.00 | 4.40 | falha forte; caso é `http-status-code` suspeito |
| front-end | 2 | 0.40 | 0.00 | 4.30 | difícil; sinal existe mas confunde com `user`/`queue-master` |
| payment | 1 | 0.20 | 0.00 | 5.40 | falha forte; caso é `http-status-code` suspeito |
| queue-master | 2 | 0.80 | 0.80 | 1.80 | resultado enganoso: alvo sem sinal, provável viés/artefato |
| shipping | 2 | 0.50 | 0.00 | 4.30 | difícil, especialmente `container-kill` |
| user | 3 | 0.47 | 0.20 | 4.53 | médio/ruidoso; `http-status-code` pesa contra |

#### S1 por tipo de falha

| Tipo de falha | Runs | Top-3 | Top-1 | Rank médio | Interpretação |
|---|---:|---:|---:|---:|---|
| container-kill | 2 | 0.50 | 0.20 | 3.90 | misto; shipping é o pior caso |
| cpu-hog | 2 | 0.80 | 0.50 | 2.50 | bom no S1, mas amostra pequena e inclui queue-master |
| http-status-code | 4 | 0.20 | 0.05 | 5.25 | pior classe; deve sair da avaliação atual |
| memory-hog | 1 | 0.60 | 0.60 | 2.60 | amostra insuficiente; caso é queue-master |
| network-latency | 2 | 0.50 | 0.00 | 3.90 | médio; front-end difícil |

#### Piores células S1

| Célula | Runs | Top-3 | Rank médio | Leitura |
|---|---:|---:|---:|---|
| http-status-code × carts | 1 | 0.00 | 4.40 | label/sinal suspeito |
| container-kill × shipping | 1 | 0.20 | 5.40 | difícil mesmo sem flag conhecida |
| http-status-code × payment | 1 | 0.20 | 5.40 | label/sinal suspeito |
| network-latency × front-end | 1 | 0.20 | 4.60 | front-end confunde com sintomas downstream |
| http-status-code × user | 2 | 0.30 | 5.60 | classe HTTP corrompida/ruidosa |

Confusões top-1 mais frequentes em S1:
- `user -> queue-master`: 6 votos.
- `shipping -> payment`: 5 votos.
- `front-end -> queue-master`: 4 votos.
- `front-end -> user`: 4 votos.
- `carts -> user`: 3 votos.

Isso sugere que o modelo está aprendendo alguns atalhos de posição/estrutura,
não uma causalidade robusta: `queue-master` aparece muito como top-1 mesmo em
casos onde o próprio nó não tem sinal.

### 5.2 S2 — held-out service (`orders`/`payment`)

S2 tem 30 runs de teste. Por seed, o GAT acertou top-3 em:

- seed 0: 12/30
- seed 1: 11/30
- seed 2: 12/30
- seed 3: 18/30
- seed 4: 12/30

#### S2 por serviço

| Serviço alvo | Runs | Top-3 | Top-1 | Rank médio | Interpretação |
|---|---:|---:|---:|---:|---|
| orders | 15 | 0.36 | 0.12 | 4.53 | muito difícil; serviço nunca aparece no treino |
| payment | 15 | 0.51 | 0.37 | 4.15 | melhor que orders, mas contaminado por HTTP suspeito |

#### S2 por tipo de falha

| Tipo de falha | Runs | Top-3 | Top-1 | Rank médio | Interpretação |
|---|---:|---:|---:|---:|---|
| cpu-hog | 5 | 0.12 | 0.00 | 5.84 | muito ruim; todos em orders |
| http-status-code | 5 | 0.04 | 0.00 | 6.84 | pior caso; todos payment, sinal suspeito |
| memory-hog | 10 | 0.58 | 0.36 | 3.54 | depende do serviço: payment bom, orders ruim |
| network-latency | 5 | 0.68 | 0.36 | 2.60 | melhor caso; todos orders |
| network-loss | 5 | 0.60 | 0.40 | 3.68 | razoável; todos payment |

#### S2 por célula falha × serviço

| Célula | Runs | Top-3 | Top-1 | Rank médio | Leitura |
|---|---:|---:|---:|---:|---|
| http-status-code × payment | 5 | 0.04 | 0.00 | 6.84 | remover/corrigir antes de usar |
| cpu-hog × orders | 5 | 0.12 | 0.00 | 5.84 | grande lacuna; verificar sinal e treino zero-shot |
| memory-hog × orders | 5 | 0.28 | 0.00 | 5.16 | difícil; sinal aparece mas não generaliza |
| network-loss × payment | 5 | 0.60 | 0.40 | 3.68 | razoável |
| network-latency × orders | 5 | 0.68 | 0.36 | 2.60 | melhor célula de S2 |
| memory-hog × payment | 5 | 0.88 | 0.72 | 1.92 | melhor célula geral |

Confusões top-1 mais frequentes em S2:
- `orders -> user`: 21 votos.
- `orders -> carts`: 16 votos.
- `payment -> payment`: 27 votos.
- `payment -> queue-master`: 14 votos.
- `payment -> user`: 14 votos.
- `orders -> payment`: 10 votos.

Leitura: `orders` tende a ser confundido com `user`/`carts`, enquanto
`payment` é ocasionalmente aprendido mesmo sendo zero-shot, mas ainda sofre
com `queue-master` e `user` como falsos positivos.

---

## 6. Onde investir primeiro

### Investimento 1 — qualidade de dado antes de modelo

Maior retorno esperado:
- remover `http-status-code` atual;
- excluir/segregar `queue-master`;
- corrigir S3;
- corrigir normalização de `chaos_type`.

Sem isso, mexer em arquitetura provavelmente só otimiza ruído.

### Investimento 2 — serviços difíceis

Prioridade de coleta/análise:
1. `shipping`: difícil mesmo em S1; provável lacuna de observabilidade/propagação.
2. `front-end`: sinal existe, mas confunde com `user`/`queue-master`; precisa
   análise de causalidade vs. sintoma.
3. `orders`: piora em S2 por zero-shot; precisa aparecer no treino em S2'.
4. `carts`: S1 ruim, mas amostra pequena e contaminada por HTTP.
5. `payment`: só avaliar depois de remover HTTP; em `memory-hog` vai muito bem.

### Investimento 3 — tipos de falha difíceis

Prioridade:
1. `http-status-code`: não usar até corrigir mensuração.
2. `cpu-hog × orders`: pior célula não-HTTP de S2.
3. `container-kill × shipping`: pior célula não-HTTP de S1.
4. `memory-hog × orders`: sinal existe, mas GAT não generaliza.
5. `network-latency` e `network-loss`: parecem os mais promissores para manter
   como núcleo confiável da avaliação.

---

## 7. Plano prático em fases

### Fase 0 — sem recoleta, só reaproveitando dados atuais

1. Implementar filtro de qualidade e relatório de exclusão.
2. Corrigir S3 para `container-kill` ou marcar S3 como inválido até recoleta.
3. Regenerar dataset limpo.
4. Rodar GAT + MLP + baselines no dataset limpo.
5. Salvar predições por run.

Resultado esperado: saber se GAT realmente ajuda no subconjunto confiável.

### Fase 1 — recoleta mínima

1. Completar células dos serviços difíceis (`shipping`, `front-end`, `orders`).
2. Garantir pelo menos 5 réplicas por célula útil.
3. Rodar piloto HTTP corrigido antes de incluir HTTP na matriz cheia.

Resultado esperado: S1/S3/S2' com menos variância e sem casos impossíveis.

### Fase 2 — revisão de modelo

Só depois da Fase 0/1:
1. Comparar GAT vs. MLP no dataset limpo.
2. Testar arestas embaralhadas para medir viés de topologia.
3. Testar GAT sem edge features e MLP com agregação de vizinhos.
4. Se GAT ainda perde para MLP/B1, tratar topologia estática como hipótese
   refutada para esta task e priorizar feature engineering/heurísticas híbridas.

---

## 8. Critério de aceite para considerar o experimento corrigido

O experimento só deve voltar a ser usado como evidência científica quando:

1. S3 tiver `n_test > 0` e significado documentado.
2. Todo run de teste tiver label e sinal observável no alvo ou no caminho
   causal definido.
3. `http-status-code` tiver validação automática do efeito antes de entrar no
   treino.
4. `queue-master` estiver fora da métrica principal ou instrumentado.
5. Relatório final incluir GAT, MLP, B0/B1/B2 e predições por run.
6. O dataset limpo e o dataset bruto forem reportados separadamente.
