# Validação MCP e prontidão para o piloto multiagente

## Pergunta

As principais consultas de investigação do MCP estão prontas para servir de
evidência no primeiro teste multiagente, e qual comparação deve vir primeiro?

## Execução

- Run de integração: `run-5e0ed295aa37`.
- Janela autoritativa: `2026-09-23T11:16:51.479151+00:00` a
  `2026-09-23T11:22:23.151913+00:00`.
- Fonte: datasource Grafana `prometheus` (UID `prometheus`), configurado para
  `prometheus-v3`; o MCP usa a mesma origem.
- Consultas executadas via MCP e confrontadas com API direta Prometheus v3 e
  proxy Grafana: erros 5xx, latência P95, OOM, restarts, CPU e memória.
- As séries brutas via Grafana e Prometheus coincidiram; os seis rollups MCP
  coincidiram com o recálculo sobre as respostas diretas de Prometheus.
- As seis perguntas em português também foram submetidas ao endpoint
  `telemetry-chat` com `use_model=false`, usando a janela do run. A pergunta
  original de restart também foi validada separadamente no chat com modelo.

## Resultados observados

- 5xx: front-end, média `0.0238%`, máximo `0.0887%`.
- A tool de 5xx retornou `found=false/count=0` porque seu piso é `1%`, apesar
  de haver valores não nulos. Consumidores não devem interpretar esses campos
  como “sem série”; distinguir existência/cobertura da regra de limiar.
- Latência P95: front-end, média `1.0401s`, máximo `1.3992s`.
- OOM: nenhuma série `reason="OOMKilled"` na janela. A família de métricas
  estava presente, com motivo `Error`; isso verifica o resultado negativo,
  não o caminho positivo da detecção.
- Restarts: RabbitMQ `0`. Os dois containers tiveram counters estáveis na
  janela (`60→60` e `59→59`), com zero incremento reset-aware.
- CPU: front-end, média `58.26%`, máximo `84.79%`.
- Memória antes da correção: carts chegou a `146.78%` e queue-master a
  `127.69%`. A causa foi somar amostras duplicadas dos jobs
  `kubernetes-nodes` e `kubernetes-cadvisor`.
- Memória após filtrar `kubernetes-cadvisor` e calcular média por réplica:
  carts `73.70%` médio / `74.09%` máximo; queue-master `64.03%` médio /
  `64.30%` máximo. A correção foi implantada e verificada no MCP ativo.

## Resolução temporal

O Prometheus está configurado com scrape e evaluation interval de 15s. Para
consultas `rate` com passo de 15s, `[15s]` gerou apenas 2 pontos por série de
5xx/P95 e 1–4 pontos de CPU; `[30s]` recuperou cobertura de 5xx/P95, mas CPU
continuou incompleta (11–19 de 23 pontos); `[1m]` produziu 23/23 pontos nos
três sinais. Decisão: manter `[1m]` por enquanto. A borda inicial ainda pode
usar amostras anteriores ao início da janela e deve permanecer uma ressalva
na interpretação.

## Interpretação e decisão

O smoke test confirma acesso às ferramentas, consistência da fonte e contrato
de janela; não mede acurácia de RCA e não deve ser contado como amostra de
benchmark. A base possui janela autoritativa, resumo de observabilidade,
consulta MCP auditável e baseline de LLM único. `telemetry-chat` é um agente
único com loop limitado de tools; `llm-analysis` usa RAG, retry limitado e
validadores determinísticos. Ainda não existem especialistas LLM
independentes, síntese multiagente ou orquestração adaptativa.

Próximo experimento: congelar 4–6 runs com ground truth e quality gates
aprovados; comparar o baseline `llm-analysis` com dois especialistas em
paralelo (métricas e traces) e um sintetizador, usando a mesma janela/contexto.
Não executar o especialista de traces quando a cobertura estiver indisponível;
adiar logs até que sua evidência por run e janela esteja no contrato. Medir
WHERE/WHY/full accuracy, validade das evidências, cobertura, chamadas MCP/LLM,
tokens, latência, erros e motivo de parada. Limitar o piloto a 10 chamadas MCP
por run e não afirmar superioridade a partir de um único caso.