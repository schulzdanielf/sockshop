# Auditoria acadêmica da avaliação Skill/RAG: 70 runs

**Data:** 14/11/2026. **Natureza:** auditoria retrospectiva de código e resultados,
com novas consultas online aos datasources históricos via MCP.
**Escopo:** 35 casos r1 + 35 casos r2, sete serviços × cinco falhas por coorte.
**Não executado:** novas injeções, reinícios, alterações de infraestrutura,
inferência RCA adicional ou nova pontuação do modelo.

## 1. Resumo executivo

**A evidência não sustenta atribuir o problema principalmente a experimentos sem
efeito ou simplesmente ao tamanho do modelo. O primeiro investimento deve ser no
pipeline que representa, seleciona e utiliza a evidência; em paralelo, é preciso
corrigir a cobertura e a validação do corpus.** Existem também lacunas reais de
observabilidade e um problema importante na validação experimental HTTP.
Essa é uma prioridade de engenharia apoiada pelos achados, não uma decomposição
causal estimada da perda de acurácia.

Os achados mais fortes são:

1. **Há informação discriminativa que a estratégia não aproveita.** Em sondas
   condicionadas ao tipo de falha, o alvo tem o maior aumento de CPU em 14/14
   casos e de memória em 14/14. Mesmo assim, a RAG acerta conjuntamente serviço
   e falha de memória em apenas 3/14. Essas sondas usam o rótulo para escolher
   a métrica: **não são uma nova RCA, nem um baseline cego com 100% de acurácia**.
2. **O corpus elegível não cobre HTTP:** dos 35 pares, só 20 são elegíveis;
   nenhum dos sete HTTP é elegível. Melhorar apenas o ranking não pode produzir
   um guia HTTP exato que não existe no pool.
3. **A projeção de evidências precisa de uma representação numérica protegida.**
   Houve redação de valores sem suporte em 66/70 casos, com 1.528 ocorrências
   registradas. Um teste adversarial demonstra que o guard atual aceita trocar
   baseline e fault quando os dois números existem na fonte. Isso prova uma
   lacuna do validador, **não que essa troca ocorreu nas respostas históricas**.
4. **Há validação de configuração apresentada como validação de efeito.**
   `front-end-http-status-code-r2` passou em `effect_validation.json`, embora
   todos os indicadores de impacto ali registrados fossem falsos. O código
   confirma o objeto VirtualService, não sua aplicação no dataplane.
5. **A cobertura é melhor do que aparenta uma busca apenas por spans próprios.**
   `catalogue`, `payment` e `user` não retornam traces próprios nas sondas fault,
   mas spans CLIENT dirigidos ao destino recuperam evidência na quase totalidade
   desses casos. Em contrapartida, consultas limitadas, falta de timestamps de
   término e janelas de kill de aproximadamente três segundos limitam a RCA.
6. **A comparação Skill × RAG atual não isola o efeito de retrieval.** Há
   execuções iniciais independentes, construção de A, checks e sínteses distintas.
   A queda r1→r2 tampouco demonstra deterioração estatisticamente estabelecida.

**Recomendação central:** corrigir representação/proveniência e validade do
efeito; auditar e completar guias; fazer ablações pareadas com evidência comum.
Só depois decidir por um modelo maior, embeddings novos ou outra campanha.

## 2. Material, método e rastreabilidade

### 2.1 Avaliações auditadas

| Coorte | Diretório de avaliação | Runs |
|---|---|---:|
| r1 | `test-base-v6-20261110T212045271499Z` | 35 |
| r2 | `test-base-v6-20261111T170701586491Z` | 35 |

