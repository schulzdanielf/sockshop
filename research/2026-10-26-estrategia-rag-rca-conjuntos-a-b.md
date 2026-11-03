# Estrategia de RAG para RCA com evidencias e guias diagnosticos

**Data:** 26/10/2026.  
**Nova execucao autorizada em 27/10/2026:** apos corrigir a coleta do primeiro
gate de memoria e o ensaio RAG, sincronizar o relogio Windows/WSL e aprovar
um ensaio sem falha nos sete servicos, foi iniciada a campanha
`controlled-20261027T211124980806Z`, com novo corpus, 35 treinos e 35 testes.
A API foi reiniciada com o coletor corrigido. O primeiro run,
`run-406af66d1b8a` (`orders-cpu-hog-r0`), foi confirmado `running`; ainda nao
se declara campanha concluida ou eficacia validada. A campanha anterior abaixo
e seus gates permanecem preservados; seus pares A/B nao sao reutilizados.
Os gates de seguranca nao foram relaxados. As correcoes passaram em 225 testes
de coleta/supervisao/ensaio e 54 testes de RAG/RCA/avaliacao.

**Status:** protocolo implementado e validado por 45 testes focados; eficacia
experimental ainda nao validada. A campanha
`experiment/eval/memory_hog/out/controlled_campaign/controlled-20261026T235109860120Z`
foi interrompida com 9/35 treinos concluidos, 1 run abortado, 25 treinos nao
executados e os 35 testes nao iniciados. Os nove pares A/B gerados estao
`pending/inconclusive`; nenhum e elegivel, a base nao foi congelada e nao ha
resultado de acuracia. O relatorio offline registra os denominadores e casos
ausentes em
[`rag_evaluation.json`](../experiment/eval/memory_hog/out/controlled_campaign/controlled-20261026T235109860120Z/rag_evaluation.json).
**Atualizacao:** 27/10/2026. O run
`carts-memory-hog-r0` (`run-e0254896b440`) falhou com recuperacao nao confirmada
e telemetria de seguranca de memoria sem amostras. O workflow Litmus ainda estava
`Running` ao vencer o timeout de 120s; a tentativa de exclusao retornou sucesso.
Depois, consultas somente leitura confirmaram que o workflow especifico nao
existia e nao havia workflows ativos/pendentes nem pods/jobs de injector
correspondentes. Isso nao aprova retroativamente os gates de recuperacao e
cleanup: o supervisor parou sem iniciar outro run, e nao se deve repetir nem
continuar ate revisao operacional manual. Nao havia run ou processo de carga
ativo. As correcoes de geracao JSON estruturada, referencias limitadas a
evidencias existentes e rejeicao auditavel de checks repetidos passaram nos
testes focados. A primeira campanha, interrompida por esgotamento do orcamento
de tokens, foi
`experiment/eval/memory_hog/out/controlled_campaign/controlled-20261026T232828593477Z`.
A RCA somente leitura persistida em
[`skill_rca_5693b3c25eba4f0a8309aa15227f4216.json`](../experiment/platform/data/object_store/run-e0254896b440/skill_rca_5693b3c25eba4f0a8309aa15227f4216.json)
tambem ficou inconclusiva: as metricas mostraram aumento de CPU media em
`carts` de 11,65% para 39,02%, memoria de 42,24% para 53,18% e P95 de 97,7 ms
para 113,1 ms, mas OOM retornou `no_data`, nenhum restart foi detectado e logs
de `carts` e traces estavam ausentes. As listas finais de evidencias de suporte
e contradicao ficaram vazias; portanto `memory-exhaustion` permanece hipotese,
nao causa confirmada nem explicacao para a falha do gate de seguranca.
Os documentos de execucao estao no [guia operacional](../experiment/eval/memory_hog/README.md#online-skill-rca-rag-ab-70-runs-35-train--35-test).

## 1. Objetivo

Produzir conhecimento operacional recuperavel a partir de incidentes historicos
para ajudar a investigar novos incidentes. O conhecimento deve permitir:

1. Encontrar casos com evidencias e sintomas semelhantes.
2. Recuperar como os casos historicos foram investigados e qual explicacao
   causal foi sustentada, incluindo alternativas e limites.
3. Orientar verificacoes na telemetria atual sem transferir automaticamente
   a conclusao historica.

A estrategia utiliza dois conjuntos relacionados:

- **A - Evidencias e sintomas:** retrato estruturado da investigacao inicial,
  produzido sem acesso ao ground truth e utilizado na busca por similaridade.
- **B - Guia diagnostico:** conhecimento revisado que conecta as observacoes
  iniciais a mecanismos, verificacoes discriminativas e conclusoes sustentadas.

**Principio central:** recuperar pelo que foi observado; usar o historico para
escolher verificacoes; concluir com base nas evidencias do incidente atual.

A proposta concretiza parte da
[metodologia de conhecimento diagnostico contextualizado](./2026-10-21-metodologia-conhecimento-diagnostico-contextualizado.md).
Nao pressupoe que RAG sera superior ao diagnostico sem historico.

## 2. Decisoes confirmadas

| Dimensao | Decisao |
|---|---|
| Executor inicial | Skill RCA, nao o executor multiagente |
| Momento da recuperacao | Apos primeira passagem delimitada de metricas, logs e traces, antes dos aprofundamentos |
| Campanha | Matriz media de 35 combinacoes, com duas execucoes independentes por combinacao: 70 runs planejados |
| Origem da base | 35 novos runs de treino, um por combinacao |
| Teste fechado | 35 novos runs de teste, uma segunda execucao de cada combinacao |
| Ordem das fases | Concluir treino, construir e congelar a base antes de iniciar os testes |
| Geracao de A | Pelo modelo, sem ground truth, orientada a recuperar incidentes proximos |
| Geracao de B | Pelo modelo, recebendo rotulos de servico e causa no treino e investigando sua sustentacao na telemetria |
| Aprovacao de B | Sem aprovacao humana obrigatoria; elegibilidade depende de validacoes automatizadas e status explicito |
| Coleta na avaliacao inicial | Ao vivo desde o primeiro ciclo |
| Alteracao dos pesos do modelo | Nao prevista neste ciclo |
| Preservacao do controle | Manter a Skill RCA original e permitir comparacao com RAG desligado e ligado |

"Treino" designa a construcao e curadoria da base de conhecimento, nao
fine-tuning. Embeddings sao uma etapa de indexacao distinta das chamadas ao
modelo generativo.

### 2.1 Matriz e divisao dos 70 runs

Reutilizar a matriz da campanha controlada media:

- Sete servicos: `front-end`, `catalogue`, `carts`, `orders`, `payment`,
  `shipping` e `user`.
- Cinco falhas: `cpu-hog`, `memory-hog`, `network-latency`, `container-kill`
  e `http-status-code`.
- 7 x 5 = 35 combinacoes.
- Duas execucoes por combinacao: uma de treino e uma de teste.
- 35 runs de treino + 35 runs de teste = 70 runs planejados.

Cada execucao tem injecao, carga, warmup, baseline, janelas, validacao do efeito,
recuperacao e identidade proprios. A segunda repeticao nao reutiliza a telemetria
da primeira. Preservar os perfis de intensidade, duracoes e gates da campanha
media, salvo mudanca explicitamente aprovada e versionada.

Persistir antecipadamente combinacao, repeticao, papel `train`/`test`, seed,
ordem e identificador unico de cada run planejado. O identificador da combinacao
e o papel pertencem ao controle experimental; nao podem fornecer o gabarito
ao diagnosticador nem selecionar o vizinho na recuperacao.

As 35 primeiras execucoes pertencem ao treino; a ordem das combinacoes dentro
de cada fase deve ser reproduzivel. A base e congelada antes das 35 execucoes
de teste. Nao misturar treino e teste em uma unica permutacao global.

Este desenho substitui a proposta anterior de base principal historica e
replicas agrupadas no mesmo split. Ele mede **transferencia entre execucoes
independentes dos mesmos cenarios conhecidos**, nao generalizacao para novas
combinacoes, novos servicos ou mecanismos ineditos. A repeticao da combinacao
e intencional; reutilizar resultados ou evidencias do proprio teste nao e.

Historico anterior pode apoiar desenvolvimento de prompts e verificacoes de
infraestrutura, mas nao integra automaticamente a base principal desta campanha.
Nao reservar um terceiro split dentro dos 70 runs nem ajustar parametros pelos
35 testes. Fixar configuracoes antes do teste e identificar separadamente
qualquer ajuste feito durante o treino.

Os totais sao planejados, nao garantia de conclusao ou elegibilidade. Runs
bloqueados, abortados, sem efeito verificado ou sem telemetria suficiente
continuam no manifesto; nao reinjetar nem substituir silenciosamente para
atingir 35 resultados favoraveis por fase. A matriz continua tendo 70 celulas
planejadas e a divisao 35/35 nao muda. Como excecao operacional, um aborto
`container-kill` de treino com um restart confirmado pode receber uma unica
tentativa substituta somente apos revisao e autorizacao explicitas, sob um
adendo de protocolo versionado. A tentativa original permanece no ledger e
fora da coorte de treino; a substituta precisa passar pelos mesmos gates.
Esse adendo pode elevar o total de tentativas submetidas a 71, que deve ser
reportado separadamente das 70 celulas planejadas. Nenhum outro retry de
injecao e permitido, e uma falha da substituta interrompe a campanha.

## 3. Relacao com a arquitetura existente

A inspecao que fundamentou esta definicao identificou:

- [Skill RCA](../experiment/platform/backend/rca_skill.py): coleta metricas e
  ciclo de vida, chama o modelo para planejar, consulta logs e traces e chama
  o modelo para diagnosticar. O executor original foi preservado; o ciclo
  adicional esta em [rca_knowledge](../experiment/platform/backend/rca_knowledge.py).
- [API da plataforma](../experiment/platform/backend/api.py): integra os
  executores com observabilidade via MCP e persistencia de tentativas.
- [Recuperacao](../experiment/platform/backend/analysis/retrieval.py): ranking
  por tags, embeddings e componentes de grafo, aproveitavel mediante controle
  dos campos usados e da elegibilidade dos candidatos.
- [Provedores de embeddings](../experiment/platform/backend/analysis/embeddings.py):
  interfaces existentes que evitam exigir um novo banco vetorial no piloto.
- [Armazenamento](../experiment/platform/backend/storage/sqlite_storage.py):
  artefatos e metadados existentes. O store de conhecimento adicional persiste
  A/B, indice e base congelada de forma imutavel no mesmo object store.
- [Contrato de evidencias](../.github/skills/rca/references/evidence-contract.md):
  referencias reais, cobertura, limitacoes e requisitos de sustentacao a preservar.

O fluxo confirmado utiliza chamadas da Skill RCA, nao chamadas separadas
para especialistas de metricas, logs e traces.

### Cuidado com os resumos e indices existentes

O [resumo atual de runs](../experiment/platform/backend/analysis/summary.py)
inclui campos como `chaos_type` e identificador do experimento. O
[montador de prompt](../experiment/platform/backend/analysis/prompt.py)
mascara alguns campos do alvo, mas mascarar somente o prompt nao protege uma
busca ou um embedding ja construidos com informacoes privilegiadas.

A deve ser produzido a partir de uma projecao permitida dos dados operacionais,
nao de um registro completo posteriormente limpo por regras textuais.
O reaproveitamento da infraestrutura nao implica reutilizar indiscriminadamente
os resumos, tags ou indices antigos.

## 4. Contrato conceitual de A

### 4.1 Conteudo

A e gerado pelo modelo a partir das evidencias permitidas, tanto no treino
quanto no teste. Seu objetivo principal e representar o padrao observavel
do incidente para recuperar outros incidentes proximos, nao antecipar o rotulo.

- Servicos observados e relacoes declaradas ou observadas, com proveniencia.
- Sintomas e anomalias detectados.
- Metricas, unidades, variacoes e referencia de comparacao.
- Padroes observados em logs e traces.
- Sequencia temporal, janelas e incerteza de medicao.
- Referencias a evidencias, ferramentas e consultas efetivamente executadas.
- Cobertura, erros de coleta e limitacoes.
- Hipoteses preliminares, separadas dos fatos observados.

Servicos identificados pela telemetria sao informacao operacional legitima.
O alvo configurado pelo injetor nao pode ser apresentado como se tivesse sido
identificado pela investigacao.

### 4.2 Ponto de corte

A deve representar o mesmo ponto do procedimento em construcao e teste:
primeira passagem delimitada por metricas, logs e traces, antes da investigacao
orientada pelo historico.

A definicao exata das consultas, janelas e orcamentos sera estabelecida no plano
de execucao. Fontes ausentes devem ser registradas como limitacoes, nao como
ausencia de falha.

Depois de produzido, A e congelado. Evidencias descobertas durante a anotacao
com ground truth nao podem melhorar retrospectivamente seu conteudo.

### 4.3 Campos para recuperacao

Priorizar sinais observacionais, variacoes, sequencia temporal e estrutura de
dependencias. Preservar hipoteses para auditoria, sem permitir que uma
classificacao preliminar domine a busca e reforce um erro inicial.

Nao incluir no texto recuperavel o tipo de falha conhecido pelo injetor,
o alvo configurado, resultados do avaliador ou nomes que revelem essas respostas.

## 5. Contrato conceitual de B

### 5.1 Conteudo

- Rotulo de servico e rotulo de causa/tipo de falha fornecidos ao modelo
  exclusivamente no treino, identificados como anotacao experimental.
- Causa e servico de origem, quando sustentados.
- Relacao entre os sintomas de A e os mecanismos considerados.
- Etapas de investigacao necessarias para distinguir as hipoteses.
- Evidencias favoraveis e contrarias, com referencias.
- Consultas reproduziveis e requisitos de disponibilidade das fontes.
- Hipoteses alternativas e criterios para distingui-las.
- Sequencia temporal e separacao entre causa, sintoma e impacto.
- Evidencias ausentes, limitacoes e condicoes de aplicabilidade.
- Condicoes de carga, contexto e versoes relevantes.
- Proveniencia da geracao pelo modelo, das consultas e da validacao automatizada.

Cada par A/B deve compartilhar um identificador estavel e manter
rastreabilidade para o experimento de origem e suas evidencias.

### 5.2 Separacoes obrigatorias

| Natureza da afirmacao | Tratamento |
|---|---|
| Intervencao configurada | Fato do experimento, acessivel ao anotador; nao e evidencia operacional |
| Efeito da intervencao verificado | Resultado sustentado por registros de validacao identificaveis |
| Observacao de telemetria | Fato com fonte, janela, cobertura e referencia |
| Interpretacao causal | Hipotese ou conclusao fundamentada, com alternativas e limites |
| Consulta executada | Argumentos e resultado registrados |
| Verificacao recomendada | Procedimento ainda nao executado, explicitamente identificado |

Acertar o nome da falha injetada nao valida automaticamente o mecanismo nem
explica a degradacao. Nao inventar consultas, observacoes ou resultados.

O modelo construtor de B recebe explicitamente o servico e a causa rotulados
para buscar evidencias, investigar alternativas e construir o guia diagnostico
do caso de treino. Nao deve apenas reescrever o rotulo como explicacao.
O rotulo de injecao deve permanecer distinto de categoria e mecanismo causal.

Nos testes, os rotulos presentes em B sao os dos incidentes de treino
recuperados, nunca os do incidente em avaliacao. Eles orientam hipoteses e
checks; nao dispensam a investigacao na telemetria atual.

### 5.3 Revisao e elegibilidade

Como proposta de representacao, separar:

- `guide_status`: `pending`, `validated` ou `rejected`;
- `causal_status`: `supported` ou `inconclusive`.

Os nomes finais e regras de transicao serao definidos na implementacao.
O status do guia nao deve ser confundido com o contrato de saida do diagnosticador.

Nao ha aprovacao humana obrigatoria. A e B sao construidos pelo modelo;
o host valida estrutura, proveniencia, referencias existentes, consultas
executadas, isolamento e criterios de sustentacao. A concordancia de um modelo
revisor com o anotador nao e evidencia independente.

Um guia pode explicar por que a causa nao e identificavel, sem transformar
o rotulo em causa validada. Guias inconclusivos permanecem registrados com
suas lacunas; para este piloto, somente guias com `guide_status=validated`
e `causal_status=supported` entram na recuperacao principal.
Guias pendentes ou rejeitados ficam fora do indice publicado.

Validacao automatizada nao garante adequacao causal. Sem revisao humana
obrigatoria, esse risco deve constar no relatorio e ser medido por auditoria
de evidencias. Uma revisao humana opcional nao pode selecionar apenas os
casos com diagnostico correto nem alterar a base durante o teste fechado.

## 6. Fluxo de construcao da base: treino

`M` identifica uma chamada ao modelo generativo. Consultas MCP, validacoes
estruturais, busca, persistencia e embeddings sao contabilizados separadamente.

```text
Run de treino: uma das 35 combinacoes
          |
Visao operacional sem ground truth
          |
Consultas iniciais de metricas e ciclo de vida
          |
[M1] Skill: plano inicial e hipoteses
          |
Consultas de logs e traces
          |
[M2] Skill original: diagnostico inicial D0
          |
[M3] Modelo: documento A orientado a recuperacao
          |
Validacao e congelamento de A e D0
          |
Processo separado de anotacao recebe:
A + evidencias registradas + rotulos de servico e causa
          |
[M4] Modelo anotador: hipoteses guiadas pelos rotulos
     e plano de busca de evidencias
          |
Executor valida e executa consultas na telemetria de treino
          |
[M4 adicional, se necessario] Interpreta resultados e propoe novo check
          |
[Repetir consulta -> chamada ao modelo, se necessario]
          |
[M5, apos os checks] Modelo constroi B com evidencias e limitacoes
          |
Validacao automatizada de B e de sua sustentacao
          |
Publicar pares elegiveis; preservar os demais com status e lacunas
          |
Gerar embedding dos campos recuperaveis de A
```

### Acionamentos

| Chamada | Entrada | Saida | Ground truth |
|---|---|---|---|
| M1 | Contexto permitido, metricas e ciclo de vida | Plano e hipoteses iniciais | Proibido |
| M2 | Contexto permitido e evidencias da primeira passagem | D0, pelo executor original | Proibido |
| M3 | Evidencias da primeira passagem, sem rotulos nem D0 como entrada | A para recuperacao | Proibido |
| M4, por rodada | A congelado, evidencias e rotulos de servico e causa | Plano de busca ou encerramento fundamentado | Permitido, no contexto separado de anotacao |
| M5, final | Evidencias iniciais, consultas adicionais e rotulos | B fundamentado, com lacunas e status | Permitido, somente na anotacao |

O caminho com uma decisao de check tem cinco chamadas generativas por run
de treino, sem reparos. Duas decisoes de check resultam em seis chamadas.
A geracao de A foi separada de D0 para preservar integralmente a Skill
original; A nao recebe o diagnostico como rotulo. Essa e uma alteracao
explicita em relacao a proposta inicial de uma chamada conjunta A/D0.

O anotador nao compartilha conversa ou memoria de execucao com o diagnosticador.
Evidencias adicionais sao incorporadas a B com proveniencia; A permanece congelado.

Construir A e investigar B junto a cada run de treino, dentro da retencao
das fontes. Nao esperar o termino dos 35 runs para iniciar todas as consultas.
Persistir as evidencias para auditoria. Dados ausentes mantem B pendente ou
inconclusivo; os rotulos nao preenchem lacunas.

Planejar 35 registros A e 35 propostas B, um par por run de treino concluido.
A base recuperavel pode ter menos de 35 pares se algum guia nao cumprir os
criterios. Reportar gerados, elegiveis, inconclusivos, rejeitados e ausentes.

## 7. Recuperacao de pares

1. Gerar o embedding dos campos permitidos de A atual.
2. Aplicar filtros de elegibilidade e separacao dos conjuntos.
3. Recuperar A historicos por similaridade.
4. Carregar B exclusivamente pelo vinculo com cada A selecionado.
5. Entregar os pares e sua proveniencia ao diagnosticador.

Parametros previstos:

- Numero maximo de incidentes recuperados.
- Limiar de relevancia.
- Provedor e versao de embedding.
- Modo de ranking compativel com a infraestrutura.
- Versao publicada da base.
- Limite de contexto reservado ao historico.
- Filtros de aplicabilidade e controle de repeticoes.

Os valores serao escolhidos no desenvolvimento, sem ajuste pelo teste fechado.
Scores indicam similaridade, nao probabilidade de compartilhar a causa.
Limiar de um provedor nao deve ser transferido automaticamente para outro.

Evitar que o top-k seja ocupado somente por replicas quase identicas.
Registrar todos os A selecionados, scores, B associados e quais guias foram
efetivamente utilizados.

Se nenhum caso superar os criterios, continuar sem apoio historico e registrar
explicitamente a recuperacao vazia. Erro de recuperacao deve ser distinguido
de ausencia legitima de casos relevantes.

## 8. Fluxo de diagnostico: teste com RAG

```text
Novo experimento
       |
Janelas operacionais fixadas
Ground truth isolado
       |
Consultas ao vivo: metricas e ciclo de vida
       |
[M1] Skill: plano inicial e hipoteses
       |
Consultas ao vivo: logs e traces
       |
[M2] Skill original: diagnostico inicial D0
       |
[M3] Modelo: A atual, sem receber D0 como rotulo
       |
Persistir A e D0
       |
Embedding de A
Recuperar A historicos elegiveis
Carregar seus B automaticamente validados e elegiveis
       |
[M4] Skill: confrontar os guias com o caso atual
     e propor verificacoes discriminativas
       |
Executor valida e executa as consultas
       |
[M4 adicional, se necessario] Skill: interpretar resultados;
     propor proxima verificacao ou encerrar
       |
[Repetir consulta -> chamada ao modelo,
 se necessario e dentro do orcamento]
       |
[M5] Skill: diagnostico final
       |
Validacao e persistencia
       |
Avaliador separado acessa ground truth
```

### Acionamentos

| Chamada | Objetivo | Restricao principal |
|---|---|---|
| M1 | Planejar a primeira passagem com sinais atuais | Sem historico recuperado e sem ground truth |
| M2 | Registrar D0, pelo executor original | Nao usar informacoes posteriores ao ponto de corte |
| M3 | Gerar A sem receber D0 como rotulo | Priorizar padroes observados para recuperacao |
| M4, por rodada | Avaliar guias, resultados e escolher checks ou parar | B orienta o procedimento, nao prova o incidente |
| M5 | Sintetizar o diagnostico final | Sustentar afirmacoes com evidencias atuais |

Com uma decisao adicional ha cinco chamadas generativas e sintese final
separada; com duas decisoes ha seis, antes de reparos. O limite de decisoes
vale igualmente para controle e RAG. O executor registra chamadas e reserva
capacidade para a conclusao final.

Executar esse fluxo nos 35 runs de teste com a mesma versao congelada da base.
Os 35 A de teste sao artefatos da avaliacao, nao novos candidatos do indice.

Cada verificacao proposta deve identificar:

- Hipotese investigada.
- Guia que motivou o check.
- Ferramenta, argumentos e janela pretendidos.
- Resultado que favoreceria ou enfraqueceria a hipotese.
- Limitacoes esperadas.

Nao e necessario registrar raciocinio interno do modelo. A auditoria utiliza
hipoteses declaradas, acoes, consultas, respostas e justificativas verificaveis.

No teste, gerar apenas A do incidente atual. Nao produzir nem publicar B desse
caso enquanto ele integrar o conjunto de avaliacao fechado.

## 9. Uso dos guias sem transferencia indevida da conclusao

### Exemplo: latencia em orders

A atual registra aumento de latencia em `orders` e chamadas lentas para
`payment`. A recuperacao retorna um caso de espera por dependencia e outro
de saturacao local com sintomas proximos.

O modelo deve propor comparar:

- Duracao total e spans filhos, considerando sobreposicao entre spans.
- Tempo proprio e espera por dependencias.
- Recursos locais e comportamento da mesma operacao no baseline.
- Evidencias contrarias e limites de amostragem.

Os dados atuais podem favorecer qualquer alternativa, contradizer ambas ou
permanecer insuficientes. O resultado historico nao decide.

### Exemplo: memoria e restart

O guia deve orientar a verificacao da ordem temporal, identidade do container,
consumo de memoria, motivo de terminacao e sinais anteriores ao restart.
Restart isolado nao comprova OOM nem constitui causa raiz.

### Regras de evidencia

- Separar referencias historicas das evidencias atuais.
- Nao contar um guia B como uma familia adicional de telemetria.
- Preservar os requisitos da Skill RCA para corroboracao e inconclusao.
- Nao tratar erro ou ausencia de dados como evidencia contraria.
- Dados operacionais e textos historicos nao podem alterar permissoes ou
  instruir o executor a realizar acoes fora do procedimento autorizado.
- Uma sugestao historica nao autoriza ampliar escopo ou consultar servicos
  fora dos limites definidos para o executor.

## 10. Isolamento e prevencao de vazamento

### Modos explicitos

| Modo | Acesso permitido |
|---|---|
| Investigacao para construir A | Contexto e dados operacionais permitidos |
| Anotacao de B | A, evidencias e ground truth autorizado |
| Diagnostico de teste | Dados operacionais atuais e pares historicos elegiveis |
| Avaliacao | Diagnostico final persistido, evidencias e ground truth |

O ground truth deve ficar fora das ferramentas, arquivos e credenciais
acessiveis ao diagnosticador. Omiti-lo do prompt nao e isolamento suficiente.

### Separacao dos incidentes

- Usar 35 runs de treino para construir A/B e 35 runs independentes para teste.
- Colocar uma repeticao de cada combinacao em cada fase, conforme manifesto.
- Concluir e congelar a base antes de iniciar a fase de teste.
- Usar historico externo para desenvolvimento, sem inclui-lo automaticamente
  na base principal nem ajustar parametros pelos testes.
- Excluir o caso atual e todos os registros derivados dele da recuperacao.
- Congelar a versao da base antes do teste.
- Nao atualizar automaticamente a base com resultados do teste.

A mesma combinacao servico/falha aparece deliberadamente nos dois conjuntos.
Isso nao permite acesso a rotulos atuais nem recuperacao por identificador
de combinacao. A busca deve continuar baseada em A observado. O resultado
e de novos incidentes de cenarios conhecidos, nao de cenarios ineditos.

Verificar vazamento por nomes de arquivos, IDs, tags, resumos, embeddings,
metadados de injecao, eventos do injetor e registros do avaliador.

## 11. Avaliacao com coleta ao vivo

Coletar ao vivo nao significa consultar janelas moveis diferentes.
Fixar baseline/fault/post por incidente e registrar:

- Janela solicitada e horario de cada consulta.
- Ferramenta, argumentos e fonte.
- Resposta, cobertura, erros e truncamentos.
- Identidade dos componentes, quando disponivel.
- Versoes do executor, prompts, base e embedding.

Executar os metodos proximos no tempo e controlar ou alternar sua ordem para
reduzir efeitos de retencao e atraso de ingestao. Persistir as respostas para
auditoria nao transforma a avaliacao em replay: a fonte continua sendo a
consulta ao vivo.

Fontes podem mudar de disponibilidade entre execucoes. Essas diferencas devem
ser explicitadas; a comparacao ao vivo nao deve ser descrita como se fornecesse
evidencia necessariamente identica aos dois metodos.

### Controles recomendados

1. **Skill original intacta:** referencia do comportamento existente.
2. **Skill com aprofundamento comparavel, sem RAG:** controle para o efeito
   de executar mais consultas.
3. **Mesmo fluxo com RAG:** mede o valor incremental do conhecimento historico.

A escolha do detalhamento experimental desses controles pertence ao plano de
execucao. Comparar somente D0 com o resultado final RAG confundiria o efeito
do historico com o efeito do aprofundamento.

Aplicar os controles aos mesmos 35 IDs de teste, sem novas injecoes por metodo.
Os 70 runs sao execucoes de caos; as tentativas de RCA sao outra contagem.
Com tres condicoes em todos os testes, prever 105 tentativas de diagnostico
de teste, alem da geracao A/B no treino, sem contar reparos ou repeticoes
adicionais do modelo. Esse total e previsao do plano, nao execucao realizada.

Manter contexto operacional, vocabulario de saida e limites comparaveis.
Registrar custo adicional da recuperacao e das chamadas, nao apenas acuracia.

### Registros e metricas

Por incidente:

- A e diagnostico inicial D0.
- A historicos recuperados e scores.
- B carregados e efetivamente utilizados.
- Consultas adicionais e seus resultados.
- Diagnostico final e momento de persistencia.
- Tempo de investigacao, chamadas e tokens.
- Falhas de coleta, reparos e limitacoes.
- Ground truth acessado exclusivamente pelo avaliador apos a persistencia.
  Essa restricao refere-se ao caso de teste; no treino, o anotador de B
  acessa os rotulos de forma autorizada e registrada.

Avaliar separadamente:

- Correcao do servico de origem.
- Correcao do tipo de falha.
- Mecanismo causal sustentado pelas evidencias.
- Fundamentacao e adequacao das referencias.
- Distincao de alternativas.
- Confirmacoes indevidas e abstencoes.
- Utilidade diagnostica da recuperacao.
- Acerto conjunto de servico e tipo de falha, alem dos acertos separados.
- Recuperacao do caso de treino da mesma combinacao (recall@k), medida
  exclusivamente pelo avaliador, sem usar esse vinculo no ranking.
- Utilidade dos checks, contradicoes detectadas e adesao indevida ao rotulo
  historico quando a telemetria atual nao o sustenta.
- Custo e latencia, sem equiparar tempo de diagnostico a MTTR operacional.

Nao avaliar recuperacao apenas por compartilhar o rotulo da falha.
Reportar denominadores, falhas e indisponibilidades, evitando selecionar
somente runs favoraveis.

O denominador planejado e 35 no teste, nao 70. Reportar separadamente
cobertura operacional (concluidos/35), disponibilidade de diagnostico,
acuracia entre casos avaliaveis e resultado sobre o total planejado, sem
atribuir acerto a diagnosticos ausentes. Para comparar metodos, usar os mesmos
IDs e contar falhas de diagnostico como incorretas no denominador pareado.
Estratificar por efeito da injecao validado, falha e servico. Com somente
uma observacao de teste por combinacao, nao estimar variabilidade dessa celula;
70 runs nao equivalem a 70 observacoes independentes de teste.

## 12. Validacoes automatizadas a planejar

O plano de implementacao deve incluir testes para:

1. Geracao e validacao dos contratos A/B, IDs e proveniencia.
2. Congelamento de A antes da anotacao com ground truth.
3. Exclusao de campos privilegiados do texto de busca, tags e embeddings.
4. Recuperacao de pares sem B isolado ou guia inelegivel.
5. Exclusao do caso atual, derivados e grupos inelegiveis.
6. Respeito a top-k, limiar, versao e recuperacao vazia.
7. Isolamento de arquivos, ferramentas e entradas entre os modos.
8. Referencias de evidencia existentes e consultas executadas versus propostas.
9. Tratamento explicito de erros, fontes ausentes e evidencias contraditorias.
10. Orcamentos e reserva da sintese final.
11. Persistencia do diagnostico antes do acesso do avaliador ao ground truth.
12. Preservacao da execucao sem RAG e avaliacao com denominadores corretos.
13. Matriz com 35 combinacoes e exatamente duas execucoes por combinacao,
    uma em cada fase, sem dupla multiplicacao de replicas.
14. Separacao de fases, retomada idempotente e bloqueio do teste antes do
    congelamento da base.
15. Rotulos disponiveis apenas ao construtor de B e ao avaliador autorizados,
    sem aprovacao humana obrigatoria para publicar guias elegiveis.
16. Recall@k calculado pelo avaliador, nunca por filtro de gabarito no ranker.

Validacoes estruturais nao demonstram adequacao causal. A dispensa de revisao
humana obrigatoria deve permanecer explicita nos limites dos resultados.

## 13. Limites e proximo passo

As principais ressalvas sao:

- Elegibilidade dos 35 pares de treino e disponibilidade de suas evidencias.
- Diferenca entre intervencao conhecida e mecanismo demonstrado.
- Vies de confirmacao induzido por casos semelhantes.
- Diferencas de disponibilidade na telemetria ao vivo.
- Ganho aparente causado apenas por mais consultas ou maior orcamento.
- Guias gerados e validados automaticamente podem conter interpretacoes
  causais inadequadas apesar de referencias estruturalmente corretas.
- Treino e teste compartilham combinacoes; o protocolo nao mede generalizacao
  para cenarios nunca vistos.

O tamanho planejado e 70 runs, dividido em 35/35. Comandos finais, schemas,
thresholds e cronograma serao fechados na implementacao do plano abaixo.

A implementacao nao demonstra superioridade experimental. O plano abaixo
permanece como rastreabilidade dos ajustes; a campanha real nao foi executada.

## 14. Plano de ajustes do experimento e da avaliacao

### Etapa 1 - Fixar o protocolo e o manifesto

- Criar uma configuracao/perfil explicito para a campanha RAG 35/35,
  preservando os perfis existentes `small`, `medium` e `large`.
- Reutilizar servicos, falhas e controles de intensidade da matriz media.
- Fixar versoes, seed, ordens por fase, janelas, orcamentos, controles
  diagnosticos e parametros de recuperacao antes do teste.
- Definir o mapeamento entre rotulo de injecao, categoria e mecanismo sem
  promover o primeiro a evidencia dos demais.

**Aceite:** manifesto reproduzivel com 70 entradas, 35 por fase e uma entrada
por combinacao em cada fase; nenhum teste iniciado sem base congelada.

### Etapa 2 - Adaptar planejamento, runner e retomada

Superficies principais:
[campanha controlada](../experiment/eval/memory_hog/controlled_campaign.py),
[runner](../experiment/eval/memory_hog/runner.py) e
[contrato controlado](../experiment/platform/backend/controlled_experiment.py).

- O planejamento atual usa uma repeticao em `medium`, cinco em `large` e
  popula `test_experiments`, deixando `train_experiments` vazio.
  Introduzir o novo perfil sem alterar silenciosamente esses comportamentos.
- Gerar repeticoes explicitas com papel e identidade unicos; nao multiplicar
  novamente pelo parametro global de replicas.
- Orquestrar treino -> construcao da base -> congelamento -> teste.
- Preservar gates de mecanismo, recuperacao, cleanup e interrupcao; cada
  run exige novo warmup e baseline.
- Retomar por identidades persistidas, sem duplicar injecoes, anotacoes ou
  tentativas ja concluidas. Bloqueios nao autorizam reinjecao automatica.
- Integrar a coleta/anotacao no tempo adequado para nao perder retencao.

**Aceite:** testes do plano, separacao de fases, gates e retomada; nenhuma
mudanca nao intencional no comportamento das campanhas existentes.

### Etapa 3 - Implementar contratos e geracao de A/B

Superficies principais:
[Skill RCA](../experiment/platform/backend/rca_skill.py),
[API](../experiment/platform/backend/api.py) e
[armazenamento](../experiment/platform/backend/storage/sqlite_storage.py).

- Introduzir modos explicitos de investigacao, anotacao e diagnostico.
- Gerar A e D0 pelo modelo no ponto de corte comum a treino e teste.
- Criar contexto separado de B com rotulos de servico e causa apenas do treino.
- Implementar busca guiada de evidencias para B, com consultas validadas e
  limite de aprofundamento, antes da sintese do guia.
- Persistir rotulos experimentais separados das afirmacoes sustentadas,
  evidencias executadas, checks propostos, contradicoes e status.
- Automatizar elegibilidade sem exigir aprovacao humana; nao publicar guias
  sem sustentacao somente para completar a quantidade prevista.

**Aceite:** A nao recebe rotulos; B cita evidencias reais; casos sem suporte
ficam registrados fora da recuperacao principal; nenhum modo de teste le o
ground truth atual.

### Etapa 4 - Publicar a base e recuperar pares

Reutilizar
[retrieval](../experiment/platform/backend/analysis/retrieval.py) e
[embeddings](../experiment/platform/backend/analysis/embeddings.py).

- Indexar somente os campos observacionais permitidos de A de treino.
- Preservar ligacao A/B e hashes/versoes dos documentos e da base.
- Aplicar top-k, limiar e filtros de elegibilidade sem consultar rotulos do
  teste, `cell_id` ou vinculo conhecido entre as repeticoes.
- Distinguir recuperacao vazia de erro; registrar candidatos e scores.
- Congelar indice e documentos antes de liberar a fase de teste.

**Aceite:** somente pares elegiveis de treino sao retornados; testes nao
entram no indice; versao da base permanece identica nas 35 avaliacoes.

### Etapa 5 - Adicionar aprofundamento RAG e controle equivalente

- Preservar a Skill original como condicao de referencia.
- Adicionar ciclo de consultas adicionais com RAG desligado e ligado,
  mesmos limites e reserva para sintese final.
- Compartilhar A/D0 apenas quando os prefixos forem identicos e isso estiver
  declarado; manter estados, mensagens e resultados posteriores separados
  para que o controle sem RAG nao receba guias ou achados do ramo RAG.
- Registrar tentativas por run, metodo e versao; nao sobrescrever resultados.
- Executar diagnosticos nos 35 incidentes de teste sem novas injecoes.

**Aceite:** ausencia de acesso historico no controle; consultas adicionais
atuais no RAG; custo, latencia e erros contabilizados por condicao.

### Etapa 6 - Adaptar o avaliador e os relatorios

Superficies principais:
[avaliacao da Skill](../experiment/eval/memory_hog/evaluate_skill_rca.py) e
[helpers de avaliacao](../experiment/eval/memory_hog/rca_evaluation_common.py).

- Selecionar teste pelo papel persistido no novo manifesto, sem confundir
  runs de treino com avaliacao ou presumir que todos os runs sao de teste.
- Acessar ground truth em processo separado, depois dos resultados finais
  persistidos; avaliar cada metodo nos mesmos IDs.
- Gerar relatorios de 70 runs planejados, 35 por fase, pares A/B gerados e
  elegiveis, tentativas por metodo e perdas operacionais.
- Medir acertos de servico, falha e conjunto, sustentacao, abstencao,
  confirmacao indevida, qualidade da recuperacao, tokens e latencia.
- Comparar ganhos, perdas e diferenca liquida entre metodos nos mesmos casos,
  com denominadores e estratos de validacao da injecao.
- Separar recall@k da combinacao conhecida de utilidade causal real do guia.

**Aceite:** relatorio nao usa os 35 treinos no placar de teste, nao exclui
silenciosamente erros e nao apresenta repeticao de cenario como generalizacao
para cenario inedito.

### Etapa 7 - Validar o fluxo antes da campanha completa

- Executar os testes automatizados da secao 12 e os testes existentes dos
  componentes alterados.
- Validar ponta a ponta com fixtures e clientes simulados, incluindo falhas
  de fontes, B inconclusivo, base vazia e tentativa de vazamento.
- Conferir API, versoes carregadas, disponibilidade e retencao de Prometheus,
  Loki e Tempo, armazenamento e limites antes da execucao real.
- Se um piloto com injecoes for necessario, aprovar e contabilizar
  separadamente: nao somar runs extras aos 70 de forma implicita.

**Aceite:** fluxo e isolamento verificados, sem campanhas ou injecoes extras
nao autorizadas.

### Etapa 8 - Executar, congelar e avaliar

1. Executar os 35 runs de treino e gerar A/B junto a cada incidente.
2. Emitir relatorio de elegibilidade da base; nao corrigir quantidades com
   reinjecoes silenciosas.
3. Persistir manifesto e hash da base congelada.
4. Executar os 35 runs de teste, com as condicoes diagnosticas aprovadas
   consultando ao vivo as janelas fixadas.
5. Persistir todas as conclusoes antes de disponibilizar o ground truth ao
   avaliador; nao interromper os gates operacionais por motivos de placar.
6. Emitir comparacao pareada e relatorio de limitacoes, inclusive ausencia
   de aprovacao humana obrigatoria e compartilhamento de cenarios.
7. Documentar comandos reais de geracao da base, retomada e RCA com/sem RAG
   nos guias operacionais dos componentes modificados.

**Aceite final:** artefatos auditaveis por run, base imutavel durante o teste,
diagnosticos isolados do gabarito e resultados reportados mesmo quando RAG
nao melhora ou piora o desempenho.
