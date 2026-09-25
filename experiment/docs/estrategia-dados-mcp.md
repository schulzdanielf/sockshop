# Estratégia de dados para RCA via MCP

> **Estado:** base de dados implementada; especialistas multiagente ainda não implementados  
> **Atualizado em:** 2026-09-25

## Objetivo

Organizar o fluxo de dados para que cada análise de RCA use exatamente a janela temporal do experimento, consulte métricas, logs e traces por meio do MCP e mantenha uma trilha explicável entre evidência, decisão e resultado.

A estratégia recomendada combina duas formas de acesso:

1. **MCP sob demanda:** fonte de evidência detalhada para o modelo investigar uma hipótese.
2. **JSON de contexto/resumo:** contrato pequeno e estável com a janela, metadados e sinais já resumidos para iniciar a análise e permitir reprodução.

O resumo não substitui o MCP. Ele reduz o contexto inicial; o agente deve voltar ao MCP quando precisar confirmar ou aprofundar uma evidência.

## Como funciona hoje

O fluxo atual da plataforma é:

```text
ExperimentVersion.timeline
        |
        v
OrchestratorEngine._execute_run()
        |
        +--> run_started / baseline / warmup / fault / recovery events
        |
        +--> run.timings: t_start, t_warmup_start/end,
        |                  t_fault_start/end, t_recovery_observed
        |
        +--> MCPPrometheusMetricsPlugin.collect_window()
        |       Prometheus range query via MCP
        |
        +--> MCPTempoTracesPlugin.collect_window()
        |       Tempo search via MCP
        |
        +--> object store por run
        |       metrics_raw.json, traces_raw.json, load_summary.json,
        |       chaos_artifacts.json, run_features.json, summary.json
        |
        +--> SQLite
                run, timings, events e referências dos artefatos
```

O engine já coleta Prometheus e Tempo com `start_iso` e `end_iso`. As métricas são divididas depois em `baseline`, `warmup`, `fault` e `post`. Os traces também são buscados por intervalo. O MCP consulta Prometheus, Loki e Tempo; o Grafana visualiza esses mesmos backends e não deve ser tratado como a fonte primária de dados para o agente.

O baseline atual é de agente único. `telemetry-chat` usa a janela do run, descobre tools MCP, aplica allowlist e executa um loop estruturado limitado; `llm-analysis` usa o contexto RAG, uma chamada LLM com retry limitado e validadores determinísticos. `mcp-evidence` executa uma PromQL configurada por chamada. Nenhum desses caminhos é ainda uma equipe de especialistas independentes.

## Fonte autoritativa da janela

Para a análise, a janela deve ser derivada dos timestamps do ciclo real de execução, e não apenas da duração configurada:

- `experiment_start`: `run.timings.t_start` ou evento `run_started`;
- `experiment_end`: `run.timings.t_recovery_observed` quando o run completa;
- `experiment_end`: `run.ended_at` quando o run é interrompido ou falha;
- limites de fase: eventos e campos correspondentes em `run.timings`;
- todos os timestamps em UTC, no formato RFC3339 com timezone.

`RunRecord.started_at` representa a criação do run e pode anteceder a execução efetiva. Por isso não deve ser usado como início da janela de observabilidade sem validação.

O engine já gera e persiste um artefato `observation_window.json` por run. O contrato contém identificação, estado e horários; queries, features e conclusões permanecem em artefatos separados:

```json
{
  "schema_version": "1",
  "run_id": "run-abc123",
  "experiment_id": "memory-hog",
  "status": "completed",
  "timezone": "UTC",
  "window": {
    "start": "2026-09-25T10:00:00+00:00",
    "end": "2026-09-25T10:08:00+00:00"
  },
  "phases": {
    "baseline": {"start": "...", "end": "..."},
    "warmup": {"start": "...", "end": "..."},
    "fault": {"start": "...", "end": "..."},
    "post": {"start": "...", "end": "..."}
  },
  "sampling_interval_seconds": 15
}
```

O JSON contém somente identificação, status e horários para não misturar a definição da janela com interpretação.

## Contrato de consulta MCP

Toda chamada usada na análise deve receber uma janela explícita. O agente não deve depender dos defaults de “última hora” do servidor.

Exemplo conceitual:

```json
{
  "run_id": "run-abc123",
  "start": "2026-09-25T10:00:00+00:00",
  "end": "2026-09-25T10:08:00+00:00",
  "step": "15s"
}
```

Nas rotas de análise por `run_id`, o backend resolve e passa limites explícitos. `GET /api/runs/{run_id}/mcp-evidence` usa a query configurada e `investigate_metric_window` devolve argumentos, janela e contagens de séries/amostras. Os seis investigadores de Prometheus também recebem `start`/`end` e devolvem query e janela. O chat de telemetria mantém a janela autoritativa e não permite que a pergunta a altere.

Isso não é uma validação global do MCP: `prometheus_range_query`, `loki_range_query` e ferramentas Tempo genéricas ainda aceitam argumentos temporais omitidos e defaults. O MCP genérico também não normaliza todos os timestamps nem aplica um limite uniforme de duração. Tratar chamadas sem janela em fluxos de RCA como pendência; não presumir que todos os consumidores estão protegidos só porque os endpoints da plataforma estão.

Evoluções pendentes do contrato global do MCP:

- rejeitar `end < start` e janelas acima de um limite configurado;
- normalizar timestamps para UTC;
- devolver `window_start` e `window_end` na resposta;
- informar fonte, query, quantidade de séries/linhas/traces e erros parciais;
- distinguir “não houve sinal” de “a consulta falhou”;
- manter limites de resultado e paginação explícitos;
- registrar duração, retries e status da chamada.

A primeira versão pode reutilizar `prometheus_range_query`, `loki_range_query` e `tempo_search_traces`, desde que o orquestrador forneça sempre os limites. Depois, podem ser adicionadas tools de mais alto nível, como `get_run_observability_context` e `correlate_window`, sem remover as consultas primitivas.

## Como o modelo acessa os dados

O fluxo multiagente recomendado para uma análise é:

1. O modelo recebe `run_id` e obtém `observation_window.json` pelo contexto da plataforma.
2. O orquestrador valida que a janela está fechada ou marca o run como parcial.
3. O modelo recebe o resumo inicial, se existir, com as fontes e janelas usadas.
4. Agentes de métricas, logs e traces consultam o MCP com `start` e `end` explícitos.
5. O sintetizador cita a tool, a janela e o sinal que sustentam cada afirmação.
6. O resultado salva `analysis_trace.json` com hipóteses, chamadas, respostas resumidas e decisão final.

Os passos 4–6 acima ainda são o desenho-alvo, não uma equipe de agentes implementada. Atualmente `telemetry-chat` é single-agent, com allowlist e loop MCP limitado; `llm-analysis` é single-agent com RAG, retry limitado e validadores determinísticos. `mcp-evidence` registra uma chamada auditada, não o traço completo de uma investigação.

**Implementado parcialmente:** o endpoint `/api/runs/{run_id}/rag-context` carrega o `observability_summary.json` no bloco `target`, e o montador de prompt inclui as evidências observáveis filtradas. Campos derivados como `verdict`, `affected_services` e `top_failure_signatures` continuam fora do contexto inicial para evitar vazamento de rótulos.

O endpoint `GET /api/runs/{run_id}/mcp-evidence` executa uma consulta Prometheus complementar usando a janela resolvida, retorna uma síntese de séries/amostras e persiste `mcp_evidence_<metric>.json`. A chamada também gera o evento `mcp_evidence_collected` para auditoria.

A página de chat de telemetria usa `POST /api/runs/{run_id}/telemetry-chat`. O endpoint resolve a janela autoritativa do run, encaminha as consultas `query_range` pelo `prometheus_range_query` do MCP e entrega a resposta do agente sem permitir que a pergunta altere o intervalo consultado. Cada interação gera o evento `telemetry_chat_completed`.

#### Observabilidade GenAI do chat

A árvore de traces deve preservar a sequência `invoke_agent` → `gen_ai.chat` (decisão/síntese) → `mcp.tools.list` → `gen_ai.tool_call` (`execute_tool`) → nova chamada `gen_ai.chat`. O agente registra `gen_ai.agent.name`, run/janela, allowlist, orçamento, quantidade de chamadas e motivo de parada. Chamadas LLM registram provider/modelo, max tokens, tokens de entrada/saída, finish reason, contagem de tools e IDs de tool calls retornados. Execuções MCP registram `gen_ai.tool.name`, `gen_ai.tool.type`, `gen_ai.tool.call.id`, descrição, servidor, duração e resultado/erro; descoberta registra `rpc.system.name=mcp`, `rpc.method=tools/list` e total descoberto.

