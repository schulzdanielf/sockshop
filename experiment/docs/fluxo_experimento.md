# Fluxo completo do experimento de chaos e análise RCA

Este documento descreve, em detalhe, como o pipeline do experimento funciona no projeto: desde o disparo do experimento até a gravação dos artefatos, coleta de métricas, geração de features, análise causal e persistência dos resultados.

O objetivo é mostrar o fluxo de dados e os serviços envolvidos, para que futuras análises, comparações e reprocessamentos possam ser feitos sem depender da execução original em tempo real.

---

## 1. Visão geral da arquitetura

O sistema é organizado em camadas com responsabilidades separadas:

- Interface HTTP / API: expõe endpoints para criar experiments, iniciar runs, consultar status e recuperar artefatos.
- Engine de orquestração: coordena a execução do experimento e o ciclo de vida do run.
- Adaptadores de infraestrutura:
  - chaos: Litmus
  - load: Locust
  - métricas: Prometheus via MCP
  - traces: Tempo via MCP
  - armazenamento: SQLite + filesystem object store
- Pipeline de análise: monta features, calcula indicadores, resume o run e aplica RCA com LLM e validação determinística.

O fluxo principal é:

1. Usuário cria um experimento.
2. O backend inicia um run.
3. O engine ativa o load generator e o chaos injector.
4. O sistema coleta métricas, traces e sinais de desempenho.
5. O engine transforma os dados em features estruturadas.
6. Gera resumo L2 para recuperação/RAG.
7. Persiste tudo em SQLite e em arquivos no object store.
8. A análise final pode aplicar regras determinísticas e/ou LLM para concluir causa raiz e categoria da falha.

---

## 2. Componentes principais e serviços chamados

### 2.1 API backend

O serviço principal do backend é criado em `experiment/platform/backend/api.py`.

Ele conecta os seguintes componentes:

- `SqliteStorage`
- `OrchestratorEngine`
- `LitmusChaosPlugin`
- `LocustLoadPlugin`
- `MCPPrometheusMetricsPlugin`
- `MCPTempoTracesPlugin`
- `NoopNotificationAdapter`

A API expõe endpoints como:

