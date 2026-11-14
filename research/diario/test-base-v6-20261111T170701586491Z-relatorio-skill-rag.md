# Avaliação comparativa Skill/RAG com guias elegíveis

- **Avaliação:** `test-base-v6-20261111T170701586491Z`
- **Coorte:** 35 runs históricos concluídos
- **Corpus:** `supplemental-training-35-supplemental-20261109T223633107132Z` (SHA-256 `5c05ba9ec79aeabe0c3c9a2f94badc8563ca7d2b736574dc794f34cb1b19cea6`)
- **Recuperador:** `opensearch-bm25-metric-gated-rrf-v6`; Top-3; candidatos elegíveis somente
- **Geração:** modelo `qwen3.5-9b-grafana`; temperatura `0.1`; seed `20261107`.
- **Estratégias:** Skill original, RAG elegível
- **Natureza:** replicação r2 com IDs inéditos em relação à coorte r1; exploratória e não cega/confirmatória.

## Resumo executivo

- **Skill original:** acerto conjunto 12/35 (34.3%).
- **RAG elegível:** acerto conjunto 14/35 (40.0%).
- **Comparação pareada:** Skill-only 4; RAG-only 6; ambos corretos 8; delta RAG-Skill +2.

A recuperação teve Hit@3 em **16/35** casos (45.7%); entre casos com par exato elegível na base, 16/20 (80.0%).
Tempo médio do diagnóstico: **277.6 s** interno e **286.5 s** por request HTTP. Tokens médios por caso: **233,087** (8,158,028 no total; teto de 300,000/caso, 10,500,000 no conjunto).

## Protocolo e elegibilidade

- Foram considerados 35 incidentes de teste; o índice congelado contém 20 guias proceduralmente validados e 15 pendentes. A recuperação recebeu somente 20 candidatos validados; os pendentes foram excluídos.
- Há par exato elegível para 20/35 incidentes. Esse é o denominador de Hit@3 condicionado, não o denominador de acurácia diagnóstica.
- Os 7 casos cpu-hog de r2 foram incluídos somente pela emenda v2: média aritmética na janela fault >= 0.80 da quota, sem requisito de duração sustentada. O valor sustentado original de 95% foi mantido como diagnóstico; essa regra é diferente da seleção r1. A emenda alterou apenas a elegibilidade da coorte de teste: não houve nova injeção e os runs não entram no treino. [Emenda v2](../../experiment/eval/memory_hog/out/test_replication/test-replication-20261110-post-reboot-2312/cpu_mean80_amendment_v2.json).
- Os rótulos foram consultados apenas depois de todos os diagnósticos estarem persistidos e validados. Eles não foram enviados como evidência ao modelo.
- O diagnóstico foi executado pela API contra telemetria histórica online; a pré-coleta abaixo serve para comprovar cobertura MCP, não é uma resposta congelada reutilizada como evidência do diagnóstico.
- A avaliação não cria incidentes nem injeta caos. Os IDs r2 são novos, mas os 35 estratos serviço×falha foram pareados com r1; não é um teste cego confirmatório.

## Recuperação

| Métrica | Resultado |
|---|---:|
| Candidatos elegíveis | 20 |
| Casos com par exato elegível | 20/35 (57.1%) |
| Resultados recuperados elegíveis / pendentes | 105 / 0 |
| Hit@3, todos os casos | 16/35 (45.7%) |
| Hit@3, condicionado a par elegível | 16/20 (80.0%) |
| Precisão@3 | 15.2% |
| MRR@3 | 0.429 |
| Teto de Hit@3 dado o corpus | 57.1% |

A precisão@3 usa como denominador três posições por caso, inclusive quando menos de três resultados são retornados. O MRR é calculado sobre todos os casos, atribuindo zero quando nenhum par exato é recuperado.

## Diagnóstico e dependência da recuperação

| Método | Serviço correto | Falha correta | Acerto conjunto | Classificação completa | Confirmados / inconclusivos / erros |
|---|---:|---:|---:|---:|---:|
| Skill original | 16/35 (45.7%) | 21/35 (60.0%) | 12/35 (34.3%) | 35/35 | 3 / 32 / 0 |
| RAG elegível | 23/35 (65.7%) | 18/35 (51.4%) | 14/35 (40.0%) | 35/35 | 0 / 35 / 0 |

