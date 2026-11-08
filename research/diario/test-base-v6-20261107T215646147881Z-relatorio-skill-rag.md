# Avaliação comparativa Skill/RAG com guias elegíveis

- **Avaliação:** `test-base-v6-20261107T215646147881Z`
- **Coorte:** 32 runs históricos concluídos
- **Corpus:** `rag-rebuild-qwen35-procedural-metrics-20261106` (SHA-256 `2b6cc672b0a8f92c81abda5a4ce28f3bd955bd2fa69b4c5a69c518d971be12f6`)
- **Recuperador:** `opensearch-bm25-metric-gated-rrf-v6`; Top-3; candidatos elegíveis somente
- **Geração:** modelo `qwen3.5-9b-grafana`; temperatura `0.1`; seed `20261107`.
- **Estratégias:** Skill original, RAG elegível
- **Natureza:** retrospectiva/exploratória; a coorte já apareceu em avaliações anteriores, portanto não é uma validação cega independente.

## Resumo executivo

- **Skill original:** acerto conjunto 13/32 (40.6%).
- **RAG elegível:** acerto conjunto 16/32 (50.0%).
- **Comparação pareada:** Skill-only 6; RAG-only 9; ambos corretos 7; delta RAG-Skill +3.

A recuperação teve Hit@3 em **16/32** casos (50.0%); entre casos com par exato elegível na base, 16/18 (88.9%).
Tempo médio do diagnóstico: **178.0 s** interno e **178.8 s** por request HTTP. Tokens médios por caso: **229,555** (7,345,763 no total; teto de 300,000/caso, 9,600,000 no conjunto).

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

| Método | Serviço correto | Falha correta | Acerto conjunto | Classificação completa | Confirmados / inconclusivos / erros |
|---|---:|---:|---:|---:|---:|
| Skill original | 18/32 (56.2%) | 19/32 (59.4%) | 13/32 (40.6%) | 32/32 | 0 / 32 / 0 |
| RAG elegível | 21/32 (65.6%) | 22/32 (68.8%) | 16/32 (50.0%) | 32/32 | 0 / 32 / 0 |

Na comparação pareada de 32 casos, apenas Skill acertou 6, apenas RAG acertou 9, ambos acertaram 7 e ambos erraram 10; delta líquido RAG-Skill: +3 casos.

| Relação com recuperação | Acertos conjuntos |
|---|---:|
| Com par relevante no Top-3 | 11/16 (68.8%) |
| Sem par relevante no Top-3 | 5/16 (31.2%) |

### Resultado da RAG por serviço

| Serviço | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| carts | 5 | 4 | 3 | 3 | 4 | 3 (60.0%) |
| catalogue | 5 | 4 | 4 | 4 | 3 | 3 (60.0%) |
| front-end | 4 | 1 | 1 | 2 | 3 | 2 (50.0%) |
| orders | 4 | 2 | 2 | 3 | 2 | 1 (25.0%) |
| payment | 5 | 2 | 2 | 2 | 3 | 1 (20.0%) |
| shipping | 5 | 2 | 1 | 3 | 4 | 3 (60.0%) |
| user | 4 | 3 | 3 | 4 | 3 | 3 (75.0%) |

### Resultado da RAG por tipo de falha

| Falha | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| container-kill | 6 | 4 | 3 | 4 | 4 | 4 (66.7%) |
| cpu-hog | 7 | 6 | 6 | 6 | 6 | 6 (85.7%) |
| http-status-code | 7 | 0 | 0 | 2 | 7 | 2 (28.6%) |
| memory-hog | 7 | 4 | 4 | 6 | 2 | 2 (28.6%) |
| network-latency | 5 | 4 | 3 | 3 | 3 | 2 (40.0%) |

## Tempo, tokens e orçamento

| Execução | Tempo interno médio | Request HTTP médio | Tokens médios/caso | Tokens totais | Teto tokens/caso | Chamadas/caso | Medidos / estimados |
|---|---:|---:|---:|---:|---:|---:|---:|
| Skill original | 87.6 s | 87.6 s | n/d | n/d | n/d | 3.031 | 0 / 97 |
| RAG elegível | 178.0 s | 178.8 s | 229,555 | 7,345,763 | 300,000 | 7.812 | 0 / 250 |
| Skill original (histórico) | n/d | 81.6 s | n/d | n/d | n/d | 3.125 | n/d |
| Controle exploratório (histórico) | 137.8 s | 134.0 s | 169,625 | 5,428,015 | 300,000 | 7.281 | 0 / 233 |
| RAG v4 exploratória, 32 candidatos (histórico) | 151.1 s | 152.0 s | 187,649 | 6,004,756 | 300,000 | 6.875 | 0 / 220 |

