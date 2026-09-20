# 2026-09-19 — Criação da branch `hibrido` e do research lab

**Toca:** [Abordagem 002](../abordagens/002-hibrido-gat-agentes.md), [H001](../hipoteses/H001-gat-localizacao-falhas.md), [H002](../hipoteses/H002-agentes-ia-motivo-falha.md)

## O que foi feito

- Criada a branch `hibrido` para testar a nova abordagem: **GAT para localizar a falha + agentes de IA para identificar o motivo**.
- Criado o diretório `research/` (research lab): hipóteses, linha do tempo de abordagens, diário e planos.
- Formalizadas as hipóteses H001 (WHERE via GAT) e H002 (WHY via agente) com critérios de validação/refutação **antes** de rodar qualquer experimento.
- Rascunhado o [plano de trabalho](../planos/2026-09-19-plano-hibrido.md).

## Por que agora (motivação)

A abordagem 001 atingiu bom desempenho em mini-runs (where/why 100%), mas os modos de falha restantes são estruturais:

1. localização depende de heurística de grafo **que não aprende**;
2. motivo depende de **thresholds manuais** com cobertura parcial (4 das 7 categorias do catálogo sem validador);
3. a interação validador→localizador já causou regressão (run-82b832839337).

## Próximos passos

- Fase 0 do plano: inventário do dataset (quantos runs rotulados por célula fault × serviço existem no `platform.db`?).
- Decidir stack do GAT e o papel dele no WHERE (substitui o LLM ou ensemble).

---

## Atualização (mesmo dia) — decisões de escopo

Conversa de planejamento definiu:

1. **Foco no WHERE** — H002 (agente/WHY) pausada até H001 ser avaliada.
2. **Stack PyTorch + PyTorch Geometric**.
3. **Primeiro modelo: GAT** puro (GATv2 como variação posterior).
4. **Métrica primária: top-3 accuracy** — acerto se o serviço raiz estiver entre os 3 primeiros do ranking; top-1 e MRR ficam como secundárias.

Racional do top-3: com ~8–14 serviços, top-3 já elimina ~75% do espaço de busca para a etapa de WHY — que é exatamente o que o agente da H002 vai precisar como âncora. Mantém a régua honesta (comparação contra baselines no mesmo critério) sem exigir precisão perfeita do primeiro modelo.

Implicação prática no design do GAT: saída continua sendo **score por nó** (softmax sobre nós), treinado com cross-entropy no nó raiz; a métrica muda apenas na avaliação (top-k do softmax), não no treino.

---

## Atualização 2 (mesmo dia) — esquema do grafo

Discussão do desenho do dataset (com crítica mútua). Decidido:

1. **Features = apenas as 3 golden metrics** (request rate, latência, % erro), por serviço (nó) e por chamada (aresta). Métricas de recurso (cpu/mem/oom/restarts) ficam **fora** — justificativa: golden metrics medem o sintoma no serviço; resource metrics medem o sintoma da máquina (containers com poucos recursos, alta variância). Generalização > cobertura. Risco registrado em H001: cpu-hog/memory-hog podem ficar mais difíceis.
2. **Log-ratio com ε** como encoding — resolve baseline de erro ≈ 0, centraliza em 0, simetriza direções. (Na conversa, "z-score" foi cogitado e descartado: depende de distribuição, não tem referência fixa ao baseline.)
3. **Arestas bidirecionais** com edge_attr marcando a direção real da chamada — evita impor direção do message passing a priori.
4. **Fonte das arestas:** service graph derivado de traces via métricas agregadas (spanmetrics/service-graph no Prometheus), não spans brutos — mais barato, já window-scoped, e o pipeline já tem metade do caminho (`mcp_traces_plugin.py`, `service_graph.py`). O "service graph do Grafana" é a mesma fonte (gerado dos mesmos dados).

Pontos da crítica incorporados: GAT puro não usa edge features nativamente → usar `GATConv`/`GATv2Conv` com `edge_dim` no PyG (fallback: agregar métricas de aresta no nó). Features de nó eram obrigatórias (label está no nó).

