# Auditoria de A e da recuperacao RAG: diagnostico e plano de ajustes

**Data:** 07/11/2026. **Estado:** auditoria realizada; primeira melhoria de retrieval implementada como v5 e avaliada em modo exploratorio. Ajustes de A, entrega e avaliacao cega seguem pendentes; ver [relatorio da v5](../diario/2026-11-07-relatorio-melhoria-recuperacao-rag.md).

**Objeto:** corpus `rag-rebuild-qwen35-procedural-metrics-20261106` e avaliacao
`test-base-v4-20261106T222721823606Z`, com 32 As de treino e 32 As do braco RAG de teste.

## 1. Parecer executivo

A intuicao de buscar um codigo, excecao ou sintoma caracteristico e adequada.
O fluxo atual possui parte dessa informacao, mas nao a trata como uma assinatura
diagnostica protegida durante a construcao de A e da consulta.

Os principais achados sao:

1. **Perda comprovada na consulta metrica:** todos os 64 As possuem 20 fatos
   metricos; em todos, o campo lexical limitado perde os nomes das metricas e
   os servicos dos fatos. Um exemplo real resulta em
   `metric decrease unchanged increase`.
2. **Ausencia de extracao obrigatoria de fingerprints em A:** codigos,
   excecoes e destinos podem aparecer no texto narrativo, mas dependem da
   selecao e parafrase do modelo. O contrato nao exige preservacao literal.
3. **Gargalos diferentes por mecanismo:** HTTP nao tem guia elegivel; CPU e
   rede possuem cobertura, mas apresentam falhas importantes de ranking.
4. **Recuperar nao significa entregar:** 7 casos possuem par exato no Top-3,
   mas apenas 6 o recebem no contexto. Em 31/32 casos algum guia e descartado
   pelo orcamento de 24 mil caracteres.
5. **Metadados de auditoria incorretos:** `omitted_pair_ids` inclui pares
   fornecidos em todos os 32 casos. A politica e registrada como
   `effect_validated_only`, embora o filtro v4 seja procedural.
6. **Ablações nao justificam uma troca imediata:** consulta lexical completa
   melhora o hibrido de 7 para 8 hits, mas nao aumenta a entrega simulada de
   pares exatos. Os ganhos e perdas precisam ser analisados individualmente.

**Direcao recomendada:** tornar a recuperacao observavel; corrigir selecao de
termos e entrega; acrescentar fingerprints verificados e caracterizacao
metrica; testar rotas de busca isoladamente; expandir cobertura onde ela falta.
Nao comecar por mais agentes, contexto maior ou aumento indiscriminado de top-k.

## 2. Escopo, fontes e limites

Foram usados:

- [Relatorio da avaliacao](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z/test_base_rag_evaluation_report.md).
- [Corpus A/B](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z/rag_base.json).
- [Respostas RAG por caso](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z/rag_results/).
- [Construcao, contrato e projecao de A](../../experiment/platform/backend/rca_knowledge.py).
- [Indice, limitacao lexical e fusao](../../experiment/eval/memory_hog/opensearch_experiments.py).
- [Normalizacao de logs](../../experiment/platform/backend/analysis/log_patterns.py)
  e [digests de evidencia](../../experiment/platform/backend/agent_console.py).
- [Reconstrucao de B](../../experiment/eval/memory_hog/training_b_rebuild.py).

A auditoria consultou somente o indice OpenSearch local, sem modifica-lo. Os
32 rankings Top-3 foram reproduzidos exatamente; hashes do corpus, IDs do indice
e os 32 embeddings de treino foram verificados. O codigo atual esta em um
worktree com modificacoes preexistentes; nao foi tratado como um commit
imutavel da execucao original. Os hashes dos arquivos usados estao no resultado
da auditoria.

Nao houve carga, caos, geracao de novos As/Bs ou inferencias RCA. As ablações
medem recuperacao dos As ja persistidos e entrega simulada, nao acuracia
diagnostica nova. Nova avaliacao RCA requer nova coleta MCP historica de
baseline/fault/post, sem substituir essa coleta pelos artefatos desta auditoria.

A coorte ja foi usada no desenvolvimento. Os resultados sao exploratorios,
nao uma estimativa cega de generalizacao. Relevancia exata significa mesmo
servico/falha; nao equivale a utilidade causal de um guia.

## 3. Como A e construido hoje

### 3.1 Coleta inicial

