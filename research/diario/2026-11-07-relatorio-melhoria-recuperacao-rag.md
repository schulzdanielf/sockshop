# Relatorio de melhoria da recuperacao RAG

**Data:** 07/11/2026. **Escopo:** retrieval-only, sem novas inferencias RCA.

## Resumo executivo

Foi implementada a versao `opensearch-bm25-field-idf-v5`, selecionada apos
comparar variantes lexical, densa e hibridas em 32 consultas de teste. A busca
usa BM25 por campo, prioriza termos raros/tecnicos do corpus, aplica boosts
maiores a logs e traces e acrescenta codigos HTTP encontrados nos sintomas,
logs e traces sem substituir os demais termos.

Na avaliacao exploratoria de todos os 32 As, a v5 obteve **14/32 Hit@3
(43,8%)**, **MRR 0,3944**, 9/32 Hit@1 e 16/32 Hit@5. O hibrido atual aplicado
ao mesmo pool obteve 9/32 Hit@3 e MRR 0,2913. No holdout interno de 22 casos,
v5 teve 9/22 Hit@3 e MRR 0,3585; o BM25 ponderado sem codigos tambem teve
9/22, com MRR ligeiramente maior (0,3665). Portanto, a melhoria e promissora,
mas ainda exploratoria, nao evidencia generalizacao cega.

A elegibilidade operacional nao foi relaxada. Por padrao, v5 continua
limitada aos guias com `procedurally_validated` e B `passed`. Consultar os 32
documentos, inclusive os pendentes, ocorreu somente no benchmark local de
retrieval, com os rotulos usados depois do ranking para pontuar.

## Desenho e criterio

- **Consultas:** 32 respostas A ja existentes da avaliacao
  `test-base-v4-20261106T222721823606Z`.
- **Candidatos:** os 32 As do corpus congelado
  (`2b6cc672b0a8f92c81abda5a4ce28f3bd955bd2fa69b4c5a69c518d971be12f6`).
- **Relevancia:** mesmo servico e tipo de falha/caos. Essa chave foi aplicada
  somente depois de gerar o ranking; nao entrou na consulta.
- **Piloto:** 10 casos, dois por tipo de falha, com selecao balanceada por
  servico antes da comparacao. Os outros 22 formam o holdout interno.
- **Criterios:** Hit@1/3/5 e MRR. O ranking e calculado sobre o pool completo,
  sem avaliar qualidade diagnostica do B.

O corpus tem 19 guias elegiveis no momento, mas apenas 18 das 32 consultas tem
um par exato dentro desse conjunto elegivel; 31/32 tem par exato quando todos
os documentos sao considerados. Hit@k por par exato e uma medida de
recuperacao, nao de aplicabilidade causal do guia.

## Comparacao de estrategias

| Estrategia | Piloto Hit@3 / MRR | Holdout Hit@3 / MRR | 32 casos Hit@3 / MRR |
|---|---:|---:|---:|
| v4 persistida, pool elegivel, Top-3 | 3/10 / 0,1833 | 4/22 / 0,1364 | 7/32 / 0,1510 |
| Logica hibrida atual, aplicada exploratoriamente aos 32 | 2/10 / 0,2169 | 7/22 / 0,3252 | 9/32 / 0,2913 |
| BM25 limitado, sem selecao de termos | 4/10 / 0,3865 | 6/22 / 0,2954 | 10/32 / 0,3239 |
| BM25 com termos IDF e boosts por campo | 5/10 / 0,4235 | 9/22 / **0,3665** | 14/32 / 0,3843 |
| **v5: BM25 IDF, boosts e codigos preservados** | **5/10 / 0,4735** | **9/22 / 0,3585** | **14/32 / 0,3944** |

O primeiro baseline e o resultado persistido, limitado aos elegiveis e
Top-3; nao e uma comparacao com o mesmo pool. A comparacao de pool completo
mais justa para a troca e a linha da logica hibrida atual, que foi reexecutada
exploratoriamente contra os mesmos 32 candidatos.