**Pendente:** granularidade temporal — um grafo por run (snapshot razão fault/baseline) vs sub-janelas early/mid/late como features extras. Recomendação pendente de confirmação: começar com um grafo por run + sub-janelas como features (sem modelo temporal na v0).

---

## Atualização 3 (mesmo dia) — granularidade temporal e falhas transientes

**Decidido: um grafo por run (snapshot único), sem modelo temporal na v0** — mas com um refinamento crucial levantado pelo usuário:

> Em container-kill / pod-delete, o container reinicia em segundos, mas a fase de "fault" do experimento é muito mais longa. Calcular a razão fault/baseline sobre a fase inteira **dilui o pico** do sintoma na média — o grafo fica parecido com um run saudável, mas com label de falha. Isso é ruído de label e corrompe o treino.

Análise: há duas durações distintas — a do **estímulo** (o que o chaos faz: segundos em kill/delete; contínua em cpu/mem/network) e a do **sintoma** (o que as métricas mostram). E as fases do run são definidas pelo *schedule do experimento*, não pelo sintoma observado — coincidem em falhas persistentes, divergem em transientes.

Decisões derivadas:

1. **Janela de fault encurtada:** primeiros ~2–3 min após o disparo do chaos (cap no fim real da fase), ancorada na duração configurada no chaos manifest (transiente → curta; persistente → até o fim da injeção). Sem detecção de anomalia na v0.
2. **Count como feature auxiliar:** janelas curtas tornam error% esparso (3 spans em 200); o nº de spans/requisições acompanha cada razão como peso de confiança.
3. **Aceitar o efeito colateral:** falhas transientes terão sinal mais fraco por natureza. Se S1 mostrar container-kill/pod-delete sistematicamente abaixo, é achado (natureza da falha), não bug — registrar na discussão da tese.

---

## Atualização 4 (mesmo dia) — mapeamento evolutivo + observabilidade

Pedido do usuário: mapear tudo que precisa mudar no experimento/coleta/avaliação para a estratégia GAT, com dois requisitos: (1) artefatos evolutivos, consumíveis por um futuro agente de LLM; (2) observabilidade fortíssima + testes de validação por etapa, para nunca ficar "no escuro" sobre o que aconteceu num run.

Descoberta principal ao mapear: **o pipeline já tem base forte de rastreabilidade** (eventos de domínio por fase, timings completos, artefatos brutos por run, ground-truth auto-labeling, llm_io_audit, validator_meta traceável, auditoria offline de prompts). O trabalho não é criar observabilidade do zero, é:

1. **Novos artefatos do GAT** (service graph por janela, `gat_graph.json` com schema versionado);
2. **Gates de validação entre etapas** — hoje quase tudo é best-effort e silencioso (`try/except: pass`); grafo malformado treina modelo errado sem ninguém perceber. Proposto gate runner com `run_quality.json` (window_sanity, graph_connectivity, feature_sanity, label_consistency, symptom_presence como informativo);
3. **Relatório consolidado de saúde** por run (`run_health.json`) — o documento único "o que aconteceu".

Princípios registrados para "consumível por agente": JSON com schema versionado, autocontido, decisões como dados (padrão `validator_meta`), resumo humano gerado dos dados estruturais, eventos de domínio para cada etapa nova.

Documento completo: [planos/2026-09-19-mapeamento-evolutivo.md](../planos/2026-09-19-mapeamento-evolutivo.md). Sequenciamento: gates → janela efetiva → coleta → graph builder → dataset → treino/avaliação. Passos 1–4 independentes de PyG (destrava paralelismo).

Decisões novas em aberto: cap de janela configurável vs hard-coded (recomendo configurável, default 180s); reprocessamento offline de runs antigos vs dataset só de runs novos (verificar no inventário); MLflow vs diretório de artefatos simples (recomendo simples).

---

## Atualização 5 (mesmo dia) — Fase 0 executada: inventário do platform.db