Em `rca_knowledge.investigate`, o fluxo executa primeiro `answer_skill_rca`,
sem checks adicionais da etapa de conhecimento. A Skill compara sinais
metricos e ciclo de vida, seleciona candidatos e consulta logs dos servicos
selecionados/reiniciados. Tambem busca traces e aprofunda amostras.

Essa triagem condiciona quais erros especificos ficam disponiveis. Um servico
fora dos candidatos pode ter seu sintoma caracteristico ausente dos logs
consultados, mesmo que apareca em metricas.

O digest de logs preserva ate quatro grupos de assinatura e tres linhas de
amostra por fase. A normalizacao separa classes amplas como `TIMEOUT`,
`CONNECTION_FAILURE` e `ERROR_OR_FAILURE`; o texto do padrao e exemplos
preservam detalhes adicionais, com limites de tamanho.

**Prior art util:** `log_pattern` ja remove timestamps e IDs volateis sem
conflar codigos de erro ou destinos. Uma sondagem confirmou a preservacao de
`MongoSocketReadException`, `E714` e `carts-db`, com remocao do request ID.
Reutilizar esse helper, em vez de criar outro normalizador.

### 3.2 Extracao narrativa pelo modelo

Uma chamada `build_a` recebe evidencias, janelas, catalogo de servicos e IDs por
familia. O prompt pede sinais observados, deduplicacao, separacao entre
alteracoes locais e propagacao e ausencia de inferencia temporal indevida.

O contrato contem:

- `observed_services` e `service_relations`;
- `symptoms`, `metric_changes`, `log_patterns`, `trace_patterns`;
- `temporal_sequence`, `preliminary_hypotheses` e `limitations`.

As observacoes possuem `description` e `evidence_ids`. Nao ha campos obrigatorios
para codigo exato, classe de excecao, operacao, destino, fingerprint normalizado
ou comparacao de ocorrencia baseline/fault/post.

`validate_a` verifica servicos conhecidos, referencias utilizaveis e familia
de evidencia. Isso e importante, mas **nao verifica se a descricao e
semanticamente sustentada pela evidencia citada**. Nos grupos especializados,
a verificacao aceita pelo menos uma referencia da familia esperada, nao
necessariamente todas. Um ID valido nao impede uma parafrase especulativa.

### 3.3 Enriquecimento deterministico

`extract_metric_facts` extrai dados de `get_run_golden_signals`: servico,
metrica, unidade, medias baseline/fault, delta, janelas, amostras e caminho de
origem. Isso protege os numeros contra invencao pelo modelo.

Neste corpus, cada A tem exatamente 20 fatos referentes a cinco metricas:
CPU, memoria, taxa de erro, latencia P95 e taxa de requisicoes.

O enriquecimento nao transforma automaticamente as outras evidencias em fatos
estruturados para busca: ciclo de vida, latencia por aresta, motivo de
terminacao e codigos permanecem dependentes da narrativa/digests.

### 3.4 Serializacao e assimetria

`retrieval_text` normaliza texto e prioriza fatos metricos. Hipoteses
preliminares, rotulos e IDs do run nao entram como campos de busca.

`retrieval_fields` cria campos separados para servicos, sintomas, relacoes,
metricas, logs, traces, sequencia e limitacoes. **Limitacoes entram na busca
lexical, mas nao no texto vetorial.**

Descricoes de `metric_changes` sao removidas quando seus IDs ja estao cobertos
pelos fatos metricos. A decisao e por ID da evidencia, nao por equivalencia
entre cada afirmacao narrativa e o fato correspondente.

A reconstrucao recente modifica B e preserva A e o indice vetorial de origem.
Portanto, melhorias supervisionadas de B nao enriquecem automaticamente A
nem acrescentam suas descobertas a chave de recuperacao.

## 4. O que aparece nos As reais

| Indicador | Treino, n=32 | Teste RAG, n=32 |
|---|---:|---:|
| Fatos metricos por A | 20 em todos | 20 em todos |
| Fracao media do texto ocupada por fatos metricos | 65,0% | 64,2% |
| `log_patterns` vazio | 10 | 11 |
| `trace_patterns` vazio | 3 | 1 |
| Consulta metrica sem nomes de metricas | 32 | 32 |
| Consulta metrica sem servicos dos fatos | 32 | 32 |
| Mencao a dados ausentes fora de `limitations` | 4 | 1 |

