# Avaliação comparativa Skill/RAG com guias elegíveis

- **Avaliação:** `test-base-v6-20261110T212045271499Z`
- **Coorte:** 35 runs históricos concluídos
- **Corpus:** `supplemental-training-35-supplemental-20261109T223633107132Z` (SHA-256 `5c05ba9ec79aeabe0c3c9a2f94badc8563ca7d2b736574dc794f34cb1b19cea6`)
- **Recuperador:** `opensearch-bm25-metric-gated-rrf-v6`; Top-3; candidatos elegíveis somente
- **Geração:** modelo `qwen3.5-9b-grafana`; temperatura `0.1`; seed `20261107`.
- **Estratégias:** Skill original, RAG elegível
- **Natureza:** retrospectiva/exploratória; a coorte já apareceu em avaliações anteriores, portanto não é uma validação cega independente.

## Resumo executivo

- **Skill original:** acerto conjunto 11/35 (31.4%).
- **RAG elegível:** acerto conjunto 18/35 (51.4%).
- **Comparação pareada:** Skill-only 3; RAG-only 10; ambos corretos 8; delta RAG-Skill +7.

A recuperação teve Hit@3 em **17/35** casos (48.6%); entre casos com par exato elegível na base, 17/20 (85.0%).
Tempo médio do diagnóstico: **257.1 s** interno e **265.9 s** por request HTTP. Tokens médios por caso: **220,032** (7,701,120 no total; teto de 300,000/caso, 10,500,000 no conjunto).

## Protocolo e elegibilidade

- Foram considerados 35 incidentes de teste; o índice congelado contém 20 guias proceduralmente validados e 15 pendentes. A recuperação recebeu somente 20 candidatos validados; os pendentes foram excluídos.
- Há par exato elegível para 20/35 incidentes. Esse é o denominador de Hit@3 condicionado, não o denominador de acurácia diagnóstica.
- Os rótulos foram consultados apenas depois de todos os diagnósticos estarem persistidos e validados. Eles não foram enviados como evidência ao modelo.
- O diagnóstico foi executado pela API contra telemetria histórica online; a pré-coleta abaixo serve para comprovar cobertura MCP, não é uma resposta congelada reutilizada como evidência do diagnóstico.
- A avaliação não cria novos incidentes nem injeta caos. Resultados não estimam desempenho em uma coorte independente.

## Recuperação

| Métrica | Resultado |
|---|---:|
| Candidatos elegíveis | 20 |
| Casos com par exato elegível | 20/35 (57.1%) |
| Resultados recuperados elegíveis / pendentes | 105 / 0 |
| Hit@3, todos os casos | 17/35 (48.6%) |
| Hit@3, condicionado a par elegível | 17/20 (85.0%) |
| Precisão@3 | 16.2% |
| MRR@3 | 0.471 |
| Teto de Hit@3 dado o corpus | 57.1% |

A precisão@3 usa como denominador três posições por caso, inclusive quando menos de três resultados são retornados. O MRR é calculado sobre todos os casos, atribuindo zero quando nenhum par exato é recuperado.

## Diagnóstico e dependência da recuperação

| Método | Serviço correto | Falha correta | Acerto conjunto | Classificação completa | Confirmados / inconclusivos / erros |
|---|---:|---:|---:|---:|---:|
| Skill original | 16/35 (45.7%) | 21/35 (60.0%) | 11/35 (31.4%) | 35/35 | 1 / 34 / 0 |
| RAG elegível | 27/35 (77.1%) | 21/35 (60.0%) | 18/35 (51.4%) | 35/35 | 0 / 35 / 0 |

Na comparação pareada de 35 casos, apenas Skill acertou 3, apenas RAG acertou 10, ambos acertaram 8 e ambos erraram 14; delta líquido RAG-Skill: +7 casos.

| Relação com recuperação | Acertos conjuntos |
|---|---:|
| Com par relevante no Top-3 | 13/17 (76.5%) |
| Sem par relevante no Top-3 | 5/18 (27.8%) |