**Decisões fechadas:** cap de janela configurável no `chaos_profile` (default 180s); reaproveitar runs antigos *se* completos (ceticismo registrado do usuário: "sempre tem algo novo" — correto, ver abaixo); tracking de treino por diretório de artefatos (sem MLflow por ora).

**Script:** `experiment/eval/gat/inventory.py` (offline, lê platform.db via `SqliteStorage`, sem subir servidor). Saída em `experiment/eval/gat/out/inventory.json` (schema versionado).

**Resultado no platform.db real (205 runs):**

- 192 completed / 9 failed / 4 running
- **192 reusáveis como estão** (label + timings + `propagation_graph`)
- 13 runs sem nada útil (os failed/running + antigos sem features)

**Distribuição por célula (chaos × serviço):** ~31 células, com contagens muito desiguais — de 2 runs (ex.: `network-loss × carts`) a 14 (ex.: `memory-hog × catalogue`). Split train/test já marcado em `is_training`.

### Leitura crítica (o que o inventário revela de verdade)

1. **`propagation_graph` ≠ o grafo que o GAT precisa.** Todos os 192 reusáveis têm `propagation_graph`, mas **189 não têm `service_graph_delta`** — que é a evidência mais próxima das golden metrics por aresta (req rate / latência / erro por chamada A→B) que o desenho exige. Ou seja: o grafo *estrutural* existe, mas as **features de aresta do nosso desenho não estão persistidas** — a coleta C1/C2 (spanmetrics por janela) é **pré-requisito real**. O usuário tinha razão: "sempre tem algo novo".

2. **Dataset pequeno e desbalanceado para GNN.** 192 runs totais, mas por célula a mediana é ~3–9. Para um GAT isso é pouco — risco real de overfitting em S1 e quebra em S2/S3. Opções a avaliar antes de treinar: (a) augmentation de grafos; (b) mais rodadas de chaos focadas nas células fracas; (c) começar com o comparador de centralidade para calibrar expectativa.

3. **Confusão de labels `chaos_type`.** Há duplicatas semânticas: `container-kill` e `orders-container-kill`, `network-latency` e `payment-network-latency` — alguns runs usam o nome do workflow (com prefixo de serviço) e outros o tipo puro. O dataset builder (A1) precisa normalizar isso, ou as células ficam artificialmente esparsas.

4. **Ground truth presente** em todos os reusáveis (tags `target:`/`fault_category:`) — a rotulagem automática funcionou. Bom.

### Próximo passo

Antes de qualquer modelagem: **verificar se os artefatos brutos (`traces_raw`/`metrics_raw`) dos runs antigos contêm os spans necessários para reprocessar o service graph offline** — isso decide se C1/C2 rodam retroativamente ou se o dataset começa só com runs novos. E normalizar `chaos_type`.

---

## Atualização 6 (mesmo dia) — Verificação dos artefatos brutos: o que dá para reaproveitar

Inspecionados os artefatos de todos os 193 runs com object store:

| Dado | Cobertura | Consequência |
|---|---|---|
| `traces_raw.json` | **0 de 193 com traces** (`count: 0` em todos) | Reprocessamento offline de spans: **impossível** |
| `metrics_raw.json` | 193/193 | Golden metrics **por serviço** (label `name`): `traffic`, `error_rate`, `latency_p50/p95/p99` ✅ |
| `propagation_graph` (nas features) | 179/192 reusáveis | Estrutura (nós/arestas/cascade) ✅, sem métricas por aresta |
| Golden metrics **por chamada A→B** (edge features) | nenhum run | Só com runs novos (C1/C2) |

**Conclusão:** a coleta de traces sempre retornou vazio no histórico (bug ou query que nunca achou nada na janela — investigar com os gates). Mas há um caminho retroativo parcial:

- **Node features: extraíveis retroativamente** de `metrics_raw` (as 3 golden metrics por serviço, nas janelas baseline/fault efetiva — janelas existem em `run.timings`).
- **Edge features: impossíveis retroativamente.** Só runs novos após C1/C2.
- **Topologia:** pode vir do `propagation_graph` do run ou da **topologia estática conhecida** da Sock Shop (front-end→catalogue/carts/orders/user; orders→payment/shipping; shipping→queue-master). A topologia é fixa; só as features mudam por run.