**Motivo da escolha da v5:** contra BM25 IDF sem codigos, manteve Hit@3 no
piloto, holdout e conjunto completo; aumentou o MRR no piloto em 0,0500 e no
conjunto completo em 0,0101, e preservou codigos numericos especificos. No
holdout o MRR caiu 0,0080, embora Hit@3 permanecesse igual. Essa pequena
regressao deve ser verificada em uma coorte nova antes de qualquer afirmacao
de superioridade geral.

## Onde a recuperacao ainda falha

### Por tipo de falha

| Tipo | Hit@3 |
|---|---:|
| container-kill | 5/6 |
| HTTP status code | 5/7 |
| memory-hog | 2/7 |
| CPU hog | 1/7 |
| network latency | 1/5 |

### Por servico

| Servico | Hit@3 |
|---|---:|
| carts | 4/5 |
| payment | 3/5 |
| catalogue | 2/5 |
| shipping | 2/5 |
| user | 2/4 |
| front-end | 1/4 |
| orders | 0/4 |

Os resultados mostram que um agregado unico esconde lacunas relevantes. CPU,
rede e `orders` devem orientar os proximos testes. Para casos sem par exato,
nenhuma mudanca de ranking pode resolver a falta de cobertura; e preciso
produzir novos casos elegiveis e/ou um par do alvo ausente, sem reduzir o gate.

## Mudanca aplicada

- Nova versao `opensearch-bm25-field-idf-v5` em
  [opensearch_experiments.py](../../experiment/eval/memory_hog/opensearch_experiments.py)
  e habilitacao das opcoes em
  [rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py).
- Selecao por IDF no corpus preserva ate 24 termos informativos por campo;
  nomes de servicos/metrica e direcao sao usados nos fatos metricos.
- Boosts lexicais fixos: logs 2,5; traces 2,0; servicos 1,5; fatos metricos
  1,25; sintomas e sequencia 1,0; limitacoes 0,2.
- Codigos HTTP numericos reconhecidos em sintomas, logs e traces sao anexados
  aos termos selecionados, sem os substituir.
- A versao v5 reutiliza o mapeamento e os filtros de status v4. Nao usa
  embeddings para ranquear.
- Testes cobrem o caminho BM25, a preservacao do codigo HTTP, a versao das
  opcoes e a suite focada de OpenSearch/conhecimento passou.

## Limites e interpretacao

1. A coorte ja foi observada durante a escolha da estrategia. O piloto nao e
   cego e o holdout e apenas uma confirmacao interna; nenhum e um teste
   independente.
2. O qrel e exato por servico/falha. Ele nao pontua guia transferivel,
   caminho de verificacao, utilidade causal, contradicoes nem qualidade da
   RCA.
3. Codigos so podem ser recuperados se sobreviverem a construcao do A. A
   regex da busca nao compensa um codigo omitido pelo digest, narracao ou
   projecao; nao se adicionou neste passo um extrator independente de
   fingerprints diretamente das evidencias.
4. A avaliacao nao executou caos, nao criou As/Bs e nao mediu latencia de
   inferencia nem qualidade diagnóstica.
5. Os resultados da busca com todos os 32 candidatos sao exploratorios. Os
   documentos pendentes nao se tornam guias operacionais por terem sido
   usados para calcular Hit@k.

## Proximos passos priorizados

1. **Congelar v5 e avaliar novos casos:** repetir retrieval-only em uma coorte
   ainda nao usada, estratificada por servico/falha, com protocolo e
   estrategias registrados antes de pontuar.
2. **Resolver cobertura:** criar casos elegiveis para as combinacoes ausentes,
   sem promover Bs pendentes; registrar separadamente cobertura exata e
   cobertura de guias aplicaveis.
3. **Isolar misses:** para CPU, rede e `orders`, registrar por consulta os
   termos de A, termos apos IDF, candidatos/ranks, filtro de elegibilidade e
   guias efetivamente entregues ao modelo. Isso diferencia falha de A,
   ranking, elegibilidade e packing.
4. **Proteger fingerprints na construcao de A:** extrair codigo, excecao,
   operacao e destino de evidencias citadas, preservando a referencia e
   ocorrencia por fase; testar quando o erro ja existe no baseline e quando
   surge apenas durante a falha.
