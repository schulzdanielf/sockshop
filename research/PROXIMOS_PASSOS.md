# Próximos passos — Exploração LLM multiagente para RCA

> **Atualizado em:** 2026-09-25 · **Abordagem:** [003 — LLM multiagente para RCA](abordagens/003-llm-multiagente-rca.md)
> Documento de recuperação: o que testar em seguida e como evoluir sem congelar prematuramente a arquitetura.

**Prioridades revisadas em 17/10/2026:** consulte o relatório
[Próximos passos — correções de maior retorno e menor esforço](./planos/2026-10-17-proximos-passos.md).
Ele avalia as sete falhas e prioriza gates, identidade/lifecycle, CPU/memória,
HTTP e rede. O restante desta página preserva o plano de exploração de 25/09.

## Direção atual

O projeto passa a explorar uma estratégia **100% baseada em LLMs e multiagentes** para RCA. A arquitetura ainda não está definida: cada experimento deve responder uma pergunta pequena, produzir evidência comparável e orientar o próximo desenho.

O foco inicial é especializar agentes por tipo de evidência:
- agente de **métricas**: Prometheus/Grafana, anomalias, latência, erros e mudanças por janela;
- agente de **logs**: Loki, eventos, mensagens de erro e correlação temporal;
- agente de **traces**: Tempo, spans, caminho da requisição e propagação;
- agente **orquestrador**: define a próxima investigação e controla o orçamento;
- agente **sintetizador/verificador**: consolida hipóteses, evidências e confiança.

Esses papéis são candidatos, não uma arquitetura obrigatória. Um único agente, agentes paralelos, debate e loops sequenciais devem ser comparados em testes pequenos.

## Observabilidade como critério de decisão

Observabilidade é um dos pilares do projeto. Cada resultado precisa ser acompanhado por métricas e evidências suficientes para explicar não só **o que** o sistema decidiu, mas também **por que** decidiu isso e **com que custo**.

O acompanhamento deve cobrir três níveis:

- **Sinais do sistema:** métricas do Prometheus/Grafana, logs do Loki, traces do Tempo, eventos e cobertura das janelas.
- **Sinais da investigação:** agentes acionados, tools e argumentos, sequência do loop, respostas, conflitos, retries e motivo de parada.
- **Sinais da pesquisa:** acurácia, qualidade das evidências, confiança, latência, custo, falhas e estabilidade entre repetições.

Uma variante só deve ser considerada uma melhoria quando houver evidência observável de ganho ou de redução de custo/complexidade. Respostas corretas sem trilha de evidências devem ser tratadas como resultado inconclusivo.

## Estado atual no código

- `GET /api/runs/{run_id}/observation-window` resolve a janela autoritativa do run.
- `observability_summary.json` é determinístico, resume métricas e traces, registra lacunas e entra no contexto RAG; não contém uma RCA gerada pelo sumarizador.
- `GET /api/runs/{run_id}/mcp-evidence?metric_id=...` executa uma PromQL configurada pela janela do run e persiste o artefato e o evento de auditoria. É uma consulta por chamada, não um agente investigador.
- `POST /api/runs/{run_id}/telemetry-chat` usa `tools/list` com allowlist de seis investigadores MCP, janela autoritativa e loop estruturado limitado (`max_steps` padrão 2); segue como agente único, não equipe multiagente. A qualidade do tool calling no Qwen real ainda precisa ser avaliada.
- O baseline de RCA é `POST /api/runs/{run_id}/llm-analysis`: RAG + uma chamada LLM (com um retry em erro de parse/`rca` ausente) + `FaultCategoryValidator` e `ServiceLocalizerValidator`. Os validadores são pós-processadores determinísticos, não agentes.
- O MCP já oferece investigadores de erro 5xx, latência P95, OOM, restarts, saturação CPU e memória. A consulta de memória foi corrigida para usar o job cAdvisor canônico e não somar percentuais das réplicas.

Não há ainda agentes especialistas independentes, síntese entre respostas de agentes, coordenação paralela ou critério de parada baseado em suficiência de evidência.

## Próximo experimento imediato

