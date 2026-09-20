# Mapeamento — o que mudar para a estratégia híbrida (GAT + agentes futuros)

- **Criado em:** 2026-09-19 · **Branch:** `hibrido`
- **Objetivo:** mapear as alterações necessárias no experimento, na coleta de dados e na avaliação, com dois princípios-guia:
  1. **Evolutivo:** cada artefato produzido hoje deve ser consumível amanhã por um agente de LLM (machine-readable, autocontido, versionado).
  2. **Observabilidade fortíssima:** ao fim de um experimento, deve ser possível responder *o que aconteceu, em que etapa, e por que chegamos nesse resultado* — sem investigação arqueológica.

---

## 0. O que JÁ existe (não reconstruir)

Inventário do que o pipeline atual já entrega de rastreabilidade:

| Capacidade | Onde | Estado |
|---|---|---|
| Eventos de domínio por fase (`run_started`, `baseline_started`, `fault_started`, `run_completed`, `run_failed`…) | `engine.py` `_event()` → tabela `events` | ✅ completo |
| Timestamps de todas as fases no `run.timings` (`t_fault_start`, `t_fault_end`, `t_recovery_observed`…) | `engine.py` | ✅ completo |
| Artefatos brutos por run (metrics_raw, traces_raw, load_summary, chaos_artifacts, run_features, run_summary_l2) | object store `data/object_store/<run_id>/` | ✅ completo |
| Features L1 + resumo L2 + tags + embedding | `run_features` table | ✅ completo |
| Ground-truth auto-labeling (`target:<svc>`, `fault_category:<cat>` das tags do spec) | `engine.py` (extração) | ✅ completo |
| `service_graph_delta` por fase (evidência de WHERE, sem ground truth) | `engine.py` + `mcp_traces_plugin.collect_phase_graphs` | ✅ existe (best-effort) |
| Auditoria LLM I/O (`llm_io_audit.jsonl`) + telemetria OTel da decisão (`observability.py`) | `api.py` | ✅ existe |
| Auditoria offline de prompts (`audit.py` reconstrói contexto sem rodar LLM) | `experiment/eval/memory_hog/` | ✅ existe |
| Validadores com `validator_meta` traceável (regra, evidência, conflito) | `analysis/` | ✅ completo |
| Testes unitários dos validadores / manifests / investigators | `experiment/platform/tests/` etc. | ✅ existe |
| `evaluation.csv` + EVAL_FIELDS para A/B | runner | ✅ existe |

**Conclusão:** a base é forte. O que falta não é "criar observabilidade", é: (a) novos artefatos do GAT, (b) **gates de validação entre etapas** (hoje quase tudo é best-effort e silencioso), (c) um **relatório consolidado de saúde** por run.

---

## 1. Princípios de design para "consumível por agente de LLM no futuro"

Toda saída nova deve obedecer:

1. **JSON estruturado com schema versionado** (`"schema_version": "x.y"`) — agentes parseiam, não leem Markdown.
2. **Autocontido:** cada artefato carrega seus identificadores (`run_id`, `graph_schema_version`, `feature_names`) — nada de depender de contexto externo para ser interpretado.
3. **Decisões como dados:** toda decisão automática (janela escolhida, gate que falhou, ranking do GAT) vira um campo estruturado com *evidência*, no padrão já consagrado do `validator_meta`.
4. **Resumo humano gerado *a partir* dos dados estruturados** (não o contrário) — o mesmo JSON alimenta o agente e o relatório humano.
5. **Eventos de domínio para tudo:** cada etapa nova emite `*_started` / `*_completed` / `*_failed` na tabela `events` (já é o padrão do engine — estender).

---

## 2. Alterações por camada

### 2.1 Experimento (definição/spec)

- **Nada estrutural novo.** O spec já versiona (`schema_version`, `experiment_version`) e já carrega ground truth nas tags.
- Registrar convenção: janela de fault efetiva do GAT será derivada do `chaos_profile` (duração do estímulo) + `run.timings`, não de campos novos no spec. *Decisão pendente: se o cap de 2–3 min precisar ser configurável por fault type, ele vai no `chaos_profile` (ex.: `symptom_window_seconds`), não hard-coded.*

### 2.2 Coleta de dados (engine + plugins) — **a maior parte do trabalho**