Fontes: manifestos, configurações, corpus congelado, respostas e verificações
salvas nas avaliações [r1](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/)
e [r2](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261111T170701586491Z/).
As respostas históricas são objeto da auditoria, **não substituem a nova consulta
MCP de telemetria**. A tentativa válida foi localizada por
[`result_path`](../../experiment/eval/memory_hog/rag_campaign.py#L157), inclusive
o retry de `carts-network-latency-r2`.

### 2.2 Nova coleta histórica online

Endpoint: `http://127.0.0.1:18082/sse`. O preflight via ferramenta MCP reportou
Prometheus, Loki e Tempo saudáveis. Coleta de 13:39 a 13:50 UTC em 14/11,
com até três workers e operações somente de leitura.

| Etapa | Conteúdo | Consultas |
|---|---|---:|
| Inicial | Três famílias × três fases; golden signals, restart, término e traces de erro | 910 |
| Follow-up | Recursos por pod, CLIENT por destino, logs de proxy e detalhes de traces | 606 |
| Sinais | Recursos com identidade de métrica preservada, arestas HTTP e latência | 126 |
| **Total** | **70 runs únicos** | **1.642** |

Estados registrados: 1.388 `ok`, 254 `no_data`, nenhum erro de consulta.
Esse `ok` é o estado do wrapper de coleta: nas respostas genéricas sem contador,
não certifica resultado não vazio. As conclusões usam pontos finitos, séries,
linhas ou spans efetivos, e não apenas o estado de transporte.

Para todos os 70 runs foram consultadas **baseline, fault e post** em
Prometheus/Loki/Tempo. Os JSONs preservam argumentos, janelas, ferramentas,
horários e hashes. Os corpos de logs não foram exportados; a auditoria preserva
contagens e resumos, e detalhes selecionados de spans.

**Limite de cegamento:** o auditor conhece alvo/falha e usa esses rótulos para
dirigir sondas. Não houve entrega desses rótulos ao modelo, nova inferência ou
reclassificação dos resultados. A auditoria mede disponibilidade e capacidade
diagnóstica dos sinais; não estima desempenho prospectivo de uma política cega.

### 2.3 Suplemento reprodutível

O suplemento entregue junto deste relatório contém:

- `telemetry_matrix_detailed.csv`: **70 linhas**, IDs únicos, janelas/durações,
  pontos de recurso por pod e contagens das três fases; limites de amostragem,
  traces próprios e CLIENT separados. `telemetry_matrix.csv` é a versão básica;
- `analysis.json`: resultados por caso, valores/deltas, ranks condicionais,
  lifecycle e arestas;
- `audit_findings.json`: estatística pareada, exemplos de evidências históricas,
  conflito de metadados e hashes do código auditado;
- `cases/`, `followup/`, `signals/`: os 210 registros de coleta;
- preflight, manifests de conclusão, oito checks e scripts de reprodução.

Os hashes de código são do **worktree atual**, que já estava modificado antes
da auditoria. Não identificam retroativamente os bytes executados nas
avaliações históricas; os manifestos destas não registram um commit suficiente
para essa garantia. O suplemento é local e não envia dados a terceiros.

## 3. O que os placares medem

| Medida RAG | r1 | r2 | Agregado |
|---|---:|---:|---:|
| Serviço correto | 27/35 | 23/35 | 50/70 |
| Tipo de falha correto | 21/35 | 18/35 | 39/70 |
| Serviço **e** falha corretos | 18/35 (51,4%) | 14/35 (40,0%) | 32/70 (45,7%) |
| Status final `confirmed` | 0/35 | 0/35 | 0/70 |
| Status final `inconclusive` | 35/35 | 35/35 | 70/70 |
| Classificação inicial interna correta | 12/35 | 13/35 | 25/70 |
| Inicial errada → final correta | 9 | 5 | 14 |
| Inicial correta → final errada | 3 | 4 | 7 |

Skill isolada: 11/35 e 12/35 de acerto conjunto. Portanto, os números agregados
favorecem a **estratégia RAG completa**, mas não identificam o ganho causal do
retriever, pois os demais componentes não foram mantidos constantes.

É fundamental não chamar 32/70 de “causas confirmadas”: são acertos da escolha
obrigatória de serviço/tipo, mesmo em estado inconclusivo. Há duas tarefas:

1. **Classificação do tipo de intervenção e localização.**
2. **Explicação causal corroborada e calibração da abstenção.**

Uma resposta pode acertar a primeira e não satisfazer a segunda. Remover o guard
de duas famílias para subir confirmação degradaria o rigor, não resolveria a RCA.

### 3.1 Diferença entre as coortes

Nas 35 combinações pareadas, sete perderam acerto e três ganharam:
diferença r2−r1 = **−11,4 pontos percentuais**. Bootstrap pareado de 20.000
amostras, seed `20261114`: IC percentil 95% **[−28,6; +5,7] pp**.
McNemar exato bilateral: **p = 0,34375**.

São análises exploratórias que tratam os 35 pares como intercambiáveis.
Dependência entre falhas do mesmo serviço e uso da mesma infraestrutura não
foram eliminados; não interpretar o intervalo como garantia de generalização.
Ausência de significância também não demonstra equivalência.

| Falha | r1: acerto conjunto | r2: acerto conjunto |
|---|---:|---:|
| CPU | 7/7 | 6/7 |
| Memória | 1/7 | 2/7 |
| Container kill | 4/7 | 4/7 |
| HTTP | 3/7 | 1/7 |
| Latência | 3/7 | 1/7 |

A emenda retrospectiva dos sete CPU de r2 merece análise de sensibilidade,
mas explica apenas **−1 dos −4 acertos líquidos**. HTTP e latência explicam
quedas maiores, compensadas parcialmente pela melhora em memória.

Não houve diferença declarada de modelo, temperatura, seed, corpus, retriever
ou orçamento nas configurações comparadas. Isso não prova igualdade binária de
implementação nem determinismo do backend.

## 4. A telemetria é suficiente?

### 4.1 Definição operacional de suficiência

Cinco níveis precisam ser separados:

1. **Disponibilidade:** a consulta respondeu com observações.
2. **Identidade e tempo:** observações pertencem ao serviço/container e fase.
3. **Discriminação:** há mudança capaz de separar candidatos/hipóteses.
4. **Corroboração:** outra família observa a mesma ocorrência, não apenas tráfego.
5. **Mecanismo:** a cadeia causal é sustentada e hipóteses concorrentes confrontadas.

Esta auditoria testa diretamente os níveis 1–3 e parte do 4. Não demonstra o
nível 5 para todos os runs. Contar duas famílias não transforma presença em
corroboração; métricas de service graph derivadas de spans continuam sendo
`metrics` no contrato, e podem compartilhar o mesmo viés de amostragem.

### 4.2 Cobertura fault por serviço

Contagens abaixo são **casos com resultados**, entre dez runs por serviço:

| Serviço | Logs app | Traces próprios | Busca CLIENT por destino | Próprios ou CLIENT | Logs proxy |
|---|---:|---:|---:|---:|---:|
| carts | 4 | 7 | 10 | 10 | 2 |
| catalogue | 10 | 0 | 9 | 9 | 1 |
| front-end | 10 | 10 | 0 | 10 | 7 |
| orders | 9 | 7 | 10 | 10 | 7 |
| payment | 10 | 0 | 10 | 10 | 2 |
| shipping | 9 | 8 | 10 | 10 | 2 |
| user | 10 | 0 | 10 | 10 | 4 |
| **Total** | **62/70** | **32/70** | **59/70** | **69/70** | **25/70** |

Resultados da busca de traces são metadados de traces candidatos, não uma
contagem de spans causalmente validados. O aprofundamento verificou o timestamp
dos spans; a busca pode retornar um trace que atravessa a janela.
No `front-end`, ausência de CLIENT interno dirigido à entrada não é anormal:
o chamador pode ser externo.

**Retificação importante:** falta de spans próprios de `catalogue`, `payment`
ou `user` não permite concluir falta geral de evidência de traces. É necessário
consultar o chamador e preservar o destino no span CLIENT.

Em logs app, houve resultados em 61/70 baselines, 62/70 faults e 62/70 posts.
Em traces próprios, 40/70, 32/70 e 40/70, respectivamente. A matriz do
suplemento registra as três fases individualmente, sem imputar zeros ausentes
como ausência do fenômeno.

### 4.3 Testes discriminativos por falha

Os ranks abaixo ordenam o **delta positivo na métrica escolhida pelo tipo
conhecido pelo auditor**, entre serviços retornados pelo golden:

| Falha | Evidência alvo | Resultado da sonda | Limite |
|---|---|---|---|
| CPU | CPU relativa à quota do pod | Delta positivo e rank 1: 14/14 | Não prova loop, throttling ou causa de CPU |
| Memória | Working set relativo ao limite do pod | Delta positivo e rank 1: 14/14 | Não prova leak, OOM ou exaustão |
| HTTP | Percentual de erros por serviço | Delta positivo: 12/14; top-1: 8/14; top-3: 12/14 | Propagação pode superar a origem; dois front-end sem aumento |
| Latência | P95 por serviço | Delta positivo e top-3: 14/14; top-1: 9/14 | Não distingue rede de processamento por si só |
| Kill | Restart com padding temporal | Alvo observado: 14/14 | Restart confirma evento, não motivo |

CPU apresenta aumentos de aproximadamente **25,1–59,0 pp**; memória,
**12,3–41,3 pp**. Os 14 alvos de memória continuam abaixo de 68% na média fault.
Isso é coerente com aumento controlado e **não** com uma obrigação de haver OOM.
A intervenção memory-hog e a expressão causal “memory-exhaustion” não são
equivalentes sem evidência adicional. O modelo deve poder reconhecer a
assinatura de intervenção e manter inconclusivo o mecanismo mais forte.

Em latência, arestas Istio têm pontos finitos nos 14 casos. Os perfis de
service graph CLIENT/SERVER aparecem em **2/7 r1 e 7/7 r2** nas sondas fault.
É uma diferença de disponibilidade histórica que precisa constar do protocolo;
não é evidência de que toda a telemetria de r2 seja pior.

Nos 12 HTTP downstream há arestas positivas com flag `FI` e spans CLIENT
HTTP 500 no aprofundamento de até dois traces. Isso corrobora abort no mesh;
não exige exceção ou log de erro no processo de destino.
Ver também a [revisão do mecanismo HTTP](../2026-11-08-validade-experimento-http-status-code.md).

### 4.4 Limitações reais, não resolvidas por mais contexto textual

- **Granularidade:** as cinco séries de recurso/limite existem para os 70 pods
  nas janelas completas, mas sem o rótulo de container da aplicação esperado
  pelo seletor inicial. O golden calcula a razão **por pod**, conforme
  [`_golden_metric_query`](../../mcp-observability-server/investigators.py#L852).
  Não comparar essa razão diretamente com o piso de 0,80 da quota do container;
  componentes auxiliares e denominadores podem mudar a interpretação.
- **Alinhamento temporal:** uma range query de 15 s ancorada na baseline só tem
  pontos dentro de fault em 61/70 casos para CPU por pod. Isso não significa
  que nove pods não tenham métricas: as janelas curtas podem cair entre pontos
  da grade. A mesma série existe na janela total; consultas ancoradas em fault
  e lifecycle com padding respondem a perguntas distintas.
- **Kill:** fault mediana de **3,19 s**, menor que o step de 15 s; `rate[1m]`
  suaviza ainda mais o transiente. Em `catalogue-container-kill-r2` nem a busca
  própria nem a CLIENT retornou traces fault. Não transformar isso em prova de
  que não houve kill.
- **Lifecycle:** a seleção histórica retornou `last_terminated_reason` nos 70
  intervalos completos, mas **nenhuma série de timestamp ou exitcode**. Um reason
  persistente pode ser anterior ao run. Eventos Kubernetes, runtime e histórico
  de probes não foram consultados nesta auditoria e continuam lacunas.
- **Censura:** logs atingem o limite de 200 em 38/70 faults; traces próprios
  atingem 40 em 26/70 e CLIENT em 50/70. Entre os 62 faults com logs, a mediana
  do intervalo entre primeira/última linha devolvida é apenas **8,49 s**.
  Isso não mede a cobertura integral da falha, especialmente antes do restart.
- **Amostragem de traces:** até dois aprofundamentos por caso e busca limitada
  não sustentam uma taxa exaustiva de erros. O cliente Tempo converte os limites
  da busca para segundos inteiros; em faults de três segundos, esse detalhe pesa.
- **Ausência de log app durante abort HTTP:** pode ser comportamento esperado,
  pois Envoy responde antes de alcançar a aplicação. O local certo de busca
  passa a ser proxy/chamador; não promover logs de tráfego normal a prova causal.

**Conclusão de suficiência:** há dados fortes para triagem em CPU/memória,
sinais de impacto em todas as latências e corroboração específica nos 12 HTTP
downstream. **Não há base para certificar que os 70 permitem uma RCA causal
confirmada.** Kill e HTTP de entrada exigem validações adicionais.

## 5. Experimentos: onde o protocolo precisa melhorar

### 5.1 HTTP: configuração não é efeito observado

Em [`evaluate_http_effect`](../../experiment/platform/backend/controlled_experiment.py#L1971),
`mechanism_observed.passed` deriva do readback da VirtualService.
No exportador de
[`effect_validation.json`](../../experiment/eval/memory_hog/controlled_campaign.py#L3537),
`passed` depende de submissão, confirmação e mecanismo, **não de impacto**.

Isso está materializado em
[`front-end-http-status-code-r2`](../../experiment/eval/memory_hog/out/test_replication/test-replication-20261110-post-reboot-2312/cases/front-end-http-status-code-r2/test-replication-20261110-post-reboot-2312-front-end-http-status-code-r2-a1/effect_validation.json):

- `passed=true`, `mechanism_observed.status="configured"`;
- o próprio readback avisa que não confirma propagação ao dataplane;
- `target_edge_error_increased=false`;
- `application_errors_increased=false`, `latency_increased=false`;
- erros de entrada: aproximadamente 0,021% → 0%.

As novas consultas também não mostram aumento de erros no front-end HTTP de
r1 ou r2. Isso **não prova que nenhuma requisição foi afetada**, mas impede
tratar os rótulos como efeitos observados já validados. Nos outros seis HTTP
r2, o artefato registra aumento na aresta, consistente com as novas sondas.

**Correção proposta:** separar `configured`, `dataplane_observed`,
`effect_observed` e `recovered`; validar o caminho real do gerador externo,
rota/porta/host e denominador. Só promover efeito com observação alinhada ao
mecanismo. Preservar os 70 resultados originais e publicar análise de
sensibilidade, sem excluir casos pós-hoc silenciosamente.

### 5.2 CPU e mudança retrospectiva de aceitação

A coorte r2 usa `test-replication-35-v2-cpu-mean80`, aceitando os sete CPU
retrospectivamente por média fault ≥0,80, sem exigir duração sustentada.
Os estados v1 `partial` foram preservados.
O [atestado da coorte](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261111T170701586491Z/source_cohort_attestation.json)
registra hashes da emenda e revalidação.

É uma mudança de seleção experimental, mesmo sem mudar o avaliador RCA.
Relatar principal + sensibilidade com/sem esses sete, e usar um único critério
pré-registrado nas próximas campanhas. Não confundir quota do container com o
percentual agregado do pod consultado pelo modelo.

### 5.3 Baseline, ordem e validade externa

A baseline real mediana passa de **110,387 s para 207,746 s**.
Nos 70 runs o executor registra `reference_source="after_load_stabilization"`;
nesse caminho, não há o recorte de 45 s aplicado a outras referências
([código](../../experiment/platform/backend/agent_console.py#L1259)).
Portanto, trata-se de variação nas janelas reais, não evidência suficiente de
mudança no código do avaliador.

Recomenda-se registrar duração mínima, amostras independentes, carga e
estacionariedade antes de pontuar; controlar ordem/blocos por serviço e horário.
Sete serviços de uma única aplicação e duas execuções temporais não equivalem
a 70 incidentes independentes de produção. Manter a intensidade segura:
não aumentar memória até OOM apenas para tornar o rótulo mais óbvio ao modelo.

## 6. RAG: separar corpus, consulta, retrieval e uso

### 6.1 Cobertura e qualidade do corpus

| Falha | Pares no corpus | Elegíveis |
|---|---:|---:|
| CPU | 7 | 6 |
| Memória | 7 | 4 |
| Kill | 7 | 5 |
| Latência | 7 | 5 |
| HTTP | 7 | **0** |
| **Total** | **35** | **20** |

A cobertura de par exato é no máximo **20/35 = 57,1%** por coorte.
Esse é um limite da recuperação exata, **não um teto de acurácia diagnóstica**:
um guia transferível ou o próprio modelo pode diagnosticar sem exemplo exato.

Os sete HTTP permanecem pendentes, com motivos que incluem diagnóstico não
confirmado e métrica alvo que não confirma a falha rotulada. Não afrouxar a
elegibilidade indiscriminadamente: revisar mecanismo e evidências, reconstruir
guias de treino e revalidá-los.

Há três suplementares com `blind_live_review_bypass` declarando revisão
independente cega ainda não concluída. A revisão supervisionada de B vê o alvo
privado e usa o modelo na validação
([implementação](../../experiment/eval/memory_hog/training_b_rebuild.py#L2370)).
Isso pode ser útil operacionalmente, mas **validação procedural não equivale a
validação causal humana independente**. O mesmo modelo gerando e julgando
conhecimento cria risco de erros correlacionados.

Prioridades do corpus:

- incluir sinais contra a hipótese, limites de aplicabilidade e próximos checks;
- separar assinatura da intervenção, mecanismo provável e impacto;
- guias curtos de verificações transferíveis, não narrativas de “caso parecido”;
- qrels graduados por utilidade causal, além do par exato serviço×falha;
- revisar por humanos uma amostra estratificada e todos os casos controversos,
  sem usar os testes para completar guias de treino.

### 6.2 O retriever atual não é uma RAG semântica densa

A rota v6 combina BM25 e perfil métrico com RRF condicionado à força do sinal
([implementação](../../experiment/eval/memory_hog/opensearch_experiments.py#L1112)).
O perfil usa threshold 10, peso numérico 0,75 e constante RRF 10
([configuração](../../experiment/eval/memory_hog/metric_profile_retrieval.py#L13)).
Metadados de provider hashing não significam uso de embeddings densos na
decisão principal dessa rota.

O ajuste das escalas usa corpus A, o que é uma proteção importante. Entretanto:
representar ausência como zero pode confundir “não medido” com “sem mudança”;
normalização e perfis positivos podem perder amplitude, direção e recuperação.
Antes de trocar embeddings, testar máscara de missingness, perfis separados
por métrica/unidade e preservação de sinais negativos.

### 6.3 Recuperação e entrega ao modelo

| Medida | r1 | r2 |
|---|---:|---:|
| Hit de par exato entre recuperados | 17/35 | 16/35 |
| Hit exato efetivamente entregue | 17/35 | 16/35 |
| Casos com algum guia excluído pelo orçamento, segundo contagem | 35/35 | 34/35 |

O packing histórico usa orçamento de **24.000 caracteres** e aceita guias
inteiros de forma greedy
([código](../../experiment/platform/backend/rca_knowledge.py#L2730)).
Foram entregues dois guias em 67 casos, um em dois casos e três em um.
**Nenhum dos 33 hits exatos recuperados foi perdido na entrega.**
Logo, omissão existe, mas não explica esses hits mal aproveitados.

Entre os pares r1/r2, o conjunto top-3 é igual em 16/35 e sua ordem em 12/35.
Hit/miss permanece igual em 34/35; nove das dez mudanças de acerto conjunto
ocorrem sem alteração de hit/miss. “Retrieval acertou ou errou” sozinho explica
pouco a variação; utilidade, ordem, composição e uso precisam ser medidos.

### 6.4 Bug de metadados de omissão: reproduzido nos 70

`omitted_pair_ids` e `provided_pair_ids` se sobrepõem nos **70/70** resultados.
Em `carts-network-latency-r2`, os três IDs aparecem nas duas listas, embora
`omitted_pair_count=0`.

A causa no código atual é comparar o dicionário completo recuperado com sua
projeção histórica carregada, em vez de comparar IDs
([linha 2843](../../experiment/platform/backend/rca_knowledge.py#L2843)).
Os checks conferiram o conflito diretamente nos artefatos históricos.

**Consequência comprovada:** a trilha de auditoria de IDs omitidos é incorreta.
Isso não demonstra que o prompt deixou de receber os guias.
Usar contagens/IDs fornecidos e conferir o contexto serializado para essa
análise. Corrigir com conjuntos de IDs e invariantes:
`provided ∩ omitted = ∅`, união = recuperados, contagens consistentes.

## 7. Representação e raciocínio sobre evidências

### 7.1 Projeção numérica: risco e custo mensuráveis

A sessão de projeção usa threshold de 60.000 caracteres, budget de 120.000 e
resumos por evidência limitados a 700 caracteres
([implementação](../../experiment/platform/backend/evidence_projection.py#L318)).
O guard compara conjuntos de números da fonte e do resumo
([validação](../../experiment/platform/backend/evidence_projection.py#L589)).

| Registro histórico | r1 | r2 | Total |
|---|---:|---:|---:|
| Casos com redação de números sem suporte | 32 | 34 | 66 |
| Ocorrências de redação | 698 | 830 | 1.528 |
| Chamadas registradas de projeção | 209 | 234 | 443 |
| Chamadas de modelo RAG totais | 478 | 511 | 989 |

São ocorrências nos registros de projeção, potencialmente repetidas em
snapshots; **não 1.528 fatos únicos falsos nem 1.528 erros finais**.
A redação mostra que uma defesa foi acionada, não que todas as versões
incorretas chegaram ao diagnóstico. Mesmo assim, demanda melhor representação.
As 443 chamadas equivalem a aproximadamente **44,8%** das 989 chamadas RAG.
Tokens registrados são estimativas; não há custo monetário exato medido aqui.

**Teste adversarial:** fonte `orders baseline=20%, fault=80%`; resumo
`orders baseline=80%, fault=20%`. O código atual retorna
`summary_validation="validated"`. O teste usa resposta fake determinística,
acionando o mesmo ramo do guard; não chama outro modelo.

Validar “os números existem” não valida as associações
**serviço ↔ métrica ↔ fase ↔ unidade ↔ valor**, nem a direção.
Prioridade: núcleo tabular determinístico/lossless para números, lifecycle e
arestas; LLM resume só texto não crítico. Validar tuplas e referências, não
apenas presença numérica. Não simplesmente aumentar o contexto ou apagar números.

### 7.2 Contraprovas à explicação “faltavam dados”

Os exemplos abaixo também têm sinais presentes em `E001` dos **resultados
históricos**, não apenas na nova coleta. Isso reduz o risco de atribuir ao modelo
um erro usando informação que só se tornou disponível depois.

| Caso r2 | Evidência histórica e nova sonda | Trajetória da RAG |
|---|---|---|
| carts HTTP | Erros carts 0→74,41%; CPU 11,16→3,58%; CLIENT front-end→carts HTTP 500 em fault | Inicial orders/HTTP; final **user/CPU** |
| orders latência | P95 orders 0,32→2,11 s; erros 0,06→0%; CLIENT front-end→orders HTTP 201 com duração elevada | Inicial **orders/latência**, final **orders/HTTP** |
| front-end CPU | CPU pod front-end 35,20→71,89%; orders 17,19→18,45% | Inicial orders/memória; final **orders/CPU** |

Os valores e IDs estão no `audit_findings.json`; os resultados originais estão
nos [casos RAG r2](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261111T170701586491Z/rag_results/).
Uma duração individual de span não prova regressão sem baseline da mesma
operação; no segundo caso, o golden sustenta a regressão agregada, não essa
comparação individual isoladamente.

Há dificuldade de **localizar origem versus propagação**, preservar tipo de
falha e confrontar evidência contrária. Em memória, oito dos 14 diagnósticos
finais escolhem CPU, apesar do delta de memória do alvo ser o maior.
Isso aponta para uso/representação/ontologia inadequados; não identifica sozinho
se o modelo, prompt ou resumo é a causa dominante.

### 7.3 Checks, parada e confirmação

Na RAG histórica, checks adicionais têm os seguintes estados:

| Estado | r1 | r2 |
|---|---:|---:|
| `ok` | 42 | 64 |
| `rejected` | 57 | 42 |
| `no_data` | 19 | 18 |
| `tool_error` | 3 | 4 |

A contagem r2 considera o retry válido. Há sinais de desperdício de orçamento
e desacoplamento entre planejamento e contrato de ferramentas. O host rebaixa
algumas paradas, inclusive `repeated_check`, para inconclusivo
([código](../../experiment/platform/backend/rca_knowledge.py#L3141)).
Isso não deve ser resolvido aceitando repetição ou relaxando confirmação.

Melhorias: cache/registro de consultas já feitas, plano com perguntas ainda não
respondidas, parâmetros validados antes da chamada e reserva de orçamento para
corroboração e síntese. Diferenciar “sem evidência após coleta adequada” de
“investigação não concluiu os checks necessários”.

O [validador](../../experiment/platform/backend/rca_skill.py#L836) protege
estrutura e famílias; a associação genérica de famílias nem sempre aplica o
mesmo filtro explícito de alvo do caminho HTTP. Deve-se testar, por tipo, que
duas famílias se referem à mesma ocorrência/serviço, não apenas a IDs válidos.

## 8. Revisão da avaliação e ameaças à validade

### 8.1 Precollection superficial não demonstra suficiência

[`_queries_for_window`](../../experiment/eval/memory_hog/test_base_rag_evaluation.py#L736)
usa `sum(up{...})`, step de 60 s, logs do namespace com limite cinco e traces
com limite um. Assim, **315/315 `ok`** significa que o pacote superficial de
disponibilidade respondeu, não que cada alvo/falha tenha sinais discriminativos
e temporalmente suficientes.

É importante não ir ao extremo oposto: a Skill faz coleta diagnóstica própria,
por [`run_diagnostics`](../../experiment/eval/memory_hog/test_base_rag_evaluation.py#L1887);
ela não depende exclusivamente dessas nove sondas superficiais.
Melhorar a precollection com matriz explícita de cobertura e validade sem
introduzir rótulos privados no contexto do modelo.

### 8.2 A comparação atual é de estratégias completas

A Skill interna da RAG coincide em classificação com a Skill isolada em apenas
19/35 r1 e 13/35 r2. Portanto, não é uma instância inicial compartilhada.
O fluxo RAG acrescenta construção de A, seleção de guias, ferramentas e nova
síntese. Os ganhos/perdas após a etapa inicial medem o pacote adicional, não
“efeito puro dos documentos”.

Controlar a evidência e a resposta inicial, o budget e o acesso a ferramentas.
Relatar separadamente efeito total da estratégia e efeito incremental do
retrieval. Esse controle é mais importante, neste momento, do que trocar
arbitrariamente o modelo.

### 8.3 Exposição anterior e seleção adaptativa

O [relatório de desenvolvimento da v5](./2026-11-07-relatorio-melhoria-recuperacao-rag.md)
documenta seleção de estratégia usando 32 consultas de teste e reconhece
caráter exploratório. Logo, o problema de desenvolvimento sobre a família de
testes não é apenas hipotético. Isso não demonstra que IDs de teste entraram
no corpus de treino nem quantifica otimismo.

**Correção de interpretação anterior:** repetir uma avaliação não torna
automaticamente r1 otimista. O risco é adaptar escolhas após observar resultados,
não a mera repetição. Não atribuir os quatro acertos de diferença a esse fator
sem experimento controlado. r2 fornece replicação temporal, mas não isolamento
completo de serviços, aplicação ou mecanismo.

### 8.4 Outras ameaças

- Mesmo serviço/arquitetura e poucos casos por estrato: validade externa limitada.
- `FI` pode revelar o mecanismo sintético; testar com/sem esse indício em uma
  ablação rotulada, sem remover a evidência do diagnóstico operacional.
- Tipo obrigatório em `inconclusive` mede classificação forçada; não calibração
  probabilística da causa. Reportar cobertura, acurácia seletiva e falsos
  confirmados quando houver confirmações.
- Corpus e prompt precisam de hashes; nome do modelo não fixa pesos,
  quantização, template, versão do servidor ou comportamento determinístico.
- Evidência consultável hoje pode diferir da disponível na avaliação antiga;
  a auditoria não prova identidade entre as duas. Os três contraexemplos acima
  também foram conferidos nos registros históricos.
- Gate pós-hoc CPU e HTTP sem efeito observado afetam o construto avaliado.
- Selecionar apenas runs concluídos ou recuperados ocultaria falhas da campanha;
  manter denominadores planejados, submetidos, abortados, bloqueados e avaliados.
- Métrica de relevância exata não mede utilidade causal e pode penalizar guias
  transferíveis; não confundir Hit@3 com fidelidade do diagnóstico.

## 9. Plano de melhorias priorizado

### P0 — Integridade e validade, antes de gastar com novas inferências

| Ação | Ponto principal | Critério de aceite |
|---|---|---|
| Proteger números e identidade | `evidence_projection.py` | Troca de fase/serviço/unidade rejeitada; tuplas e IDs críticos preservados sem LLM |
| Corrigir metadados de packing | `rca_knowledge.py` | IDs fornecidos/omitidos disjuntos e contagens iguais ao contexto real em todos os caminhos |
| Separar configuração de efeito HTTP | `controlled_experiment.py`, `controlled_campaign.py` | API readback sem sinal dataplane não recebe “efeito validado”; negativos cobertos por teste |
| Validar cobertura por fase/alvo | `test_base_rag_evaluation.py`, `rag_campaign.py` | Manifesto distingue vazio, censurado, resolução inadequada e sinal discriminativo |
| Fixar proveniência | Manifesto/API/runner | Commit + diff hash, pesos/template/servidor, prompts/skill/corpus/queries e parâmetros registrados |

Essas correções precisam de testes de regressão que falhem no comportamento
atual; os checks desta auditoria **documentam** o comportamento defeituoso e
não devem ser copiados como critérios permanentes de sucesso.

### P1 — Maior probabilidade de melhorar a RAG de forma interpretável

1. **Revalidar corpus HTTP e memória**, sem promover pendentes automaticamente;
   auditar também os três bypasses e registrar julgamento independente.
2. **A determinístico nos fatos críticos:** serviço, operação, unidade, fase,
   delta, amostras, incerteza e IDs extraídos diretamente das ferramentas.
   Narrativa do modelo não deve apagar a tabela nem substituir identidade.
3. **Guia como procedimento verificável:** pré-condições, teste que distingue
   hipótese de alternativa, evidência favorável/contrária e stop condition.
   Packing por seções úteis, registrando exatamente o texto entregue.
4. **Busca orientada a aresta:** origem/destino explícitos para HTTP e latência;
   fallback para CLIENT quando faltarem spans próprios; não confundir vítima
   com origem nem requisito de error span com toda a evidência possível.
5. **Check planner compatível com contrato:** validação prévia, deduplicação,
   orçamento por pergunta e distinção entre esgotamento e refutação.
6. **Avaliação pareada de componentes:** evidência comum e início comum para
   controlar os fatores que hoje mudam simultaneamente.

Critérios: reduzir perda semântica e checks inválidos; aumentar utilidade dos
guias e acerto conjunto **sem aumentar falsos confirmados**; medir ganho
pareado com intervalo e revisão de contradicões, não apenas score agregado.

### P2 — Observabilidade e alternativas de modelo/retrieval

- Habilitar e preservar histórico de timestamp/exitcode/UID, eventos e probes,
  com retenção/namespace corretos e teste de leitura retrospectiva.
- Cobrir container exato e pod, com denominadores explicitamente distintos;
  preservar idade da amostra e a resolução real, não só step da query.
- Amostragem estratificada de logs/traces por fase/operação/erro e paginação
  auditável, com seleção de logs anteriores ao restart.
- Comparar BM25, v6, variações de missingness/direção e embeddings semânticos
  apenas com corpus e budgets controlados; não assumir que “dense” será melhor.
- Comparar o mesmo Qwen com outro modelo local somente em desenho autorizado,
  pesos fixados e evidência igual; medir estrutura, fidelidade e diagnóstico.
- Acrescentar controles sem falha e negativos de configuração sem efeito,
  além de intensidades variadas em futuras campanhas pré-registradas.

## 10. Experimentos de confirmação propostos

Não foram executados nesta auditoria. Devem ser pré-registrados antes de novos
scores e respeitar coleta histórica **online nova por avaliação**. Uma coleta
`live_shared` pode ser selada e reutilizada entre braços/repetições somente
dentro da mesma avaliação; o suplemento desta auditoria não pode virar um
pacote offline para pontuar o próximo experimento.

### Etapa A — Isolar o efeito dos guias

- Mesmos runs, coleta, início Skill, evidências, esquema, modelo e orçamento.
- Braços: sem guia; guias BM25; guias v6; guias aleatórios do mesmo pool e
  tamanho, com seed registrada.
- Primeiro manter os checks iguais para isolar contexto; depois permitir
  investigação adaptativa, avaliando o efeito total separadamente.
- Guia oracle apenas como controle diagnóstico de limite superior, identificado
  como acesso privilegiado e **fora do placar principal publicável**.

### Etapa B — Isolar representação e síntese

- Núcleo tabular lossless versus projeção LLM atual, com o mesmo retriever.
- Medir preservação de tuplas, contradições, regressão inicial→final, números
  sem suporte, checks úteis/rejeitados, tokens efetivos e latência.
- Acrescentar teste do mesmo modelo sem narrativa intermediária de A para
  verificar se a construção de consulta perde sinais.

### Etapa C — Corpus e modelo

- Comparar corpus atual e corpus revalidado, congelados antes da pontuação;
  completar treino usando dados de treino, nunca os 70 incidentes de teste.
- Só depois comparar outro modelo ou embeddings mantendo o restante fixo.
- Para inferência estocástica, seeds pareadas entre braços e repetições
  suficientes definidas após piloto de variância; cinco seeds podem ser um
  piloto, não uma garantia de poder estatístico.

### Métricas e análise

- Primárias: acerto conjunto serviço×falha; taxa de confirmação incorreta e
  fidelidade causal julgada sem acesso à condição experimental.
- Secundárias: recall do alvo no ranking/escopo, Hit@k/Recall@k/MRR/nDCG com
  relevância graduada, guia efetivamente entregue e citado de modo válido,
  completude de checks, abstenção, custo e latência.
- Reportar intenção de avaliar em todos os runs e análise estratificada por
  efeito validado/cobertura; não ocultar inválidos ou falhas de infraestrutura.
- Pareamento por run e seed; bootstrap respeitando blocos/dependência e análise
  por serviço/falha. Usar McNemar exato para comparações binárias adequadas e
  correção de Holm nas comparações confirmatórias múltiplas.
- Repetições do modelo não aumentam o número de incidentes independentes.
  Pré-definir efeito mínimo relevante e poder usando a variância do piloto.
- Dois revisores, se possível, cegos ao método, em amostra estratificada e
  casos divergentes; registrar concordância e adjudicação. Juiz LLM não deve ser
  o único avaliador de conhecimento produzido pelo mesmo modelo.
- Após estabilizar decisões nos dados de desenvolvimento, realizar teste
  prospectivo realmente não usado para escolher variantes.

## 11. Testes realizados e conclusão calibrada

### 11.1 Validação executada

- **205 testes existentes passaram** nas suítes focadas de OpenSearch,
  perfis métricos, avaliador, projeção, conhecimento e Skill.
- **Oito checks da auditoria passaram**, com log persistido no suplemento:
  integridade de 70 runs/consultas/hashes; follow-up; recursos sem colisão de
  identidade; matriz/ranks condicionais; reconstrução dos scores; troca
  adversarial baseline/fault aceita; IDs omitidos conflitantes; HTTP aprovado
  sem impacto observado.
- **1.642 consultas MCP históricas** concluídas, sem erro; ausências e censura
  foram preservadas, sem fallback para telemetria congelada.

Os dois tipos de teste não são intercambiáveis. Testes unitários verificam
contratos; as sondas online verificam dados disponíveis e parte de sua
discriminação. Nenhum deles certifica sozinho a verdade causal de 70 respostas.

### 11.2 Afinal, o problema maior é qual?

| Explicação | Evidência atual | Conclusão |
|---|---|---|
| “Os experimentos não produziram sinal” | CPU/memória com deltas fortes; latência positiva; 12 HTTP downstream corroborados | **Não explica o problema geral** |
| “Toda a telemetria de r2 piorou” | Logs app 31→31 casos; traces próprios/CLIENT combinados 35→34; proxy 6→19; service graph mais disponível | **Não sustentada como explicação global** |
| “O retriever piorou muito” | Hit 17→16; hit/miss igual em 34/35; guias relevantes entregues em todos os hits | **Insuficiente para explicar a queda sozinho** |
| “A RAG usa mal os sinais/experiências” | Contradições históricas; inicial correta degradada; projeção frágil; memória confundida com CPU | **Principal prioridade técnica sustentada** |
| “O corpus precisa melhorar” | Só 20 elegíveis e zero HTTP; revisão procedural com bypasses | **Problema estrutural comprovado** |
| “Há falhas experimentais/de observabilidade” | HTTP configuração≠efeito; kill curto; sem timestamp/exitcode; amostras censuradas | **Comprovado em dimensões específicas** |
| “O modelo é incapaz” | Não há ablação com evidência tabular/guia oracle/outro modelo controlado | **Ainda não identificável** |

**Não é possível distribuir percentuais causais entre modelo, RAG, telemetria e
experimentos com estas duas execuções.** Há evidência suficiente para decidir
onde intervir primeiro: integridade e representação, corpus e comparação
pareada. A melhora deve ser demonstrada pelas ablações, não presumida a partir
de mais contexto, mais guias ou mais runs.

## 12. Referências e vínculo com a pesquisa

- Lewis et al. *Retrieval-Augmented Generation for Knowledge-Intensive NLP
  Tasks*. NeurIPS, 2020. <https://arxiv.org/abs/2005.11401>.
- Es et al. *RAGAS: Automated Evaluation of Retrieval Augmented Generation*.
  <https://arxiv.org/abs/2309.15217>.
- Saad-Falcon et al. *ARES: An Automated Evaluation Framework for
  Retrieval-Augmented Generation Systems*. NAACL, 2024.
  <https://arxiv.org/abs/2311.09476>.

As páginas/abstracts dessas referências foram consultados. Elas apoiam separar
recuperação, uso do contexto e avaliação/calibração; não certificam a verdade
causal de incidentes nem validam automaticamente este protocolo RCA.

Relacionados: [hipótese de localização](../hipoteses/H001-gat-localizacao-falhas.md),
[hipótese de explicação](../hipoteses/H002-agentes-ia-motivo-falha.md),
[estratégia A/B](../2026-10-26-estrategia-rag-rca-conjuntos-a-b.md),
[comparação histórica r1/r2](./test-base-v6-20261111T170701586491Z-comparacao-r1-r2-70.md)
e [revisão anterior de redes/HTTP](./2026-11-08-revisao-skill-rag-redes-plano-cientifico.md).
