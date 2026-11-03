# RCA: correcoes e plano de melhoria

## Resumo simples

O problema nao era apenas fazer poucas consultas. Parte dos dados importantes era
cortada antes de chegar ao modelo; algumas verificacoes repetiam consultas e
outras pediam mecanismos que a ferramenta escolhida nao media. Historico exato
tambem nao garantia um diagnostico correto.

**Implementado:** preservar fatos estruturados, informar omissoes, expor checks
executaveis, permitir uma alternativa limitada e oferecer consultas especificas
para lifecycle, HTTP, latencia de rede, throttling e GC JVM.

**Reavaliacao concluida:** a RAG marcou 6/33 contra 4/33 do controle e 6/33 da
Skill em classificacao conjunta. Todos os resultados seguem inconclusivos. A
comparacao com a rodada anterior nao isola efeito da compactacao: houve varias
mudancas juntas e o sampling nao tinha seed fixo. O relatorio esta em
[avaliacao v5](../2026-10-31-avaliacao-offline-rag-v5.md).

## 1. O que foi medido

A ultima avaliacao completa comparava tres condicoes sobre o mesmo pacote
congelado. Nao era uma comparacao operacional end-to-end.

| Falha | Casos | Skill: servico + falha | Controle | RAG |
|---|---:|---:|---:|---:|
| CPU | 7 | 3 | 4 | 1 |
| Memoria | 7 | 0 | 5 | 3 |
| Rede | 6 | 0 | 0 | 0 |
| Container kill | 6 | 0 | 0 | 0 |
| HTTP | 7 | 0 | 1 | 1 |
| **Total** | **33** | **3** | **10** | **5** |

Todas as 99 conclusoes eram inconclusivas. Acertar o rotulo experimental nao
constitui confirmacao causal. Dez casos recuperaram historico exato no top-3,
mas somente dois desses dez tiveram classificacao conjunta correta pela RAG.

### Validacao da nova compactacao

- 33/33 pacotes continuam dentro do limite explicito de 56.000 caracteres.
- Preservacao exata de 1.344 linhas de deltas de metricas, 414 linhas de caminhos
  criticos/spans de erro e 23 observacoes de lifecycle/readiness.
- Coberturas preservadas; nenhum arquivo de evidencia original foi alterado.
- Os 99 prompts completos ficaram entre 68.165 e 98.726 caracteres, abaixo do
  limite existente de 100.000. Todos os 99 pares historicos recuperados cabem.
- Esses numeros contam linhas de digests, nao fontes independentes nem eventos
  causais confirmados.
- Validacao final: 168 testes focados passaram, cobrindo MCP, backend,
  compactacao, Skill, RAG e replay. Dois avisos de depreciacao de Authlib
  permaneceram; nao houve falha de teste.

## 2. Compactacao corrigida

A compactacao antiga distribuia 24.000 caracteres igualmente entre evidencias e
cortava strings JSON pelo prefixo. Assim, um bloco grande de CPU podia esconder
memoria, eventos ou a aresta que identificava o destino afetado.

A nova versao `structured-evidence-v2`:

1. Usa objetos JSON, nao strings JSON cortadas ou duplamente serializadas.
2. Preserva deltas, unidades, cobertura, identificadores, transicoes e spans
   causais. Linhas homogeneas podem virar tabelas `columns`/`rows`, sem perda.
3. Inclui `source_pointer` nas tabelas; a linha N remete ao indice N da lista
   original. O vinculo individual `source_path` dos fatos de A permanece.
4. Reduz listas auxiliares e exemplos com contagens explicitas de omissao.
   Mensagens/consultas longas possuem contagem de caracteres omitidos.
5. Falha explicitamente se nem os fatos protegidos couberem. Nao retorna um
   pacote vazio com aparencia de sucesso.
6. Evita repetir pacotes inteiros de etapas anteriores na conversa da Skill.

O limite aumentou para 56.000 porque o conjunto real de fatos discriminativos
nao cabe no limite anterior. A serializacao compacta foi validada no prompt
completo: aumentar apenas o limite de evidencia sem essa verificacao ultrapassava
100.000 caracteres na condicao RAG.

Arquivos principais:
[projecao](../../experiment/platform/backend/evidence_projection.py),
[Skill](../../experiment/platform/backend/rca_skill.py),
[continuacao e RAG](../../experiment/platform/backend/rca_knowledge.py).