5. **Julgar aplicabilidade:** criar qrels graduados revisados sem acesso ao
   resultado da variante (guia generico, transferencia util, passo
   discriminativo), e medir nDCG/Hit@k condicionado a existir guia elegivel.
6. **So entao medir RCA:** executar novos runs com coleta observacional
   comparavel e orcamento igual, avaliando diagnostico separado do retrieval.

Referencia de planejamento e diagnostico detalhado: [Auditoria de A e da
recuperacao RAG](../planos/2026-11-07-auditoria-a-recuperacao-rag-plano-ajustes.md).

## Acompanhamento: hipoteses para CPU, rede e `orders`

Esta rodada continuou sendo **retrieval-only**: os As de teste e o corpus foram
mantidos fixos; nenhum diagnostico ou label foi usado para construir consultas.
Os labels so foram aplicados depois do ranking. Nao foi feita consulta MCP
historica nesta rodada; portanto, os resultados abaixo nao sao uma nova RCA nem
evidencia sobre a causa operacional dos casos.

### Hipoteses e testes

| Hipotese | Teste | Resultado | Decisao |
|---|---|---|---|
| IDs e duracoes variaveis ocupam o limite de termos | Remover hashes de 8+ caracteres e duracoes dos termos ja selecionados, preservando codigos | Sem mudanca: 14/32 Hit@3 e MRR 0,3944; elegiveis 8/32 e MRR 0,2637 | Sem evidencia nesta forma do teste; nao prova que a normalizacao antes do limite seja inutil |
| Fatos metricos estao subponderados | Boost `a_metric_facts` de 1,25 para 2,5 | Hit@3 permaneceu 14/32, MRR caiu para 0,3823; Hit@1 e Hit@5 tambem cairam | Rejeitar o boost global |
| Relacoes/traces estao subponderados | Boost de relacoes para 2,5 e traces para 3,0 | 11/32 Hit@3, MRR 0,3200; perdeu tres hits Top-3 existentes | Rejeitar; evidencia que aumentar pesos indiscriminadamente atrapalha casos ja recuperados |
| Consultar cada fato como conjuncao servico+metrica+direcao reduz ruido | Uma clausula AND por fato, mantendo as outras familias | 14/32 Hit@3, MRR 0,3840 | Nao justifica a complexidade nesta coorte |
| Stopwords em portugues incompletas deixam termos funcionais no topo | Acrescentar uma lista ampla de stopwords PT | 12/32 Hit@3, MRR 0,3745; HTTP caiu de 5/7 para 3/7 e carts de 4/5 para 3/5 | Rejeitar lista generica; palavras como `sem` e `mais` podem carregar sinal/negacao |
| O limite de 24 termos corta sinais uteis em campos narrativos de assinatura | Elevar apenas sintomas, logs, traces e sequencia temporal para 32; manter os outros campos em 24 | 15/32 Hit@3, 17/32 Hit@5, MRR 0,4016; holdout interno 10/22 Hit@3 e MRR 0,3676 | Melhor candidata entre as variantes com escopo limitado |

O limite de 32 em **todos** os campos nao melhorou o agregado (14/32 Hit@3,
MRR 0,3931). Isso apoia a delimitacao a sintomas, logs, traces e sequencia,
em vez de aumentar indiscriminadamente a consulta.

### Efeito da candidata `signature32`

| Pool | v5 atual (24) | `signature32` | Cobertura qrel |
|---|---:|---:|---:|
| Todos os candidatos: Hit@3 | 14/32 (14/31 cobertos) | **15/32 (15/31 cobertos)** | 31/32 |
| Todos os candidatos: MRR | 0,3944 | **0,4016** | — |
| So elegiveis: Hit@3 | 8/32 (8/18 cobertos) | **9/32 (9/18 cobertos)** | 18/32 |
| So elegiveis: MRR | 0,2637 | **0,2662** | — |
| Holdout interno: Hit@3 / MRR | 9/22 / 0,3585 | **10/22 / 0,3676** | 22/22 |