Na comparação pareada de 35 casos, apenas Skill acertou 4, apenas RAG acertou 6, ambos acertaram 8 e ambos erraram 17; delta líquido RAG-Skill: +2 casos.

| Relação com recuperação | Acertos conjuntos |
|---|---:|
| Com par relevante no Top-3 | 10/16 (62.5%) |
| Sem par relevante no Top-3 | 4/19 (21.1%) |

### Resultado da RAG por serviço

| Serviço | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| carts | 5 | 4 | 4 | 3 | 3 | 3 (60.0%) |
| catalogue | 5 | 4 | 4 | 4 | 3 | 3 (60.0%) |
| front-end | 5 | 1 | 1 | 1 | 2 | 1 (20.0%) |
| orders | 5 | 3 | 2 | 5 | 2 | 2 (40.0%) |
| payment | 5 | 2 | 2 | 3 | 3 | 2 (40.0%) |
| shipping | 5 | 2 | 1 | 3 | 3 | 2 (40.0%) |
| user | 5 | 4 | 2 | 4 | 2 | 1 (20.0%) |

### Resultado da RAG por tipo de falha

| Falha | n | Par exato elegível | Hit@3 | Serviço correto | Falha correta | Conjunto correto |
|---|---:|---:|---:|---:|---:|---:|
| container-kill | 7 | 5 | 3 | 6 | 4 | 4 (57.1%) |
| cpu-hog | 7 | 6 | 6 | 6 | 7 | 6 (85.7%) |
| http-status-code | 7 | 0 | 0 | 3 | 3 | 1 (14.3%) |
| memory-hog | 7 | 4 | 4 | 6 | 2 | 2 (28.6%) |
| network-latency | 7 | 5 | 3 | 2 | 2 | 1 (14.3%) |

## Tempo, tokens e orçamento

| Execução | Tempo interno médio | Request HTTP médio | Tokens médios/caso | Tokens totais | Teto tokens/caso | Chamadas/caso | Medidos / estimados |
|---|---:|---:|---:|---:|---:|---:|---:|
| Skill original | 190.2 s | 190.2 s | 169,129 | 5,919,518 | 300,000 | 8.057 | 0 / 282 |
| RAG elegível | 277.6 s | 286.5 s | 233,087 | 8,158,028 | 300,000 | 14.6 | 0 / 511 |
| Skill original (histórico) | n/d | 81.6 s | n/d | n/d | n/d | 3.125 | n/d |
| Controle exploratório (histórico) | 137.8 s | 134.0 s | 169,625 | 5,428,015 | 300,000 | 7.281 | 0 / 233 |
| RAG v4 exploratória, 32 candidatos (histórico) | 151.1 s | 152.0 s | 187,649 | 6,004,756 | 300,000 | 6.875 | 0 / 220 |

O orçamento máximo comum por método/caso foi de 300,000 tokens e 30 chamadas por caso; Skill e RAG receberam esses mesmos limites. O teto teórico para 35 casos por método é 10,500,000 tokens. O uso agregado foi 77.7% desse teto; o maior uso individual foi 96.9%. O orçamento é por caso, não um pool compartilhado entre casos. O consumo de tokens usa a contagem do provedor quando disponível e, nos demais casos, a estimativa por caracteres do backend.
Outros limites comuns: 4 rodadas adicionais/checks e 600 s por caso. A RAG também limita seu histórico a 24,000 caracteres; esse limite específico da recuperação não se aplica à Skill.

Tempos internos vêm de `duration_ms` no endpoint e cobrem a investigação; request HTTP inclui o tempo de transporte/serialização. Pré-coleta MCP não está incluída nesses tempos.

## Cobertura da telemetria histórica MCP

Foram feitas 315 consultas novas (35 casos × 9 consultas: Prometheus, Loki e Tempo em baseline, fault e post), em 65.172 s. Estados agregados: {'ok': 315}. `no_data` é limitação de cobertura, não falha de backend; erros de backend bloquearam a inferência.

