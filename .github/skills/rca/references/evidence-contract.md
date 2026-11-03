# Contrato de evidencias e saida

Cada consulta recebe `query_ref` e `evidence_id`, nivel, tool, argumentos,
fonte (`live`, `snapshot` ou `archive`), familia, servico/escopo, estado,
contagem de observacoes e digest sanitizado. `observation_count` conta todas as fases;
`fault_observation_count` conta somente fault e governa a corroboração causal.
IDs inexistentes, tools indisponiveis, erros, respostas vazias e sinais apenas
de baseline/post nao sustentam uma causa. Resultados parciais preservam suas
limitacoes.

## Plano e hipoteses

O coordenador seleciona uma categoria permitida e ate tres candidatos do
ranking de metricas. O plano deve conter ao menos duas hipoteses distintas,
cada uma com:

- `hypothesis_id` estavel (`H1`-`H5`);
- `service` conhecido ou `null`;
- `category` permitida e `mechanism` verificavel;
- `supporting_evidence` e `contradicting_evidence` (IDs, possivelmente vazios
  antes da coleta);
- `unresolved_questions` para orientar os checks.

Metricas heterogeneas nao devem ser somadas em um escore bruto. O executor
normaliza/rankeia separadamente perfis com unidades diferentes; a selecao de
um candidato e triagem, nao evidencia causal. Relatorios estruturados devem
referenciar `hypothesis_ids` e `evidence_ids` em cada achado.

Perfis `edge_latency_*` preservam o chamador e o destino (`source_workload` /
`destination_workload` ou `client` / `server`). Compare somente a mesma aresta
entre baseline e fault; um destino com mais de um chamador deve manter a aresta
que explica o delta, sem misturar unidades ou agregar deltas entre perfis.
`request_server_seconds` descreve a duracao do lado servidor; a serie
`request_client_seconds` ajuda a avaliar o tempo observado pelo chamador.
Ambas sao metricas Prometheus derivadas de spans e pertencem a familia
`metrics`, nao contam como uma segunda familia `traces`. Os pontos de uma
range query sao observacoes de avaliacao, nao contagens de requisicoes.

## Escopo e follow-ups

O escopo inicial e derivado dos candidatos metricos e dos sinais de lifecycle,
com limite global de quatro servicos. Logs, traces e consultas detalhadas so
podem usar servicos do snapshot autorizado. O resultado registra o historico
de escopos para que chamadas anteriores continuem auditaveis apos uma revisao.

No maximo uma revisao pode adicionar um servico, e somente se:

- o servico constar no ranking completo da triagem inicial;
- a solicitacao citar evidencia inicial de metricas `ok`, com observacoes
  positivas em fault, que identifique explicitamente esse servico;
- a expansao mantiver o limite total e registrar justificativa, evidencia e
  snapshots anterior/novo.

O modelo nao pode ampliar o escopo por hipotese, categoria, dependencia
presumida ou sinal de logs/traces. Follow-ups devem referenciar uma ou mais
hipoteses existentes. Filtros de log sao limitados a Loki e o host valida
ferramenta, fase, servico, repeticao e parametros; uma correcao estrutural pode
ser tentada uma vez. Falhas de agente/consulta devem ser explicitas e nao podem
apagar evidencia previamente coletada. O orcamento reserva capacidade para a
sintese final.

## Sintese causal

A resposta final contem:

- `status`: `confirmed` ou `inconclusive`; confirmacao significa hipotese
  corroborada, nao prova experimental definitiva;
- `primary_service`: servico autorizado ou `null`;
- `category`: obrigatoriamente uma categoria selecionavel do cartao, nunca
  `unknown`, mesmo quando o estado e inconclusivo;
- `fault_type`: quando `possible_fault_types` vier no contexto, escolha
  obrigatoriamente e de forma exata um tipo dessa lista, inclusive em
  `inconclusive`. No benchmark de cinco falhas, as opcoes sao `container-kill`,
  `cpu-hog`, `memory-hog`, `network-loss` e   `http-status-code`. Esse rotulo e
  distinto de `category`; nao e evidencia e nao confirma o mecanismo. A matriz
  controlada atual usa `network-latency` no lugar de `network-loss`; a lista
  enviada junto ao run define as opcoes exatas daquela avaliacao;