Nao houve perda de Hit@3 em nenhuma consulta nos dois pools. Houve dois ganhos
em pools diferentes: `shipping-cpu-hog-r1` passou de rank 5 a 3 no pool completo
(o guia-alvo e pendente, portanto esse ganho e apenas exploratorio), e
`orders-network-latency-r1` passou de rank 4 a 3 no pool elegivel. Por grupo,
no pool completo CPU subiu de 1/7 para 2/7 e shipping de 2/5 para 3/5; no pool
elegivel, network subiu de 1/5 para 2/5 e `orders` de 0/4 para 1/4.

O ganho de `orders` e especifico ao caso de rede; `orders` continua em 0/4 no
pool completo e nao ha ganho de Hit@3 nos casos de CPU no pool elegivel. Ha
tambem movimentos de rank para baixo em alguns casos que ja estavam fora do
Top-3. Portanto, “sem regressao” aqui significa **sem perda de hits@3**, nao
preservacao da posicao de todos os pares.

Uma variante mais ampla, limite 48 em todos os campos, chegou a 15/32 Hit@3 e
MRR 0,4175 no pool completo e MRR 0,3863 no holdout, mas teve MRR elegivel
0,2639, abaixo de 0,2662 da `signature32`, e deslocou alguns pares para ranks
piores. Ela e uma alternativa se a prioridade for MRR no pool exploratorio; a
`signature32` e a escolha mais conservadora para validar no fluxo operacional,
pois melhora tambem o pool elegivel e amplia somente as familias que carregam
assinaturas narrativas.

### Recomendacao e limites

**Recomendacao:** congelar `signature32` como candidata para uma avaliacao
prospectiva, sem substituir ainda o limite v5 de 24 em producao. A coorte foi
reutilizada e o holdout interno participou da selecao; os ganhos nao sao uma
estimativa independente nem uma garantia de generalizacao. A implementacao de
32 deve ser aceita somente se uma nova coorte, congelada antes dos rankings,
confirmar o ganho e nao mostrar perdas relevantes por servico/falha.

O teste que melhorou retrieval nao extraiu fatos novos de Prometheus, Loki ou
Tempo: apenas permitiu que mais termos ja presentes em A participassem do
BM25. A hipotese de enriquecer A com fingerprints e arestas de chamadas segue
aberta. Para testa-la com rigor, a proxima coleta deve consultar online as
janelas baseline/fault/post, cobrir todos os servicos sem usar o alvo de caos,
registrar cobertura/`no_data` e comparar A atual contra A estruturada em novos
runs. Ate essa coleta, nao atribuir a estes rankings uma explicacao causal.

## Segunda revisao: recuperacao quantitativa condicionada

### Diagnostico do gargalo

O ganho pequeno da `signature32` nao resolvia o problema principal: **A ja
contem numeros discriminativos, mas a busca lexical nao compara sua magnitude
nem preserva essa magnitude associada ao servico**. Os nomes de CPU, memoria,
latencia e servicos aparecem em muitos documentos. Aumentar seus boosts
confunde disponibilidade de vocabulario com poder de discriminacao.

Exemplos observados nos fatos de A:

| Caso | A do treino: CPU baseline -> fault | A do teste: CPU baseline -> fault |
|---|---:|---:|
| orders CPU | 17,40% -> 66,34%, delta 48,94 | 15,13% -> 61,59%, delta 46,46 |
| shipping CPU | 1,90% -> 57,62%, delta 55,72 | 4,85% -> 58,51%, delta 53,66 |

No BM25, esses fatos tendem a participar como palavras comuns: nome do
servico, CPU e aumento. Uma assinatura quantitativa consegue aproximar os
perfis sem usar o tipo de caos na consulta.

Em contrapartida, BM25 ja funciona relativamente bem em container-kill
(5/6) e HTTP (5/7), cujas narrativas carregam sinais lexicais distintivos.
Substitui-lo integralmente por similaridade metrica perde acertos de
container-kill. A melhor estrategia encontrada e **complementar**, nao uma
troca global de modalidade.

### Estrategia implementada para os testes

Implementacao experimental:
[metric_profile_retrieval.py](../../experiment/eval/memory_hog/metric_profile_retrieval.py),
com runner
[metric_profile_benchmark.py](../../experiment/eval/memory_hog/metric_profile_benchmark.py).
A v5 operacional, seus defaults e o indice existente permanecem inalterados.