Argumentos e resultados completos não são atributos por padrão. Captura de prompt/completion no cliente LLM segue `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`; manter desativada em produção sem uma revisão de privacidade, pois o conteúdo pode incluir perguntas e telemetria. Argumentos no span MCP ficam limitados a nomes/quantidade de chaves.

### Estratégia de tools MCP para `telemetry-chat`

O chat deve atuar como host/cliente MCP: inicializar a sessão, descobrir tools com `tools/list`, filtrar por uma allowlist própria do caso de uso e entregar ao modelo somente os schemas dessa allowlist. Não expor todo o catálogo MCP ao chat.

Allowlist inicial de investigação de métricas:

- `check_http_error_rate_top`
- `check_request_latency_top`
- `get_cpu_saturation_top`
- `get_memory_saturation_top`
- `check_pod_restarts`
- `check_oom_kills`

Manter fora do escopo inicial: `prometheus_range_query` genérica (PromQL livre), Loki, Tempo, ferramentas de cluster/administração e tools de escrita. Perguntas sobre o que foi injetado devem receber contexto do run e do resultado Litmus fornecido pelo backend; não devem inferir a injeção a partir de uma métrica de impacto.

Para cada resposta do modelo, o host valida que a tool pedida pertence à allowlist e que os argumentos respeitam o schema. `start`, `end` e `namespace` vêm do run selecionado e são fixados pelo backend; não são controlados pela pergunta/modelo. O host chama a tool via `ClientSession.call_tool`, devolve o resultado estruturado ao modelo e limita o loop por orçamento de chamadas. Respostas devem distinguir zero, ausência de séries, cobertura insuficiente e erro de consulta.

**Implementado (2026-09-25):** `MCPToolClient` chama `tools/list`; o `telemetry-chat` expõe somente a allowlist de seis investigadores de métricas, converte seus schemas para o contrato de tools OpenAI-compatible, chama o LLM com `tool_choice="auto"`, valida o nome/argumentos, injeta janela/namespace/step/limite no host, executa `call_tool` e devolve o resultado como mensagem `tool`. `tool_choice="none"` encerra a fase de investigação e pede síntese. Nenhuma tool genérica de PromQL, logs, traces ou administração é exposta ao modelo.

O servidor Qwen aceita `tools`, `tool_choice` e `parallel_tool_calls`, e responde com `message.tool_calls`/`finish_reason="tool_calls"` quando o texto gerado contém uma chamada JSON válida. `call_llm_tool_messages` envia esse contrato. A implementação adapta saída textual JSON do Qwen ao envelope OpenAI; não é decodificação nativa de tokens/tool calls do modelo. Os testes unitários e uma chamada ao MCP real com planner simulado passaram; ainda falta validar a seleção/geração do Qwen real após reiniciar o servidor de modelo com a versão atual.

Critério de aceite: provar descoberta e filtragem da allowlist; tool calls com argumentos tipados; injeção de janela pelo backend; retorno de resultado como mensagem `tool`; encerramento por limite; trilha auditável de tool/argumentos/resultado. A avaliação deve medir acerto da escolha da tool separadamente da qualidade da resposta. Manter roteamento determinístico para intenções inequívocas como CPU como restrição/atalho validado, não como substituto da execução MCP.

A API da plataforma já expõe `GET /api/runs/{run_id}`, `/events`, `/features` e `/summary`. O endpoint `GET /api/runs/{run_id}/observation-window` resolve o contrato temporal para o modelo sem exigir leitura direta do object store. O primeiro adaptador do modelo pode usar essa rota para obter contexto e o MCP para buscar evidência viva. O acesso ao object store deve ser reservado ao backend/orquestrador, evitando que cada agente leia arquivos arbitrariamente.

## Resumo JSON para o modelo

Quando o payload bruto for grande, usar o `observability_summary.json` derivado da mesma janela, contendo:

- identificação do run e `observation_window`;
- estatísticas por fase para métricas;
- top serviços afetados e deltas baseline-fault;
- contagem de logs por severidade e principais assinaturas;
- quantidade de traces, erros, latência e dependências;
- lacunas de cobertura e falhas de consulta;
- referências às fontes que permitem aprofundamento via MCP.

Esse resumo deve ser determinístico e versionado. **Implementado:** o engine gera o artefato ao concluir o run. Ele não contém uma RCA inventada pelo próprio sumarizador; hipóteses e decisões ficam em um artefato posterior.

## Grafana e MCP

O Grafana deve validar visualmente o mesmo intervalo usado pelo MCP. Para cada run, o contexto pode fornecer um link de dashboard com `from` e `to` correspondentes à janela, mas o modelo não deve depender de scraping da interface do Grafana.

A divisão de responsabilidades é:

- **Prometheus/Loki/Tempo:** fontes de dados;
- **MCP:** acesso programático, filtros, normalização e auditoria;
- **Grafana:** exploração visual humana e validação de dashboards;
- **plataforma:** ciclo do experimento, janela, artefatos e controle de acesso;
- **modelo/agentes:** investigação e explicação baseada nas evidências.

## Validação operacional de 2026-09-25

O datasource Grafana `prometheus` e o MCP foram confirmados usando `prometheus-v3`. Para `run-5e0ed295aa37` (`2026-09-23T11:16:51.479151+00:00` a `2026-09-23T11:22:23.151913+00:00`), as seis tools de investigação de métricas foram comparadas com o mesmo PromQL via API direta do Prometheus e proxy Grafana; os dados brutos e rollups coincidiram.

A consulta de saturação de memória agora restringe o numerador a `job="kubernetes-cadvisor"`. Sem esse filtro, séries de `kubernetes-nodes` e `kubernetes-cadvisor` foram somadas para alguns pods e produziram picos artificiais acima de 100%. A média do serviço também deve ser a média dos percentuais por pod, não a soma. A imagem MCP com a correção foi implantada e verificada.

O Prometheus raspa/avalia a cada 15s. Testando `rate` com passo de consulta de 15s na janela acima: `[15s]` produziu somente 2 pontos por série de 5xx/P95 e 1–4 para CPU; `[30s]` ainda perdeu pontos de CPU; `[1m]` produziu 23/23 pontos nos três sinais. Portanto, manter `[1m]` como padrão por enquanto. Isso não elimina a ressalva de borda: a primeira avaliação da janela pode usar amostras anteriores ao início, pois `rate` olha para trás pelo intervalo informado.

O teste OOM não encontrou amostras `reason="OOMKilled"` nesse run; a família de métricas estava presente com outro motivo. O retorno atual do chat (“nenhum dado”) não distingue ausência de evento de falha de cobertura. Além disso, esse run não valida o caminho positivo de detecção de OOM.

## Incrementos em ordem

1. Criar e persistir `observation_window.json` no encerramento de cada run. **Implementado.**
2. Adicionar testes para timestamps e relação entre fases. **Implementado.**
3. Criar um resolvedor de janela por `run_id` para o orquestrador. **Implementado:** `GET /api/runs/{run_id}/observation-window`.
4. Garantir que todas as tools MCP usadas pela análise recebam intervalo explícito e o devolvam na resposta. **Parcial:** rotas por run e investigadores de métricas recebem janela; as tools MCP genéricas ainda mantêm defaults temporais.
5. Gerar `observability_summary.json` determinístico a partir dos artefatos existentes. **Implementado.**
6. Auditoria inicial existe nos eventos `mcp_evidence_collected` e `telemetry_chat_completed`; falta trilha por agente/especialista e links Grafana com janela para validação humana.
7. Janela, resumo e consulta MCP por run estão disponíveis. Próximo: congelar runs com ground truth e quality gates e implementar a comparação single-agent vs. especialistas paralelos.

## Critério para iniciar o piloto multiagente

A base para o piloto está disponível: janela autoritativa, resumo determinístico, consulta MCP auditada por run e baseline single-agent. Antes de pontuar resultados, congelar runs com ground truth e quality gates aprovados. O primeiro MVP deve comparar especialista de métricas + especialista de traces em paralelo com o baseline; só usar o especialista de traces quando houver cobertura. Logs ficam para uma etapa posterior, após serem incluídos no contrato de evidência com cobertura e janela.
