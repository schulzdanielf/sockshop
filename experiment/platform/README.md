?# Chaos Platform (MVP Phase 1)

This module implements a product-oriented chaos engineering platform with:

- Headless API backend (FastAPI)
- Plugin-first orchestration engine (ports/adapters)
- Basic GUI wizard and run console
- SQLite metadata store + filesystem artifact store
- Run timeline events and run comparison endpoint

## Architecture

### Skill RCA knowledge extension

The original `POST /api/runs/{id}/skill-rca` is unchanged. The new
`POST /api/runs/{id}/skill-rag` accepts `KnowledgeOptions` with `corpus_id`,
`mode` (`annotate`, `control`, `rag`), `possible_fault_types` and bounded
retrieval/investigation settings. It requires the existing `chaos_engineer`
or `admin` role. It rejects caller-supplied ground-truth labels.

Annotation is authorized only for a run pinned to
`knowledge_campaign: {corpus_id: ..., split: train}` in its experiment version;
the host obtains the service/fault labels from that training spec only after
the blind initial investigation. Test modes require a pinned `test` split and
a frozen base. Model tools never expose experiment specs or evaluator files.
The platform's header-based role gate is an operator boundary, not a substitute
for authentication or OS isolation when deploying to an untrusted environment.

`POST /api/skill-rag/{corpus_id}/freeze` accepts `training_run_ids`, verifies
their pinned training membership, and publishes only supported A/B pairs.
Records and the base use immutable, atomically published JSON and a corpus
lock. Changed configuration, embedding provider, model or Skill version is
rejected instead of silently reusing incompatible results. Every attempt,
including an explicit model/query error, is persisted as an artifact.

The web UI's **Explorar pares A/B** panel lists installed frozen corpora and
shows each source pair's A (initial evidence and symptoms) beside B (diagnostic
guide), along with its validation metadata. The read-only
`GET /api/skill-rag/corpora` and `GET /api/skill-rag/{corpus_id}/pairs`
endpoints require the `chaos_engineer` or `admin` role and validate the stored
base checksum before returning its pairs.