**Proposta de estratégia em 2 estágios** (a validar com o usuário):

- **GAT v0 (topologia + node features):** grafo = topologia estática + node features log-ratio extraídas retroativamente dos 192 runs antigos + edge features neutras (constantes). Destrava treino imediato com dataset real. Limitação registrada: sem sinal de propagação por dependência — exatamente onde falhas de rede moram.
- **GAT v1 (+ edge features):** runs novos com C1/C2 completos; dataset registra `edge_features: none|full` por amostra para medir o delta das edge features isoladamente.

**Pendente de investigação:** por que `traces_raw` está sempre vazio? Se for bug na query/janela do `collect_window`, afetará runs novos também — os gates (`symptom_presence`/`graph_connectivity`) devem capturar isso.

---

## Atualização 7 (mesmo dia) — Decisão v1 + checklist de experimento como prioridade

**Decisão do usuário: ir direto para o GAT v1 (com edge features)**, pulando o v0 de topologia estática. Implicação aceita conscientemente: o dataset depende de **runs novos** com a coleta C1/C2 completa — os 192 runs antigos servem para validar o pipeline (gates, janela efetiva, graph builder sobre node features) mas **não entram no dataset de treino** por não terem edge features. A escolha simplifica a tese (um modelo só, com o desenho completo) ao custo de precisar gerar dados novos — o que reforça a importância do inventário de células e do balanceamento.

**Segundo pedido: checklist de validação por experimento.** O usuário quer que bugs como o do `traces_raw` (193 runs coletados com 0 traces, despercebido) não se repitam — um relatório que valida todas as etapas de cada run.

Análise: isso é exatamente o gate runner + `run_quality.json` + `run_health.json` já mapeados, mas o usuário o elevou de "etapa do pipeline" a **relatório por experimento**. Duas consequências registradas:

1. **Prioridade:** o checklist vira o **primeiro entregável** — antes de C1/C2 e do graph builder. Justificativa: (a) faz falta hoje, não depois do GAT; (b) quando investigarmos o bug do traces_raw, o checklist recém-implementado deve *capturá-lo* — o bug vira o caso de teste que valida o checklist.
2. **Escopo honesto:** o checklist detecta **sintomas** ("traces_raw.count = 0 → gate falha"), não corrige o bug. A correção exige a investigação separada. Registrado para não vender falsa sensação de "nunca mais acontece".

Gate novo adicionado ao catálogo: **`collection_completeness`** — cada artefato esperado existe E tem conteúdo (ex.: `traces_raw.count > 0`; `metrics_raw` com ≥ 1 série por métrica essencial). É o gate que teria capturado o bug.

---

## Atualização 8 (mesmo dia) — Checklist implementado e validado no histórico

**Implementado:**

- `experiment/platform/backend/quality/gates.py` — catálogo de 6 gates (funções puras sobre `RunBundle`): `collection_completeness`, `window_sanity`, `label_consistency`, `graph_connectivity`, `feature_sanity`, `symptom_presence` (informativo).
- `experiment/platform/backend/quality/runner.py` — gate runner → `run_quality.json` (schema versionado; `usable` = nenhuma falha blocking; `weak_signal` separado).
- `experiment/platform/backend/quality/report.py` — `run_health.json`: timeline ordenada + checklist de artefatos (esperados/presentes/faltantes) + quality embutido. Autocontido.
- `experiment/eval/gat/checklist.py` — CLI offline que aplica o catálogo retroativamente a todos os runs (sem subir servidor), salvando `out/quality/<run>.json`, `out/health/<run>.json` e `out/checklist_summary.json`.
- `experiment/platform/tests/test_quality_gates.py` — **21 testes, todos passando** (cada gate: passa/falha/skip; agregação do runner; estrutura do run_health).

**Resultado retroativo (205 runs): 0 utilizáveis.** O checklist capturou **três bugs silenciosos reais**:

| Gate | Runs | Bug revelado |
|---|---|---|
| `collection_completeness` | 205/205 | `traces_raw` vazio em todos (já conhecido) |
| `graph_connectivity` | 192/192 | **`propagation_graph` vazio em todos** (0 nós, 0 arestas) — o inventário contou "tem a chave", mas o conteúdo é vazio. O pipeline nunca produziu um grafo real. |
| `feature_sanity` | 24/205 | **NaN/Inf em `latency_p50/p95`** — provável divisão por zero na geração de hotspots. |
| `label_consistency` | 30 | 13 sem ground truth + 17 com serviço-alvo não observado nas métricas |

**Lição:** a contagem do inventário ("192 reusáveis") era otimista — media existência de *chaves*, não de *conteúdo*. O checklist mede conteúdo. É exatamente o comportamento que o usuário pediu ("não ficar no escuro").

**Impacto no planejamento v1:** a decisão de usar runs novos fica ainda mais forte — os antigos não têm nem o grafo estrutural. E agora há **dois bugs para investigar** (traces vazio, propagation_graph vazio) antes de C1/C2, além do NaN em latência.

---

## Atualização 9 (mesmo dia) — Investigação: por que traces e propagation_graph saem vazios

Investigação ao vivo no cluster (Tempo + OTel Collector + MCP). Resultado: **não é um bug de código do engine — é uma falha estrutural de instrumentação**, e o checklist capturou corretamente.

### Cadeia de evidências

1. **Tempo tem traces AGORA** (namespace sock-shop, traces de 1 min atrás). Não é ausência de dados.
2. **Query do engine está correta** — `{ resource.k8s.namespace.name = "sock-shop" }` funciona quando há dados na janela.
3. **Runs antigos estão vazios por RETENÇÃO** — `tempo-config` tem `block_retention: 24h`; o run de 16/09 (~72h) já expirou. Isso explica runs históricos vazios *hoje*, mas não explica por que estavam vazios *logo após o run*.
4. **O evento `service_graph_delta_failed` do run registra:** `"unhandled errors in a TaskGroup (1 sub-exception)"` — erro transitório do cliente MCP/anyio que o engine **engole** com `try/except: pass`. Reproduzido ao vivo: é erro de conexão, não de query.
5. **Causa raiz estrutural:** os traces **não são distribuídos**. Inspecionando traces completos:
   - front-end: 9 spans, **todos do próprio front-end**
   - queue-master: 1 span, só dele
   - Nenhum trace cruza front-end→catalogue→etc.
   
   **Os serviços estão instrumentados com protocolos diferentes:** front-end usa OTel nativo (imagem `node18-otel`); catalogue usa **Zipkin** (`ZIPKIN=http://otel-collector:9411`). O collector tem receiver Zipkin ligado ao trace pipeline, mas **a propagação de contexto W3C (`traceparent`) não flui entre os serviços** — cada um inicia seu próprio trace. Resultado: `edges: 0` sempre → `propagation_graph` vazio → edge features impossíveis.

### Conclusão e implicação para o GAT v1

| Problema | Tipo | Correção |
|---|---|---|
| traces_raw vazio em runs antigos | retenção 24h + erro transitório engolido | aceitar (runs antigos expiraram); engine deve logar/fracassar visivelmente |
| TaskGroup error engolido | engine `try/except: pass` | tornar falha de coleta **blocking** ou pelo menos muito visível no checklist |
| **propagation_graph vazio** | **propagação W3C quebrada entre stacks OTel/Zipkin** | **bloqueante para edge features** — precisa consertar instrumentação ou aceitar v0 com node features |
| NaN em latency_p50/p95 (24 runs) | divisão por zero nos hotspots | sanitizar no graph builder |

**Decisão crítica a tomar com o usuário:** as **edge features dependem de traces distribuídos funcionando**. Sem propagação de contexto, o service graph do Tempo nunca terá arestas — e o GAT v1 (com edge features) é impossível. Opções: (a) consertar a instrumentação (padronizar OTel, garantir `traceparent`); (b) derivar arestas de **métricas de rede** (conntrack/istio/envoy) em vez de traces; (c) voltar ao v0 (topologia estática + node features) até a instrumentação estar pronta.