### Resultado da RAG por serviço

| Serviço | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| carts | 5 | 4 | 4 | 5 | 5 | 5 (100.0%) |
| catalogue | 5 | 4 | 4 | 4 | 3 | 3 (60.0%) |
| front-end | 5 | 1 | 1 | 2 | 1 | 1 (20.0%) |
| orders | 5 | 3 | 2 | 5 | 3 | 3 (60.0%) |
| payment | 5 | 2 | 2 | 3 | 3 | 2 (40.0%) |
| shipping | 5 | 2 | 1 | 4 | 3 | 2 (40.0%) |
| user | 5 | 4 | 3 | 4 | 3 | 2 (40.0%) |

### Resultado da RAG por tipo de falha

| Falha | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| container-kill | 7 | 5 | 4 | 6 | 4 | 4 (57.1%) |
| cpu-hog | 7 | 6 | 6 | 7 | 7 | 7 (100.0%) |
| http-status-code | 7 | 0 | 0 | 4 | 4 | 3 (42.9%) |
| memory-hog | 7 | 4 | 4 | 6 | 1 | 1 (14.3%) |
| network-latency | 7 | 5 | 3 | 4 | 5 | 3 (42.9%) |

## Tempo, tokens e orçamento

| Execução | Tempo interno médio | Request HTTP médio | Tokens médios/caso | Tokens totais | Teto tokens/caso | Chamadas/caso | Medidos / estimados |
|---|---:|---:|---:|---:|---:|---:|---:|
| Skill original | 181.7 s | 181.7 s | 168,440 | 5,895,405 | 300,000 | 8.571 | 0 / 300 |
| RAG elegível | 257.1 s | 265.9 s | 220,032 | 7,701,120 | 300,000 | 13.657 | 0 / 478 |
| Skill original (histórico) | n/d | 81.6 s | n/d | n/d | n/d | 3.125 | n/d |
| Controle exploratório (histórico) | 137.8 s | 134.0 s | 169,625 | 5,428,015 | 300,000 | 7.281 | 0 / 233 |
| RAG v4 exploratória, 32 candidatos (histórico) | 151.1 s | 152.0 s | 187,649 | 6,004,756 | 300,000 | 6.875 | 0 / 220 |

O orçamento máximo comum por método/caso foi de 300,000 tokens e 30 chamadas por caso; Skill e RAG receberam esses mesmos limites. O teto teórico para 35 casos por método é 10,500,000 tokens. O uso agregado foi 73.3% desse teto; o maior uso individual foi 92.0%. O orçamento é por caso, não um pool compartilhado entre casos. O consumo de tokens usa a contagem do provedor quando disponível e, nos demais casos, a estimativa por caracteres do backend.
Outros limites comuns: 4 rodadas adicionais/checks e 600 s por caso. A RAG também limita seu histórico a 24,000 caracteres; esse limite específico da recuperação não se aplica à Skill.

Tempos internos vêm de `duration_ms` no endpoint e cobrem a investigação; request HTTP inclui o tempo de transporte/serialização. Pré-coleta MCP não está incluída nesses tempos.

## Cobertura da telemetria histórica MCP

Foram feitas 315 consultas novas (35 casos × 9 consultas: Prometheus, Loki e Tempo em baseline, fault e post), em 73.285 s. Estados agregados: {'ok': 315}. `no_data` é limitação de cobertura, não falha de backend; erros de backend bloquearam a inferência.

| Fonte | Fase | OK | no_data | Erros | Observações |
|---|---|---:|---:|---:|---:|
| metrics | baseline | 35 | 0 | 0 | 75 |
| metrics | fault | 35 | 0 | 0 | 103 |
| metrics | post | 35 | 0 | 0 | 125 |
| logs | baseline | 35 | 0 | 0 | 175 |
| logs | fault | 35 | 0 | 0 | 175 |
| logs | post | 35 | 0 | 0 | 175 |
| traces | baseline | 35 | 0 | 0 | 35 |
| traces | fault | 35 | 0 | 0 | 35 |
| traces | post | 35 | 0 | 0 | 35 |

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