- `POST /api/experiments`
- `GET /api/experiments`
- `POST /api/runs/start`
- `POST /api/runs/{run_id}/approve`
- `POST /api/runs/{run_id}/stop`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/events`
- `GET /api/runs/{run_id}/features`
- `GET /api/runs/{run_id}/summary`
- `GET /api/runs/{run_id}/similar`
- `POST /api/runs/{run_id}/llm-analysis`

Esses endpoints são o ponto de entrada para a execução e consulta do experimento.

### 2.2 Engine de orquestração

O motor central é `OrchestratorEngine` em `experiment/platform/backend/engine.py`.

Ele coordena:

- ciclo de vida do run
- validação do carregamento e chaos
- execução das etapas do timeline
- coleta de métricas e traces
- cálculo de verdict e fases do run
- persistência dos artefatos
- geração do resumo L2
- análise final para RCA

### 2.3 Adaptador de chaos

O adaptador de chaos é `LitmusChaosPlugin`.

Responsabilidades:

- validar o perfil de chaos
- preparar o ambiente do experimento
- aplicar a falha em um serviço alvo
- acompanhar o status do workload do chaos
- parar a falha quando necessário

Ele conversa com o cluster Kubernetes/Litmus e usa manifests de ChaosExperiment.

### 2.4 Adaptador de carga

O adaptador de carga é `LocustLoadPlugin`.

Responsabilidades:

- validar o profile de carga
- iniciar o Locust com os parâmetros do experimento
- gerar tráfego na aplicação
- medir comportamento da infraestrutura durante as fases baseline, warmup, fault e post
- parar o tráfego quando o run termina

### 2.5 Adaptador de métricas

O adaptador de métricas é `MCPPrometheusMetricsPlugin` em `experiment/platform/backend/plugins/mcp_metrics_plugin.py`.

Ele é responsável por:

- consultar Prometheus via MCP
- coletar janelas temporais de métricas
- sumarizar métricas por fase
- calcular hotspots por label / serviço
- extrair sinais como:
  - error_rate
  - latency_p95
  - cpu_saturation_pct
  - memory_saturation_pct
  - oom_killed
  - pod_restarts_total
  - cpu_throttled

Esses sinais são a base para a validação do tipo de falha e para a localização do serviço ofensivo.

### 2.6 Adaptador de traces

O adaptador de traces é `MCPTempoTracesPlugin`.

Responsabilidades:

- buscar traces no backend de observabilidade
- recuperar falhas / spans de erro
- correlacionar comportamentos com serviços afetados
- identificar top failure signatures e hipóteses de RCA

### 2.7 Armazenamento

`SqliteStorage` em `experiment/platform/backend/storage/sqlite_storage.py` salva:

- metadados do experimento
- registros de runs
- eventos do run
- artefatos
- features do run
- summary_text e tags JSON
- análise LLM

Além disso, o filesystem object store salva arquivos por run em `experiment/platform/data/object_store/<run_id>/`.

---

## 3. Fluxo de dados do experimento

### 3.1 Criação do experimento

O processo começa com a criação de um experimento via API.

Fluxo:

1. Frontend ou cliente envia uma especificação JSON de experimento para `POST /api/experiments`.
2. A API chama `storage.create_experiment(...)`.
3. O storage grava a versão do experimento em SQLite.
4. O sistema retorna o `experiment_id` e a versão atual.

A estrutura do experimento contém campos como:

- id do experimento
- schema version
- timeline
- governance
- scope
- load profile
- chaos profile
- observability config
- thresholds de SLO

---

### 3.2 Início de um run

Quando o usuário dispara um run:

1. `POST /api/runs/start` recebe a requisição.
2. A API procura o experimento em SQLite.
3. O engine cria um `RunRecord` com `run_id` novo.
4. O status é definido como `PENDING` ou `PENDING_APPROVAL`, dependendo da governança.
5. O engine dispara uma thread para executar o run em segundo plano.

A execução real acontece dentro de `OrchestratorEngine._execute_run(...)`.

---

### 3.3 Fase de preparação

Dentro de `_execute_run`:

1. O engine lê o `exp.spec`.
2. Valida os perfis de load e chaos.
3. Valida os adaptadores de métricas e traces.
4. O load plugin prepara o ambiente de carga.
5. O chaos plugin prepara a falha.
6. O sistema cria eventos de timeline como:
   - `run_started`
   - `baseline_started`
   - `warmup_started`
   - `fault_started`
   - `post_started`

---

### 3.4 Timeline do run

O experimento geralmente segue fases sequenciais:

- baseline
- warmup
- fault
- post / recuperação

As fases são definidas no `timeline` do experimento e normalmente incluem:

- baseline_seconds
- warmup_seconds
- fault_duration_seconds
- post_recovery_observation_seconds
- sampling_interval_seconds

O engine executa o load durante todo esse tempo para capturar comportamento real antes, durante e depois da falha.

---

### 3.5 Ativação do traffic e chaos

A ordem de execução é:

1. Inicia o load de aplicação com `LocustLoadPlugin.start_load(...)`.
2. Se necessário, inicia a falha com `LitmusChaosPlugin.start(...)`.
3. O sistema aguarda o tempo das fases do timeline.
4. O engine registra eventos de começo e fim de cada fase.

Durante a execução, o chaos e o load trabalham em paralelo:

- o load gera pressão e tráfego real
- o chaos injeta a falha no serviço alvo
- métricas e traces capturam o impacto no sistema

---

### 3.6 Coleta de métricas

A coleta usa `MCPPrometheusMetricsPlugin`.

O engine extrai dados de Prometheus para a janela do run e calcula:

- baseline
- warmup
- fault
- post

Métricas típicas:

- `error_rate`
- `latency_p95`
- `latency_p99`
- `cpu_saturation_pct`
- `memory_saturation_pct`
- `cpu_throttled`
- `oom_killed`
- `pod_restarts_total`

A partir disso, o plugin produce:

- métricas agregadas por fase
- análise de SLO por fase
- hotspots por serviço/label
- delta entre baseline e fault
- ranking dos serviços mais afetados

---

### 3.7 Coleta de traces

A coleta de traces usa `MCPTempoTracesPlugin`.

Esse adaptador:

- lê spans e traces da aplicação
- identifica falhas por serviço
- detecta padrões de cascata
- agrega falhas por serviço e tipo
- calcula top failure signatures
- produz hipóteses de RCA

Os traces são muito importantes para responder:

- qual serviço falhou primeiro
- qual serviço sofreu efeito colateral
- qual serviço estava no centro da cascata

---

### 3.8 Construção das features do run (L1)

Depois da coleta, o engine constrói o `run_features` em `OrchestratorEngine._build_run_features(...)`.

Esse bloco monta um dicionário estruturado com:

- `verdict`
- `chaos_type`
- `slo_thresholds`
- `phase_metrics`
- `slo_violations`
- `recovery_time_seconds`
- `recovery_reference_metric`
- `affected_services`
- `top_failure_signatures`
- `rca_hypotheses`
- `trace_summary`
- `hotspots`
- `metric_hotspots`

O verdict é derivado pela lógica SLO-aware:

- sem violação + sem traces de erro => `resilient`
- há violação e recuperação dentro do limite => `degraded_recoverable`
- falha persiste ao final da janela => `degraded_persistent`

---

### 3.9 Cálculo de SLOs e recuperação

A lógica usa thresholds configurados em `spec.analysis.slo`, por exemplo:

- error_rate_threshold = 0.05
- latency_p95_threshold_ms = 800
- recovery_tolerance_pct = 0.20
- max_recovery_seconds = 600

O engine calcula:

- se cada fase violou o SLO
- qual foi a métrica que mais ultrapassou o limite
- quanto tempo depois da falha a falha volta ao limiar
- qual foi a métrica de referência para a recuperação

Isso gera os sinais necessários para classificar o resultado do experimento.

---

### 3.10 Geração do resumo L2

Após a montagem de features, o engine chama `build_run_summary_l2` e também calcula `tags`.

A intenção é transformar um run bruto em um resumo interpretável para recuperação e para LLM/RAG.

O L2 inclui:

- informação do run
- verdict final
- tipo de chaos
- duração da falha
- total do experimento
- SLO violations
- resumo da recuperação
- per-phase metrics
- failure signatures
- RCA hypotheses

Além disso, é gerado um conjunto de tags como:

- `verdict:degraded_recoverable`
- `chaos:pod-delete`
- `svc:catalogue`
- `violation:error_rate@fault`
- `recovery:fast`

Esses tags alimentam a recuperação de runs semelhantes para análise e para o pipeline de RAG.

---

### 3.11 Persistência em SQLite e object store

No fim do processamento, o engine salva:

1. Meta do run em SQLite
2. Eventos do timeline em `events`
3. Features em `run_features`
4. Summary L2 em `summary_text`
5. Tags em `tags_json`
6. Objetos em `experiment/platform/data/object_store/<run_id>/`

Arquivos típicos por run:

- `metrics_raw.json`
- `traces_raw.json`
- `run_features.json`
- `run_summary_l2.json`
- `load_summary.json`
- `chaos_artifacts.json`

Esse conjunto é o que permite replay analítico do experimento.

---

## 4. Fluxo de análise causal e RCA

Depois que o run é persistido, o sistema pode executar uma nova análise sobre o mesmo run, sem precisar repetir a execução do caos.

### 4.1 Validação de categoria de falha

A lógica de validação está em `experiment/platform/backend/analysis/fault_category_validator.py`.

Ela recebe o resultado bruto do LLM e compara com sinais objetivos da métrica:

- `oom_killed`
- `memory_saturation_pct`
- `cpu_saturation_pct`
- `cpu_throttled`
- `pod_restarts_total`
- `latency_p95`
- `latency_p99`
- `error_rate`

O objetivo é corrigir o `fault_category` quando há evidência forte e unambígua.

Exemplos de categorias tratadas:

- `memory-exhaustion`
- `cpu-exhaustion`
- `pod-failure`
- `network-latency`
- `http-error`

Se houver conflito entre duas regras fortes, a decisão é preservada e o sistema registra o conflito em `validator_meta`.

### 4.2 Localização do serviço

A lógica de localização está em `experiment/platform/backend/analysis/service_localizer.py`.

Ela tenta responder: "qual serviço é a causa raiz do problema?"

Ela usa os mesmos hotspots, mas agora ancorado por categoria:

- memory-exhaustion -> service with largest OOM delta or memory saturation
- cpu-exhaustion -> service with largest cpu throttling delta
- pod-failure -> service with largest pod restart delta
- network-latency -> service with largest latency increase
- http-error -> service with largest error-rate spike

### 4.3 Pipeline de análise final

A análise final faz:

1. recuperar as features do run
2. aplicar `validate_fault_category`
3. aplicar `localize_service`
4. comparar com o resultado bruto do LLM
5. registrar o que foi sobrescrito e por quê
6. persistir a resposta final em `llm_analysis_json`

---

## 5. Fluxo completo em um único caminho

Abaixo segue uma dimensão consolidada do sistema:

```text
Usuário / frontend
   ↓
