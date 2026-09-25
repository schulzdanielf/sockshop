# 003 — LLM multiagente especializado para RCA

- **Status:** ativa — arquitetura em exploração
- **Início:** 2026-09-25
- **Antecessora / histórico:** [002 — Híbrido GAT + Agentes](002-hibrido-gat-agentes.md)
- **Plano vigente:** [Plano de experimentos rápidos multiagente](../planos/2026-09-25-plano-llm-multiagente.md)

## Direção

Investigar se uma equipe de agentes especializados, coordenada por LLMs e apoiada por ferramentas MCP, consegue localizar e explicar causas raiz em microsserviços com mais qualidade, rastreabilidade e flexibilidade do que um LLM único com contexto consolidado.

A arquitetura não está definida. O projeto adota uma estratégia ágil: cada mudança deve ser um experimento pequeno, executável em poucos runs ou fixtures, com uma decisão explícita sobre o próximo passo.

## Observabilidade como pilar

Observabilidade é requisito tanto do sistema analisado quanto do pipeline de RCA. Não basta produzir uma resposta correta: é necessário acompanhar os sinais que sustentam a resposta e entender o caminho que levou à decisão.

Cada execução deve permitir responder:

- quais métricas, logs e traces estavam disponíveis;
- quais ferramentas cada agente chamou, com filtros e janelas usadas;
- quais evidências foram consideradas relevantes ou conflitantes;
- qual hipótese foi formulada, atualizada ou descartada;
- por que o loop terminou e qual foi o nível de confiança;
- quanto tempo, tokens, chamadas e recursos foram consumidos.

Esses dados devem ser registrados junto do resultado do experimento. Uma decisão sem evidência observável é uma hipótese de trabalho, não uma conclusão.

## Agentes candidatos

Os papéis abaixo são hipóteses de decomposição, não contratos permanentes:

- **Orquestrador:** recebe o objetivo, controla o estado da investigação, escolhe a próxima ação e encerra quando há evidência suficiente.
- **Especialista em métricas:** investiga Prometheus/Grafana, anomalias, latência, erros, saturação e mudanças por janela.
- **Especialista em logs:** investiga Loki, mensagens de erro, eventos de reinício e correlação temporal.
- **Especialista em traces:** investiga Tempo, spans, dependências, latência e propagação da falha.
- **Correlacionador:** cruza sinais por serviço, tempo e requisição, explicitando concordâncias e conflitos.
- **Sintetizador/verificador:** produz a RCA estruturada, aponta evidências, incertezas e perguntas ainda abertas.

A primeira versão pode usar menos agentes. A especialização só deve permanecer se melhorar a decisão ou a qualidade das evidências sem custo desproporcional.

## Padrões a comparar

1. **LLM único:** um agente recebe um resumo consolidado.
2. **Paralelo:** especialistas consultam seus sinais independentemente e um sintetizador consolida.
3. **Sequencial:** o orquestrador chama especialistas conforme as lacunas encontradas.
4. **Loop de investigação:** o sistema formula hipóteses, consulta o MCP, verifica evidências e repete até atingir um critério de parada.
5. **Híbridos:** combinações dos padrões acima, sempre comparadas contra o baseline mais simples.

## MCP como camada de investigação

O MCP deve oferecer ferramentas pequenas, composáveis e observáveis para:

- consultar métricas por `run_id`, serviço e janela;
- buscar logs filtrados por serviço, período, severidade e termos;
- consultar traces e dependências relevantes;
- correlacionar métricas, logs e traces por tempo e serviço;
- retornar resumo para o raciocínio e dado bruto sob demanda;
- informar cobertura, intervalo efetivamente consultado e qualidade da resposta.

Cada chamada deve ser registrável para permitir auditoria do caminho seguido pelo agente.

Além das respostas funcionais, o MCP e o orquestrador devem expor telemetria do próprio processo: duração, erros, retries, tamanho dos resultados, chamadas por agente e motivo de encerramento. Essa telemetria permite distinguir uma melhoria real de uma resposta ocasionalmente correta obtida com custo ou complexidade inviáveis.

## Saída mínima da RCA

Toda variante deve produzir uma estrutura comparável contendo:

- serviço ou componente candidato à causa raiz;
- categoria da falha;
- evidências citadas com origem e janela;
- hipótese alternativa, quando houver;
- nível de confiança e conflitos entre sinais;
- ações consultadas e motivo da parada;
- custo, latência e quantidade de chamadas ao MCP/LLM.

## Critérios de avaliação

A avaliação inicial é exploratória e combina:

- acerto de serviço e categoria em runs conhecidos;
- qualidade e verificabilidade das evidências;
- capacidade de reconhecer incerteza;
- número de chamadas, latência e custo;
- robustez a sinal ausente ou conflitante;
- facilidade de adicionar uma nova tool ou especialização.

Também devem ser avaliados os sinais do processo: cobertura dos dados consultados, rastreabilidade da decisão, estabilidade entre repetições, latência, custo e taxa de falha das ferramentas.

Não se deve concluir que uma arquitetura é superior por um único run. Cada experimento precisa registrar limitações, tamanho da amostra e o que será testado depois.

## Registro de andamento

Os resultados devem ser registrados em entradas datadas de `research/diario/`, incluindo experimentos descartados e falhas de coordenação. Toda entrada deve separar fato observado, interpretação, decisão e próxima hipótese.