| # | Mudança | Arquivo(s) | Artefato novo |
|---|---|---|---|
| C1 | **Service graph completo por janela** (nós = serviços, arestas = chamadas com req rate / latência / erro **agregados por chamada A→B**), coletado na janela baseline e na janela fault efetiva | `mcp_traces_plugin.py` (estender `collect_phase_graphs` ou novo método) | `service_graph_windows.json` |
| C2 | **Golden metrics por serviço (nó)** nas mesmas duas janelas — req rate, latência, erro do serviço em si | `mcp_metrics_plugin.py` (queries novas) | dentro de `service_graph_windows.json` ou `node_metrics_windows.json` |
| C3 | **Janela de fault efetiva encurtada** (~2–3 min pós-disparo, cap no fim da fase; duração do estímulo vem do manifest) | `engine.py` | campos em `run.timings`: `t_fault_window_end`, `fault_window_rule` (qual regra escolheu a janela — decisão como dado) |
| C4 | **Construtor de grafo GAT** (PyG `Data`): log-ratio com ε, counts como features auxiliares, arestas bidirecionais com marcação de direção, label = ground truth do spec | **módulo novo** `analysis/graph_builder.py` | `gat_graph.json` (representação serializada) |
| C5 | Persistência do grafo por run | `engine.py` + storage | artefato `gat_graph.json` + coluna/flag em `run_features` (`gat_graph_built`, `graph_node_count`…) |

**Formato do `gat_graph.json` (rascunho de schema):**

```json
{
  "schema_version": "1.0",
  "run_id": "run-...",
  "graph_schema_version": "gat-v1",
  "windows": {
    "baseline": {"start": "...", "end": "..."},
    "fault": {"start": "...", "end": "...", "rule": "manifest_duration|cap_180s|phase_end"}
  },
  "nodes": [{"idx": 0, "service": "catalogue", "features": {"req_rate_lr": 0.12, "latency_lr": 1.83, "error_lr": 2.40, "span_count": 1843}}],
  "edges": [{"src": 0, "dst": 5, "direction": "real", "features": {...}}, {"src": 5, "dst": 0, "direction": "reverse", "features": {...}}],
  "feature_spec": {"node": ["req_rate_lr", "latency_lr", "error_lr", "span_count"], "edge": ["req_rate_lr", "latency_lr", "error_lr", "span_count"], "epsilon": 1e-6, "encoding": "log-ratio"},
  "label": {"root_service_idx": 0, "fault_category": "memory-hog"},
  "is_training": true
}
```

> Nota: `service` names ficam no JSON para auditoria humana, mas o **tensor de features não os usa** — o modelo só vê `features` + topologia. Isso mantém S2 honesto e a auditoria legível.

### 2.3 Validação por etapa (gates) — **o que hoje é silencioso**

Hoje o engine faz quase tudo em modo `try/except: best-effort` (service_graph_delta, summary, embedding). Para o GAT isso é inaceitável: um grafo malformado treina um modelo errado e ninguém percebe. Proposta: **gate runner**.

> **Status: priorizado a primeiro entregável** (decisão 2026-09-19). O checklist de experimento — gates + `run_quality.json` + `run_health.json` — é o que garante "nunca mais ficar no escuro". O bug do `traces_raw` (193 runs com 0 traces) é o caso de teste que ele deve capturar.

```
engine._execute_run: ... → coleta → [GATE RUNNER] → features/grafo → ...
```

Cada gate = função pura + registrada num catálogo. Resultado consolidado em `run_quality.json`:

| Gate | Valida | Ação ao falhar |
|---|---|---|
| `window_sanity` | baseline e fault têm ≥ N pontos de métrica; janelas não se sobrepõem | marca run como `unusable` |
| `graph_connectivity` | grafo tem ≥ 1 aresta; nó-alvo (ground truth) está presente no grafo | `unusable` + razão |
| `feature_sanity` | features finitas (sem NaN/Inf após log-ratio); counts > 0 | `unusable` |
| `label_consistency` | ground truth existe (tags `target:`/`fault_category:`) e o serviço ∈ nós do grafo | `unusable` || `collection_completeness` | **cada artefato esperado existe E tem conteúdo** (ex.: `traces_raw.count > 0`, `metrics_raw` com ≥ 1 série por métrica essencial). Teria capturado o bug do traces_raw | `unusable` + qual artefato || `symptom_presence` (informativo) | ao menos 1 nó ou aresta com |log-ratio| > limiar — falha transiente pode legitimamente falhar aqui | **não** bloqueia; registra `weak_signal: true` |

- Run `unusable` não entra no dataset, mas **fica registrado com a razão** (aprendizado sobre falhas de coleta).
- `run_quality.json` é também a **interface do futuro agente**: "por que este run não está no dataset?" é uma query sobre esse arquivo.

### 2.4 Avaliação / treino (lado GAT)

| # | Mudança | Artefato |
|---|---|---|
| A1 | Dataset builder: platform.db → `dataset.jsonl` (1 grafo/run) + `split_assignment.json` (qual run vai para S1/S2/S3/S4 — decisão persistida, reproduzível) | `experiment/eval/gat/` (novo dir) |
| A2 | Treino com **tracking** (MLflow se já estiver no stack, senão `training_runs/<id>/` com config.json + metrics.json + curvas) | `training_runs/` |
| A3 | Avaliador: top-3/top-1/MRR por split e por categoria → `metrics.json` + **tabela Markdown gerada dos dados** | `metrics.json` + `report.md` |
| A4 | **Análise de erros estruturada:** cada erro registra `run_id`, predição top-3 com scores, label, e os sinais do grafo — para o padrão de erro ser consultável, não anedótico | `errors.jsonl` |
| A5 | **Explainability:** pesos de atenção do GAT exportados por run de teste | `attention/<run_id>.json` |