## 3. Telemetria: consultas historicas verificadas

Os endpoints localhost usuais estavam inacessiveis. Entretanto, o MCP Grafana
configurado conseguiu consultar os dados historicos. Prometheus e Loki passaram
no health check. O health check do Tempo retornou HTTP 404, metodo nao
implementado; isso **nao prova** que a consulta de traces esteja indisponivel.

O adaptador Grafana recusou timestamps com fracoes de segundo. As janelas externas
de observacao foram arredondadas para fora em menos de um segundo. Os limites
originais da falha continuam sendo a referencia para atribuir eventos.

### Container kill: lacuna confirmada nos seis casos

#### Auditoria do MCP local concluida

O codigo registra ferramentas de consulta Prometheus instant/range, discovery de
metricas/series e scrape targets; Loki query/range, labels e logs por fase; Tempo
search/get/analyze, service graph por janela/fases; golden signals, readiness,
restarts, OOM, CPU, memoria e packet drops. Esse conjunto permite as consultas
necessarias quando a instrumentacao existe. Nao ha ferramenta dedicada a eventos
historicos de Kubernetes/runtime, GC ou threads; GC pode ser consultado via
Prometheus onde suas series existem.

As alteracoes salvas do MCP foram verificadas diretamente apos o cancelamento da
auditoria delegada. Os testes de lifecycle, discovery de targets e evidencias
HTTP/traces passaram. Isso valida o codigo local, nao comprova que o servidor
implantado ja esteja usando essas alteracoes.

O contador de restart agora consulta amostras historicas brutas com padding
baseline/post e sem os timestamps sinteticos de avaliacao de `query_range`.
Separa containers e UIDs, trata resets e duplicacao de scrape, conserva todos os
servicos observados e informa cobertura parcial. Um incremento em intervalo
sobreposto a falha e um candidato com precisao `between_scrapes`, nao um evento
exatamente datado. O backend preserva essa incerteza e nao conta as amostras
externas como observacoes dentro da fase fault.

As consultas baseline-ate-post retornaram somente
`kube_pod_container_status_last_terminated_reason` para os containers afetados.
Nao retornaram `last_terminated_timestamp` ou `last_terminated_exitcode`.

| Caso | Resultado historico |
|---|---|
| payment | Completed antes da falha; Error observado posteriormente |
| user | Completed antes da falha; Error observado posteriormente |
| front-end | Error persistente, inclusive antes da falha |
| shipping | Error persistente, inclusive antes da falha |
| catalogue | Error persistente, inclusive antes da falha |
| carts | Error persistente, inclusive antes da falha |

Reason persistente nao data um evento novo. O resumo agora declara
`missing_metrics` e `event_time_coverage=unavailable`; ausencia de timestamp nao
vira evidencia de que nenhum restart/kill ocorreu. Consultar com step de 1s
nao cria dados que o scrape nao capturou.

**Proximo requisito:** verificar/exportar timestamp e exit code ou coletar eventos
historicos de Kubernetes/runtime. Isso exige instrumentacao/deploy separado;
nenhuma alteracao de infraestrutura foi feita aqui.

### CPU e memoria

- No caso catalogue/CPU, a consulta por pod mostrou aumento de consumo de CPU.
  O ratio de periodos CFS throttled cresceu, mas permaneceu abaixo de 4% nas
  amostras consultadas: isso nao sustenta, sozinho, throttling como causa.
- A metrica cAdvisor de CPU nao possui label `container` nesse ambiente.
  Uma consulta com esse label retornou vazio; por pod retornou dados.
  Seletores devem evitar incluir `catalogue-db` junto com `catalogue`.
- GC JVM existe para carts, orders e shipping, com labels
  `kubernetes_namespace`/`kubernetes_name`, nao `namespace`/`pod`.
- Em carts/memoria, o tempo de coleta young GC mudou, mas ficou na ordem de
  milesimos de segundo por segundo. Nao ha base para afirmar uma grande pausa
  de GC somente com esse resultado.

### Rede e HTTP

- Em carts/rede, as arestas front-end->carts e orders->carts aumentaram claramente
  de latencia durante a janela: aproximadamente 130->1.550 ms e 36->638 ms
  nas amostras source-side. Isso localiza impacto em carts, mas nao separa
  atraso de rede, processamento local e dependencia sem logs/spans adicionais.