1. Ajustar somente nos As do corpus a escala de cada
   `(metrica, unidade, estatistica)`: mediana dos deltas absolutos nao nulos.
2. Representar cada fato pela chave
   `(servico, metrica, unidade, estatistica)`, com delta positivo dividido pela
   escala. Normalizar o vetor e calcular similaridade cosseno entre As.
3. Ativar essa rota quando o maior delta positivo normalizado da consulta
   for **>=10**; abaixo disso, preservar exatamente o ranking BM25 v5.
4. Quando ativa, fundir rankings por
   `0,25 / (10 + rank_BM25) + 0,75 / (10 + rank_numerico)`.
5. Aplicar elegibilidade e exclusao do run de origem **antes** da fusao.
   No pool elegivel, as escalas continuam ajustadas nos As do corpus completo;
   nenhum guia pendente pode entrar no ranking filtrado.

O limiar e relativo a variabilidade observada no corpus, nao uma regra
especifica de CPU ou uma lista de cenarios. Nao utiliza labels ou B para
construir features. Contudo, limiar, peso, perfil e constante RRF foram
escolhidos apos observar esta coorte: **a selecao e pos-hoc**.

### Comparacao de estrategias no corpus completo

Hit@3 condicionado aos **31 casos cobertos**, com MRR calculado sobre as
**32 consultas**. Todos os 32 documentos sao candidatos exploratorios.

| Estrategia | Hit@3 coberto | MRR | Ganhos / perdas de Hit@3 vs v5 |
|---|---:|---:|---:|
| BM25 v5 | 14/31 = 45,2% | 0,3944 | referencia |
| `signature32`, rodada anterior | 15/31 = 48,4% | 0,4016 | 1 / 0 |
| Delta assinado, somente numerico | 23/31 = 74,2% | 0,7200 | 14 / 5 |
| Delta positivo, somente numerico | 26/31 = 83,9% | 0,7261 | 15 / 3 |
| Positivo + RRF 75%, sem condicao | 27/31 = 87,1% | 0,7000 | 15 / 2 |
| **Positivo + RRF 75%, condicionado >=10** | **27/31 = 87,1%** | **0,7444** | **13 / 0** |

A variante condicionada nao apenas preservou os 14 hits anteriores: **nenhum
par relevante ficou em rank pior** no corpus completo ou no pool elegivel.
Esse resultado e observado nesta coorte, nao uma garantia para novos runs.
Hit@1 passou de 9/32 para 20/32; Hit@5, de 16/32 para 28/32.
A meta de 60% exigiria 19/31; a candidata encontrou 27/31.

No canal condicionado, limiares **8, 10 e 12** produziram os mesmos hits,
MRR e ausencia de regressao nos dois pools. No corpus completo, limiar 15
produziu 26/31 e limiar 20 produziu 24/31, ambos sem perder hits anteriores.
Isso reduz a dependencia de um unico limiar exato, mas nao elimina overfitting.

Tambem foi removida, uma por vez, cada familia de fatos **somente da consulta
numerica**, mantendo corpus, escalas e BM25 inalterados:

| Canal numerico | Hit@3 coberto, todos | Hit@3 coberto, elegiveis |
|---|---:|---:|
| Completo | 27/31 | 17/18 |
| Sem CPU | 21/31 | 12/18 |
| Sem erros | 25/31 | 17/18 |
| Sem latencia p95 | 25/31 | 15/18 |
| Sem memoria | 25/31 | 16/18 |
| Sem request rate | 27/31 | 17/18 |

A queda ao remover CPU corrobora o mecanismo proposto para os ganhos desse
grupo. Latencia, memoria e erros tambem contribuem; nao e apenas um detector
de CPU. Request rate nao alterou os resultados nesta amostra, mas isso nao
justifica remove-lo para futuras falhas com queda de throughput.
Sem erros houve uma perda de Hit@3 anterior no pool completo: a preservacao
observada depende da assinatura completa, nao de qualquer subconjunto.

### Elegibilidade e denominadores

