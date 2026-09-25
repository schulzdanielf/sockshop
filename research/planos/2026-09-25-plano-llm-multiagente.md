# Plano de experimentos rápidos — LLM multiagente para RCA

> **Criado em:** 2026-09-25  
> **Abordagem:** [003 — LLM multiagente especializado para RCA](../abordagens/003-llm-multiagente-rca.md)
> **Fluxo de dados:** [Estratégia de dados para RCA via MCP](../../experiment/docs/estrategia-dados-mcp.md)

## Objetivo

Evoluir uma estratégia de RCA baseada em LLMs e múltiplos agentes por ciclos curtos. A prioridade é aprender qual decomposição, ferramenta e loop ajudam de fato a investigação, mantendo a arquitetura aberta até haver evidência suficiente.

Antes de comparar arquiteturas de agentes, a plataforma deve garantir uma janela de observabilidade única por `run_id`, com acesso MCP explícito e resumo JSON reproduzível.

## Princípios de execução

- Começar com fixtures e poucos runs conhecidos antes de novas campanhas de chaos.
- Comparar toda variante com o baseline de menor complexidade disponível.
- Uma mudança por experimento sempre que possível.
- Separar qualidade da resposta, qualidade das evidências, custo e latência.
- Registrar também resultados negativos e decisões de descarte.
- Não adicionar agentes apenas por intuição: cada papel precisa de uma tarefa observável.

## Observabilidade do sistema e da pesquisa

Observabilidade é um critério de execução, não apenas uma fonte de dados. O experimento deve observar simultaneamente:

- **sistema sob análise:** métricas, logs, traces, eventos e cobertura das janelas consultadas;
- **pipeline de agentes:** prompts, ferramentas chamadas, argumentos, respostas, retries, erros e motivo de parada;
- **resultado da pesquisa:** acerto, evidências, confiança, custo, latência e estabilidade entre repetições.

O registro deve permitir reconstruir a decisão sem depender apenas do texto final produzido pelo LLM. Quando uma variante for escolhida, alterada ou descartada, a justificativa deve apontar os sinais e métricas que sustentam essa decisão.

## Fases iniciais

### Fase 0 — Contrato de avaliação (próximo)

Selecionar 4–6 runs concluídos com ground truth, `run_quality` aprovado e cobertura suficiente para os sinais que serão comparados. Manter serviço, categoria, citações/evidências, janela e confiança como campos avaliados. `run-5e0ed295aa37` é smoke test de integração MCP/Prometheus/Grafana, não amostra pontuada de RCA.

**Saída:** manifesto congelado dos runs e chamada reproduzível do baseline em cada um.

### Fase 1 — Baseline e acesso à evidência (implementado; precisa ser congelado na avaliação)

O código já oferece `GET /api/runs/{id}/observation-window`, `GET /api/runs/{id}/mcp-evidence`, `POST /api/runs/{id}/telemetry-chat` e `observability_summary.json`. O fluxo baseline de RCA é `POST /api/runs/{id}/llm-analysis?force=true`: RAG + LLM único, retry limitado e `FaultCategoryValidator`/`ServiceLocalizerValidator`. O chat de telemetria é um loop de ferramenta de agente único, não a variante multiagente.

O MCP dispõe de seis investigadores de Prometheus com janela explícita: 5xx, latência P95, OOM, restarts, CPU e memória. A consulta de memória usa `job="kubernetes-cadvisor"` e a média de saturação entre réplicas. Grafana e MCP apontam para Prometheus v3. A comparação de janelas `rate[15s]`, `rate[30s]` e `rate[1m]` favoreceu manter 1m: no run de smoke, 15s teve apenas 2 pontos para 5xx/P95 e 1–4 para CPU; 30s ainda perdeu amostras de CPU; 1m teve 23/23 pontos.

**Saída:** baseline, dados e prompts congelados; lacunas de cobertura marcadas, sem convertê-las em ausência de falha.

### Subfase — MCP restrito para `telemetry-chat` (implementação inicial)

Migrar o planner JSON para o ciclo MCP padrão, mantendo o catálogo do chat restrito:

1. **Implementado:** o host inicializa MCP, chama `tools/list` e filtra a allowlist dos seis investigadores Prometheus.
2. **Implementado:** o modelo recebe somente essas descrições e schemas de argumentos vazios; janela, namespace, step e top-K são injetados pelo host. Tools são solicitadas em formato estruturado e executadas por `ClientSession.call_tool`.
3. **Implementado:** resultado MCP retorna como mensagem `tool`; chamadas são limitadas por `max_steps`, uma por vez, e a síntese final usa `tool_choice="none"`.
4. **Implementação adaptada:** servidor Qwen aceita `tools`/`tool_choice` e devolve `tool_calls` no formato OpenAI traduzindo JSON gerado. Não é geração nativa de tool call pelo modelo.
5. **Pendente:** validar com o Qwen real após reiniciar o servidor para carregar a versão nova; medir `tool_selection_accuracy`, chamadas inválidas, cobertura, custo e latência em paráfrases de cada intent. A integração ao MCP real foi exercitada com planner simulado; testes focados passaram.

