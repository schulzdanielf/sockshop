# Plano inicial — workload test-only para avaliação online Skill vs. RAG

**Data:** 01/11/2026  
**Status:** proposta para revisão; workload ainda não executado  
**Escopo:** coletar e arquivar novos runs de teste, depois comparar a Skill sem
recuperação histórica com a mesma Skill usando os pares A/B já disponíveis no
OpenSearch.

## 1. Objetivo e limites

Executar uma campanha nova apenas para avaliação. Cada run produz telemetria
atual e é arquivado nos serviços históricos; depois, as duas condições de RCA
consultam esse histórico. Não haverá:

- runs de treino, fine-tuning, geração de novos guias B ou publicação de dados
de teste na base RAG;
- alteração ou reindexação do corpus atual do OpenSearch;
- acesso do diagnosticador ao rótulo de serviço/falha, ao plano do injetor ou
ao resultado do avaliador;
- substituição silenciosa de runs bloqueados, abortados ou sem telemetria.

A representação A do incidente atual pode ser construída como consulta efêmera
para o retrieval. Ela e os resultados das análises ficam auditáveis junto ao
run, mas não são incorporados ao corpus congelado.

## 2. Contexto e premissas

- A avaliação OpenSearch compartilhada em 01/11/2026 usou o índice
  `sockshop-rca-experiments-v1-8255d164bb4d41b8`, OpenSearch 2.19.0 e 32 pares
  congelados (24 com efeito da intervenção validado e 8 sem validação do
  efeito). No replay de 33 casos, a classificação conjunta foi 5/33 para Skill
  e 6/33 para RAG; nenhum diagnóstico confirmou causa. A recuperação do par
  exato no top-3 ocorreu em 7/32. Esses números são referência exploratória,
  não uma meta nem evidência de superioridade.
- A busca deve fixar o hash completo da base, versão do texto A, versão do
  retriever, parâmetros BM25/k-NN/RRF e opções de elegibilidade. Para a análise
  principal, usar `include_unvalidated_experimental=false`; os oito pares sem
  efeito validado não entram. Os guias ainda devem ser apresentados como
  heurísticas pendentes/inconclusivas, nunca como causas comprovadas.
- A ingestão da campanha será dual-write temporária: Prometheus encaminha
  somente métricas `sock-shop`, e o Collector encaminha logs e traces aos
  destinos históricos mantendo os exporters live. O corpus OpenSearch continua
  congelado e é consultado somente pela condição RAG.
- Há duas integrações a validar antes de executar a campanha:
  1. Skill pura e Skill+RAG devem ler os mesmos intervalos de Prometheus
     History, Loki History e Tempo History via MCP, com read-back validado para
     o run antes da avaliação.
  2. Skill+RAG deve consultar o índice OpenSearch congelado e usar a telemetria
     histórica online do run para construir A; os dados recém-gerados não podem
     ser indexados como exemplos B nem substituir o corpus existente.

Referências de implementação e protocolo:
[estratégia A/B](../2026-10-26-estrategia-rag-rca-conjuntos-a-b.md),
[dispatcher histórico](../../experiment/platform/backend/history_job.py),
[migração de telemetria](../../experiment/platform/tools/migrate_observability_history.py),
[leitura histórica de traces](../../experiment/platform/backend/history_archive.py),
[API Skill-RAG](../../experiment/platform/backend/api.py),
[retriever OpenSearch](../../experiment/eval/memory_hog/opensearch_experiments.py)
e [harness legado desativado](../../experiment/eval/memory_hog/frozen_rca_replay.py).
O harness nao pode pontuar RCA; use telemetria historica nova consultada via
MCP a cada avaliacao.

## 3. Desenho proposto

### Workload

Reutilizar a matriz média conhecida: sete serviços (`front-end`, `catalogue`,
`carts`, `orders`, `payment`, `shipping`, `user`) por cinco tipos de falha
(`cpu-hog`, `memory-hog`, `network-latency`, `container-kill`,
`http-status-code`), totalizando **35 runs novos, todos de teste** e uma
execução por combinação.

Cada run recebe identidade própria, janelas baseline/fault/post, ordem e
estado persistidos em manifesto. O manifesto mantém os rótulos para o avaliador,
mas essas informações não podem ser consultadas pelo executor de RCA nem entrar
em tags, texto de busca, embeddings ou contexto recuperado. Aplicar os gates de
segurança, recuperação e cleanup já existentes. Se um gate bloquear a execução,
registrar o resultado e parar conforme o protocolo, sem reinjeção automática.

### Condições pareadas

Para cada um dos mesmos 35 `run_id`s:

| Condição | Contexto |
|---|---|
| **Skill sem RAG** | Skill e telemetria histórica do run, sem recuperar A/B |
| **Skill + RAG** | Mesma Skill, telemetria, modelo e orçamento; acrescenta somente A/B recuperados do OpenSearch congelado |

O fluxo, system card, evidências atuais, número máximo de consultas, janela,
limites de tokens e formato de saída devem ser iguais. Alternar ou randomizar a
ordem das condições por run. Assim, o contraste principal mede o acréscimo da
memória, não o efeito de mais consultas ou de uma versão diferente da Skill.

Fixar modelo, versão/hash disponível e parâmetros de geração. Se o endpoint
não permitir seed determinístico, preferir três repetições pareadas por run e
condição, sem multiplicar as injeções; caso o custo impeça isso, declarar a
análise como uma rodada exploratória e registrar a limitação.