O orçamento máximo da RAG nesta execução foi de 300,000 tokens e 30 chamadas por caso; o teto teórico para 32 casos é 9,600,000 tokens. O uso agregado foi 76.5% desse teto; o maior uso individual foi 96.1%. Isso não é um orçamento compartilhado entre casos. O consumo de tokens usa a contagem do provedor quando disponível e, nos demais casos, a estimativa por caracteres do backend.
Outros limites: 30 chamadas, 4 rodadas adicionais, 600 s por caso e 24,000 caracteres de histórico.

Tempos internos vêm de `duration_ms` no endpoint e cobrem a investigação; request HTTP inclui o tempo de transporte/serialização. Pré-coleta MCP não está incluída nesses tempos.

## Cobertura da telemetria histórica MCP

Foram feitas 288 consultas novas (32 casos × 9 consultas: Prometheus, Loki e Tempo em baseline, fault e post), em 51.333 s. Estados agregados: {'ok': 288}. `no_data` é limitação de cobertura, não falha de backend; erros de backend bloquearam a inferência.

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

| Run | Serviço/falha real | Skill: predição | Skill: conjunto correto | RAG: predição | RAG: conjunto correto | Top-3 IDs | Hit@3 |
|---|---|---|---:|---|---:|---|---:|
| `run-c5193706324d` | shipping / cpu-hog | orders / memory-hog | não | shipping / cpu-hog | sim | 300eccdd8ab4, 31bca01243f8, 4b1b53f0c9f7 | não |
| `run-ac8c25ba5ecb` | carts / network-latency | carts / memory-hog | não | carts / network-latency | sim | 0d0c235d3ceb, bd9c99a5886b, 7279bb798b57 | sim |
| `run-77a850bb2b08` | payment / memory-hog | payment / memory-hog | sim | payment / cpu-hog | não | c487b98cd5a1, 7651d6acf156, 4a3415752cd7 | não |
| `run-000cb5e46587` | catalogue / cpu-hog | catalogue / cpu-hog | sim | catalogue / cpu-hog | sim | d7efbce88d7e, 28c2f1ea77b2, 4b1b53f0c9f7 | sim |
| `run-4e61cd3c0094` | payment / container-kill | payment / container-kill | sim | payment / container-kill | sim | c487b98cd5a1, 7f0521b5691f, 38bb0d05027e | sim |
| `run-c033bed4a459` | orders / http-status-code | orders / http-status-code | sim | front-end / http-status-code | não | bd9c99a5886b, 6df83d661d79, 9703a91da294 | não |
| `run-761649aa5739` | orders / cpu-hog | orders / cpu-hog | sim | orders / cpu-hog | sim | 7279bb798b57, 38bb0d05027e, 7f0521b5691f | sim |
| `run-7aaae55a7940` | user / http-status-code | front-end / container-kill | não | user / http-status-code | sim | 6df83d661d79, bd9c99a5886b, 572584428880 | não |
| `run-da3099a13b0a` | front-end / memory-hog | carts / memory-hog | não | orders / http-status-code | não | 4b1b53f0c9f7, 31bca01243f8, 7651d6acf156 | não |
| `run-983d79ec062f` | user / container-kill | front-end / container-kill | não | user / container-kill | sim | 38bb0d05027e, 4b1b53f0c9f7, 7f0521b5691f | sim |
| `run-1e4211d42b7e` | front-end / container-kill | front-end / container-kill | sim | front-end / container-kill | sim | 7f0521b5691f, 40802daa21f2, 6df83d661d79 | não |
| `run-1d997947943c` | payment / http-status-code | orders / http-status-code | não | orders / http-status-code | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-5d48ea197a04` | catalogue / memory-hog | catalogue / memory-hog | sim | catalogue / cpu-hog | não | 28c2f1ea77b2, d7efbce88d7e, 4b1b53f0c9f7 | sim |
| `run-2c30d6dda5ea` | orders / memory-hog | orders / memory-hog | sim | orders / cpu-hog | não | 7279bb798b57, 38bb0d05027e, 40802daa21f2 | não |
| `run-2f07ba5acb44` | carts / cpu-hog | carts / memory-hog | não | carts / cpu-hog | sim | 9703a91da294, 719442df2156, c487b98cd5a1 | sim |
| `run-0aa05e98178f` | shipping / network-latency | orders / http-status-code | não | orders / http-status-code | não | 719442df2156, 300eccdd8ab4, 31bca01243f8 | não |
| `run-b6c6804f9e35` | carts / http-status-code | orders / memory-hog | não | orders / http-status-code | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-a14fbe9a1b1a` | shipping / container-kill | shipping / container-kill | sim | shipping / container-kill | sim | 300eccdd8ab4, 0d0c235d3ceb, 719442df2156 | não |
| `run-4b24ab5e3544` | user / memory-hog | user / memory-hog | sim | user / cpu-hog | não | 572584428880, 9809fafb6e6d, 4a3415752cd7 | sim |
| `run-08459ef8f204` | shipping / http-status-code | orders / http-status-code | não | orders / http-status-code | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-5c455c3e7335` | front-end / http-status-code | shipping / network-latency | não | orders / http-status-code | não | 0d0c235d3ceb, 6df83d661d79, bd9c99a5886b | não |
| `run-488cd163f0bb` | shipping / memory-hog | shipping / memory-hog | sim | shipping / memory-hog | sim | 300eccdd8ab4, 4b1b53f0c9f7, bd9c99a5886b | sim |
| `run-eb9da30de4ac` | carts / memory-hog | carts / memory-hog | sim | carts / memory-hog | sim | 719442df2156, 9703a91da294, c487b98cd5a1 | sim |
| `run-8c1a5356aab7` | front-end / cpu-hog | orders / memory-hog | não | front-end / cpu-hog | sim | 31bca01243f8, 4b1b53f0c9f7, 38bb0d05027e | sim |
| `run-23711f045b1d` | payment / network-latency | front-end / network-latency | não | front-end / network-latency | não | 719442df2156, 31bca01243f8, 4b1b53f0c9f7 | não |
| `run-f8097606680a` | catalogue / container-kill | front-end / container-kill | não | front-end / network-latency | não | 4b1b53f0c9f7, 38bb0d05027e, 719442df2156 | sim |
| `run-deba313e8ed5` | orders / network-latency | orders / memory-hog | não | orders / cpu-hog | não | bd9c99a5886b, 7279bb798b57, 40802daa21f2 | sim |
| `run-96e49d70591f` | catalogue / http-status-code | front-end / container-kill | não | catalogue / http-status-code | sim | 6df83d661d79, bd9c99a5886b, 572584428880 | não |
| `run-897c049ee6cf` | user / cpu-hog | user / memory-hog | não | user / cpu-hog | sim | 572584428880, 7f0521b5691f, 4b1b53f0c9f7 | sim |
| `run-68059c9f5d2c` | carts / container-kill | carts / container-kill | sim | orders / http-status-code | não | 300eccdd8ab4, 7651d6acf156, 31bca01243f8 | não |
| `run-f93fd70c8529` | catalogue / network-latency | catalogue / cpu-hog | não | catalogue / network-latency | sim | 4a3415752cd7, 0d0c235d3ceb, c487b98cd5a1 | sim |
| `run-9b39cd0f3400` | payment / cpu-hog | orders / http-status-code | não | orders / http-status-code | não | 7f0521b5691f, 7651d6acf156, 31bca01243f8 | sim |

## Limitações e leitura

- A amostra de 32 casos é reutilizada e pequena; não permite afirmar generalização nem uma melhoria causal sobre as estratégias históricas.
- Só 18 casos têm par exato elegível na base; os demais não são falhas de ranking mensuráveis sob esse qrel.
- A avaliação mede classificação de serviço e tipo de falha, não a validade causal completa de cada explicação. Um acerto conjunto não substitui revisão de evidências e causalidade.
- Resultado `no_data`, truncamento e ruído de consultas namespace-wide precisam ser considerados na interpretação por fase e fonte.
- Para evidência independente, o próximo passo é adicionar novos runs elegíveis, sem reutilizar esta coorte para ajuste e validação.

## Artefatos

- Dados completos e respostas por run: [test_base_rag_evaluation.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/test_base_rag_evaluation.json)
- Manifesto de consultas MCP: [precollection_manifest.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/precollection_manifest.json)