Os 32 casos de teste possuem alguma evidencia inicial de logs utilizavel.
Isso **nao significa que todos contenham erro discriminativo**. Em 16 casos
ha grupos de assinatura no digest; um deles, `carts-http-status-code-r1`, tem
`log_patterns` vazio em A. Esse e um caso concreto para auditar perda na
extracao, sem assumir que a assinatura identifica a falha injetada.

Em cinco As de teste o alvo experimental nao aparece em `observed_services`:
`shipping-cpu-hog-r1`, `payment-memory-hog-r1`, `catalogue-cpu-hog-r1`,
`shipping-network-latency-r1` e `shipping-http-status-code-r1`.
Isso nao autoriza preencher o alvo pelo gabarito. Exige verificar se havia
evidencia positiva dele e por que ela nao foi selecionada. Fatos metricos
podem preservar outros servicos fora dessa lista narrativa.

### Exemplos que explicam a preocupacao com palavras especificas

- A de treino `user/container-kill` registra `IllegalStateException` e
  `Connection refused`, mas tambem coloca ausencia de dados de rede no grupo
  de sintomas. Essa ausencia nao deveria aumentar similaridade de mecanismo.
- A de treino `carts/network-latency` associa conexoes de banco sem erros a
  possivel sobrecarga de I/O. A expressao mistura observacao e interpretacao.
- Em `carts-cpu-hog-r1`, a evidencia inicial inclui
  `MongoSocketReadException: Prematurely reached end of stream` no baseline.
  Ser especifico nao torna o erro novo nem prova que explica o caos atual.
- `UNKNOWN_ERROR` aparece nas descricoes de sintomas/logs/traces de 18 As
  de treino e 13 de teste. `Payment declined` aparece em 7 e 3,
  respectivamente. Essas contagens sao mencoes textuais por documento,
  nao taxas de evento ou verificacao de causalidade.

**Conclusao:** especificidade lexical precisa ser combinada com novidade
temporal, entidade e utilidade discriminativa. UUID, timestamp e request ID
sao unicos da ocorrencia, mas inadequados como chave de generalizacao.

## 5. Recuperacao atual e suas limitacoes

### 5.1 Campos lexicais truncados

`_bounded_field_queries` divide 24 termos entre os valores de cada campo.
Com 20 fatos metricos, o limite por valor e dois termos. O algoritmo preserva
inicio/fim antes de deduplicar.

Nos fatos, o primeiro token e `metric` e o ultimo e a direcao do sinal.
Os nomes da metrica e do servico, localizados no meio, desaparecem.
O orcamento e consumido antes da deduplicacao e nao e redistribuido depois.

Uma sondagem sintetica tambem confirmou que `ERROR_XYZ_714`, colocado no meio
de uma descricao longa, desaparece da consulta lexical.

**Nao basta aumentar o limite:** o problema e selecionar termos por posicao,
sem proteger atomos diagnosticos. O caso real dos 64 As deve virar teste de
regressao, nao apenas o teste existente de comprimento maximo.

### 5.2 Vetores, campos e fusao

O provider e `hashing-v1`, com 256 dimensoes, tokens e bigramas. Nao e um
encoder semantico treinado. Textos com muitos fatos compartilhados, numeros e
boilerplate podem ter similaridade pouco discriminativa.

O indice possui campos de texto padrao e KNN com similaridade cosseno.
As consultas por campo usam `match` sem boosts explicitos e sem exigir que
servico/metrica/direcao estejam vinculados como uma unidade.

A fusao RRF atribui o mesmo peso aos rankings vetorial e BM25, com constante
60. Assim, uma posicao lexical excelente pode ser diluida por uma posicao
vetorial ruim:

- `carts-memory-hog-r1`: par exato em rank **1 no BM25**, **15 no vetor**,
  **6 no hibrido**.
- `catalogue-network-latency-r1`: rank **3 no vetor**, **17 no BM25**,
  **11 no hibrido**.

Nao ha um braco universalmente superior. Sao casos para comparar sinais de
cada rota e calibrar fusao/reranking, nao escolher pesos pelo gabarito atual.

### 5.3 O piso RRF nao mede relevancia

Com `min_score=0.05`, todos os 19 pares elegiveis passam nas 32 consultas.
O menor score observado no ranking completo foi 0,772152.

RRF mede posicao relativa, nao compatibilidade absoluta. Com rankings curtos,
ate candidatos ruins podem obter scores normalizados altos. Nao interpretar
esse piso como probabilidade, confianca ou garantia de pertinencia.