### 2.5 Observabilidade do experimento (consolidação)

- **Nada de stack nova** (Prometheus/Grafana/OTel já existem).
- Novo: **health report consolidado por run** — `run_health.json` agregando: eventos da timeline, gates (passou/falhou + razões), artefatos presentes vs esperados, sinais fracas. É o documento único "o que aconteceu neste run".
- Opcional (barato, alto valor): dashboard Grafana lendo os eventos/gates — mas o JSON é o contrato primário (agente-consuumível); o dashboard é view.

---

## 3. Plano de testes de validação por etapa

| Etapa | Testes (novos) | Tipo |
|---|---|---|
| Janela efetiva (C3) | cap de duração; fault mais curto que o cap; manifest sem duração → fallback; janela nunca ultrapassa `t_fault_end` real | unit (puro) |
| Coleta service graph (C1/C2) | mock de spanmetrics: agregação A→B correta; serviço sem chamadas de saída vira nó sem arestas; janela vazia → estrutura válida vazia | unit (mock) |
| Graph builder (C4) | log-ratio com baseline 0 → finito (ε); arestas bidirecionais com `direction` correto; label mapeia para o índice do nó certo; sem nome de serviço nos tensores | unit (puro) |
| Gates (2.3) | cada gate com caso passa/falha; run `unusable` excluído do dataset com razão registrada | unit (puro) |
| Dataset builder (A1) | determinismo (mesmo db → mesmo split); nenhum run de teste vaza em treino (invariante anti-leakage); schema do JSONL validado | unit + property-based |
| Treino (A2) | overfit em batch único (sanity: loss → ~0); shapes PyG; NaN guard | unit (smoke) |
| Avaliação (A3/A4) | top-k correto em ranking sintético; métricas por split batem com cálculo manual num caso pequeno | unit (puro) |
| Integração | run falso de ponta a ponta (fixtures) → artefatos completos + gates + grafo | integration |

Convenção: testes novos em `experiment/platform/tests/` (unit) e `experiment/eval/gat/tests/` (dataset/treino/avaliação).

---

## 4. Como isso responde "por que chegamos nesse resultado?"

Cadeia de rastreabilidade completa, todas as setas consultáveis:

```
run_id
 ├─ events (timeline: o que aconteceu, quando)
 ├─ run_quality.json (por que entrou/não entrou no dataset)
 ├─ gat_graph.json (o que o modelo viu: features, janelas, label)
 ├─ split_assignment.json (em que split caiu, por qual regra)
 ├─ training_runs/<id>/config.json+metrics.json (com que config foi treinado)
 ├─ errors.jsonl + attention/<run_id>.json (por que errou/acertou)
 └─ run_health.json (visão consolidada de tudo acima)
```

E cada arquivo é JSON versionado → o futuro agente de LLM audita qualquer elo da cadeia sem ler código.

---

## 5. Sequenciamento proposto (ordem de execução)

1. **Checklist de experimento = gates + run_quality + run_health** (2.3 + 2.5) — **priorizado a primeiro** (decisão 2026-09-19). Motivo: o bug do `traces_raw` (193 runs com 0 traces, despercebido) prova que a validação faz falta *antes* de qualquer modelagem. Implementar primeiro valida o próprio checklist quando investigarmos o bug. Barato (funções puras), aplicável retroativamente aos runs existentes.
2. **Janela efetiva (C3)** — pré-requisito de C1/C2.
3. **Coleta service graph + node metrics (C1/C2)** — offline primeiro (reprocessa runs existentes via artefatos brutos), integração no engine depois.
4. **Graph builder + persistência (C4/C5)** — com os testes puros.
5. **Dataset builder + splits (A1)** — fecha a Fase 0 do plano híbrido.
6. **Treino/avaliação/explainability (A2–A5)** — Fases 2–3 do plano híbrido.

> Passos 1–4 são independentes de PyG estar instalado (grafos como JSON; PyG só entra no passo 5/6). Isso destrava trabalho paralelo.

## 6. Decisões em aberto adicionadas por este mapeamento

1. Cap da janela de fault: hard-coded (180s) ou configurável no `chaos_profile` (`symptom_window_seconds`)? *Recomendo configurável com default 180s.*
2. Reprocessamento offline de runs antigos para o service graph (C1) — os artefatos `traces_raw`/`metrics_raw` antigos têm dados suficientes, ou o dataset começa só com runs novos? *Verificar no inventário da Fase 0.*
3. Tracking de treino: MLflow ou diretório de artefatos simples? *Recomendo diretório simples (menos infra), migrando para MLflow só se doer.*