| Pool | Cobertura exata | v5: Hit@3 coberto | Candidata: Hit@3 coberto | Candidata: ponta a ponta |
|---|---:|---:|---:|---:|
| Todos os 32 documentos | 31/32 | 14/31 = 45,2% | **27/31 = 87,1%** | **27/32 = 84,4%** |
| 19 documentos elegiveis procedurais | 18/32 | 8/18 = 44,4% | **17/18 = 94,4%** | **17/32 = 53,1%** |

O pool elegivel tem nove ganhos de Hit@3 e nenhuma perda. Seu Hit@1 passou de
7/32 para 16/32; MRR ponta a ponta, de 0,2637 para 0,5175.
Logo, **94,4% nao significa que o sistema operacional cobre 94,4% dos casos**:
faltam correspondentes elegiveis para 14 consultas. O ranking deixou de ser
o maior limitante nesta amostra; cobertura passa a ser o gargalo dominante.
Guias pendentes usados exploratoriamente nao foram promovidos.

### Decomposicao por falha e servico

No corpus completo, denominadores abaixo consideram consultas com
correspondente existente:

| Falha | v5 | Candidata |
|---|---:|---:|
| container-kill | 5/6 | **5/6** |
| CPU | 1/7 | **7/7** |
| HTTP | 5/7 | **6/7** |
| Memoria | 2/6 | **6/6** |
| Rede | 1/5 | **3/5** |

Por servico: carts 4/5 -> 5/5; catalogue 2/5 -> 5/5; front-end
1/3 -> 2/3 entre os cobertos; orders **0/4 -> 4/4**; payment 3/5 -> 4/5;
shipping 2/5 -> 3/5; user 2/4 -> 4/4.
Front-end tem quatro consultas, mas somente tres correspondentes no corpus.

Restam quatro misses entre os 31 cobertos:

- `shipping-network-latency-r1`: rank 28, sinal normalizado 4,86.
- `payment-network-latency-r1`: rank 13, sinal 4,29.
- `front-end-http-status-code-r1`: rank 4, sinal 1,52.
- `shipping-container-kill-r1`: rank 8, sinal 0,65.

Neles, o canal numerico nao e ativado e o ranking anterior e preservado.
`front-end-memory-hog-r1` continua sem correspondente: nao e um erro do ranking.
No pool elegivel, o unico miss coberto e shipping/rede, em rank 17.
Nao reduzir o limiar apenas para esses misses: isso tambem ativa o canal em
casos de container-kill que os testes sem condicao mostraram ser vulneraveis.

### Rigor e limites da evidencia

- Os rankings foram produzidos antes de consultar labels de relevancia;
  features e escalas usam somente A, nao B, evidencias supervisionadas ou
  tipo/servico injetado. O match de avaliacao permanece servico+falha exatos.
- A auditoria encontrou **zero runs compartilhados**, **zero sobreposicoes
  entre janelas metricas de treino/teste** e **zero As identicos** entre splits.
  Isso nao elimina dependencia da mesma campanha, configuracao ou carga.
- Foram inspecionados 640 fatos de treino e 640 de teste: todos com status
  `ok`, unidade consistente por metrica e delta compativel com fault-baseline.
  Algumas janelas fault tem somente oito amostras; status `ok` nao prova
  estabilidade estatistica nem fontes independentes.
- Ausencia de fato e preenchida com zero apenas para similaridade.
  O top-4 por metrica pode omitir sintomas relevantes; nao interpretar esse
  zero como ausencia operacional. A rota positiva tambem nao representa bem
  falhas cujo discriminante seja uma queda.
- Normalizacao e unidades sao verificadas; a medida nao considera incerteza,
  contagem de amostras como peso ou cobertura completa por servico.
- Esta coorte participou de todas as rodadas de selecao. O antigo holdout
  interno **nao e mais cego**. Nem 87,1% nem 94,4% sao estimativas independentes.
- Nao houve coleta historica MCP ou nova inferencia RCA. O ganho vem de fatos
  ja presentes em A, nao de descoberta causal nova ou aumento de telemetria.
- O benchmark usa ranking completo e `min_score=0`, nao o fluxo de packing
  operacional. Melhor retrieval nao implica guia entregue, guia aplicavel
  ou diagnostico correto.