### 5.4 Entrega e metadados

O packing inclui guias inteiros em ordem de ranking enquanto cabem no
orcamento. Houve:

- 30 casos com um guia descartado;
- um caso com dois descartados;
- um caso sem descarte.

O par exato de `payment-cpu-hog-r1`, rank 3, foi recuperado, mas nao fornecido:
os tres guias totalizavam 25.966 caracteres; os dois carregados, 15.202.
Logo, o unico hit de CPU nao chegou ao contexto do modelo.

`omitted_pair_ids` e calculado comparando wrappers de recuperacao com objetos
de historico compactado. Como sao estruturas diferentes, lista todos os
recuperados, inclusive os fornecidos. A auditoria derivou omissoes por
diferenca de IDs e verificou os contadores para nao propagar esse erro.

`eligibility_policy=effect_validated_only` tambem nao descreve o filtro
procedural v4. Corrigir a instrumentacao antes de criar dashboards a partir
desses campos.

## 6. Decomposicao por falha e servico

### 6.1 Falha verdadeira, usada somente na auditoria

| Falha | Testes | Com par elegivel | Hit@3 | Falha de ranking entre cobertos | Par exato entregue |
|---|---:|---:|---:|---:|---:|
| container-kill | 6 | 4 | 4 | 0 | 4 |
| cpu-hog | 7 | 6 | 1 | 5 | 0 |
| http-status-code | 7 | 0 | 0 | 0 | 0 |
| memory-hog | 7 | 4 | 2 | 2 | 2 |
| network-latency | 5 | 4 | 0 | 4 | 0 |
| **Total** | **32** | **18** | **7** | **11** | **6** |

- **HTTP:** prioridade de cobertura/validacao do corpus, nao tuning de ranking.
  Os sete guias de treino HTTP estao pendentes; os motivos incluem falta de
  confirmacao metrica da falha no alvo e, em alguns, problemas procedurais.
- **CPU:** prioridade de representacao, ranking e entrega; seis guias de treino
  sao elegiveis, mas cinco testes cobertos falham no ranking.
- **Rede:** prioridade de relacoes chamador/destino e caracterizacao temporal
  da latencia; existe cobertura em quatro testes, todos perdidos pelo ranking.
- **Memoria:** problema misto de cobertura e ranking.
- **Container-kill:** recuperacao exata funciona em todos os testes cobertos;
  preservar esse comportamento nas ablações.

### 6.2 Servico verdadeiro, usado somente na auditoria

| Servico | Testes | Com par elegivel | Hit@3 | Falha de ranking entre cobertos | Par exato entregue |
|---|---:|---:|---:|---:|---:|
| carts | 5 | 4 | 1 | 3 | 1 |
| catalogue | 5 | 4 | 2 | 2 | 2 |
| front-end | 4 | 1 | 0 | 1 | 0 |
| orders | 4 | 2 | 0 | 2 | 0 |
| payment | 5 | 2 | 2 | 0 | 1 |
| shipping | 5 | 2 | 1 | 1 | 1 |
| user | 4 | 3 | 1 | 2 | 1 |

Nao classificar `payment` como problema de ranking: acerta os dois casos
cobertos, mas tem tres lacunas de cobertura e uma perda de entrega.
`front-end` tem predominantemente falta de cobertura; `carts`, `orders` e
`user` exigem investigar representacao/ranking.

### 6.3 Matriz operacional

`H`: hit exato; `M`: par elegivel existe, mas ficou fora do Top-3;
`C`: sem cobertura exata elegivel; `-`: celula ausente da coorte.

| Servico | container-kill | cpu-hog | http-status-code | memory-hog | network-latency |
|---|---|---|---|---|---|
| carts | H | M | C | M | M |
| catalogue | H | M | C | H | M |
| front-end | C | M | C | C | - |
| orders | - | M | C | C | M |
| payment | H | H | C | C | C |
| shipping | C | C | C | H | M |
| user | H | M | C | M | - |

Cada celula tem uma observacao de teste. Esta matriz localiza problemas,
mas nao permite estimar robustez de cada servico/falha sem replicas.

## 7. Testes e ablações executados

### 7.1 Contratos existentes

Comando:

```bash
./.venv/bin/python -m pytest \
  experiment/platform/tests/test_rca_knowledge.py \
  experiment/eval/memory_hog/tests/test_opensearch_experiments.py \
  experiment/eval/memory_hog/tests/test_test_base_rag_evaluation.py \
  -k 'a_v2 or incident_a or lexical or hybrid or search or retrieval or relevance or report or self_retrieval'
```

**Resultado: 41 testes passaram; 44 foram desselecionados.**
Eles validam contratos, referencias, enriquecimento e limites. Nao demonstram
eficacia da recuperacao nem cobrem a preservacao de termos metricos com 20
fatos reais. O teste de discriminacao local existente exercita o ranker local,
nao a limitacao lexical do OpenSearch.

### 7.2 Rankings consultados online no indice local

Corpus, elegibilidade, As, vetores e top-k=3 permaneceram fixos. Os bracos
lexicais completos consultam o mesmo indice, retirando somente o corte de
termos; a fusao usa o mesmo RRF. A entrega foi simulada com os guias e
orcamento originais, e reproduziu as listas de fornecidos do baseline em
32/32 casos.

| Variante | Hits exatos no Top-3 | Hits exatos na entrega simulada |
|---|---:|---:|
| Baseline persistido/reproduzido | 7 | 6 |
| Vetor hashing somente | 4 | 3 |
| BM25 limitado somente | 7 | 6 |
| BM25 completo somente | 7 | 7 |
| Hibrido com campos lexicais completos | 8 | 6 |
| Hibrido completo sem campos de servicos/limitacoes | 7 | 5 |

O hibrido completo ganha `carts-cpu-hog-r1` e
`catalogue-network-latency-r1`, mas perde `payment-cpu-hog-r1`.
O ganho liquido no ranking nao se traduz em ganho de entrega.

BM25 limitado/completo recupera `carts-memory-hog-r1`, mas perde
`catalogue-memory-hog-r1`. As variantes empatadas no agregado nao resolvem
necessariamente os mesmos casos.

**Nao promover nenhuma variante com base nesses totais.** BM25 completo merece
prosseguir como ablação, mas nao houve nova inferencia para medir seu efeito
diagnostico. Retirar servicos e limitacoes ao mesmo tempo nao isola o papel de
cada campo; separar essas mudancas no proximo experimento.

## 8. Plano de ajustes em etapas

### P0. Instrumentar e corrigir a medicao

**Superficies:** `rca_knowledge.py`, `test_base_rag_evaluation.py`,
seus testes e gerador do relatorio.

- Corrigir `omitted_pair_ids` por identidade, preservando artefatos antigos.
- Registrar politica procedural real, estados dos candidatos e contadores.
- Persistir consulta completa e limitada por campo, termos descartados,
  tamanhos, hashes de A e do texto, normalizadores e versao do retriever.
- Persistir todos os ranks necessarios ao benchmark: lexical, vetorial,
  fusionado, motivo de exclusao e score bruto por rota quando disponivel.
- Medir cobertura -> candidatos -> Top-3 -> contexto -> checks usados.

**Aceite:** IDs fornecidos e omitidos disjuntos; uniao igual ao conjunto
recuperado; contadores consistentes; 32/32 casos reconciliados; nenhum guia
fora da elegibilidade configurada.

### P1. Corrigir a consulta sem reconstruir A inicialmente

**Superficies:** `IncidentA.retrieval_fields`, `_bounded_field_queries`,
testes de OpenSearch e benchmark somente leitura.

- Substituir selecao inicio/fim por selecao de atomos diagnosticos.
- Remover boilerplate da query e deduplicar antes de alocar o orcamento.
- Preservar servico, metrica e direcao dos fatos selecionados.
- Limitar quantidade de fatos por relevancia observada e diversidade de
  mecanismo/unidade; nao misturar magnitudes heterogeneas como se fossem iguais.
- Separar termos de limitacao de termos positivos de mecanismo.
- Avaliar query por campo com boosts e vinculo de entidades, sem usar rotulos.

**Aceite de contrato:** nenhum fato selecionado perde sua identidade
servico/metrica; codigo/excecao selecionados sobrevivem independentemente da
posicao no texto; limites seguem explicitos; o caso dos 20 fatos fica coberto.

**Aceite experimental:** comparar ganhos/perdas entre os 18 casos cobertos e
entrega, mantendo `container-kill` como guarda de regressao. Nao escolher a
variante apenas por um total maior na coorte de desenvolvimento.

### P1. Entregar guias uteis dentro do orcamento

