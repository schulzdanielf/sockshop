# Avaliação RAG-only com guias elegíveis

- **Avaliação:** `test-base-v6-20261107T121952077600Z`
- **Coorte:** 32 runs históricos concluídos
- **Corpus:** `rag-rebuild-qwen35-procedural-metrics-20261106` (SHA-256 `2b6cc672b0a8f92c81abda5a4ce28f3bd955bd2fa69b4c5a69c518d971be12f6`)
- **Recuperador:** `opensearch-bm25-metric-gated-rrf-v6`; Top-3; candidatos elegíveis somente
- **Estratégia executada nesta avaliação:** somente `exploratory_rag`
- **Natureza:** retrospectiva/exploratória; a coorte já apareceu em avaliações anteriores, portanto não é uma validação cega independente.

## Resumo executivo

A RAG acertou conjuntamente serviço e falha em **12/32** casos (37.5%); serviço correto em 18/32 e tipo de falha correto em 18/32.
A recuperação teve Hit@3 em **16/32** casos (50.0%); entre casos com par exato elegível na base, 16/18 (88.9%).
Tempo médio do diagnóstico: **149.8 s** interno e **150.6 s** por request HTTP. Tokens médios por caso: **189,216** (6,054,905 no total; teto de 300,000/caso, 9,600,000 no conjunto).

A recuperação encontrou 16 dos 18 pares exatos disponíveis, mas só 8/16 casos com recuperação relevante tiveram acerto conjunto; outros 4/16 acertaram sem recuperar um par relevante. Portanto, o ranking já está perto do teto possível nesta base, mas não basta para corrigir o diagnóstico. Com cobertura exata elegível de 18/32, 60% de Hit@3 sobre todos os 32 casos é impossível sem ampliar a cobertura de guias elegíveis.
A acurácia conjunta foi 12/32, ante 10/32 na RAG exploratória histórica (diferença descritiva de +2 casos, não causal: versão, candidatos e telemetria diferem). Todos os 32 resultados atuais ficaram inconclusivos; os acertos de rótulo não significam confirmação causal.

## Protocolo e elegibilidade

- Foram considerados 32 incidentes de teste; o índice congelado contém 19 guias proceduralmente validados e 13 pendentes. A recuperação recebeu somente 19 candidatos validados; os pendentes foram excluídos.
- Há par exato elegível para 18/32 incidentes. Esse é o denominador de Hit@3 condicionado, não o denominador de acurácia diagnóstica.
- Os rótulos foram consultados apenas depois de todos os diagnósticos estarem persistidos e validados. Eles não foram enviados como evidência ao modelo.
- O diagnóstico foi executado pela API contra telemetria histórica online; a pré-coleta abaixo serve para comprovar cobertura MCP, não é uma resposta congelada reutilizada como evidência do diagnóstico.
- A avaliação não cria novos incidentes nem injeta caos. Resultados não estimam desempenho em uma coorte independente.

## Recuperação

| Métrica | Resultado |
|---|---:|
| Candidatos elegíveis | 19 |
| Casos com par exato elegível | 18/32 (56.2%) |
| Resultados recuperados elegíveis / pendentes | 96 / 0 |
| Hit@3, todos os casos | 16/32 (50.0%) |
| Hit@3, condicionado a par elegível | 16/18 (88.9%) |
| Precisão@3 | 16.7% |
| MRR@3 | 0.469 |
| Teto de Hit@3 dado o corpus | 56.2% |

A precisão@3 usa como denominador três posições por caso, inclusive quando menos de três resultados são retornados. O MRR é calculado sobre todos os casos, atribuindo zero quando nenhum par exato é recuperado.

## Diagnóstico e dependência da recuperação

| Medida | Casos | Percentual |
|---|---:|---:|
| Serviço correto | 18/32 | 56.2% |
| Tipo de falha correto | 18/32 | 56.2% |
| Acerto conjunto | 12/32 | 37.5% |
| Classificação completa | 32/32 | 100.0% |
| Confirmados / inconclusivos / erros | 0 / 32 / 0 | — |

| Relação com recuperação | Acertos conjuntos |
|---|---:|
| Com par relevante no Top-3 | 8/16 (50.0%) |
| Sem par relevante no Top-3 | 4/16 (25.0%) |

### Por serviço