### Decisao e proximo teste

**Nova recomendacao:** priorizar `metric-profile-gated-rrf-v1` para validacao
prospectiva, em vez de `signature32` isoladamente. A candidata supera 60%,
preserva os acertos existentes e tem fundamento observacional explicito.
O runner aplica a estrategia a toda a base nos dois pools e persiste o
relatorio JSON com ranks, escalas, hashes, auditoria e testes por canal metrico.
Nao houve substituicao silenciosa da v5 operacional.

Validacao da implementacao: **103 testes focados passaram**, incluindo as
regressoes de OpenSearch e conhecimento RCA. Sintaxe/diagnosticos dos tres
arquivos novos, compilacao Python e `git diff --check` passaram. O benchmark
foi repetido com o runner persistente e reproduziu os dois pools.

Antes de promover:

1. **Congelar este algoritmo e os parametros**, qrels, corpus e criterios de
   aceitacao antes de novas consultas. Nao ajustar com os quatro misses.
2. Executar nova coorte, variando **carga, intensidade e duracao**, incluindo
   sintomas semelhantes com causas diferentes e incidentes sem guia adequado.
   Separar resultados por servico/falha e testar transferencia entre campanhas.
3. Aceitar somente com Hit@3 coberto >=60% na coorte nova, limite de regressao
   previamente acordado, cobertura reportada e analise de incerteza amostral.
   Medir abstencao/no-match para nao forcar guia em incidentes desconhecidos.
4. Para rede fraca, testar assinatura de arestas **origem, destino, operacao,
   latencia/timeout e fase** em coleta uniforme de todos os servicos, sem
   consultas orientadas pelo alvo injetado. Confrontar excesso vs baseline.
5. Para HTTP e kill restantes, testar fingerprints de **codigo/excecao e
   transicao de ciclo de vida**, sem transformar restart em causa.
6. Resolver a cobertura elegivel sem relaxar validacao de B e auditar packing.
   So depois medir ganho de RCA com nova coleta historica MCP obrigatoria.

## Integracao para novas avaliacoes

A candidata quantitativa foi integrada como
`opensearch-bm25-metric-gated-rrf-v6`. Novas preparacoes de avaliacao da base de
teste e novas campanhas test-only sobre o corpus procedural Qwen 3.5
selecionam v6 explicitamente; configuracoes antigas nao sao reescritas.
V4 e v5 permanecem disponiveis para comparacao.

**A, B, a base congelada e o indice OpenSearch nao foram alterados.** V6
reutiliza schema 4 e `incident-a-v2`. A elegibilidade procedural e a exclusao
do run de origem sao aplicadas nas duas rotas antes da fusao. Inclusao de
pendentes continua restrita ao opt-in de avaliacao externa experimental.

Os scores RRF sao normalizados pelo maximo teorico e, abaixo do limiar,
os scores e a ordem BM25 v5 sao preservados. Novas configuracoes v6 utilizam
`min_score=0`, para reproduzir o protocolo exploratorio; isso nao estabelece
um limiar validado de abstencao para incidentes desconhecidos.
Ativacao, intensidade, parametros e ranks das rotas ficam na resposta da API.

O benchmark passou a executar tambem a implementacao integrada e exigir
igualdade de **todos os rankings** com a referencia, antes de pontuar labels.
Reproduziu 27/31 Hit@3 no pool completo e 17/18 no elegivel, com os mesmos
MRRs e sem perdas. Esta verificacao foi retrieval-only sobre As persistidos:
nao houve nova inferencia RCA nem coleta MCP.

Validacao da integracao: 138 testes focados passaram, incluindo API, opcoes,
preparacao de avaliacoes, isolamento de rotulos e ranking v6. Diagnosticos do
editor, compilacao e verificacao de whitespace passaram. A API foi reiniciada
com autorizacao e sua capability passou a anunciar v6. Nenhuma avaliacao nova
foi iniciada.

A suite ampliada de orquestracao tambem revelou sete falhas em mocks de
validacao historica de `test_rag_campaign.py`, fora da integracao v6. Essas
falhas nao foram ocultadas nem corrigidas relaxando os gates de coleta online.