- Criar projecao curta por guia: condicoes de aplicacao, sinais discriminantes,
  consultas essenciais e alternativas, com links/proveniencia para detalhes.
- Reservar espaco para ate tres guias e permitir aprofundamento seletivo,
  em vez de descartar inteiramente o terceiro pelo tamanho.
- Fazer selecao/packing consciente de tamanho e utilidade observada, nunca
  do rotulo do teste.
- Verificar que a projecao nao remove os checks que tornam o guia aplicavel.

**Aceite:** o par recuperado de `payment-cpu-hog-r1` nao pode desaparecer
silenciosamente; todas as omissoes devem ter motivo e medida de perda.
Se top-k guias elegiveis nao couberem, a limitacao deve ser explicita.

### P2. Evoluir A para fingerprints estruturados e verificaveis

Criar contrato versionado, mantendo A original imutavel e migracao explicita
de texto, embeddings e indice. Nao alterar apenas o A do teste ou usar B para
preencher a consulta com a conclusao historica.

Proposta de `signature_facts`, extraidos deterministicamente quando possivel:

- fonte, evidence ID e caminho de origem;
- servico/container, operacao/endpoint e chamador/destino;
- classe de excecao, codigo e mensagem normalizada;
- fase, timestamps observados e contagens por fase;
- `count_scope`, cobertura, truncamento e normalizador usado.

A comparacao temporal deve diferenciar:

- assinatura nova no fault;
- assinatura preexistente, mas intensificada;
- assinatura de baseline sem agravamento;
- comparacao desconhecida por falta de cobertura.

Contagens de amostra nao podem virar taxas populacionais. Nao inventar
`new_in_fault=true` quando a busca baseline foi truncada ou incompleta.
Raridade deve ser estimada no corpus de treino, nao ajustada pelos rotulos
do teste.

Manter uma camada narrativa para leitura humana e uma camada curta,
estruturada para recuperacao. O modelo pode interpretar fingerprints, mas
nao deve inventar, substituir ou omitir os atomos selecionados sem registro.

### P2. Caracterizar falhas sem depender de erro textual

CPU, memoria e rede podem nao produzir excecao unica. Acrescentar fatos
discriminativos de:

- saturacao absoluta, delta, duracao e throttling, quando medidos;
- crescimento de memoria e ciclo de vida/OOM, quando sustentados;
- latencia por aresta, client/server latency e operacao;
- aumento de taxa de erros por codigo, servico e endpoint.

Descrever medicao e compatibilidade, nao converter automaticamente um delta
em rotulo `cpu-hog` ou `memory-hog`. Valores proximos de zero precisam de
tratamento explicito para nao gerar rankings por percentuais explosivos.

### P2. Comparar rotas de recuperacao

Testar separadamente:

1. BM25 por campos, com consulta atomica protegida.
2. Busca exata/frase por fingerprints, com codigos em campo apropriado.
3. Perfil metrico/relacional estruturado.
4. Hashing atual versus encoder semantico local, com corpus/indice separados.
5. Fusao e reranking pela compatibilidade dos sinais atuais.

Nao exigir que exista uma assinatura lexical para consultar as outras rotas.
Nao usar filtro rigido pelo servico suspeito: ele pode ser o componente
afetado, nao a origem. Permitir analogia entre servicos com mecanismo comum.

Separar scores relativos de ranking de um gate de pertinencia. Calibrar
esse gate com exemplos positivos e negativos de desenvolvimento, incluindo
"nenhum guia aplicavel"; nao usar RRF alto como confianca causal.

### P3. Expandir cobertura seletivamente

- Auditar os sete Bs HTTP pendentes e distinguir ausencia de sinal no run,
  erro de consulta/projecao e falha do procedimento. Corrigir a origem,
  nao aprovar o guia apenas para melhorar cobertura.
- Priorizar lacunas de `front-end`, `shipping` e `payment`.
- Em CPU/rede, resolver primeiro os misses com par elegivel existente.
- Novos runs devem ser calibrados, independentes e ter efeito/cobertura
  discriminativa verificaveis. Replicas ensinam variabilidade do mecanismo,
  nao apenas novas palavras para o mesmo experimento.

Elegibilidade procedural e validacao do efeito devem continuar separadas e
visiveis. Nao relaxar gates apos observar os resultados do teste.

## 9. Benchmark e relatorio a implementar

### 9.1 Qrels e dimensoes