| Run | Serviço/falha real | Skill: predição | Skill: conjunto correto | RAG: predição | RAG: conjunto correto | Top-3 IDs | Hit@3 |
|---|---|---|---:|---|---:|---|---:|
| `run-c5193706324d` | shipping / cpu-hog | shipping / cpu-hog | sim | shipping / cpu-hog | sim | 300eccdd8ab4, 31bca01243f8, 40802daa21f2 | não |
| `run-ac8c25ba5ecb` | carts / network-latency | carts / memory-hog | não | carts / network-latency | sim | 0d0c235d3ceb, bd9c99a5886b, 572584428880 | sim |
| `run-77a850bb2b08` | payment / memory-hog | orders / memory-hog | não | payment / cpu-hog | não | 7651d6acf156, c487b98cd5a1, 7279bb798b57 | não |
| `run-000cb5e46587` | catalogue / cpu-hog | orders / cpu-hog | não | catalogue / cpu-hog | sim | d7efbce88d7e, 28c2f1ea77b2, 4b1b53f0c9f7 | sim |
| `run-4e61cd3c0094` | payment / container-kill | payment / container-kill | sim | payment / container-kill | sim | 7f0521b5691f, c487b98cd5a1, 38bb0d05027e | sim |
| `run-c033bed4a459` | orders / http-status-code | orders / http-status-code | sim | orders / http-status-code | sim | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-761649aa5739` | orders / cpu-hog | orders / memory-hog | não | orders / cpu-hog | sim | 7279bb798b57, 38bb0d05027e, 7f0521b5691f | sim |
| `run-7aaae55a7940` | user / http-status-code | front-end / container-kill | não | user / cpu-hog | não | 6df83d661d79, 572584428880, bd9c99a5886b | não |
| `run-da3099a13b0a` | front-end / memory-hog | orders / memory-hog | não | orders / http-status-code | não | c6b22007a8ce, 4b1b53f0c9f7, 31bca01243f8 | não |
| `run-983d79ec062f` | user / container-kill | front-end / container-kill | não | user / container-kill | sim | 38bb0d05027e, 4b1b53f0c9f7, 7651d6acf156 | sim |
| `run-1e4211d42b7e` | front-end / container-kill | front-end / container-kill | sim | front-end / http-status-code | não | 6df83d661d79, 4a3415752cd7, 300eccdd8ab4 | não |
| `run-1d997947943c` | payment / http-status-code | orders / http-status-code | não | orders-db / container-kill | não | 572584428880, 6df83d661d79, 7279bb798b57 | não |
| `run-5d48ea197a04` | catalogue / memory-hog | catalogue / cpu-hog | não | catalogue / cpu-hog | não | 28c2f1ea77b2, 4b1b53f0c9f7, d7efbce88d7e | sim |
| `run-2c30d6dda5ea` | orders / memory-hog | orders / cpu-hog | não | orders / cpu-hog | não | 38bb0d05027e, 7279bb798b57, 40802daa21f2 | não |
| `run-2f07ba5acb44` | carts / cpu-hog | carts / cpu-hog | sim | carts / cpu-hog | sim | 9703a91da294, 719442df2156, 0d0c235d3ceb | sim |
| `run-0aa05e98178f` | shipping / network-latency | carts / memory-hog | não | shipping / memory-hog | não | 7651d6acf156, 31bca01243f8, 300eccdd8ab4 | não |
| `run-b6c6804f9e35` | carts / http-status-code | orders / http-status-code | não | carts / http-status-code | sim | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-a14fbe9a1b1a` | shipping / container-kill | shipping / container-kill | sim | shipping / container-kill | sim | 7f0521b5691f, c487b98cd5a1, 300eccdd8ab4 | não |
| `run-4b24ab5e3544` | user / memory-hog | user / memory-hog | sim | user / cpu-hog | não | 9809fafb6e6d, 572584428880, 7f0521b5691f | sim |
| `run-08459ef8f204` | shipping / http-status-code | orders / http-status-code | não | orders / http-status-code | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-5c455c3e7335` | front-end / http-status-code | shipping / memory-hog | não | shipping / memory-hog | não | 300eccdd8ab4, 31bca01243f8, 40802daa21f2 | não |
| `run-488cd163f0bb` | shipping / memory-hog | orders / http-status-code | não | shipping / cpu-hog | não | 300eccdd8ab4, 4b1b53f0c9f7, bd9c99a5886b | sim |
| `run-eb9da30de4ac` | carts / memory-hog | orders / http-status-code | não | carts / memory-hog | sim | 719442df2156, 9703a91da294, bd9c99a5886b | sim |
| `run-8c1a5356aab7` | front-end / cpu-hog | orders / memory-hog | não | front-end / cpu-hog | sim | 31bca01243f8, 4b1b53f0c9f7, 38bb0d05027e | sim |
| `run-23711f045b1d` | payment / network-latency | orders / network-latency | não | orders / network-latency | não | 719442df2156, bd9c99a5886b, 300eccdd8ab4 | não |
| `run-f8097606680a` | catalogue / container-kill | front-end / container-kill | não | front-end / network-latency | não | 4b1b53f0c9f7, 300eccdd8ab4, 4a3415752cd7 | sim |
| `run-deba313e8ed5` | orders / network-latency | orders / http-status-code | não | orders / network-latency | sim | bd9c99a5886b, 40802daa21f2, 7279bb798b57 | sim |
| `run-96e49d70591f` | catalogue / http-status-code | front-end / container-kill | não | catalogue / http-status-code | sim | 6df83d661d79, bd9c99a5886b, 572584428880 | não |
| `run-897c049ee6cf` | user / cpu-hog | user / cpu-hog | sim | user / cpu-hog | sim | 572584428880, 7f0521b5691f, 4b1b53f0c9f7 | sim |
| `run-68059c9f5d2c` | carts / container-kill | carts / container-kill | sim | carts / container-kill | sim | 7f0521b5691f, c487b98cd5a1, 38bb0d05027e | sim |
| `run-f93fd70c8529` | catalogue / network-latency | catalogue / network-latency | sim | catalogue / network-latency | sim | 4a3415752cd7, 0d0c235d3ceb, d7efbce88d7e | sim |
| `run-9b39cd0f3400` | payment / cpu-hog | orders / memory-hog | não | payment / cpu-hog | sim | 7651d6acf156, 31bca01243f8, 7f0521b5691f | sim |
| `run-ff26994cbd96` | user / network-latency | orders / network-latency | não | orders / network-latency | não | 40802daa21f2, bd9c99a5886b, 0d0c235d3ceb | não |
| `run-4f923e79a4f8` | front-end / network-latency | orders / http-status-code | não | orders / http-status-code | não | c487b98cd5a1, 4a3415752cd7, 0d0c235d3ceb | não |
| `run-2295bb82c712` | orders / container-kill | orders / container-kill | sim | orders / network-latency | não | 40802daa21f2, 4b1b53f0c9f7, 572584428880 | não |

## Limitações e leitura

- A amostra de 32 casos é reutilizada e pequena; não permite afirmar generalização nem uma melhoria causal sobre as estratégias históricas.
- Só 18 casos têm par exato elegível na base; os demais não são falhas de ranking mensuráveis sob esse qrel.
- A avaliação mede classificação de serviço e tipo de falha, não a validade causal completa de cada explicação. Um acerto conjunto não substitui revisão de evidências e causalidade.
- Resultado `no_data`, truncamento e ruído de consultas namespace-wide precisam ser considerados na interpretação por fase e fonte.
- Para evidência independente, o próximo passo é adicionar novos runs elegíveis, sem reutilizar esta coorte para ajuste e validação.

## Artefatos

- Dados completos e respostas por run: [test_base_rag_evaluation.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/test_base_rag_evaluation.json)
- Manifesto de consultas MCP: [precollection_manifest.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/precollection_manifest.json)