Primeiro, usar `run-5e0ed295aa37` como smoke test de contrato, não como pontuação de RCA: na janela `2026-09-23T11:16:51.479151+00:00`–`2026-09-23T11:22:23.151913+00:00`, a resposta MCP de RabbitMQ foi zero restarts e a consulta corrigida de memória coincidiu com Prometheus v3 e o datasource Grafana.

Para a comparação de RCA, congelar 4–6 runs concluídos com ground truth e gates de qualidade aprovados, cobrindo as categorias disponíveis. Uma ausência de traces deve ser registrada como lacuna e não tratada como evidência negativa.

Comparar duas variantes sobre os mesmos runs, janela, contexto e configuração do modelo:

1. **Baseline A:** `POST /api/runs/{id}/llm-analysis?force=true`, com o LLM único e os validadores atuais.
2. **Variante B:** especialista de métricas e especialista de traces executados em paralelo, seguidos por um sintetizador. Não adicionar ainda um orquestrador LLM; isso isolaria o efeito da especialização/paralelismo. Só executar o especialista de traces quando a cobertura do run for válida. Adiar o especialista de logs até haver evidência Loki por janela disponível no contrato do run.

O runner de avaliação atual (`experiment/eval/memory_hog/runner.py`) chama somente `/llm-analysis`. A variante B precisa de um adapter/runner próprio ou de extensão explícita do harness que grave os mesmos campos de acurácia e acrescente resultados/custos por agente; não basta adicioná-la como mais uma estratégia YAML.

Cada especialista deve retornar JSON com `agent`, `status`, `window`, `tools`, `evidence`, `gaps`, `confidence`, `calls` e `elapsed_ms`. O sintetizador só pode afirmar fatos sustentados por evidências citadas; ausência de sinal e falha de coleta precisam ser estados distintos.

Medir `where_top1`, acurácia de categoria, validade das citações/janelas, cobertura e lacunas, chamadas MCP/LLM, tokens, latência, erros e motivo de parada. Usar orçamento inicial de até 10 chamadas MCP por run. Não declarar superioridade com o smoke test ou com um único run; registrar a decisão em `research/diario/` após a comparação.

## Incrementos do MCP

O MCP deve evoluir de consultas isoladas para uma interface de investigação:
- filtros consistentes por `run_id`, serviço e janela temporal;
- respostas resumidas e também acesso ao dado bruto quando necessário;
- ferramentas específicas para métricas, logs, traces e correlação entre sinais;
- metadados de qualidade, cobertura e intervalo consultado;
- limites de resultado, paginação e observabilidade das chamadas;
- testes com respostas reais e fixtures pequenas para iteração rápida.

O MCP também deve registrar a telemetria das próprias consultas: duração, erros, retries, tamanho da resposta e relação entre a chamada e a evidência usada na RCA.

## Loop de trabalho

1. Formular uma pergunta investigável.
2. Implementar o menor experimento que a responda.
3. Rodar em fixtures ou poucos runs conhecidos.
4. Registrar resultado, falha, custo e decisão no diário.
5. Manter ou descartar a variante e escolher o próximo teste.

Cada resultado deve ser registrado em `research/diario/`, incluindo a variante testada, runs, prompt/tools, acerto, custo, latência, falhas e decisão.

O registro deve separar observação, interpretação e decisão. A justificativa precisa apontar quais métricas, logs, traces ou sinais do pipeline sustentam a escolha do próximo experimento.

## Referências preservadas

Os artefatos do ciclo GAT/Istio continuam disponíveis para comparação e aprendizado, mas não são mais o próximo passo obrigatório. O detalhamento histórico está em `research/diario/` e nos documentos da abordagem 002.

## Histórico das decisões

- `research/diario/2026-09-19-*.md` e `2026-09-20-*.md` — ciclo GAT/Istio e suas conclusões.
- `research/diario/2026-09-25-validacao-mcp-e-prontidao-multiagente.md` — smoke test MCP vs Prometheus/Grafana e decisão sobre a primeira comparação multiagente.
- Próximas decisões da estratégia multiagente devem ser registradas com data, mesmo quando o resultado for negativo.