Não expor `prometheus_range_query` livre, tools Loki/Tempo nem administração ao chat nesta etapa. Metadados da falha injetada e outcome do Litmus são contexto do run fornecido pela plataforma, não inferências do especialista de métricas. Esta migração é uma trilha separada do baseline `/llm-analysis`; não bloquear o piloto paralelo de métricas+traces por ela.

### Fase 2 — Primeiro MVP multiagente (a implementar)

Executar em paralelo um especialista de métricas e um de traces, usando a mesma janela fechada e contrato comum de saída. Um sintetizador recebe somente as respostas estruturadas. Não introduzir ainda um orquestrador LLM nem o especialista de logs: isso mantém o primeiro teste pequeno e evita atribuir ao desenho paralelo efeitos de novos sinais ou planejamento adaptativo. Pular o especialista de traces quando o gate de cobertura falhar e registrar `status="unavailable"`/`gaps`, não uma conclusão negativa.

Contrato mínimo por agente: `agent`, `status`, `run_id`, `window`, `tools`, `evidence`, `gaps`, `confidence`, `calls`, `elapsed_ms` e usage de tokens quando disponível. Cada evidência deve carregar fonte, query/tool, rótulo/serviço, valor/unidade e intervalo. O sintetizador não deve repetir consultas nem criar evidência.

**Pergunta:** paralelismo/especialização melhora acurácia e evidência verificável contra o baseline, dentro de até 10 chamadas MCP/run?

### Fase 3 — Orquestração e loops (depois do MVP)

Só após medir a variante paralela, testar um orquestrador que escolha a próxima consulta a partir de lacunas/conflitos. Comparar chamada fixa, loop limitado e parada por suficiência de evidência, contabilizando custo incremental.

### Fase 4 — Robustez

Introduzir sinal ausente, respostas vazias, atraso, evidências conflitantes e traces incompletos. Testar explicitamente a diferença entre `no_signal`, `unavailable` e `tool_error`, além da qualidade da RCA.

## Matriz mínima de comparação

| Variante | Contexto | Coordenação | Objetivo |
|---|---|---|---|
| A | resumo atual | LLM único | baseline |
| B | evidência de métricas e traces com cobertura válida | especialistas paralelos + sintetizador | primeiro MVP: medir especialização/paralelismo |
| C | tools de métricas/logs/traces | orquestrador sequencial | medir investigação adaptativa após B |
| D | tools + histórico curto | loop com verificação | medir refinamento de hipóteses após C |

As variantes podem ser interrompidas ou combinadas quando um resultado já responder a pergunta do ciclo.

## Métricas por experimento

- `where_top1` e `fault_category_accuracy`, quando houver rótulo;
- precisão e rastreabilidade das evidências;
- hipóteses corretas, alternativas úteis e alucinações;
- número de chamadas ao MCP e ao LLM;
- latência total e custo estimado;
- taxa de falha de parsing, timeout ou encerramento prematuro.
- cobertura e qualidade dos sinais consultados;
- completude da trilha de auditoria da decisão;
- estabilidade do resultado em repetições com a mesma entrada;
- motivo de parada do loop e divergências entre agentes.
- disponibilidade de cada sinal e distinção entre ausência de evento e falha de coleta;
- contagem de chamadas por agente e total, comparada ao limite de 10 chamadas MCP/run.

## Critério de avanço

Avançar uma variante quando ela demonstrar ganho observável em qualidade/evidência, ou redução de custo/latência sem regressão, em mais de um run. O smoke test valida integração e contrato, não acurácia. Caso contrário, registrar a falha e simplificar ou mudar a hipótese no próximo ciclo.

## Registro obrigatório

Para cada ciclo, criar uma entrada em `research/diario/` com:

- pergunta do experimento;
- variante e prompt/template usado;
- runs ou fixtures analisados;
- ferramentas chamadas e respostas relevantes;
- resultados por métrica;
- telemetria da execução: ferramentas, janelas, erros, retries, latência e custo;
- evidências que explicam a decisão de manter, alterar ou descartar a variante;
- limitações e decisão;
- próximo experimento sugerido.