| Serviço | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| carts | 5 | 4 | 3 | 3 | 3 | 2 (40.0%) |
| catalogue | 5 | 4 | 4 | 2 | 2 | 1 (20.0%) |
| front-end | 4 | 1 | 1 | 3 | 4 | 3 (75.0%) |
| orders | 4 | 2 | 2 | 4 | 3 | 3 (75.0%) |
| payment | 5 | 2 | 2 | 2 | 2 | 1 (20.0%) |
| shipping | 5 | 2 | 1 | 0 | 2 | 0 (0.0%) |
| user | 4 | 3 | 3 | 4 | 2 | 2 (50.0%) |

### Por tipo de falha

| Falha | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| container-kill | 6 | 4 | 3 | 4 | 4 | 4 (66.7%) |
| cpu-hog | 7 | 6 | 6 | 5 | 5 | 4 (57.1%) |
| http-status-code | 7 | 0 | 0 | 2 | 6 | 1 (14.3%) |
| memory-hog | 7 | 4 | 4 | 5 | 1 | 1 (14.3%) |
| network-latency | 5 | 4 | 3 | 2 | 2 | 2 (40.0%) |

## Tempo, tokens e orçamento

| Execução | Tempo interno médio | Request HTTP médio | Tokens médios/caso | Tokens totais | Teto tokens/caso | Chamadas/caso | Medidos / estimados |
|---|---:|---:|---:|---:|---:|---:|---:|
| RAG v6 elegível (atual) | 149.8 s | 150.6 s | 189,216 | 6,054,905 | 300,000 | 6.844 | 0 / 219 |
| Skill original (histórico) | n/d | 81.6 s | n/d | n/d | n/d | 3.125 | n/d |
| Controle exploratório (histórico) | 137.8 s | 134.0 s | 169,625 | 5,428,015 | 300,000 | 7.281 | 0 / 233 |
| RAG v4 exploratória, 32 candidatos (histórico) | 151.1 s | 152.0 s | 187,649 | 6,004,756 | 300,000 | 6.875 | 0 / 220 |

O orçamento máximo da RAG nesta execução foi de 300,000 tokens e 30 chamadas por caso; o teto teórico para 32 casos é 9,600,000 tokens. O uso agregado foi 63.1% desse teto; o maior uso individual foi 93.5%. Isso não é um orçamento compartilhado entre casos. O consumo de tokens usa a contagem do provedor quando disponível e, nos demais casos, a estimativa por caracteres do backend.
Outros limites: 30 chamadas, 4 rodadas adicionais, 600 s por caso e 24,000 caracteres de histórico.

Tempos internos vêm de `duration_ms` no endpoint e cobrem a investigação; request HTTP inclui o tempo de transporte/serialização. Pré-coleta MCP não está incluída nesses tempos.

## Cobertura da telemetria histórica MCP

Foram feitas 288 consultas novas (32 casos × 9 consultas: Prometheus, Loki e Tempo em baseline, fault e post), em 62.667 s. Estados agregados: {'ok': 288}. `no_data` é limitação de cobertura, não falha de backend; erros de backend bloquearam a inferência.

| Fonte | Fase | OK | no_data | Erros | Observações |
|---|---|---:|---:|---:|---:|
| metrics | baseline | 32 | 0 | 0 | 63 |
| metrics | fault | 32 | 0 | 0 | 98 |
| metrics | post | 32 | 0 | 0 | 108 |
| logs | baseline | 32 | 0 | 0 | 160 |
| logs | fault | 32 | 0 | 0 | 160 |
| logs | post | 32 | 0 | 0 | 160 |
| traces | baseline | 32 | 0 | 0 | 32 |
| traces | fault | 32 | 0 | 0 | 32 |
| traces | post | 32 | 0 | 0 | 32 |

As consultas de logs e traces usam filtro neutro de namespace, sem selecionar o serviço ou tipo de falha do rótulo-alvo. As consultas abrangem o namespace `sock-shop`; isso aumenta a chance de cobertura, mas pode incluir ruído de outros serviços. O modelo consulta os datasources históricos online para cada diagnóstico; a evidência pré-coletada não é seu fallback.

## Comparação histórica (não pareada)

Referência: avaliação `test-base-v4-20261107T092616241528Z` (estratégias não executadas novamente nesta avaliação). A referência usou outra recuperação e telemetria; comparações não isolam efeito causal. O teste atual filtra guias elegíveis, ao passo que a RAG exploratória anterior permitia os 32 cartões.