## 4. Fases e gates

### Fase 0 — tornar o caminho online verificável

1. Confirmar onde o executor será executado e que ele consegue alcançar, sem
   exposição pública, o OpenSearch congelado e os três serviços históricos.
2. Garantir que o modo sem RAG e o modo RAG consultem Prometheus History,
   Loki History e Tempo/S3 History por `run_id` e pelas janelas persistidas.
3. Confirmar que a RAG realmente busca no índice OpenSearch congelado, exclui o
   próprio run e não usa rótulos ou identificadores da combinação para ranking.
4. Fazer um smoke test de leitura com run já arquivado, se disponível; se não
   houver um, definir um canary separado e fora dos 35 casos avaliados.

**Gate:** interromper antes da campanha caso qualquer fonte seja lida do
backend operacional corrente, o retrieval use outro corpus ou o ground truth
possa chegar ao diagnosticador.

### Fase 1 — congelar protocolo e manifesto

Registrar antes do primeiro run: 35 combinações, identificadores, ordem,
versões do experimento/Skill/modelo, parâmetros de coleta, janelas, configuração
do migrador, endpoint histórico, hash do corpus OpenSearch, versão do índice e
do retriever, elegibilidade e orçamento de investigação. Manter separado o
manifesto de execução do arquivo de rótulos acessível ao avaliador.

### Fase 2 — executar e persistir a telemetria

1. Ativar dual-write para o ID da campanha e desativar o migrador pós-run.
2. Executar os 35 runs com novos IDs e guardar snapshots brutos, janelas,
   evidências, eventos e resultados dos gates.
3. Aguardar read-back histórico por run: métricas em baseline/fault, logs do
   serviço-alvo e traces do serviço-alvo. Persistir argumentos, contagens e
   status da verificação.
4. Executar Skill/RAG somente após o read-back estar completo. Ambas as
   condições devem consultar o MCP histórico; a RAG também consulta o índice
   OpenSearch congelado. O bundle S3 não é requisito do modo dual-write.
5. Preferir runs não sobrepostos e processamento em ordem temporal; não iniciar
   outro run antes de validar o anterior e confirmar recuperação/cleanup.

Runs com arquivo `partial`, `missing` ou falha continuam no manifesto e são
reportados como indisponibilidade técnica. Não os substituir silenciosamente;
registrar denominador planejado (35), número avaliável e motivos de exclusão.

### Fase 3 — executar as avaliações online pareadas

Para cada run arquivado, chamar ambas as condições contra os backends históricos,
usando a mesma janela e o mesmo procedimento de coleta. Persistir consultas,
argumentos, horários, respostas, cobertura e hashes de evidência por tentativa.
Comparar os hashes/cobertura das evidências atuais entre as condições; divergência
invalida o pareamento até ser explicada.

Na condição RAG, registrar hash da base, candidatos, scores, filtros e pares A/B
efetivamente usados. Falha de retrieval e recuperação vazia são resultados
distintos. Não permitir fallback silencioso ao corpus alternativo, ao backend
corrente ou à entrada de teste recém-produzida.

Persistir as duas respostas finais antes de liberar o arquivo de ground truth ao
avaliador. O avaliador calcula métricas fora do caminho de ferramentas/prompts
do diagnosticador.

### Fase 4 — análise e relatório

Reportar resultados pareados pelos mesmos IDs e separar:

- classificação de serviço e tipo de falha, individualmente e em conjunto;
- suporte causal baseado em evidência atual, alternativas, abstenções e
  confirmações indevidas;
- cobertura e completude por fonte, runs sem resultado e falhas técnicas;
- recuperação (IDs, recall@k quando aplicável e utilidade dos pares), latência,
  número de consultas/chamadas e custo.

Acerto do rótulo de injeção não equivale a confirmação causal. Apresentar
contagens, denominadores e resultados por tipo de falha; não declarar vitória da
RAG como critério de conclusão do plano. Congelar relatório, manifestos,
evidências e saídas. Não promover nenhum dos 35 casos à base OpenSearch.

## 5. Critérios de aceite

- Os 35 casos planejados têm identidade, janelas e desfecho explícitos; nenhuma
  execução foi adicionada ou removida para melhorar resultados.
- Cada diagnóstico elegível foi obtido consultando os serviços históricos; as
  três famílias tiveram read-back verificado, não apenas envio aos exporters.
- Ambas as condições usam evidências atuais equivalentes e diferem somente pela
  recuperação A/B; o índice e sua política de elegibilidade permanecem
  congelados.
- Ground truth não aparece nas entradas da Skill, nas consultas, no retrieval ou
  nos artefatos fornecidos ao modelo.
- Relatório distingue desempenho classificatório, sustentação causal,
  disponibilidade de dados e custo. Runs ausentes e resultados inconclusivos
  permanecem visíveis.

## 6. Entregáveis do próximo ciclo

1. Decisão sobre ambiente do runner e integração de leitura histórica de métricas
   e logs.
2. Adaptador/caminho online para OpenSearch usando o corpus já congelado, sem
   reindexar os dados de teste.
3. Manifesto test-only e configuração congelada da campanha.
4. Ledger de runs e registros de read-back histórico por fonte.
5. Artefatos de cada tentativa Skill/RAG e relatório pareado final.

Este documento organiza a execução futura; não autoriza por si só injeção de
falhas, mudança de infraestrutura, publicação do OpenSearch ou início da
campanha.