- Em front-end/HTTP, a metrica de entrada mostrou principalmente 200/201/202;
  nao apareceu um aumento sustentado de 500/503. A origem `unknown` era
  preservada. Uma media global de erro em orders nao substitui a verificacao
  de rota, response code e reporter do componente suspeito.
- As taxas usam `rate([1m])`: podem misturar observacoes anteriores ao inicio
  da fase e nao equivalem a contagens exatas de requests/eventos.

## 4. Checks mais uteis, nao simplesmente mais loops

A continuacao operacional, tanto controle quanto RAG, passa a fornecer ao modelo:

- Ferramentas MCP realmente descobertas e combinacoes executaveis
  `kind/service/phase`, com preflight das consultas concretas.
- Candidato atual de trace. Trace detail sem candidato compativel e rejeitado
  antes de executar a ferramenta.
- Limites de medicao: CPU/memoria nao medem GC nem threads.
- Uma unica oportunidade adicional para corrigir uma proposta inviavel.
  Rejeicoes continuam registradas; falhas reais de transporte continuam
  explicitas. Rejeicao nao resolvida conserva a conclusao inconclusiva.
- Reserva de chamadas para reparar o JSON final. O limite de verificacoes uteis
  continua `max_rounds`; tentativas de corrigir propostas nao o aumentam.

Checks novos usam somente PromQL fixo, construido pelo host, escopo autorizado
e janelas baseline-ate-post. O modelo nao recebe liberdade para criar consultas
arbitrarias. `no_data` continua sendo uma lacuna de cobertura.

| Check | O que mede |
|---|---|
| lifecycle | Timestamp, reason e exit code quando exportados |
| cpu_throttling | Percentual de periodos CFS throttled |
| gc | Tempo de coleta JVM em s/s, onde instrumentado |
| http | Taxas por response code, reporter e aresta em req/s |
| network | Latencia media por aresta Istio source-side em ms |

A Skill base recebe a nova compactacao e os resumos de cobertura. Os checks acima
sao escolhas da continuacao operacional; nao foram adicionadas consultas
indiscriminadas a todos os servicos da Skill base.

## 5. RAG: o que mudar e o que ja mudou

A recuperacao continua simples e local: Python/Pydantic, BM25, vetores NumPy
`hashing-v1` de 256 dimensoes e fusao RRF. Nao depende de LangChain,
LlamaIndex ou banco vetorial. Trocar framework nao resolve, por si so, a
falta de dados discriminativos.

O A v2 ja normaliza o texto e associa valores, unidades, cobertura, servico,
janela e caminho da evidencia. Esta rodada tambem evita que os quatro fatos
historicos apresentados sejam apenas quatro linhas de CPU: seleciona a maior
variacao absoluta **dentro de cada metrica**, mantendo diversidade de sinais.
Nao compara grandezas de unidades diferentes entre si.

As observacoes narrativas historicas foram reduzidas de dez para seis, com
contagem de omissoes. Os fatos numericos selecionados mantem seus vinculos.
Rotulos e diagnosticos historicos continuam fora do prompt exploratorio.

### Proximos experimentos, em ordem

1. Rodar novo replay **v5** em diretorio separado, com amostragem controlada,
   verificando acuracia e fidelidade factual. Nao comparar como teste independente.
2. Resolver a cobertura de lifecycle antes de repetir os seis container kills.
3. Comparar dois versus quatro checks **uteis**, com orcamento suficiente para
   reparos e finalizacao. Medir ganho por consulta, tempo, tokens e no_data.
4. Testar recuperacao seletiva: rejeitar historico que so coincide com sintomas
   globais e privilegiar sinais/arestas compativeis com o incidente atual.
   Manter uma condicao sem memoria para detectar quando RAG prejudica.
5. So depois avaliar embeddings semanticos ou reranking. Separar cobertura da
   base, elegibilidade, ranking, fornecimento e uso correto pelo modelo.

## 6. Matriz de acompanhamento

`sim` significa classificacao conjunta correta no replay anterior, nao causa
confirmada. Todos os casos recebem a compactacao corrigida. A coluna de historico
indica top-3 exato anterior; nao e garantia de utilidade.