POST /api/experiments
   ↓
SQLite: create experiment
   ↓
POST /api/runs/start
   ↓
OrchestratorEngine.start_run()
   ↓
Thread _execute_run()
   ↓
- validate load profile
- validate chaos profile
- validate metrics/traces
- prepare load plugin
- prepare chaos plugin
   ↓
Load plugin starts load traffic (Locust)
Chaos plugin starts injected failure (Litmus)
   ↓
run timeline:
  baseline -> warmup -> fault -> post
   ↓
MCPPrometheusMetricsPlugin collects raw metrics
MCPTempoTracesPlugin collects traces
   ↓
Engine computes per-phase metrics, SLO violations, recovery time
   ↓
build_run_features()
   ↓
summary L2 + tags
   ↓
SqliteStorage save run + features + summary
object_store save raw artifacts and JSONs
   ↓
Later analysis:
  - validate_fault_category
  - localize_service
  - LLM RCA
  - compare runs and similar runs
```

---

## 6. Serviços efetivamente chamados na execução real

A execução depende do conjunto abaixo:

- API FastAPI em `experiment.platform.backend.api`
- `SqliteStorage`
- `OrchestratorEngine`
- `LitmusChaosPlugin`
- `LocustLoadPlugin`
- `MCPPrometheusMetricsPlugin`
- `MCPTempoTracesPlugin`
- Prometheus via MCP observability server
- Tempo / traces backend via MCP observability server
- Kubernetes / Litmus / Argo workflows
- Servidores de aplicação Sock Shop afetados pelo chaos
- Modelo LLM (Qwen ou compatível) para análise final de RCA

Em termos de arquitetura de produção, o pipeline real envolve:

- cliente web ou linha de comando
- backend Python/FastAPI
- observability server MCP
- Prometheus / Tempo
- cluster Kubernetes
- serviço alvo do chaos
- banco SQLite local
- object store em filesystem

---

## 7. Onde ficam os artefatos finais

Os artefatos principais ficam em:

- `experiment/platform/data/platform.db`
- `experiment/platform/data/object_store/<run_id>/`
- `experiment/eval/memory_hog/out/`

Os principais artefatos são:

- `metrics_raw.json`
- `run_features.json`
- `run_summary_l2.json`
- `runs.csv`
- `evaluation.csv`

Esses arquivos são os pontos de entrada para análise offline, comparação experimental e replay de novas heurísticas.

---

## 8. Conclusão

O fluxo do experimento é um pipeline de observabilidade + chaos engineering + análise causal, com persistência em múltiplas camadas. A principal ideia é simples:

- a execução do chaos gera sinais reais de falha;
- as métricas e traces são salvas em um snapshot do run;
- esse snapshot vira entrada para features estruturadas;
- essas features alimentam a análise, o LLM e a validação determinística;
- tudo fica persistido para comparação, replay e novas análises.

Essa arquitetura é a base correta para investigar resultados, testar hipóteses de diagnóstico e repetir análises sem depender de um ambiente vivo.