| Estratégia anterior | Acerto conjunto | Hit@3 anterior | MRR@3 anterior |
|---|---:|---:|---:|
| Skill original | 10/32 (31.2%) | n/a | n/a |
| Controle exploratório | 4/32 (12.5%) | n/a | n/a |
| RAG v4 exploratória | 10/32 (31.2%) | 10/32 (31.2%) | 0.276 |

Relatório Markdown anterior: [test-base-v4-20261107T092616241528Z](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261107T092616241528Z/test_base_rag_evaluation_report.md).

## Resultado por run

| Run | Serviço real | Falha real | Predição serviço/falha | Top-3 IDs (prefixos) | Hit@3 | Serviço correto | Falha correta | Conjunto correto | Tempo interno | Tokens |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| `run-c5193706324d` | shipping | cpu-hog | user / cpu-hog | 300eccdd8ab4, 31bca01243f8, 4b1b53f0c9f7 | não | não | sim | não | 158.2 s | 130,050 |
| `run-ac8c25ba5ecb` | carts | network-latency | carts / network-latency | 0d0c235d3ceb, bd9c99a5886b, 40802daa21f2 | sim | sim | sim | sim | 168.9 s | 214,380 |
| `run-77a850bb2b08` | payment | memory-hog | payment / cpu-hog | 7651d6acf156, c487b98cd5a1, 7f0521b5691f | não | sim | não | não | 183.4 s | 274,492 |
| `run-000cb5e46587` | catalogue | cpu-hog | catalogue / cpu-hog | d7efbce88d7e, 28c2f1ea77b2, 38bb0d05027e | sim | sim | sim | sim | 148.7 s | 201,410 |
| `run-4e61cd3c0094` | payment | container-kill | payment / container-kill | 7f0521b5691f, c487b98cd5a1, 572584428880 | sim | sim | sim | sim | 178.7 s | 261,003 |
| `run-c033bed4a459` | orders | http-status-code | orders / http-status-code | 6df83d661d79, bd9c99a5886b, 572584428880 | não | sim | sim | sim | 136.8 s | 158,192 |
| `run-761649aa5739` | orders | cpu-hog | orders / cpu-hog | 7279bb798b57, 38bb0d05027e, 7f0521b5691f | sim | sim | sim | sim | 201.9 s | 280,553 |
| `run-7aaae55a7940` | user | http-status-code | user / network-latency | 6df83d661d79, bd9c99a5886b, 572584428880 | não | sim | não | não | 130.0 s | 164,231 |
| `run-da3099a13b0a` | front-end | memory-hog | front-end / memory-hog | 31bca01243f8, 4b1b53f0c9f7, 7651d6acf156 | não | sim | sim | sim | 192.1 s | 270,694 |
| `run-983d79ec062f` | user | container-kill | user / container-kill | 38bb0d05027e, 4b1b53f0c9f7, 7f0521b5691f | sim | sim | sim | sim | 113.1 s | 118,815 |
| `run-1e4211d42b7e` | front-end | container-kill | front-end / container-kill | 572584428880, 7f0521b5691f, 4b1b53f0c9f7 | não | sim | sim | sim | 163.3 s | 209,482 |
| `run-1d997947943c` | payment | http-status-code | orders / http-status-code | 572584428880, 6df83d661d79, bd9c99a5886b | não | não | sim | não | 125.5 s | 162,566 |
| `run-5d48ea197a04` | catalogue | memory-hog | catalogue / cpu-hog | 28c2f1ea77b2, d7efbce88d7e, 4b1b53f0c9f7 | sim | sim | não | não | 118.8 s | 128,745 |
| `run-2c30d6dda5ea` | orders | memory-hog | orders / cpu-hog | 7279bb798b57, 38bb0d05027e, 7f0521b5691f | não | sim | não | não | 130.5 s | 165,894 |
| `run-2f07ba5acb44` | carts | cpu-hog | carts / memory-hog | 719442df2156, 9703a91da294, 0d0c235d3ceb | sim | sim | não | não | 135.4 s | 165,686 |
| `run-0aa05e98178f` | shipping | network-latency | user / cpu-hog | 6df83d661d79, 572584428880, 31bca01243f8 | não | não | não | não | 151.7 s | 195,168 |
| `run-b6c6804f9e35` | carts | http-status-code | orders / http-status-code | 572584428880, 6df83d661d79, bd9c99a5886b | não | não | sim | não | 187.7 s | 259,355 |
| `run-a14fbe9a1b1a` | shipping | container-kill | orders / http-status-code | 7f0521b5691f, 300eccdd8ab4, c487b98cd5a1 | não | não | não | não | 114.5 s | 127,061 |
| `run-4b24ab5e3544` | user | memory-hog | user / cpu-hog | 9809fafb6e6d, 572584428880, 7f0521b5691f | sim | sim | não | não | 195.4 s | 259,654 |
| `run-08459ef8f204` | shipping | http-status-code | orders / http-status-code | 572584428880, 6df83d661d79, bd9c99a5886b | não | não | sim | não | 152.7 s | 224,581 |
| `run-5c455c3e7335` | front-end | http-status-code | orders / http-status-code | 40802daa21f2, 0d0c235d3ceb, 7f0521b5691f | não | não | sim | não | 146.6 s | 189,053 |
| `run-488cd163f0bb` | shipping | memory-hog | orders / http-status-code | 300eccdd8ab4, 4b1b53f0c9f7, 0d0c235d3ceb | sim | não | não | não | 138.4 s | 165,337 |
| `run-eb9da30de4ac` | carts | memory-hog | orders / http-status-code | 719442df2156, c487b98cd5a1, 9703a91da294 | sim | não | não | não | 129.1 s | 131,270 |
| `run-8c1a5356aab7` | front-end | cpu-hog | front-end / cpu-hog | 31bca01243f8, 4b1b53f0c9f7, 38bb0d05027e | sim | sim | sim | sim | 129.6 s | 126,443 |
| `run-23711f045b1d` | payment | network-latency | front-end / memory-hog | 7651d6acf156, bd9c99a5886b, 300eccdd8ab4 | não | não | não | não | 141.6 s | 183,429 |
| `run-f8097606680a` | catalogue | container-kill | front-end / http-status-code | 4b1b53f0c9f7, 4a3415752cd7, 38bb0d05027e | sim | não | não | não | 118.1 s | 150,454 |
| `run-deba313e8ed5` | orders | network-latency | orders / network-latency | bd9c99a5886b, 0d0c235d3ceb, 40802daa21f2 | sim | sim | sim | sim | 134.8 s | 187,814 |
| `run-96e49d70591f` | catalogue | http-status-code | front-end / http-status-code | 6df83d661d79, bd9c99a5886b, 572584428880 | não | não | sim | não | 171.9 s | 232,474 |
| `run-897c049ee6cf` | user | cpu-hog | user / cpu-hog | 572584428880, 7f0521b5691f, 4b1b53f0c9f7 | sim | sim | sim | sim | 159.5 s | 229,877 |
| `run-68059c9f5d2c` | carts | container-kill | carts / container-kill | 31bca01243f8, 0d0c235d3ceb, 9703a91da294 | não | sim | sim | sim | 110.1 s | 117,615 |
| `run-f93fd70c8529` | catalogue | network-latency | orders / http-status-code | 4a3415752cd7, 0d0c235d3ceb, bd9c99a5886b | sim | não | não | não | 157.4 s | 203,096 |
| `run-9b39cd0f3400` | payment | cpu-hog | orders / http-status-code | 7651d6acf156, 7f0521b5691f, 31bca01243f8 | sim | não | não | não | 168.5 s | 166,031 |

## Limitações e leitura

- A amostra de 32 casos é reutilizada e pequena; não permite afirmar generalização nem uma melhoria causal sobre as estratégias históricas.
- Só 18 casos têm par exato elegível na base; os demais não são falhas de ranking mensuráveis sob esse qrel.
- A avaliação mede classificação de serviço e tipo de falha, não a validade causal completa de cada explicação. Um acerto conjunto não substitui revisão de evidências e causalidade.
- Resultado `no_data`, truncamento e ruído de consultas namespace-wide precisam ser considerados na interpretação por fase e fonte.
- Para evidência independente, o próximo passo é adicionar novos runs elegíveis, sem reutilizar esta coorte para ajuste e validação.

## Artefatos

- Dados completos e respostas por run: [test_base_rag_evaluation.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T121952077600Z/test_base_rag_evaluation.json)
- Manifesto de consultas MCP: [precollection_manifest.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T121952077600Z/precollection_manifest.json)
