# Descrição do experimento e plano de auditoria — campanha gat-istio-v1

> **Para validação do usuário antes de disparar.** Revisar e aprovar ou ajustar.

> **Atualização 2026-09-20:** smoke test (carts pod-delete) **PASSOU** — auditoria verde:
> `run_completed` ✓, `ground_truth_present` ✓, `metrics_raw_golden` ✓,
> `istio_edges_present` ✓ (69 séries), `gat_graph_full` ✓ (edge_source: full, 14
> arestas com features). Dois bugs encontrados e corrigidos no processo:
> (1) plataforma rodava código antigo (reiniciada); (2) query Istio usava `rate()`
> (retornava 1 ponto) — corrigida para valores brutos por janela.
> Auditor: `experiment/eval/gat/audit_run.py`.

## 1. O que o experimento mede

**Pergunta:** com edge features reais (Istio), um GAT localiza o serviço raiz de
uma falha melhor que (a) o mesmo GAT com topologia estática, (b) um MLP por-nó, e
(c) heurísticas triviais?

Cada run injeta uma falha conhecida num serviço, coleta golden metrics por nó e
por aresta durante as fases, e o engine constrói um grafo (`gat_graph`) com
`edge_source: full`. Offline, treinamos o GAT sobre esses grafos e comparamos
contra os baselines — tudo com splits anti-leakage.

## 2. Fases de cada run (timeline)

Cada run segue 5 fases (total ≈ 5.5 min de coleta + overhead de chaos):

| Fase | Duração | O que acontece |
|---|---|---|
| `baseline` | 30s | carga normal, sem falha — vira o denominador do log-ratio |
| `warmup` | 30s | estabilização |
| `fault` | 90s | chaos injetado (a janela efetiva é recortada para transientes) |
| `post` | 180s | observação da recuperação |

O engine coleta métricas a cada 5s (`sampling_interval_seconds`) em todas as fases,
mais as métricas Istio por aresta, e ao fim constrói o grafo.

## 3. Matriz: qual chaos em qual serviço, por fase

**TRAIN (13 células)** — alimentam o corpus:

| Serviço | Chaos | Categoria |
|---|---|---|
| catalogue | memory-hog | memory-exhaustion |
| carts | pod-delete | pod-failure |
| orders | cpu-hog | cpu-exhaustion |
| payment | memory-hog | memory-exhaustion |
| payment | cpu-hog | cpu-exhaustion |
| user | pod-delete | pod-failure |
| shipping | pod-delete | pod-failure |
| queue-master | memory-hog | memory-exhaustion |
| front-end | cpu-hog | cpu-exhaustion |
| **catalogue** | **network-latency** | network-latency |
| **carts** | **network-loss** | network-loss |
| **user** | **http-status-code** | http-error |
| **orders** | **container-kill** | pod-failure |

**TEST (10 células)** — held-out, avaliam generalização:

| Serviço | Chaos | Categoria |
|---|---|---|
| user | memory-hog | memory-exhaustion |
| user | cpu-hog | cpu-exhaustion |
| shipping | pod-delete | pod-failure |
| queue-master | cpu-hog | cpu-exhaustion |
| front-end | pod-delete | pod-failure |
| orders | memory-hog | memory-exhaustion |
| **payment** | **network-latency** | network-latency |
| **shipping** | **network-loss** | network-loss |
| **orders** | **http-status-code** | http-error |
| **user** | **container-kill** | pod-failure |

**Notas de desenho:**
- Faults de **rede** (latência/perda) estão em negrito — são o foco do GAT-full, e
  estão held-out em **serviços diferentes** do treino (generalização S2).
- `pod-delete`/`container-kill` são transientes → a janela efetiva é recortada (C3).
- DBs e rabbitmq ficam de fora (chaos neles torna o ground truth ambíguo).
- Cada célula = 1 réplica (`replicas: 1`). Reprodutibilidade (S1) usa réplicas
  temporais quando coletarmos mais.

## 4. Como auditar que os experimentos foram bem-sucedidos e geraram métricas

Três camadas de auditoria, todas antes de escalar:

### 4.1 Smoke test (1 célula) — agora
Rodamos **1 célula rápida** (`carts pod-delete`) e auditamos o run de ponta a ponta.
Só se passar, disparamos a matriz.

### 4.2 Por run (automático, já implementado)
O checklist (`backend/quality/`) valida cada run: coleta completa, janelas
coerentes, ground truth presente, grafo conectado, sem NaN, e **edge features
presentes** (novo gate). Produz `run_quality.json` + `run_health.json` por run.

### 4.3 Auditoria de dados coletados (script)
Um auditor que, por run, verifica:
- o run chegou a `completed` (não `failed`)
- `metrics_raw` tem as 3 golden metrics por serviço com pontos > 0
- `istio_edges` existe e tem séries > 0 nas arestas da topologia
- `gat_graph` existe com `edge_source: full` e ≥ 1 aresta com feature não-nula
- ground truth (target/fault_category/intensity) presente

## 5. Critério para prosseguir à matriz completa

O smoke test passa se **todos** os itens de 4.3 estiverem verdes para o run. Se
faltar algo, paramos e corrigimos antes de gastar 2–4h na matriz.