Manter correspondencia exata como metrica auxiliar. Acrescentar avaliacao
manual de aplicabilidade dos guias, com rubrica e, quando viavel, revisores
independentes sem acesso a resposta final da variante:

- 0: irrelevante/contraditorio;
- 1: sintoma generico compartilhado;
- 2: mecanismo transferivel ou check discriminativo util;
- 3: caminho investigativo diretamente aplicavel.

Rotulos do teste entram somente depois da consulta/ranking, para decomposicao.
Observacao de servico na telemetria e permitida; alvo da injecao como chave de
busca nao e.

### 9.2 Metricas

- cobertura exata e cobertura de conhecimento aplicavel;
- Hit/Recall@1, @3 e @5, MRR e nDCG com qrels graduados;
- Hit@3 condicionado a existencia de par elegivel;
- relevancia entregue no contexto, nao apenas recuperada;
- retencao de codigo/excecao/tupla metrica na projecao e consulta;
- misses por coleta, extracao de A, elegibilidade, ranking e packing;
- contribuicao e contradicoes por rota;
- custo/latencia de recuperacao e tamanho do contexto;
- ganhos/perdas pareados por variante.

Gerar tabelas por servico, falha e cruzamento, com denominador e intervalo de
incerteza quando aplicavel. Celulas sem cobertura ou sem amostras nao devem
aparecer como uma acuracia comparavel de ranking.

### 9.3 Sequencia de testes

| Teste | Pergunta | Controle obrigatorio |
|---|---|---|
| T1: 20 fatos e descricoes longas | Atomicos diagnosticos sobrevivem? | Limite real; sentinel no inicio/meio/fim |
| T2: ID variavel, codigo/destino estaveis | Normalizacao preserva mecanismo? | Trocar IDs; manter/diferenciar codigo e destino |
| T3: erro existente no baseline | Raridade e novidade estao separadas? | Mesma assinatura antes da falha |
| T4: propagacao entre servicos | Origem nao e trocada por impacto? | Chamador/destino e fases preservados |
| T5: BM25/vetor/fusao | Qual rota perde o caso? | A/corpus/elegibilidade fixos |
| T6: guia no rank 3, longo | O ranking chega ao modelo? | Orcamento original; IDs reconciliados |
| T7: nenhum guia util | O gate evita contexto enganoso? | Negativos sem autorrecuperacao |
| T8: novo A e novo indice | Ha ganho de representacao? | Ambos lados versionados; nenhuma conclusao B na query |
| T9: RCA completa | O ganho melhora diagnostico? | Coleta MCP nova; evidencia comum ou investigacao explicitamente separada |

Primeiro executar testes de contrato e retrieval-only no conjunto de
desenvolvimento. Depois congelar uma variante e avaliar novos casos cegos.
Novas inferencias sobre os mesmos As nao aumentam o numero de incidentes
independentes. Uma base maior e mais replicas devem permitir analise de
variabilidade por mecanismo.

## 10. Entregaveis e proxima decisao

A auditoria reproduzivel e os resultados estruturados foram preservados no
workspace da sessao:

- [Script da auditoria](/home/daniel/.copilot/session-state/53e4f700-ea06-418c-816d-ac224de6c37e/files/a_retrieval_audit.py).
- [Resultado completo por A, caso e variante](/home/daniel/.copilot/session-state/53e4f700-ea06-418c-816d-ac224de6c37e/files/a_retrieval_audit.json).

Para repetir com o interpretador do projeto:

```bash
./.venv/bin/python - <<'PY'
import runpy
import sys
sys.argv = [
    'a_retrieval_audit.py',
    '--evaluation',
    'experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z',
    '--output',
    '/home/daniel/.copilot/session-state/53e4f700-ea06-418c-816d-ac224de6c37e/files/a_retrieval_audit.json',
]
runpy.run_path(
    '/home/daniel/.copilot/session-state/53e4f700-ea06-418c-816d-ac224de6c37e/files/a_retrieval_audit.py',
    run_name='__main__',
)
PY
```

O indice precisa estar acessivel em `http://127.0.0.1:9200`. O script nao
cria/reindexa documentos e falha explicitamente em contratos inconsistentes.

**Primeiro lote recomendado:** P0 de medicao + P1 de termos + P1 de entrega,
com testes de regressao e benchmark pareado. So depois reconstruir A e
testar fingerprints/encoder. Essa ordem distingue uma perda de transporte
comprovada de uma insuficiencia semantica ainda em investigacao.