> O checklist fez exatamente o que foi pedido: transformou um modo de falha silencioso de 205 runs em um diagnóstico acionável em ~15 min de investigação.

---

## Atualização 10 (mesmo dia) — Graph builder (Opção D) implementado

Após a decisão pela Opção D (ver [2026-09-19-avaliacao-arestas](2026-09-19-avaliacao-arestas.md)), implementado o construtor de grafo do GAT:

- **`experiment/platform/backend/analysis/graph_builder.py`** — módulo puro (sem I/O de rede). Por run, produz o `gat_graph.json` (schema versionado):
  - **Nós** = 8 core services; features = 3 golden metrics como **log-ratio** vs baseline + `span_count` (confiança). Fonte: `metrics_raw`.
  - **Arestas** = topologia estática conhecida, bidirecionais com `direction: real|reverse`. Features: métricas `user→X` do Prometheus-v3 quando existirem (`edge_source: user_only`), senão neutras (`edge_source: static`).
  - **Label** = ground truth mapeado para o índice do nó.
  - **Sanitização:** `_log_ratio` retorna None para NaN/Inf (o bug histórico de latência) e para baseline zero (via ε).
- **`experiment/platform/tests/test_graph_builder.py`** — 11 testes passando.
- **Validado num run real:** grafo com 8 nós, 14 arestas (7 reais + 7 reversas), features log-ratio extraídas. Dois achados:
  1. runs sem ground truth → `label: None` (o builder lida; esses runs não entram no dataset);
  2. **só 3/8 nós têm `error_rate`** (só front-end/payment/user reportam erro quando zerado) — o builder preenche `None`; **o treino PyG vai precisar de máscara ou imputação para None**.

- **Suíte completa: 89 testes passando** (sem regressão).

**Próximo:** C3 (janela efetiva de fault) e o dataset builder que reprocessa os 192 runs em grafos + splits.

---

## Atualização 11 (2026-09-20) — Dataset builder versionado + splits S1–S4

**Problema encontrado:** o dataset (`dataset.jsonl`, 192 grafos) já existia em `out/`, mas **o script que o gerou não estava versionado** e os **splits S1–S4 não estavam atribuídos** (só `is_training`, regra `is_training_flag`). Violação de rastreabilidade — corrigida.

**Implementado `experiment/eval/gat/build_dataset.py`** (reconstruído, versionado, reproduzível):

1. **C3 — janela efetiva de fault** (`effective_fault_window`): transientes (container-kill, pod-delete) recortadas para 180s pós-disparo (cap no fim real da fase); persistentes usam a fase inteira. Regra registrada em `fault_window_meta` (decisão como dado).
2. **Normalização de chaos_type** (`normalize_chaos_type`): `orders-container-kill` → `container-kill`, etc. — 31 → 23 células.
3. **Splits S1–S4 anti-leakage** (`assign_splits`), por run, com regra descritiva:
   - **S1 (repetição):** réplica mais recente de cada célula = teste (time-split).
   - **S2 (held-out service):** orders/payment = teste.
   - **S3 (held-out fault):** pod-delete = teste.
   - **S4 (held-out intensity):** `None` — intensidade não registrada retroativamente.

**Resultado:** 192 grafos reproduzidos exatamente (mesmas 23 células), agora com splits. Distribuição: S1 170 train/22 test; S2 134/58; S3 142/50.

**Invariantes validados:** zero leakage em S2 (orders/payment nunca treino) e S3 (pod-delete nunca treino); cada célula ≥2 réplicas tem exatamente 1 teste em S1.

**Testes:** `test_build_dataset.py` (12 testes). **Suíte completa: 101 passando.**

**Achado para o treino:** S4 (intensidade) não é recuperável retroativamente — se a tese precisar dele, runs novos terão que registrar intensidade no chaos_profile.