| Fonte | Fase | OK | no_data | Erros | Observações |
|---|---|---:|---:|---:|---:|
| metrics | baseline | 35 | 0 | 0 | 140 |
| metrics | fault | 35 | 0 | 0 | 91 |
| metrics | post | 35 | 0 | 0 | 122 |
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
| `run-192413e0725e` | carts / container-kill | carts / container-kill | sim | carts / container-kill | sim | 7f0521b5691f, c487b98cd5a1, 38bb0d05027e | sim |
| `run-40235745c324` | shipping / http-status-code | shipping / http-status-code | sim | orders / http-status-code | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-d55bb4e48be4` | catalogue / memory-hog | catalogue / cpu-hog | não | catalogue / cpu-hog | não | 28c2f1ea77b2, d7efbce88d7e, 4b1b53f0c9f7 | sim |
| `run-baafa780b59a` | user / http-status-code | orders / http-status-code | não | user / network-latency | não | 6df83d661d79, bd9c99a5886b, 572584428880 | não |
| `run-96c3af7aaf38` | front-end / cpu-hog | orders / http-status-code | não | orders / cpu-hog | não | 31bca01243f8, 38bb0d05027e, 4b1b53f0c9f7 | sim |
| `run-ec14a255ffc0` | orders / container-kill | orders / container-kill | sim | orders / container-kill | sim | 38bb0d05027e, 7f0521b5691f, bd9c99a5886b | não |
| `run-8445ccc6855b` | front-end / memory-hog | orders / memory-hog | não | orders / container-kill | não | c6b22007a8ce, 4b1b53f0c9f7, 31bca01243f8 | não |
| `run-be419532a564` | orders / memory-hog | orders / memory-hog | sim | orders / http-status-code | não | 38bb0d05027e, 7f0521b5691f, 7279bb798b57 | não |
| `run-be5e9889cd0d` | payment / cpu-hog | payment / cpu-hog | sim | payment / cpu-hog | sim | 7651d6acf156, c487b98cd5a1, 7f0521b5691f | sim |
| `run-260d8e671216` | front-end / container-kill | front-end / container-kill | sim | front-end / container-kill | sim | 4b1b53f0c9f7, 6df83d661d79, 719442df2156 | não |
| `run-4e5f02b5fe42` | catalogue / container-kill | front-end / container-kill | não | orders / http-status-code | não | 4b1b53f0c9f7, 38bb0d05027e, 4a3415752cd7 | sim |
| `run-f35e6c60f5c2` | orders / cpu-hog | orders / http-status-code | não | orders / cpu-hog | sim | 7279bb798b57, 38bb0d05027e, 7f0521b5691f | sim |
| `run-9050f9f8d46e` | payment / http-status-code | orders / http-status-code | não | orders / http-status-code | não | 572584428880, bd9c99a5886b, 6df83d661d79 | não |
| `run-fc81f373ff7a` | catalogue / http-status-code | catalogue / http-status-code | sim | catalogue / http-status-code | sim | 6df83d661d79, bd9c99a5886b, 572584428880 | não |
| `run-8185a933c48e` | front-end / network-latency | user / memory-hog | não | orders / http-status-code | não | c487b98cd5a1, 0d0c235d3ceb, 4a3415752cd7 | não |
| `run-ba8fec13ac6e` | shipping / memory-hog | orders / http-status-code | não | shipping / memory-hog | sim | 300eccdd8ab4, 4b1b53f0c9f7, bd9c99a5886b | sim |
| `run-077d367a6eb5` | payment / memory-hog | orders / memory-hog | não | payment / cpu-hog | não | 7651d6acf156, c487b98cd5a1, 7279bb798b57 | não |
| `run-a8fe22874ca2` | carts / http-status-code | orders / http-status-code | não | user / cpu-hog | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |
| `run-066973be130f` | user / container-kill | front-end / container-kill | não | user / cpu-hog | não | 40802daa21f2, 572584428880, 4b1b53f0c9f7 | não |
| `run-1a40a0286c7e` | front-end / http-status-code | orders / http-status-code | não | user / cpu-hog | não | 40802daa21f2, 572584428880, c6b22007a8ce | não |
| `run-abf3b8ab4c79` | payment / network-latency | orders / cpu-hog | não | orders / http-status-code | não | 6df83d661d79, 4a3415752cd7, 7651d6acf156 | não |
| `run-6200291e534d` | carts / network-latency | orders / memory-hog | não | orders / http-status-code | não | 0d0c235d3ceb, bd9c99a5886b, 4a3415752cd7 | sim |
| `run-1452f5906c1e` | user / memory-hog | orders / memory-hog | não | user / cpu-hog | não | 9809fafb6e6d, 572584428880, 7f0521b5691f | sim |
| `run-b32b59444f49` | shipping / container-kill | shipping / container-kill | sim | shipping / network-latency | não | 40802daa21f2, 572584428880, 4a3415752cd7 | não |
| `run-1b6f86a36d59` | catalogue / network-latency | orders / http-status-code | não | catalogue / network-latency | sim | 4a3415752cd7, c487b98cd5a1, d7efbce88d7e | sim |
| `run-074adf987796` | shipping / network-latency | shipping / network-latency | sim | orders / http-status-code | não | 300eccdd8ab4, 31bca01243f8, 7f0521b5691f | não |
| `run-3c0d31574407` | user / network-latency | orders / http-status-code | não | orders / network-latency | não | bd9c99a5886b, 40802daa21f2, 7279bb798b57 | não |
| `run-b7d0a719d33b` | shipping / cpu-hog | shipping / cpu-hog | sim | shipping / cpu-hog | sim | 300eccdd8ab4, bd9c99a5886b, 7279bb798b57 | não |
| `run-0a66890bb6cb` | orders / network-latency | orders / memory-hog | não | orders / http-status-code | não | bd9c99a5886b, 40802daa21f2, 0d0c235d3ceb | sim |
| `run-f9ad06435ff7` | catalogue / cpu-hog | orders / memory-hog | não | catalogue / cpu-hog | sim | d7efbce88d7e, 28c2f1ea77b2, 38bb0d05027e | sim |
| `run-8a0f365ba7a1` | carts / cpu-hog | orders / http-status-code | não | carts / cpu-hog | sim | 719442df2156, 9703a91da294, c487b98cd5a1 | sim |
| `run-f98e6e5e585c` | payment / container-kill | payment / container-kill | sim | payment / container-kill | sim | 7f0521b5691f, c487b98cd5a1, 38bb0d05027e | sim |
| `run-d55ad59e8592` | user / cpu-hog | orders / memory-hog | não | user / cpu-hog | sim | 572584428880, 7f0521b5691f, 4b1b53f0c9f7 | sim |
| `run-e411c527226f` | carts / memory-hog | carts / memory-hog | sim | carts / memory-hog | sim | 719442df2156, 0d0c235d3ceb, 9703a91da294 | sim |
| `run-66dc40157a42` | orders / http-status-code | orders / memory-hog | não | orders / memory-hog | não | 572584428880, 6df83d661d79, bd9c99a5886b | não |

## Limitações e leitura

- A amostra de 32 casos é reutilizada e pequena; não permite afirmar generalização nem uma melhoria causal sobre as estratégias históricas.
- Só 18 casos têm par exato elegível na base; os demais não são falhas de ranking mensuráveis sob esse qrel.
- A avaliação mede classificação de serviço e tipo de falha, não a validade causal completa de cada explicação. Um acerto conjunto não substitui revisão de evidências e causalidade.
- Resultado `no_data`, truncamento e ruído de consultas namespace-wide precisam ser considerados na interpretação por fase e fonte.
- Para evidência independente, o próximo passo é adicionar novos runs elegíveis, sem reutilizar esta coorte para ajuste e validação.

## Artefatos

- Dados completos e respostas por run: [test_base_rag_evaluation.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261111T170701586491Z/test_base_rag_evaluation.json)
- Manifesto de consultas MCP: [precollection_manifest.json](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261111T170701586491Z/precollection_manifest.json)