For commands, outputs and evaluation, see the
[70-run campaign guide](../eval/memory_hog/README.md#online-skill-rca-rag-ab-70-runs-35-train--35-test).

- Domain core: experiment definitions, run records, timeline events
- Ports: chaos/load/metrics/traces/storage/notification
- Adapters:
  - Chaos: Litmus
  - Load: Locust
  - Metrics: MCP Prometheus
  - Traces: MCP Tempo
- Engine: timeline execution, idempotency key support, persistence, summary generation

## Folder Layout

- backend: API, engine, ports, adapters, plugins, storage
- frontend: web GUI (wizard + monitor + compare)
- contracts: versioned JSON schemas for experiment and plugins
- data: generated at runtime (SQLite DB + object_store)

## Run

1. Install dependencies:

```bash
# Run once if .venv does not already exist; keep platform dependencies out of
# the Python/Conda environment used by Qwen3-14B.
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r experiment/platform/backend/requirements.txt
```

2. Start API + GUI:

```bash
HISTORY_MIGRATION_ENABLED=true \
uvicorn experiment.platform.backend.main:app --host 0.0.0.0 --port 8010 --reload
```

3. Open:

- http://localhost:8010/

The Makefile targets `experiment-platform-up` and `experiment-platform-up-bg`
enable per-run telemetry-history migration by default. Set
`HISTORY_MIGRATION_ENABLED=false` to opt out. When starting Uvicorn directly,
set `HISTORY_MIGRATION_ENABLED=true` as shown above.
For the test-only Skill/RAG campaign, use temporary dual-write instead: start
the API with `HISTORY_MIGRATION_ENABLED=false` and
`HISTORY_ARCHIVE_ENABLED=false`, then use
`experiment.platform.tools.history_dual_write` to enable and disable the
campaign-scoped historical sinks. The campaign validates online read-back
through the history MCP before running either diagnostic; see the memory-hog
README for the full canary/resume sequence.

The default listener binds to all WSL interfaces so the notebook can reach it.
Use `--host 127.0.0.1` instead if the platform should be WSL-local only.

## Run with the local Qwen3.5-9B model

This setup keeps Qwen3.5 in its separate `llama.cpp` runtime and sends its
telemetry through the existing OpenTelemetry Collector. It does not change the
Qwen3-14B model or its Python environment. Run each command in a separate
terminal from the repository root, with the platform `.venv` activated for the
Python commands.

With the Kubernetes OTLP NodePort reachable, the Makefile shortcuts are
`make model` to ensure the Qwen3.5 runtime, its native Grafana MCP tools on
`:8001`, and telemetry proxy are running, then
`make experiment-platform-up` to start the UI. The platform target also ensures
the official Grafana MCP Docker service is available in read-only SSE mode on
`127.0.0.1:18081`. It uses `GRAFANA_URL` and
`GRAFANA_SERVICE_ACCOUNT_TOKEN` from the repository `.env`.

Model-driven Telemetry Chat composes the official Grafana MCP with the existing
observability MCP; run RCA continues to use only the existing server on
`127.0.0.1:18080`. `.vscode/mcp.json` is a separate `stdio` configuration for
VS Code clients; the platform uses its own SSE container. The manual steps below
are useful when you want each process in its own terminal.

1. Start the Qwen3.5 model server:

```bash
make model-serve
```

The llama.cpp Web UI on `http://127.0.0.1:8001` discovers the official Grafana
tools from `model/qwen35_grafana_mcp.json`. They appear with the `grafana_`
prefix and are read-only. The server starts the pinned MCP image over stdio;
`make model-serve` loads the Grafana credentials from the repository `.env`
without adding them to the MCP config file.

2. Forward the observability MCP server used by run-scoped telemetry chat:

```bash
kubectl port-forward --address 127.0.0.1 -n mcp-server svc/mcp-observability 18080:8000
```

3. Forward the OTLP receiver used for model-call metrics:

```bash
kubectl port-forward --address 127.0.0.1 -n monitoring svc/otel-collector 4318:4318
```

4. Start the Qwen3.5 OpenAI-compatible telemetry proxy:

```bash
OTEL_SERVICE_NAME=llm-qwen35 \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
LLM_MODEL=qwen3.5-9b-grafana \
LLM_IO_AUDIT_PATH="$HOME/.local/share/qwen35-grafana/qwen35-audit.jsonl" \
python -m uvicorn model.qwen35_otel_proxy:app --host 127.0.0.1 --port 8011
```

5. Ensure the official read-only Grafana MCP container is running:

```bash
make ensure-grafana-mcp
```

6. Start the platform using both MCP servers while keeping client-side telemetry:

```bash
LLM_URL=http://127.0.0.1:8011/v1/chat/completions \
LLM_MODEL=qwen3.5-9b-grafana \
GRAFANA_MCP_SSE_URL=http://127.0.0.1:18081/sse \
GRAFANA_MCP_TIMEOUT_SECONDS=30 \
HISTORY_MIGRATION_ENABLED=true \
OTEL_SERVICE_NAME=rca-platform \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
LLM_IO_AUDIT_PATH="$HOME/.local/share/qwen35-grafana/platform-audit.jsonl" \
python -m uvicorn experiment.platform.backend.main:app --host 0.0.0.0 --port 8010
```

From the WSL environment, open <http://127.0.0.1:8010/>; from the notebook,
use the VS Code forwarded address or the WSL IP. Select an existing run before
using **Telemetry Chat**. This chat is run-scoped; the run's observability
configuration must provide `mcp_sse_url`, normally
`http://127.0.0.1:18080/sse`. Grafana MCP is used for read-only Grafana
datasource, dashboard, Prometheus, Loki, and Tempo queries in model-driven
Telemetry Chat. The platform listener is reachable on WSL interfaces; the
Qwen3.5 server, telemetry proxy, and Grafana MCP endpoint remain loopback-only.
Prompt/response content capture remains opt-in.

## Development & Tests

Dev tooling is configured at the repository root (`pyproject.toml` for
`black`/`isort`/`pytest`, `.flake8` for linting).

```bash
# install dev tooling
pip install -r requirements-dev.txt

# format + import sort (line length 88)
isort . && black .

# lint
flake8 experiment mcp-observability-server model

# run the unit-test suite (from the repo root)
pytest
```

The suite covers the pure-logic modules: the fault-category validator and
service localizer (`experiment/platform/tests/`), the chaos-manifest
generator (`experiment/eval/memory_hog/tests/`) and the MCP investigator
helpers (`mcp-observability-server/tests/`).

## Current MVP Capabilities

- Create/version experiment definitions from GUI wizard
- Start run and monitor timeline events in near real-time (polling)
- Stop run manually (kill-switch path)
- Store run summaries and artifacts (metrics/traces/load/chaos)
- Compare runs by run id
- Persist manual conclusion per run

## Skill RCA com Qwen

A plataforma oferece dois fluxos independentes no chat de telemetria:
**Skill RCA** (novo) e **RCA antiga** (compatibilidade). Nenhum injeta falhas
ou reinicia workloads. A nova skill é carregada explicitamente pelo
[executor](./backend/rca_skill.py) a partir do
[procedimento versionado](../../.github/skills/rca/SKILL.md), usando o mesmo
Qwen configurado por `LLM_URL`/`LLM_MODEL`; `make experiment-platform-up`
seleciona o Qwen3.5 e seu proxy.

Selecione um run encerrado com janelas baseline/fault e clique em **Skill RCA**,
ou execute da raiz:

```bash
./.venv/bin/python -m experiment.platform.rca_cli --run-id run-835437377b07

# Plataforma em outro endereço e cópia local opcional, sem sobrescrever arquivos:
./.venv/bin/python -m experiment.platform.rca_cli \
  --api-base http://127.0.0.1:8010 --run-id run-835437377b07 \
  --timeout 900 --output /tmp/skill-rca-run-835437377b07.json
```

A CLI chama o mesmo `POST /api/runs/{run_id}/skill-rca` usado pelo botão.
Durante a execução ela avisa que está aguardando; ao terminar imprime o JSON
com diagnóstico, hipóteses alternativas, investigação de restarts, consultas,
evidências e caminho do artefato. Falhas retornam código de saída 1 e mensagem
no stderr. `inconclusive` é uma investigação concluída sem causa corroborada,
não uma confirmação nem um erro de transporte.

### Leitura de traces arquivados

Para RCA de runs migrados, habilite a leitura somente leitura do bundle S3. O
API local usa o endpoint configurado e busca a credencial de serviço no Secret
Kubernetes sem imprimir ou persistir a chave. O port-forward é manual e não é
iniciado nem restaurado pelo executor:

```bash
kubectl -n monitoring-history port-forward svc/seaweedfs-history 8333:8333

HISTORY_ARCHIVE_ENABLED=true \
HISTORY_ARCHIVE_ENDPOINT=127.0.0.1:8333 \
uvicorn experiment.platform.backend.main:app --host 0.0.0.0 --port 8010 --reload
```

O leitor localiza `history-runs/<campaign>/<run_id>/source-bundle.json.gz`,
valida o SHA-256 e a paridade das contagens de traces/spans com o manifesto e
resume os spans que intersectam cada janela antes de enviá-los ao modelo. Os
IDs e timestamps de origem não são alterados; status da busca no índice Tempo e
cobertura dos spans arquivados são reportados separadamente. Se o experimento
não corresponder ao nome da campanha, informe-o explicitamente:

```bash
./.venv/bin/python -m experiment.platform.rca_cli \
  --run-id run-835437377b07 \
  --history-campaign-id exploratory-rag-20261028T191703038725Z
```

Sem `HISTORY_ARCHIVE_ENABLED=true`, a RCA mantém o caminho MCP atual. Se o
bundle não existir ou falhar na verificação, a limitação é registrada e a
leitura ao vivo continua disponível.

### Investigação e garantias

1. Compara RED/CPU/memória e latência por aresta Istio/service graph com a
   referência imediatamente anterior à falha. Mantém caller/destino e perfis
   client/server separados; as três consultas Prometheus permanecem uma única
   família `metrics`.
2. Consulta readiness, OOM e restarts; Qwen propõe até três candidatos e
   pelo menos duas hipóteses.
3. Busca logs de referência/fault/post dos candidatos e serviços com restart,
   com container exato, até seis serviços e 150 linhas por consulta.
4. Compara o grafo Tempo entre fases e aprofunda até dois traces de erro da
   fase fault. Consulta drops de interface se a tool estiver disponível.
5. Qwen produz diagnóstico JSON. O host valida serviços, categorias,
   referências e investigação de cada serviço com restart. Para confirmar,
   exige duas famílias independentes com observações na janela fault,
   cadeia causal e motivo corroborado do restart no serviço de origem.
   Vários checks Prometheus continuam contando como uma única família.
6. Janela de falha apenas planejada, ausência de corroboração ou motivo de
   restart desconhecido rebaixam a conclusão a `inconclusive`.

As respostas CLI/API e os artefatos preservam cobertura e regressões por
métrica de latência. O avaliador de campanhas Skill RCA e o multiagente
registram, por métrica, os runs com amostras em fault e com arestas em
regressão; esses sinais não são tratados como fontes independentes.

IDs/contagens verificam rastreabilidade e diversidade de fontes; não provam
sozinhos causalidade. O procedimento exige que o modelo compare hipóteses e
diferencie mecanismo, sintoma e impacto. Ground truth da injeção não é enviado.
Logs e spans são dados não confiáveis, com sanitização e consultas controladas.

Cada tentativa grava `object_store/<run-id>/skill_rca_<attempt-id>.json` e
evento com o horário da análise, inclusive quando o modelo ou MCP falham
durante a investigação. Retentativas não sobrescrevem esses arquivos nem
`rca_investigation.json`. A resposta contém `artifact_path`, `attempt_id`,
modelo, hash da skill, consultas e cobertura. Erro de investigação retorna
HTTP 502 com a referência ao artefato, sem diagnóstico de sucesso fictício.

### Limitações e ativação

- As janelas e o namespace vêm dos registros locais; métricas/logs/traces são
  reconsultados **online** pelo MCP próprio em Prometheus/Loki/Tempo, não pela
  API Grafana. O MCP configurado no experimento precisa estar acessível.
- A nova skill não faz replay de `metrics_raw`/`traces_raw` nem do snapshot
  opcional de logs da RCA antiga. Replay offline/híbrido continua pendente.
- Eventos Kubernetes históricos, exit code e resultados históricos de probes
  ainda não têm consulta dedicada. Quando necessários para explicar um
  restart, a skill registra a lacuna e o próximo check, sem inventar o motivo.
- Limites de amostragem, retenção, lookback e truncamento permanecem relevantes
  mesmo com 128k de contexto; contexto maior não cria evidência ausente.
- Um backend já iniciado sem reload precisa ser reiniciado para registrar a
  nova rota. Reinicie a plataforma, não o modelo, e recarregue o navegador:
  `make experiment-platform-up`. Com `--reload`, aguarde o reload terminar.

## Investigação multiagentes (v1)

O botão **Investigação multiagentes** executa uma investigação somente leitura
em `POST /api/runs/{run_id}/multiagent-rca`. O endpoint de planejamento
determinístico continua disponível separadamente em
`GET /api/runs/{run_id}/multiagent-rca/plan`. A investigação não injeta falhas
nem altera workloads; Skill RCA e RCA antiga continuam funcionando
separadamente.

As rotas exigem um run encerrado com fases baseline/fault válidas e um cartão
de serviços disponível para o namespace. Run inexistente retorna 404; janela,
namespace ou cartão indisponível/inválido retorna 409. O plano não consulta
MCP/modelo. A execução usa todas as fases disponíveis, independentemente do
seletor de período do chat, preserva a origem temporal e marca janelas
parciais; isso não confirma que a injeção ocorreu nem que há telemetria.

A execução coleta sinais métricos/ciclo de vida, pede ao coordenador hipóteses
e candidatos conhecidos, consulta logs para os serviços selecionados e grafo
Tempo com até dois traces candidatos, chama os três especialistas em contextos
separados e pede ao coordenador a síntese final. As consultas dos especialistas
compartilham um registro central de evidências com IDs `E###` e referências de
consulta `Q###`. Logs e spans são tratados como dados não confiáveis e
saneados antes de serem enviados ao modelo.

Após os relatórios iniciais, o coordenador pode demandar até duas rodadas
adaptativas, com uma consulta discriminativa e a reanálise de apenas um
especialista por rodada. Pode escolher logs de um serviço/fase (com filtro
literal legado `contains`, ou até cinco `patterns` literais/regex RE2 restrita
com `match_mode` explícito), grafo Tempo focado em um serviço comparando as fases, ou
saturação de CPU/memória na fase escolhida. O host fixa namespace e janelas,
rejeita serviços/fases desconhecidos, ferramentas não permitidas/indisponíveis,
argumentos extras e consultas repetidas. Não há PromQL, LogQL ou TraceQL
arbitrário gerado pelo modelo.

O coordenador recebe achados, divergências, próximas perguntas e registro de
consultas anteriores; pode parar sem nova consulta, com justificativa. Cada
rodada fica em `followup_rounds` com pergunta, motivo, estado, consulta e ID
de evidência. `stop_reason` distingue parada pelo coordenador, limite de
rodadas/orçamento, pedido inválido ou falha de execução. A interface mostra
esses dados após a conclusão. O histórico de relatórios é preservado por
`task_id`; concordância ou repetição do mesmo achado não cria nova fonte.

O host valida IDs de evidência, serviços e categorias conhecidos, cobertura
positiva na fase fault e corroboração causal por pelo menos duas famílias
independentes. `no_data`, erro de ferramenta e ausência de observações fault
são lacunas, não suporte ou contradição. Confirmações sem cobertura suficiente
são rebaixadas a inconclusivas. Restarts observados sem causa suportada ficam
com motivo desconhecido e próximos checks; restart, por si só, não explica a
falha.

Cada tentativa é imutável e salva como `multiagent_rca_<attempt_id>.json`,
com evento `multiagent_rca_completed` ou `multiagent_rca_failed`. Resultado
inconclusivo é uma resposta válida; erro de execução retorna HTTP 502 com o
caminho do artefato. Os limites padrão são 32 consultas MCP, 10 chamadas de
modelo, 120.000 tokens acumulados nas chamadas do modelo, 180 segundos e 2
rodadas adaptativas.
Sem aprofundamento, são 5 chamadas de modelo; uma decisão de parada consome
mais uma. Duas rodadas executadas consomem 9 chamadas no total. O host só
inicia uma decisão se houver chamadas para ela, um especialista e a síntese
final; o orçamento MCP e a contagem de tokens permanecem globais. Os números
acima são máximos para especialistas com dados: domínios sem observações
utilizáveis produzem relatório determinístico sem chamar o modelo. Em uma
investigação sem dados, são três chamadas (planejamento, parada e síntese).
O tamanho do contexto é estimado antes da chamada; o consumo retornado pelo modelo,
quando disponível, substitui a estimativa na contabilização. A estimativa
por caracteres não é um tokenizer exato. `max_followup_rounds: 0` preserva
o fluxo fixo para comparação. Limites de amostragem, retenção e truncamento
das fontes continuam relevantes.

### Contexto compacto e cobertura de telemetria

O provedor de contexto é determinístico, não um quarto agente LLM. Coordenador
e especialistas recebem uma projeção versionada e com hash do cartão da
aplicação; especialistas veem os candidatos e seus vizinhos diretos. Notas
livres, configuração de injeção e ground truth não entram nessa projeção.
O cartão descreve a arquitetura conhecida, não prova a topologia histórica
nem constitui evidência causal.

Os prompts usam regras causais compactas e saída JSON Schema com referências
restritas aos IDs existentes. Na reanálise, o especialista recebe somente a
nova evidência e seu último relatório; o coordenador recebe os últimos achados
e metadados de consulta, não cópias repetidas de todos os digests. Artefatos
preservam o histórico completo e métricas de tamanho por chamada em
`model_attempts`.

Logs normalizam timestamps e IDs variáveis, agrupam mensagens por padrão e
preservam um exemplo por grupo. `count_scope: sample` significa somente linhas
retornadas; truncagem considera o limite real da consulta. Quando a coleta
inicial de fault é truncada e há orçamento, uma consulta Loki adicional
agrega classes fixas de erro para todos os candidatos na janela fault.
`count_scope: window` conta a primeira classe reconhecida por linha, não
todas as mensagens ou todas as causas. As contagens agregadas não devem ser
somadas às amostras; podem representar os mesmos eventos.

Com restart observado, o host consulta métricas históricas de última
terminação, quando disponíveis. Um valor amostrado pode descrever um evento
anterior à janela e continua sendo evidência da família `metrics`.
Não foram adicionados coletores de eventos Kubernetes, logs de runtime ou
histórico de probes; essas fontes permanecem explicitamente não coletadas.

Quando o grafo Tempo está inteiramente vazio, buscas limitadas sem namespace
por fase distinguem ausência na janela de possível problema de filtro.
Esses probes só diagnosticam disponibilidade: podem encontrar outras
aplicações, não recuperam automaticamente traces do serviço investigado e
não entram no registro de evidências causais. Não distinguem, sozinhos,
retenção de falha de ingestão ou ausência de tráfego.

`telemetry_coverage` resume observações fault, erros, truncagem e estados por
serviço/fase; `coverage_probes` preserva os diagnósticos adicionais.
A interface mostra essas lacunas e a versão/hash de `application_context`.
Contagens de cobertura somam consultas e não representam eventos únicos.

### Contratos de alto nível

Os modelos em [multiagent_rca.py](backend/multiagent_rca.py) rejeitam campos
extras. Plano, relatório especializado e conclusão carregam `schema_version:
"1.0"`; alterações incompatíveis exigirão outra versão.

| Contrato | Responsabilidade |
|---|---|
| `InvestigationContext` | Run, namespace, serviços/categorias conhecidos e fases com timezone. Sem alvo, tipo de caos ou ground truth. |
| `InvestigationPlan` | `planning_only`, `execution_available: false`, etapas, dependências, limites propostos e salvaguardas. |
| `SpecialistTask` | Contrato de alto nível para perguntas discriminativas, serviços, hipóteses, fases e IDs; o executor atual monta essas tarefas internamente. |
| `CoordinatorDecision` / `FollowupRequest` | Parar com motivo ou pedir uma consulta da lista permitida, com serviço/fase e pergunta discriminativa. |
| `EvidenceObservation` | Fato observado, família, fonte live/snapshot, referência da consulta, serviço, janela, contagem, estado e limitações. |
| `SpecialistReport` | Achados, referências, lacunas e próximas perguntas. Estado `error` exige erro explícito. |
| `CausalAssessment` | Serviço, eventos observados, mecanismo provável, causa raiz, categoria, impacto, cadeia, alternativas e próximos checks separados. |
| `InvestigationBudget` | Limites aplicados pelo executor: 2 rodadas adaptativas, 32 consultas, 10 chamadas de modelo, 120.000 tokens cumulativos e 180 s. Ainda precisam de calibração com latência real. |

IDs de evidência são globais por tentativa; `query_ref` aponta para a consulta
e seu resultado sanitizado no registro central, não para texto inventado.
Observações sem cobertura não corroboram nem contradizem uma hipótese. A
identidade do run também é validada pelo host antes de aceitar uma conclusão.

### Papéis e fluxo previsto

1. **Métricas:** triagem de RED, recursos e ciclo de vida, com ordem temporal.
2. **Coordenador:** confronta pelo menos duas hipóteses e formula perguntas.
3. **Logs e traces coordenados:** contextos separados, respostas estruturadas
   e consultas fixadas pelo host para os candidatos; ausência de dados é lacuna.
4. **Coordenador:** demanda consultas discriminativas e reanálise seletiva,
   respeita o orçamento, registra parada e consolida. Não há consenso
   artificial por ausência de dados.
5. **Host:** valida referências e corroboração da mesma ocorrência; separa
   evento, mecanismo e impacto. Concordância entre agentes não é independência
   de telemetria. Restart exige investigar o motivo, não inventá-lo.

Eventos Kubernetes históricos, exit codes e probes serão uma capacidade
compartilhada, inicialmente sem um quarto especialista. O desenho não exige
quatro modelos carregados nem serviços separados: os papéis poderão usar o
mesmo Qwen com contextos isolados. Paralelismo de inferência depende de medição.

### Próximas etapas

| Parte | Entrega e critério de aceite | Dependências |
|---|---|---|
| 1 — contratos e plano | Concluída: contratos tipados, rota de planejamento e salvaguardas. | Nenhuma |
| 2 — evidências e cobertura | Parcial: registro compartilhado, IDs estáveis, saneamento, limites e estados de ausência/erro; replay offline/híbrido e cobertura histórica ainda pendentes. | 1 |
| 3 — especialistas | Parcial: especialistas de métricas/logs/traces e coordenador executam com relatórios; investigação histórica de restart ainda pendente. | 2 |
| 4 — coordenação adaptativa | Implementada: consultas seletivas, reanálise, orçamento e parada explícita. Streaming de progresso e cancelamento pelo usuário continuam pendentes; o POST é síncrono. | 3 |
| 5 — execução na API/interface | Concluída em v1: rota POST, botão, artefatos, eventos, erros e conclusão. Preserva rotas antigas. | 1–3 |
| 6 — avaliação comparativa | Pendente: Skill atual vs agente único e especialistas nas mesmas evidências congeladas. Medir causa, localização, abstinência, falsa confirmação, custo e latência. | 5 |

Não flexibilizar a confirmação apenas para reduzir abstinências nem usar
ground truth nos agentes.
Um backend sem reload precisa ser reiniciado para registrar esta nova rota;
recarregue também a página. Nenhum reinício é feito automaticamente.

## Fluxo de RCA antiga

A investigação é iniciada por `POST /api/runs/{run_id}/rca`. Ela analisa somente
o run existente; não injeta nem reinicia chaos.

```mermaid
flowchart TD
   A[POST /api/runs/{run_id}/rca] --> B{Run tem fases baseline e fault?}
   B -- Não --> B1[HTTP 409]
   B -- Sim --> C[Carrega run, experimento, namespace e endpoint MCP]
   C --> D[Escolhe janela de referência]
   D --> D1[Últimos 45 s antes do fault; fallback para baseline do run se curta]
   D1 --> E[Descobre tools MCP e consulta métricas RED/resource]
   E --> F{Há próximo check obrigatório?}
   F -- Sim --> G[Host executa prontidão, OOM, restarts e Tempo por fase]
   G --> G1[Analisa no máximo 2 traces de erro da fase fault]
   G1 --> H[Guarda resultados saneados e envia digests compactos ao modelo]
   H --> F
   F -- Não --> I[Modelo escolhe tools adicionais ou sintetiza conclusão]
   I --> J{Resposta tem formato válido e serviço conhecido?}
   J -- Sim --> K[Host consulta Loki com serviço e janelas fixados]
   K --> L[Modelo sintetiza a causa sem trocar o serviço]
   L --> M[Host anexa evidências e limitações]
   J -- Não --> N[Modelo recebe feedback e tenta corrigir uma vez]
   N --> O{Resposta corrigida é válida?}
   O -- Sim --> K
   O -- Não --> P[Registra avaliação indisponível; não inventa causa]
   M --> Q[Persiste artefato e evento RCA concluído]
   P --> Q
   Q --> R[Retorna resposta, evidências, tools, iterações e stop_reason]
```

### Etapas

1. **Validação e contexto:** a rota exige janelas `baseline` e `fault`; carrega
  o run e a versão do experimento para obter namespace e endpoint MCP. Fases
  ausentes ou endpoint não configurado resultam em HTTP 409; run/experimento
  inexistente resulta em HTTP 404.
2. **Referência:** quando há histórico suficiente, a baseline da RCA é
  substituída pelos últimos 45 segundos anteriores ao início do fault. Isso
  reduz o viés do ramp-up de carga. Se a janela for curta ou inválida, usa-se a
  baseline original do run. A fonte escolhida fica registrada como
  `reference_source`.
3. **Métricas iniciais:** o host chama `get_run_golden_signals` com as janelas
  de referência e fault. O modelo recebe um digest das métricas, não o payload
  completo.
4. **Investigação MCP:** as ferramentas disponíveis são descobertas via MCP e
  a investigação é limitada a até 10 chamadas. Antes da localização, o host
  força os checks disponíveis nesta ordem: `check_service_readiness`,
  `check_oom_kills`, `check_pod_restarts` e `tempo_service_graph_phases`, mesmo
  que uma indisponibilidade ou restart já tenha sido detectado. Readiness
  compara baseline, fault e post quando disponíveis. Um incremento de restart
  fica associado a pod/container e ao intervalo entre os scrapes que o
  observaram; não é apresentado como horário exato do evento. Transições de
  readiness também preservam essa precisão limitada. Depois do grafo, o host
  chama `tempo_analyze_trace` para no máximo dois IDs de traces de erro
  encontrados na fase `fault`; o modelo recebe spans de erro, caminho crítico,
  operações, dependências e latências relevantes, não o trace OTLP bruto.
  O modelo então propõe uma localização. Quando ela é válida e corresponde a um
  serviço conhecido, o host fixa esse serviço e consulta Loki antes da síntese.
  Se `check_service_logs` estiver disponível, usa suas consultas filtradas e
  limitadas nas fases disponíveis. Para MCPs antigos que só expõem
  `loki_range_query`, o host constrói LogQL a partir do serviço e namespace do
  cartão, consulta apenas a janela `fault` e limita o retorno a 300 linhas.
  Nesse fallback, o modelo não recebe a tool genérica: não escolhe LogQL,
  timestamps, limite nem outro serviço.
  Se a latência subir muito
  e a causa continuar sem explicação, ele pode chamar
  `check_network_packet_drops`, que compara drops de receive/transmit entre
  baseline e fault. Essa análise é opcional; latência alta, sozinha, não prova
  perda de pacotes. Os contadores são do nó/interface, não localizam um serviço,
  e não provam causalidade isoladamente.
  Janelas e namespace são controlados pelo host; o modelo não pode alterá-los.
5. **Escopo das evidências:** logs são filtrados por serviço/namespace e por
  janelas definidas pelo host; o caminho de compatibilidade consulta somente a
  fase `fault`. Linhas, assinaturas, spans e mensagens de erro são limitados e
  redigidos antes de entrar no contexto do modelo ou no artefato. Logs e
  atributos de spans são dados não confiáveis e nunca instruções. O digest do
  Tempo preserva assinaturas de falha, destino, operação, trace ID, fase e
  horário observado. Contagens, nós, arestas e deltas continuam disponíveis;
  caminhos heurísticos não são prova causal. `no_data` e arestas ausentes não
  provam ausência de evento ou chamada.
6. **Síntese:** a localização é fixada após a primeira resposta válida; a
  resposta causal precisa manter o mesmo serviço. Um restart isolado é evento
  de ciclo de vida, não mecanismo causal. Timeouts isolados não confirmam perda
  de rede, e restart posterior a uma falha de conexão não prova que ela o causou.
  A resposta do modelo tem duas linhas (`Localização provável` e `Causa
  provável`); o host acrescenta `Evidências` e `Limitações`. Sem evidência
  suficiente, o resultado correto é mecanismo não confirmado, não uma causa
  determinística.
7. **Persistência e interface:** `rca_investigation.json` mantém janelas,
  `reference_source`, chamadas e resultados saneados, resposta, iterações,
  `stop_reason`, tentativas do modelo limitadas/redigidas (`model_attempts`) e
  `evidence_bundle` versão 1 com localização, cobertura, timeline compacta,
  mensagens de log/span e IDs de evidência. A API retorna o bundle, que aparece
  recolhido abaixo da resposta do RCA no chat de telemetria.

### Snapshot de evidências (opt-in)

A captura automática antecipada de logs fica desativada por padrão; a consulta
sob demanda feita pelo botão de RCA continua limitada às janelas do run. Para
habilitar a captura antecipada, configure
`observability.rca_evidence_snapshot.enabled: true`.
`max_total_lines` aceita no máximo 1200 linhas e é distribuído uniformemente
entre os serviços do cartão e as fases disponíveis; `timeout_seconds` é limitado
a 15 segundos. O artifact `rca_evidence_snapshot.json` guarda mensagens já
saneadas, manifest de janelas/cobertura e referências a métricas, traces e
observation window, sem duplicar esses payloads. Falhas de captura ou gravação
nunca mudam o verdict; sem snapshot compatível, uma RCA usa o MCP live. Snapshot
e replay precisam coincidir em namespace, serviço e janelas para serem usados.

O limite de até 10 iterações restringe a investigação; uma síntese final e a
tentativa de correção de formato podem ocorrer depois do loop de tools.

## Product Evolution Hooks Implemented

- Contracts versioning (`schema_version`)
- Domain events (`run_started`, `warmup_started`, `fault_started`, `run_completed`, etc.)
- Idempotency support with `idempotency_key`
- Approval gate (`requires_approval` -> `pending_approval`)
- Separation between raw artifacts and summarized run data
- Headless API as primary integration surface

## Notes

- Litmus adapter uses `kubectl` and optional manifest apply/delete.
- Locust adapter executes configured command and tracks process by pid.
- MCP adapters expect SSE endpoint configured in experiment observability section.

---

## RAG Pipeline (Phases A & B)

The platform produces a layered representation of every run so that a 4k‑context
LLM (Qwen 14B served at `localhost:8001/v1/chat/completions`) can reason over historical
campaigns without loading raw Prometheus/Tempo payloads.

```
L0  raw artifacts            metrics_raw.json, traces_raw.json, load_summary, chaos_artifacts
                                  │
L1  structured features       run_features.json   ← Phase A
                                  │
L2  RAG‑friendly summary      summary_text + tags ← Phase B
                                  │
L3  retrieval‑augmented prompt   target + top‑K neighbours (≤ 4k tokens)
```

### Phase A — Structured features (L1)

For every completed run the engine builds an L1 feature record with:

| Field | Description |
| --- | --- |
| `verdict` | `resilient` / `degraded_recoverable` / `degraded_persistent` — derived from SLOs |
| `chaos_type` | Litmus engine name (or manifest path) used |
| `slo_thresholds` | The thresholds taken from `spec.analysis.slo` |
| `phase_metrics` | `{metric_id: {baseline,warmup,fault,post: stats}}` (`count,min,max,mean,p95`) |
| `slo_violations` | `[{phase, metric, threshold, observed, delta_pct}]` |
| `recovery_time_seconds` | Seconds after `fault_end` until metric returned to `baseline×(1+tolerance)`, two consecutive samples |
| `recovery_reference_metric` | Which metric drove the recovery measurement |
| `affected_services` | Deduped from trace failure signatures + error spans + hot spans |
| `top_failure_signatures` | Up to 5, ranked by `occurrence_count × max_duration_ms` |
| `rca_hypotheses` | Top 3 HIGH/MEDIUM confidence hypotheses from MCP traces |
| `trace_summary` | `{trace_count, error_trace_count, duration_ms}` |

SLO‑aware verdict logic (`OrchestratorEngine._build_run_features`):

- no SLO violation **and** no error traces → **resilient**
- violations exist **and** `recovery_time ≤ max_recovery_seconds` → **degraded_recoverable**
- violations remain at the end of the post window → **degraded_persistent**

The SLO is configured in the GUI wizard (step 5) and serialized as:

```json
"analysis": {
  "slo": {
    "error_rate_threshold": 0.05,
    "latency_p95_threshold_ms": 800,
    "recovery_tolerance_pct": 0.20,
    "max_recovery_seconds": 600
  }
}
```

By convention, the wizard ships Prometheus queries with ids `error_rate` and
`latency_p95`; those are the metrics consumed by the SLO/recovery logic.

### Phase B — L2 summary + retrieval

For each completed run the engine also generates a deterministic Markdown
summary (~400–700 tokens) plus a controlled‑vocabulary tag set:

```
# Run <id> (experiment <id>)
Verdict: degraded_recoverable | chaos_type: pod-delete | fault_duration: 120s | total: 480s
SLOs: error_rate<=0.050, p95<=800ms, tolerance=0.20, max_recovery=600s

## SLO violations
- [fault] error_rate: observed=0.18 > threshold=0.05 (Δ=260.0%)

## Recovery
Recovered in 47s (reference metric: `error_rate`).

## Per-phase metrics
- error_rate: baseline:mean=0.01 p95=0.02 | fault:mean=0.18 p95=0.21 | post:mean=0.02 p95=0.03
...

## Top failure signatures
- HTTP 500 on `catalogue` (x12, max_duration=842ms)

## RCA hypotheses
- [HIGH] Cascading failure from catalogue → front-end retries exhausted
```

Tag namespace used for retrieval:

| Prefix | Example |
| --- | --- |
| `verdict:` | `verdict:degraded_recoverable` |
| `chaos:` | `chaos:pod-delete` |
| `svc:` | `svc:catalogue` |
| `violation:` | `violation:error_rate@fault` |
| `recovery:` | `recovery:fast` (≤60s), `recovery:medium` (≤300s), `recovery:slow`, `recovery:none` |

Similarity is a deterministic Jaccard over the tag set (+0.1 bonus when
verdicts match). The interface in `analysis/retrieval.py` is intentionally
swap‑in compatible with future sentence‑transformer / Qwen embeddings.

### Storage

New table populated by the engine at run completion:

```sql
CREATE TABLE run_features (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    chaos_type TEXT,
    verdict TEXT,
    slo_violation_count INTEGER NOT NULL DEFAULT 0,
    recovery_time_seconds REAL,
    affected_services_json TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL,
    summary_text TEXT,                      -- L2
    tags_json TEXT NOT NULL DEFAULT '[]',   -- L2 retrieval keys
    created_at TEXT NOT NULL
);
-- Indexes: experiment_id, verdict, chaos_type
```

A copy of the L1 features is also saved as artifact `run_features.json`,
and the L2 record as `run_summary_l2.json`.

### API endpoints (added)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runs/{run_id}/features` | Full L1 feature record |
| `GET` | `/api/runs/{run_id}/summary` | L2 summary + tags |
| `GET` | `/api/runs/{run_id}/similar?limit=5` | Top‑K similar past runs (tag Jaccard) |
| `GET` | `/api/runs/{run_id}/rag-context?limit=3` | Bundle target + neighbour summaries ready for prompt assembly |
| `GET` | `/api/features?experiment_id=&verdict=&limit=` | List feature index records |

Example RAG context response (truncated):

```json
{
  "target": {
    "run_id": "r-2026-05-22-001",
    "verdict": "degraded_recoverable",
    "tags": ["verdict:degraded_recoverable", "chaos:pod-delete", "svc:catalogue", "violation:error_rate@fault", "recovery:fast"],
    "summary_text": "# Run r-2026-05-22-001 ..."
  },
  "neighbours": [
    {"run_id": "r-...-998", "score": 0.71, "verdict": "degraded_recoverable", "summary_text": "..."},
    {"run_id": "r-...-944", "score": 0.55, "verdict": "degraded_persistent", "summary_text": "..."}
  ]
}
```

### Module layout

```
backend/
├── analysis/
│   ├── __init__.py
│   ├── summary.py        # build_run_summary_l2, build_summary_tags
│   ├── retrieval.py      # score_similarity, rank_similar_runs, rank_by_embedding
│   ├── embeddings.py     # HashingEmbeddingProvider, SentenceTransformerProvider
│   ├── prompt.py         # assemble_prompt (4k‑token RAG)
│   └── llm_client.py     # call_llm, parse_verdict_response, llm_health
├── plugins/
│   ├── mcp_metrics_plugin.py   # + summarize_per_phase, find_recovery_time
│   └── mcp_traces_plugin.py    # + aggregate_failures
├── engine.py             # _build_run_features, SLO-aware verdict, L2 + embedding hooks
└── storage/sqlite_storage.py   # run_features + summary/tag/embedding/llm_analysis methods
```

### Phase C — Semantic retrieval with embeddings

The Jaccard ranker from Phase B is now backed by a vector store and
cosine similarity, while keeping the `analysis.retrieval` interface
untouched (the API decides which mode to use).

**Pluggable embedding providers** (`analysis/embeddings.py`):

| Provider | When used | Notes |
| --- | --- | --- |
| `HashingEmbeddingProvider` (default) | `RAG_EMBEDDING_PROVIDER` unset or `hashing` | Zero‑dependency feature‑hashing vectorizer (signed hashes, 1‑2 grams). 256‑dim by default. Deterministic. |
| `SentenceTransformerProvider` | `RAG_EMBEDDING_PROVIDER=sentence-transformers` | Lazy‑loads `sentence-transformers`; defaults to `all-MiniLM-L6-v2` (384‑dim). Override with `RAG_EMBEDDING_MODEL`. |

Both expose:

```python
class EmbeddingProvider(Protocol):
    name: str            # used to invalidate stale vectors across providers
    dim: int
    def embed(self, text: str) -> np.ndarray: ...
    def embed_batch(self, texts: Sequence[str]) -> np.ndarray: ...
```

Vectors are L2‑normalized at write time so cosine reduces to a dot
product. They are stored as raw float32 bytes in the `run_features`
table via three additional columns:

```sql
ALTER TABLE run_features ADD COLUMN embedding_blob BLOB;
ALTER TABLE run_features ADD COLUMN embedding_provider TEXT;
ALTER TABLE run_features ADD COLUMN embedding_dim INTEGER;
```

(The migration runs automatically on startup for older databases.)

**Engine integration** — after persisting the L2 summary the engine
calls `get_embedding_provider().embed(summary_text)` and stores the
vector. Failures degrade gracefully: retrieval falls back to the tag
Jaccard mode if the target run has no embedding or the cached vector
was produced by a different provider.

**Retrieval API (updated)**:

| Method | Path | Modes |
| --- | --- | --- |
| `GET` | `/api/runs/{id}/similar?mode=embedding\|tags&limit=5` | Default `embedding`, auto‑falls back to `tags` |
| `GET` | `/api/runs/{id}/rag-context?mode=embedding\|tags&limit=3` | Returns `mode` actually used |
| `GET` | `/api/embeddings/info` | Provider name, dim, count of runs pending reindex |
| `POST` | `/api/features/reindex?limit=500` | Recomputes embeddings for runs missing or stale under the current provider; uses `embed_batch` for throughput |

Example response:

```json
{
  "run_id": "r-2026-05-22-001",
  "mode": "embedding",
  "neighbours": [
    {"run_id": "r-...-998", "score": 0.91, "verdict": "degraded_recoverable", "chaos_type": "pod-delete", "tags": ["..."]},
    {"run_id": "r-...-944", "score": 0.74, "verdict": "degraded_persistent", "chaos_type": "pod-delete", "tags": ["..."]}
  ]
}
```

**Switching providers** without losing history:

```bash
# Restart with sentence-transformers
export RAG_EMBEDDING_PROVIDER=sentence-transformers
export RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
uvicorn experiment.platform.backend.main:app --port 8010 --reload

# Re-embed existing summaries under the new provider
curl -X POST 'http://localhost:8010/api/features/reindex?limit=1000'
```

Until reindex finishes, mixed‑provider runs are skipped from semantic
search (the candidate query filters by `embedding_provider`) and the
endpoint falls back to tag mode for the target. After reindex, all
runs become semantically searchable again. The `embedding_provider`
column means embeddings produced by different models never get mixed
in cosine ranking.

### Phase D — LLM verdict with citations

Phase D closes the RAG loop by feeding `/rag-context` into the local
Qwen 14B server (`model/server.py`, default `http://localhost:8001/v1/chat/completions`).
The assembler stays under the 4k‑token window and asks the model to
return a structured JSON answer that always cites the neighbour runs it
used.

**Prompt assembly** (`analysis/prompt.py`):

```python
prompt, meta = assemble_prompt(
    rag_context,          # payload of GET /api/runs/{id}/rag-context
    budget_tokens=3200,   # leaves ~900 tokens of headroom for the answer
    max_neighbours=3,     # K retrieved past runs
    target_share=0.45,    # fraction of the budget for the target summary
)
# meta = {
#   "prompt_tokens_estimate": 362,
#   "target_tokens_estimate": 90,
#   "neighbour_count": 2,
#   "citations": [{"ref": "n1", "run_id": "r-998", ...}, ...],
#   "budget_tokens": 3200,
# }
```

Token accounting uses a cheap chars/4 heuristic — Qwen's tokenizer runs
in a separate process and we just need to stay safely below the 4k
context window. The body is truncated proportionally: target summary
gets `target_share * budget`, the remainder is split evenly across the
embedded neighbours, each prefixed with a `[n1]`/`[n2]` citation tag.

**LLM client** (`analysis/llm_client.py`):

| Function | Purpose |
| --- | --- |
| `call_llm(prompt, max_new_tokens=512, url=LLM_URL)` | POST to OpenAI-compatible `/v1/chat/completions`; raises `LLMClientError` on transport/JSON failure. |
| `parse_verdict_response(text)` | Extracts the first JSON block, validates `verdict ∈ {resilient, degraded_recoverable, degraded_persistent}`, clamps `confidence` to `[0,1]`, returns `parse_error` instead of raising. |
| `llm_health()` | `GET /health` probe; returns `{ok, ...}` with the model name. |

Environment knobs: `LLM_URL` (default `http://localhost:8001/v1/chat/completions`),
`LLM_TIMEOUT` (default `120` seconds).

**Persistence** — `run_features` gains two columns:

```sql
ALTER TABLE run_features ADD COLUMN llm_analysis_json TEXT;
ALTER TABLE run_features ADD COLUMN llm_analysis_at   TEXT;
```

(Idempotent migration on startup, like the embedding columns.)

**Endpoints**:

| Method | Path | Notes |
| --- | --- | --- |
| `GET`  | `/api/llm/health` | Probes the Qwen server. |
| `POST` | `/api/runs/{id}/llm-analysis?limit=3&mode=embedding&max_new_tokens=512&budget_tokens=3200&force=false` | Reads cache unless `force=true`. Builds RAG context, assembles prompt, calls Qwen, parses, persists. Returns `502` if the LLM is unreachable. |
| `GET`  | `/api/runs/{id}/llm-analysis` | Returns the cached analysis or `404`. |

Response shape:

```json
{
  "run_id": "r-2026-05-22-001",
  "heuristic_verdict": "degraded_recoverable",
  "cached": false,
  "analysis": {
    "verdict": "degraded_recoverable",
    "confidence": 0.82,
    "reasoning": "Recovery in 42s matches the pattern of past run [n1] under the same chaos...",
    "citations": ["n1"],
    "follow_ups": ["Add PodDisruptionBudget to catalogue"],
    "parse_error": null,
    "raw_response": "...",
    "rag_mode": "embedding",
    "prompt_meta": {
      "prompt_tokens_estimate": 362,
      "neighbour_count": 2,
      "citations": [{"ref": "n1", "run_id": "r-998", "score": 0.91, "verdict": "degraded_recoverable"}]
    },
    "max_new_tokens": 512
  }
}
```

If the model returns prose without a JSON object, `verdict` stays
`null` and `parse_error` describes what went wrong — the call still
succeeds (the raw response is preserved for offline inspection).

### Phase E — Operator feedback loop

Phase E closes the human‑in‑the‑loop side: the operator can confirm,
correct or flag every run, and the platform persists those labels next
to the heuristic and LLM verdicts so they can later be used as a
labelled evaluation set.

**Storage** — `run_features` gains four columns (idempotent migration):

```sql
ALTER TABLE run_features ADD COLUMN operator_label     TEXT;
ALTER TABLE run_features ADD COLUMN operator_label_at  TEXT;
ALTER TABLE run_features ADD COLUMN operator_label_by  TEXT;
ALTER TABLE run_features ADD COLUMN operator_note      TEXT;
```

Allowed labels: `resilient`, `degraded_recoverable`, `degraded_persistent`, `unsure`.

**Endpoints**:

| Method | Path | Notes |
| --- | --- | --- |
| `GET`  | `/api/runs/{id}/verdicts` | Consolidated view: heuristic, LLM verdict + confidence, operator label, plus `agreement`/`disagreement` booleans. |
| `GET`  | `/api/verdicts?experiment_id=&disagreement_only=&limit=` | Same shape, list form. `disagreement_only=true` keeps only runs where at least two of the three verdicts differ. (Path lives under `/api/verdicts` to avoid colliding with `/api/runs/{id}/...`.) |
| `POST` | `/api/runs/{id}/operator-label` | Body `{label, by?, note?}`. 404 if the run has no `run_features` yet. |

Response example:

```json
{
  "run_id": "r-2026-05-22-001",
  "heuristic_verdict": "degraded_recoverable",
  "llm_verdict": "degraded_persistent",
  "llm_confidence": 0.7,
  "operator_label": "degraded_recoverable",
  "operator_label_by": "alice",
  "agreement": false,
  "disagreement": true
}
```

`agreement` is `true` when every available verdict is identical (a run
with only the heuristic populated counts as agreement); `disagreement`
is its negation, or `null` when nothing has been recorded yet.

**GUI** — the *Histórico de Runs* table now shows three verdict columns
(`Heur.` / `🤖 LLM` / `👤 Op.`), a ⚠ marker on runs where they diverge,
and:

* a `<select>` per row that PATCHes the operator label inline;
* a `🤖` button that triggers `POST /api/runs/{id}/llm-analysis?force=true`
  on demand (the engine does not call the LLM automatically);
* a "só divergências" checkbox that toggles `disagreement_only=true`
  on `/api/verdicts` and filters the table.

This setup gives a cheap path to bootstrap a labelled dataset for the
future Phase F/G evaluation pipelines without ever forcing the operator
to leave the platform UI.

### Roadmap

- **Phase F** — optional vector index (sqlite‑vss / faiss) once the run corpus grows past a few thousand records and the linear cosine scan becomes a bottleneck.
- **Phase G** — automatic post‑run LLM analysis hook in the engine (currently on‑demand via `POST /api/runs/{id}/llm-analysis` or the GUI 🤖 button).
- **Phase H** — offline evaluation script that uses `operator_label` as ground truth and reports heuristic vs LLM accuracy/confusion matrices.

---

## Phase F1 — Trace‑aware retrieval & temporal feature engineering

Phases A–E gave us a deterministic L1 → L2 → vector pipeline with a
semantic RAG retriever. Two systematic blind spots remained:

1. **Two runs with identical aggregate stats but different propagation
   shapes** (e.g. `front-end → orders → carts` vs `front-end → carts`)
   were collapsed together by the cosine ranker.
2. **The dynamics of a fault** — how fast it ramps, when it peaks, how
   the recovery curve looks — were thrown away by per‑phase
   min/max/mean aggregates.

Phase F1 closes both gaps without changing the existing LLM contract.

### Temporal features

A new pure module — [experiment/platform/backend/analysis/temporal.py](experiment/platform/backend/analysis/temporal.py)
— derives per‑metric descriptors from the raw Prometheus range‑query
points and persists them under `features.temporal_features`:

| Field                              | Meaning                                                       |
| ---------------------------------- | ------------------------------------------------------------- |
| `time_to_first_violation_s`        | Seconds between fault start and first point above the SLO/baseline threshold. |
| `time_to_peak_s` / `peak_value`    | When and how high the metric peaked inside the fault window.  |
| `max_abs_derivative`               | Largest `|dv/dt|` between consecutive points (proxy for "violence"). |
| `coefficient_of_variation_fault`   | `stddev/mean` during the fault — instability indicator.       |
| `recovery_shape`                   | One of `step`, `exponential`, `oscillating`, `persistent`, `unknown`. |
| `overall_recovery_shape`           | Worst shape across all metrics (`persistent > oscillating > exponential > step`). |

`recovery_shape` is classified on the post‑fault window using the
fraction of time above target, the number of sign changes of the first
derivative and how quickly the series collapses back to baseline. This
turns *"recovered in 90s"* into *"recovered in 90s with an oscillating
curve"* — a much richer signal for the operator and the LLM.

### Propagation graph (trace‑aware retrieval)

`MCPTempoTracesPlugin.build_propagation_graph(raw)` aggregates the
per‑trace `dependency_map` returned by `tempo_analyze_trace` into a
single service‑level graph for the run, persisted under
`features.propagation_graph`:

```json
{
  "nodes": ["front-end", "orders", "carts"],
  "edges": [
    {"from": "orders", "to": "carts", "trace_count": 12, "error_count": 8},
    {"from": "front-end", "to": "orders", "trace_count": 12, "error_count": 5}
  ],
  "cascade_order": [
    {"service": "orders", "t_offset_s": 0.0},
    {"service": "carts",  "t_offset_s": 1.234}
  ],
  "edge_count": 2,
  "error_edge_count": 2,
  "trace_count": 12
}
```

To support the cascade ordering, `collect_window` now also preserves
`startTimeUnixNano`, `rootServiceName` and `durationMs` for every
analysed trace.

### Hybrid retrieval mode

`analysis/retrieval.py` gains two new helpers:

* `score_graph_similarity(qg, cg)` — returns a multi‑component
  similarity dict containing `nodes_jaccard`, `edges_jaccard`,
  `cascade_overlap` (order‑aware via Kendall‑tau on the common
  services) and the final blended `score`.
* `rank_hybrid(query_vec, query_graph, candidates, weights)` —
  combines L2‑summary cosine similarity with `score_graph_similarity`.
  Default weights are `0.6 semantic / 0.4 graph`. Weights are
  renormalised per candidate so a missing component never penalises the
  rest of the score.

Both `GET /api/runs/{id}/similar?mode=hybrid` and
`GET /api/runs/{id}/rag-context?mode=hybrid` now expose this ranker.
The hybrid `/similar` response carries the extra fields
`semantic_score`, `graph_score`, `cascade_overlap_services` and
`graph_detail` to keep the ranking explainable in the GUI/notebook.

To make this efficient the storage helper
`iter_run_embeddings(..., include_features=True)` returns
`features_json` in the same query that fetches the embedding blob, so
the hybrid path still costs a single SQL round‑trip.

### Updated L2 summary & tag namespace

`build_run_summary_l2` now emits two extra sections:

```text
## Temporal dynamics
Overall recovery shape: **oscillating**
- error_rate: ttfv=12s | ttp=45s | |dv/dt|max=0.0134 | cv=0.62 | shape=oscillating

## Propagation graph
Cascade: orders(+0.00s) -> carts(+1.23s)
Edges: 2 total, 2 with errors (top 2 below)
- orders → carts (traces=12, errors=8) ❗
- front-end → orders (traces=12, errors=5) ❗
```

`build_summary_tags` adds three new controlled‑vocabulary tags so the
existing Jaccard retriever also benefits from the new signals:

| Tag           | Example                              |
| ------------- | ------------------------------------ |
| `cascade:`    | `cascade:orders->carts->shipping`    |
| `edges:`      | `edges:7`                            |
| `shape:`      | `shape:oscillating`                  |

These tags are consumed transparently by `mode=tags`, so even
deployments without embeddings benefit from Phase F1.

### Engine integration

`engine._build_run_features` calls `compute_temporal_features` and
`MCPTempoTracesPlugin.build_propagation_graph` in best‑effort mode
right after the existing per‑phase aggregation. Failures are logged as
events but never abort the run; the new fields default to empty dicts
so older replays remain compatible.

### Module layout (additions)

```
experiment/platform/backend/analysis/
├── temporal.py        # NEW — Phase F1 temporal features
├── retrieval.py       # + score_graph_similarity, rank_hybrid
└── summary.py         # + Temporal/Propagation sections & tags
experiment/platform/backend/plugins/
└── mcp_traces_plugin.py  # + build_propagation_graph,
                          #   preserves trace start_time_unix_nano
experiment/platform/backend/storage/
└── sqlite_storage.py  # iter_run_embeddings(include_features=True)
experiment/platform/backend/api.py  # mode=hybrid on /similar and /rag-context
```

No new dependencies and no schema migration: the new fields ride
inside `run_features.features_json`.


---

## Phase F2 — Persistent system context (system cards)

Every LLM verdict was being assembled with only the *dynamic* slice of
the incident (target summary + retrieved neighbours). The model had to
re‑infer the *static* moldura — service roles, stacks, dependencies,
allowed verdict vocabulary — on every call, and frequently invented
service names that did not exist in the system under test.

Phase F2 introduces a **system card**: a static, versioned YAML
document describing the target application that is injected once into
the LLM system prompt. The card lives outside the RAG context so it
never competes with retrieved runs for token budget, and the static
prefix is cacheable by the LLM runtime (Qwen / vLLM KV cache).

### What goes in a card

- Application description and platform (orchestrator, namespace, mesh).
- Per‑service block with: short `role`, `stack`, `type`
  (stateless / stateful / worker), `upstream`, `downstream`,
  `storage`, plus optional operational `notes`.
- **Controlled vocabulary** for the verdict: `allowed_rca_targets`
  (exact list of service names the LLM may use in `rca`) and
  `allowed_fault_categories`.

The Sock Shop card lives at
[experiment/platform/data/system_cards/sock-shop.yaml](experiment/platform/data/system_cards/sock-shop.yaml)
(13 services, ~3.9 KB → ~980 tokens rendered).

### Wiring

```
experiment/platform/backend/analysis/system_card.py  # loader + renderer
experiment/platform/backend/analysis/prompt.py       # injects <system_card>
                                                     # block right after
                                                     # the system role
experiment/platform/backend/api.py                   # /api/system-cards
                                                     # and system_id param
                                                     # on llm-analysis
experiment/platform/data/system_cards/sock-shop.yaml # the card itself
```

`assemble_prompt(..., system_id="sock-shop")` now produces a prompt of
the form:

```
<system>You are a senior resilience engineer ...</system>

<system_card>
## System under test: sock-shop (card v2026-05-23)
...
### Services (role, stack, dependencies)
- orders [java/spring-boot, stateful]
    role: Order orchestration. On checkout, fans out to ...
    upstream: front-end
    downstream: carts, payment, shipping, user, orders-db
    note: Highest fan-out service in the system ...
...
### Allowed values for the verdict `rca` field
Reply MUST use exactly one of: front-end, catalogue, ..., unknown.
</system_card>

<context> ... target run + retrieved neighbours ... </context>

<task> ... JSON schema ... </task>
```

Missing card files are tolerated silently — the prompt is built without
the `<system_card>` block, matching pre‑F2 behaviour.

### API

- `GET /api/system-cards` — list known card ids.
- `GET /api/system-cards/{system_id}` — return parsed metadata
  (`version`, `service_count`, `allowed_rca_targets`) plus the rendered
  text block that will be inlined in the prompt.
- `POST /api/runs/{run_id}/llm-analysis?system_id=<id>` — new optional
  query parameter; defaults to `sock-shop`.

`prompt_meta` on the persisted verdict now reports `system_card_id` and
`system_card_tokens_estimate` so ablations can be reproduced.

### How to extend

1. Drop a new file `data/system_cards/<system_id>.yaml` following the
   Sock Shop schema.
2. Call `/api/runs/{id}/llm-analysis?system_id=<id>`.
3. Validate via `/api/system-cards/<id>` that the rendered text fits
   your token budget (target ≤ 1200 tokens; the loader is cached per
   process so the cost is paid once).

Cards are deliberately hand‑written (or semi‑generated from K8s
manifests + baseline `propagation_graph`) rather than learned: the
goal is auditability and version control, not adaptation.