| Caso | Skill | Controle | RAG | Historico exato |
|---|---|---|---|---|
| shipping-cpu-hog-r1 | nao | sim | nao | nao |
| carts-network-latency-r1 | nao | nao | nao | sim |
| payment-memory-hog-r1 | nao | sim | sim | nao |
| catalogue-cpu-hog-r1 | sim | sim | nao | nao |
| payment-container-kill-r1 | nao | nao | nao | nao |
| orders-http-status-code-r1 | nao | nao | sim | sim |
| user-network-latency-r1 | nao | nao | nao | nao |
| orders-cpu-hog-r1 | sim | sim | sim | nao |
| user-http-status-code-r1 | nao | nao | nao | nao |
| front-end-memory-hog-r1 | nao | nao | nao | sim |
| user-container-kill-r1 | nao | nao | nao | nao |
| front-end-container-kill-r1 | nao | nao | nao | nao |
| payment-http-status-code-r1 | nao | nao | nao | sim |
| catalogue-memory-hog-r1 | nao | sim | nao | nao |
| orders-memory-hog-r1 | nao | sim | sim | nao |
| carts-cpu-hog-r1 | sim | sim | nao | nao |
| shipping-network-latency-r1 | nao | nao | nao | nao |
| carts-http-status-code-r1 | nao | nao | nao | sim |
| shipping-container-kill-r1 | nao | nao | nao | nao |
| user-memory-hog-r1 | nao | sim | nao | nao |
| shipping-http-status-code-r1 | nao | nao | nao | sim |
| front-end-http-status-code-r1 | nao | sim | nao | nao |
| shipping-memory-hog-r1 | nao | nao | nao | sim |
| carts-memory-hog-r1 | nao | sim | sim | sim |
| front-end-cpu-hog-r1 | nao | nao | nao | nao |
| payment-network-latency-r1 | nao | nao | nao | nao |
| catalogue-container-kill-r1 | nao | nao | nao | nao |
| orders-network-latency-r1 | nao | nao | nao | nao |
| catalogue-http-status-code-r1 | nao | nao | nao | sim |
| user-cpu-hog-r1 | nao | nao | nao | nao |
| carts-container-kill-r1 | nao | nao | nao | nao |
| catalogue-network-latency-r1 | nao | nao | nao | sim |
| payment-cpu-hog-r1 | nao | nao | nao | nao |

### Acao para cada grupo de casos

- **CPU:** confrontar maior delta local com CFS, logs e arestas; consultar GC
  apenas com instrumentacao correspondente. Nao promover orders porque agrega
  os erros de dependencias.
- **Memoria:** preservar sinal e direcao numerica; confrontar working set,
  limite, GC e OOM. Queda numerica nao pode ser descrita como aumento.
- **Rede:** investigar a aresta de destino e comparar lado cliente/servidor,
  logs de timeout e trace detail. Packet drops ausentes nao refutam latencia.
- **Kill:** resolver timestamp/exitcode/eventos e detectar mudanca de UID ou
  contadores. Sem isso, repetir o mesmo historico nao fecha a causa.
- **HTTP:** verificar codigo/rota/reporter e erros locais antes de atribuir
  cascata a orders. Resposta sintetica de proxy pode nao aparecer no log da app.

Nenhuma injecao, restart, port-forward ou alteracao do corpus foi realizada.

## 7. Resultado da avaliacao apos as correcoes

A rodada completa v5 gerou 99 respostas validas, sem erros de inferencia. O
acerto conjunto foi Skill 6/33, controle 4/33 e RAG 6/33. RAG venceu controle
em 3 casos, perdeu em 1 e empatou em 29; os dez casos com retrieval exato
permanecem inalterados. Todos os diagnósticos foram inconclusivos.

Em relacao a v4, o ganho RAG ficou concentrado em CPU (1/7 para 4/7); memoria
caiu de 3/7 para 1/7, rede e container kill continuam 0/6 e HTTP ficou 1/7.
`orders` foi escolhido pela RAG em 21/33 casos, abaixo de 23/33 na v4, mas ainda
dominante. Duas das dez recuperacoes exatas coincidiram com resposta conjunta
correta; nos 23 casos sem historico exato, foram quatro acertos.

Os resultados demonstram que a execucao corrigida funciona, nao que a
compactacao tenha causado o ganho: mudaram o formato de evidencias, selecao
diversa de sinais historicos e prompts, e o sampling usou defaults sem seed.
Repeticoes controladas e ablations de uma variavel por vez sao necessarias antes
de atribuir causalidade ou promover uma estrategia.