- `observed_events`, `probable_mechanism`, `root_cause`, `impact` e
  `causal_chain`, separando causa, sintoma e impacto;
- `supporting_evidence` e `contradicting_evidence` com IDs reais;
- ao menos duas `alternatives` estruturadas com servico, categoria,
  mecanismo, evidencias favoraveis/contrarias e perguntas em aberto;
- `restart_analysis` por servico reiniciado, com `reason_status` (`supported`
  ou `unknown`), motivo, evidencias e proximo check;
- `limitations` e `next_checks`, sem executar remediacao.

Para confirmar, use ao menos duas familias independentes entre `metrics`,
`logs` e `traces`, vinculadas a mesma ocorrencia e janela fault. OOM,
readiness e restart sintetizados de kube-state-metrics continuam sendo uma
familia (`metrics`); duas consultas Prometheus nao sao fontes independentes.
Se a corroboração, cobertura temporal ou causa de restart for insuficiente,
registre `inconclusive`, preserve as alternativas e indique o check pendente.
As evidencias de suporte tambem devem estar temporalmente sobrepostas e
identificar o servico primario ou uma dependencia declarada. O host pode
rebaixar a conclusao. `category_confidence` deve refletir a confianca na
classificacao: conclusoes inconclusivas recebem `low`; uma categoria obrigatoria
nao significa que seu mecanismo foi comprovado.

Traces fault sem spans de erro continuam sendo candidatos uteis quando sua
duracao excede o P95 baseline da mesma operacao. O aprofundamento deve
priorizar traces de erro e, se nao existirem, traces com essa regressao de
latencia. Sem amostras baseline comparaveis, nao classifique a duracao isolada
como regressao; registre a limitacao.

`history_archive_traces` e uma leitura direta do bundle de spans do run,
verificada pelo SHA-256 do manifesto, e pertence a familia `traces`. Confira a
paridade entre contagens arquivadas e do manifesto, cobertura das janelas,
busca de origem completa e erros de coleta. `search_index_status` mede a
busca no Tempo e nao invalida, por si so, spans legiveis diretamente do
arquivo. A copia arquivada e os mesmos traces do Tempo, nao uma familia
independente; nunca use a ausencia no indice como prova de ausencia de spans.

## Avaliacao

Preflight deve conferir `strategy_version` e `prompt_version` expostas pela
API antes de iniciar a campanha; cada resposta tambem deve corresponder a
essas versoes. Reporte por run e agregado: recall do servico-alvo no ranking
metrico e escopo final, revisoes e violacoes de escopo, reparos/falhas de
validacao/agentes, cobertura de cada familia em fault, confirmacoes sem apoio
independente, tokens/chamadas/latencia e acuracia de servico e `fault_type`,
estratificada pela validacao do efeito da injecao. A comparacao pontua somente
localizacao do servico e `fault_type` (correspondencia exata com `chaos_type`);
`category` continua parte do diagnostico RCA, mas nao entra no placar.
Comparacoes entre avaliacoes da Skill RCA e da estrategia multiagente devem
usar a mesma lista `possible_fault_types` e somente IDs de run compartilhados.
Conte falhas/ausencias como incorretas no denominador pareado e reporte ganhos,
perdas, diferenca liquida e acuracia por tipo de falha.

## Fontes e precisao

Metricas e eventos sintetizados de kube-state-metrics pertencem a `metrics`;
nao conte OOM, readiness e restart como tres telemetrias. Logs e traces sao
familias diferentes, mas copias da mesma mensagem nao sao prova independente.
Cruze servico, pod/container, operacao e janela; indique quando nao conseguir.

Referencia pre-falha: ate 45 s. PromQL pode usar lookback de 1 min: a taxa
inclui observacoes anteriores ao primeiro timestamp consultado.
Ausencia de trace pode refletir filtros, sampling, ingestao ou retencao.
Ausencia de logs pode refletir identidade incorreta ou processo terminado.
