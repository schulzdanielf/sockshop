# Revisão Skill/RAG: redes, HTTP e plano científico de melhoria

**Data:** 08/11/2026.  
**Entrega escolhida:** relatório e plano, sem modificar código, executar nova inferência, publicar Bs ou reinjetar falhas.  
**Natureza:** auditoria exploratória de resultados persistidos, implementação e corpus B, complementada por consultas históricas MCP novas. Não é uma nova avaliação pontuada.

## 1. Parecer executivo

Há evidência útil que os dois métodos não estão aproveitando adequadamente. O problema não se resume ao recuperador:

1. **Skill perde referências de suporte por erro de contrato em 9/12 casos de rede/HTTP.** Strings como `E001: explicação` não correspondem ao ID `E001`; o guard remove essas referências e mantém o diagnóstico inconclusivo. Não se deve relaxar a validação: deve-se melhorar emissão e reparo do contrato.
2. **A investigação aprofunda traces pouco discriminativos para a aresta anômala.** Em shipping/network-latency, ambos se desviam para HTTP 406 em orders, embora existam spans CLIENT orders→shipping lentos, bem-sucedidos e comparáveis entre fases.
3. **HTTP é sobretudo um problema de localização/direção causal.** A RAG acerta o tipo em 7/7, mas o serviço em 2/7. Ambos frequentemente confundem o chamador orders com a dependência afetada.
4. **A RAG elegível não possui nenhum par HTTP exato para os sete incidentes.** Ajustar ranking não recupera um guia inexistente no pool permitido. Isso não justifica liberar guias causalmente defeituosos.
5. **Proceduralmente validado não significa mecanismo causal correto.** Há B network aceito que transforma latência front-end→carts em problema de carts-db, e B HTTP payment aprovado que inventa origem em IO/orders-db.
6. **O gate HTTP aceita falsos corroboradores.** Três probes sintéticos novos reproduziram aceitação de erro somente em baseline, erro de outro destino e código 500 somente nos metadados da consulta.
7. **O resultado global não demonstra superioridade científica.** A comparação atual é 13/32 versus 16/32, com seis vitórias exclusivas da Skill e nove da RAG. A coorte foi reutilizada e os executores/orçamentos efetivos diferem.

**Ordem recomendada:** integridade do contrato → investigação por aresta/operação → validade das afirmações B → comparação pareada com controles. Não começar por aumentar retries, inserir o alvo no prompt ou tornar a aprovação mais permissiva.

## 2. Escopo e rastreabilidade

### 2.1 Resultados e código examinados

- Quatro reports test-base completos, de 06–07/11, e uma preparação sem report final.
- Os **24 diagnósticos** Skill/RAG dos 12 casos atuais de `network-latency` e `http-status-code`.
- Planos, alternativas, referências, checks adicionais, traces selecionados, orçamento e critérios de confirmação.
- Auditoria somente leitura dos rebuilds B e pilotos HTTP de 05–08/11.
- Campanha fonte: [runs.csv](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/runs.csv), [manifesto](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/exploratory_manifest.json) e [validação do efeito](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/effect_validation.json).
- Worktree sobre `985f820bd9c4d9869fc0825a4b8a3e8ce80b3ca0`, com alterações preexistentes. O commit sozinho **não** identifica a implementação revisada.

Os resultados antigos abaixo são scores já persistidos, agregados para análise. Não foram produzidos novos diagnósticos ou scores usando essas respostas como evidência atual.

### 2.2 Telemetria nova

Foram executadas **209 consultas de dados via MCP histórico**, em `http://127.0.0.1:18082/sse`:

- 144 consultas iniciais: 12 casos × 3 fases × 4 consultas;
- 63 consultas dirigidas: busca CLIENT, aprofundamento de traces e métricas client/server;
- dois probes de descoberta de busca CLIENT e leitura de trace original.

As janelas vieram dos metadados da avaliação anterior; **nenhuma resposta antiga de telemetria foi reutilizada como resposta atual**.

| Fonte | Consultas padronizadas | OK | no_data | Erro |
|---|---:|---:|---:|---:|
| Prometheus | 90 | 72 | 18 | 0 |
| Loki | 36 | 36 | 0 | 0 |
| Tempo | 81 | 81 | 0 | 0 |
| Total, sem os dois probes | 207 | 189 | 18 | 0 |

Os dois probes também retornaram dados. Foram validados os hashes das 207 respostas padronizadas e a inclusão temporal de **30 spans CLIENT** nas fases correspondentes, sem inconsistências.

**Limitações:** todas as 36 buscas gerais de logs atingiram 200 linhas; todas as 36 buscas gerais de traces atingiram 50 candidatos; as 15 buscas CLIENT dirigidas atingiram 20 candidatos. São **87 consultas com limite atingido**, além do probe de busca. Somente dois traces por fase/caso dirigido foram aprofundados. Disponibilidade não significa cobertura exaustiva.

As 18 consultas sem dados são client/server service-graph para três arestas de rede nas três fases. Isso demonstra ausência de resposta nessas consultas específicas, não ausência de latência ou de spans.

As taxas/quantis Prometheus usam lookback de 1 min e avaliação a cada 15 s. Podem misturar samples anteriores ao início da fase; pontos de avaliação não são novas amostras de scrape. As médias dos quantis temporais abaixo **não são P95 calculado sobre todas as requisições**.

Comprovantes locais, respostas brutas, argumentos, timestamps, hashes e probes:
[network-review-online](../../../../.copilot/session-state/aad3ced8-382a-473d-a967-12e3deaddc13/files/network-review-online/).
O diretório pertence à sessão local, não acompanha um clone limpo e não deve ser usado para pontuar uma avaliação futura. Uma nova avaliação exige nova coleta MCP.

## 3. Resultados por avaliação

Todas as preparações test-base examinadas usam os mesmos 32 IDs de teste. Comparar linhas de datas diferentes **não isola efeito causal**: corpus permitido, recuperação, telemetria e caminho de execução variam.

| Avaliação | Skill: conjunto | Controle: conjunto | RAG: conjunto | Rede: Skill / controle / RAG | HTTP: Skill / controle / RAG |
|---|---:|---:|---:|---|---|
| v4, 06/11 22:27 UTC | 10/32 | 4/32 | 13/32 | 0/5 / 1/5 / 1/5 | 1/7 / 2/7 / 2/7 |
| v4, 07/11 09:26 UTC | 10/32 | 4/32 | 10/32 | 0/5 / 1/5 / 1/5 | 1/7 / 2/7 / 3/7 |
| v6 elegível, 07/11 12:19 UTC | não executada | não executado | 12/32 | — / — / 2/5 | — / — / 1/7 |
| v6 elegível pareada, 07/11 21:56 UTC | 13/32 | não executado | 16/32 | 0/5 / — / 2/5 | 1/7 / — / 2/7 |

A preparação v4 de 06/11 22:15 UTC não tem report completo; não constitui outra observação de desempenho.

Fontes:
[v4 06/11](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z/test_base_rag_evaluation.json),
[v4 07/11](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261107T092616241528Z/test_base_rag_evaluation.json),
[v6 RAG-only](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T121952077600Z/test_base_rag_evaluation.json),
[v6 pareada](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/test_base_rag_evaluation.json).

### 3.1 Comparação atual por falha

| Falha | n | Skill: conjunto | RAG: conjunto | Só Skill | Só RAG |
|---|---:|---:|---:|---:|---:|
| cpu-hog | 7 | 2 | 6 | 0 | 4 |
| memory-hog | 7 | 6 | 2 | 4 | 0 |
| container-kill | 6 | 4 | 4 | 1 | 1 |
| network-latency | 5 | 0 | 2 | 0 | 2 |
| http-status-code | 7 | 1 | 2 | 1 | 2 |
| Total | 32 | 13 | 16 | 6 | 9 |

Nenhum dos 64 diagnósticos atuais foi confirmado: todos estão `inconclusive`. Portanto, esse placar mede **classificação de serviço/tipo**, não causas corroboradas.

O McNemar exato bilateral sobre as discordâncias globais 6 versus 9 dá **p = 0,6072**, como análise descritiva pós-hoc. A diferença observada não sustenta superioridade estatística; a reutilização da coorte também impede tratá-la como confirmação independente.

### 3.2 Denominadores operacionais

A campanha fonte registra 70 células, mas reutiliza o treino: não são 70 novos incidentes independentes nessa campanha.

- Treino registrado: 32 completed, dois aborted, um blocked; 32 runs reutilizados.
- Teste: 35 planejados, 32 completed, um aborted, dois blocked.
- Teste fora da coorte: orders/container-kill aborted; user/network-latency e front-end/network-latency blocked.
- A validação privada do efeito consta como positiva nos **12 casos de rede/HTTP** examinados.
- No conjunto de 32, há **25 efeitos positivos e sete negativos** no artefato privado. `completed` não deve ser apresentado como sinônimo de efeito validado.

Manter denominadores de execução e de diagnóstico separados. Os três casos não concluídos não devem receber causa inventada; tampouco devem desaparecer do relato da campanha.

## 4. Revisão dos 12 casos atuais

Predições e Hit@3 abaixo são os registros selados da avaliação atual. “Par exato” significa serviço/tipo correspondente dentro do pool elegível; não mede transferibilidade de um procedimento.

| Run | Alvo/tipo privado | Skill | RAG | Par exato elegível | Hit@3 |
|---|---|---|---|---|---|
| run-ac8c25ba5ecb | carts / network-latency | carts / memory-hog | carts / network-latency | sim | sim |
| run-0aa05e98178f | shipping / network-latency | orders / http-status-code | orders / http-status-code | sim | não |
| run-23711f045b1d | payment / network-latency | front-end / network-latency | front-end / network-latency | não | não |
| run-deba313e8ed5 | orders / network-latency | orders / memory-hog | orders / cpu-hog | sim | sim |
| run-f93fd70c8529 | catalogue / network-latency | catalogue / cpu-hog | catalogue / network-latency | sim | sim |
| run-c033bed4a459 | orders / http-status-code | orders / http-status-code | front-end / http-status-code | não | não |
| run-7aaae55a7940 | user / http-status-code | front-end / container-kill | user / http-status-code | não | não |
| run-1d997947943c | payment / http-status-code | orders / http-status-code | orders / http-status-code | não | não |
| run-b6c6804f9e35 | carts / http-status-code | orders / memory-hog | orders / http-status-code | não | não |
| run-08459ef8f204 | shipping / http-status-code | orders / http-status-code | orders / http-status-code | não | não |
| run-5c455c3e7335 | front-end / http-status-code | shipping / network-latency | orders / http-status-code | não | não |
| run-96e49d70591f | catalogue / http-status-code | front-end / container-kill | catalogue / http-status-code | não | não |

Respostas completas: [rag_results](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/rag_results/).

### 4.1 Rede: o sinal existe sem exigir erro

As novas consultas mostram regressão localizada e recuperação:

| Run | Aresta | Média temporal do P95 Istio baseline → fault → post, ms |
|---|---|---|
| carts | front-end → carts | 137,51 → 1.642,72 → 206,90 |
| shipping | orders → shipping | 25,14 → 223,69 → 10,79 |
| payment | orders → payment | 7,67 → 272,10 → 9,85 |
| orders | front-end → orders | 368,02 → 1.896,51 → 362,55 |
| catalogue | front-end → catalogue | 93,22 → 1.684,40 → 100,06 |

Os traces originais corroboram degradação na operação CLIENT, sem provar sozinhos o mecanismo de rede:

- **shipping:** `POST http://shipping/shipping`, status 201; baseline 3,983/5,140 ms, fault 204,735/204,340 ms, post 12,875/3,829 ms. Trace fault representativo: `eac01e6b001bac471b0c59e8495a88a`.
- **payment:** `POST http://payment/paymentAuth`, status 200; baseline 3,693/3,352 ms, fault 203,566/383,896 ms, post 2,574/2,984 ms. Trace fault: `13ab5299399449de651b809e7437b651`.
- **carts:** operações DELETE no mesmo padrão de rota, status 202; baseline 108,474/107,717 ms, fault 892,667/824,368 ms, post 94,870/21,967 ms. Os IDs dos carrinhos diferem; comparar a rota parametrizada, não afirmar identidade da requisição.

São dois exemplos por fase, não distribuições completas ou quantis robustos. Rede, processamento local, filas e dependência continuam explicações alternativas até haver dados discriminativos.

**Desvios observados:**

- shipping: ambos privilegiam HTTP 406 em orders; a hipótese da Skill chega a ligar shipping a RabbitMQ sem demonstração nessa cadeia. A RAG termina com alternativas user/DB e carts/memória.
- payment: ambos localizam front-end, embora a aresta orders→payment apresente regressão forte. O trace aprofundado é um GET de front-end, não a operação paymentAuth.
- orders: ambos aprofundam DELETE /cart e GET /catalogue; a RAG classifica CPU, apesar de recuperar um par network exato. Recuperação correta não garante uso correto.
- carts: a Skill associa +0,13 ponto percentual de memória à regressão muito maior de latência; a RAG melhora a classificação, mas ainda admite IO/DB sem mecanismo específico sustentado.

**Melhoria:** selecionar traces pela aresta/operação anômala, comparar sucesso lento entre fases e confrontar client/server. Drops ausentes não refutam netem; ausência de spans de erro não encerra a investigação.

### 4.2 HTTP: dependência afetada não é chamador nem produtor

Na telemetria nova:

- payment: orders→payment tem 500/FI; spans CLIENT paymentAuth retornam 500 em 0,969/1,134 ms, contra 200 em baseline/post.
- shipping: orders→shipping tem 500/FI; spans CLIENT shipping retornam 500 em 1,410/0,655 ms, contra 201 em baseline/post.
- carts: front-end→carts e orders→carts têm 500/FI; front-end→orders também mostra erro propagado sem FI.
- orders: front-end→orders tem 500/FI, mas a RAG atribui o serviço a front-end.
- user e catalogue: a RAG localiza o destino corretamente mesmo sem guia HTTP exato.

O baixo tempo CLIENT da resposta 500 não comprova falha interna rápida: é compatível com resposta local do proxy. A confirmação privada da intervenção fornece contexto experimental, mas não pode ser usada como evidência oculta do diagnóstico.

O caso front-end exige tratamento específico de observabilidade: a consulta geral não mostrou aresta `source` com 5xx para esse destino. Isso não contradiz automaticamente o efeito privado validado; é uma limitação da perspectiva/reporter/busca. Não transplantar a regra de chamadas internas para ingress sem verificar sua identidade.

No diagnóstico payment, a Skill reconhece exceção genérica de IO em orders, mas ainda inverte o sentido da propagação. A RAG explicita IO/orders-db como hipótese principal. `HttpServerErrorException` no cliente demonstra erro recebido, não saturação de disco nem defeito interno do destino.

Separar na saída:

1. chamador;
2. destino/aresta afetada;
3. produtor da resposta, conhecido ou desconhecido;
4. status/operação/fase observados;
5. mecanismo sustentado ou ainda hipotético.

Para a validade de construto e a ablação de FI, ver [revisão específica HTTP](../2026-11-08-validade-experimento-http-status-code.md).

## 5. Contrato, hipóteses e caminhos executáveis

### 5.1 Perda de IDs na Skill

Em **9/12** respostas atuais, todos os IDs de suporte da Skill são removidos como inexistentes. As limitações preservam o erro; não é silêncio do backend. As strings removidas contêm IDs acrescidos de prosa.

O [guard](../../experiment/platform/backend/rca_skill.py#L723) exige correspondência exata, corretamente. O [plano executável](../../experiment/platform/backend/rca_skill.py#L80) ainda representa hipóteses como strings; a [alternativa](../../experiment/platform/backend/rca_skill.py#L113) também é menos estruturada que o procedimento textual.

**Correção proposta para ambos:** esquema com IDs permitidos na chamada, narrativas separadas de referências, hipóteses com identidade estável e uma tentativa de reparo explícita de contrato antes do guard final. Preservar a resposta inválida e a reparada. Não extrair automaticamente “algum ID” de qualquer narrativa para converter suporte ruim em confirmação.

### 5.2 Seleção de traces

O [seletor compartilhado](../../experiment/platform/backend/agent_console.py#L1733) prioriza failure signatures e depois regressões de latência até seu limite. A [Skill](../../experiment/platform/backend/rca_skill.py#L1382) aprofunda até dois candidatos. O problema não é inexistência total de fallback de latência: é a seleção não suficientemente ligada à aresta/operação discriminativa.

**Correção proposta:** reservar parte do orçamento para uma operação da aresta anômala, inclusive com sucesso HTTP; buscar CLIENT no chamador e validar URL/destino, timestamp e relação pai-filho. Dividir janelas quando o limite for atingido, sob orçamento predefinido. Não consultar apenas o serviço escolhido pelo gabarito.

### 5.3 Gate HTTP: três falsos positivos reproduzidos

O [helper HTTP](../../experiment/platform/backend/rca_skill.py#L425) exige aresta positiva 5xx/FI, mas busca texto 500/HttpServerErrorException no resultado inteiro do segundo item.

Probes sintéticos desta revisão, não telemetria nem novos scores:

| Segundo item, com observação fault positiva | Resultado indevido |
|---|---|
| Erro 500 apenas em baseline; fault normal | inclui logs como corroboração |
| Erro fault orders→shipping; alvo payment | inclui logs como corroboração |
| 500 somente no texto da consulta; observação normal | inclui logs como corroboração |

**Correção proposta:** validar observações fault e a mesma aresta/operação; excluir texto de consulta/metadata; distinguir evidência do impacto de evidência do mecanismo. Uma mesma mensagem exportada para duas fontes não vira prova independente.

Esconder FI sem separar esse gate da validação privada torna a condição sanitizada inelegível por construção. Primeiro separar **validador do efeito experimental** de **validador das afirmações sobre a evidência visível**.

## 6. Conteúdos B e evolução dos rebuilds

Os números são estados persistidos dos artefatos auditados, não novas aprovações desta revisão.

| Rebuild | Network, n=6 | HTTP, n=7 |
|---|---|---|
| 05/11 | 6 insufficient_evidence | 7 insufficient_evidence |
| 06/11 procedural-metrics | 5 procedurally_validated; 1 validation_error | 0 aceitos; 3 validation_error; 4 insufficient_evidence |

No rebuild de 06/11, os 13 registros de rede/HTTP têm `causal_status=not_assessed`. Foram registradas 13 hipóteses/26 consultas network, sem `no_data`, e 14 hipóteses/37 consultas HTTP, com três `no_data`. O campo `unsupported_claims` não está vazio em um network e três HTTP; campo vazio não é certificado de correção semântica.

Fonte: [corpus procedural](../../experiment/platform/data/object_store/.rca_knowledge/rag-rebuild-qwen35-procedural-metrics-20261106/exploratory_base.json).

### 6.1 Exemplos que impedem usar elegibilidade como objetivo

- **Carts/network, run-5497aeb99ba5, par 0d0c235d3ceb…:** B aceito. N003 mede front-end→carts 89,76→803,21→91,60 ms; a cadeia atribui o mecanismo a carts-db/acúmulo de requisições. As quatro consultas são métricas, e duas alternativas são textualmente idênticas.
- **Payment/network, run-7bc23901ac7d, par 7f760f3119f8…:** rejeitado apesar de latência orders→payment positiva. O reviewer identifica uso de zero restarts como exclusão definitiva, sem cobertura suficiente. Rejeição por lógica não equivale a ausência de sinal.
- **Payment/HTTP, piloto rerun:** B aprovado com 500/FI orders→payment e exceção no chamador, mas narrativa de IO/orders-db e propagação invertida.
- **Shipping/HTTP, mesmo piloto:** pendente; o auditor critica um guia histórico user/CPU como se fosse o candidato atual, confundindo contexto de recuperação com conteúdo sob auditoria.
- **Payment pós-migração:** tamanho 153.185 > 150.000 caracteres foi registrado como `collection_error`. Isso é falha de orçamento/estágio, não prova de indisponibilidade MCP.

Fontes:
[piloto rerun](../../experiment/eval/memory_hog/out/knowledge_rebuild/rag-training-http-eligibility-pilot-rerun-20261108/),
[piloto pós-migração](../../experiment/eval/memory_hog/out/knowledge_rebuild/rag-training-http-post-migration-20261108/pending_reaudit.json).

Pilotos HTTP: inicial 0/3 aceitos; rerun 1/3; factual-A-only payment 0/1; pós-migração 0/2 concluídos, mais um dos três selecionados interrompido. Todos registram `publication_allowed=false`. São variantes sobre casos conhecidos, não repetições independentes.

### 6.2 Melhorar B sem contaminar teste

- Identificar cada salto com origem, destino/operação, fase, observação e IDs.
- Proibir especificidade DB/IO/GC/RabbitMQ quando só existe duração/5xx genérico.
- Separar “passo observado” de “explicação ainda candidata”.
- Exigir alternativas semanticamente distintas, não apenas duas entradas.
- Revisar guias aprovados com a mesma rubrica usada nos rejeitados.
- Delimitar no prompt o incidente atual, o candidato B e os guias recuperados; o auditor não pode citar contexto histórico como prova atual.
- Corrigir a divergência de chamadas de `_causal_issues` com/sem `fault_type`, relatada na auditoria de [training_b_rebuild](../../experiment/eval/memory_hog/training_b_rebuild.py#L1792).
- Classificar erros por estágio: coleta, projeção, autoria, reparo, revisão, auditoria, publicação.
- Reservar orçamento para executar a correção final; decisão terminal `correct` sem execução permanece pendente.
- Se B recebe alvo/tipo privado na autoria supervisionada, declarar esse acesso e impedir seu vazamento para A/retrieval/teste.

Não reescrever Bs ou parâmetros após observar o teste confirmatório. A geração de procedimentos generalizáveis deve usar apenas treino/desenvolvimento; a avaliação independente fica congelada.

## 7. Recuperação: medir o que ela pode realmente melhorar

Na avaliação atual:

- pool de 19 guias permitidos, com 13 pendentes fora da recuperação;
- network: quatro dos cinco testes têm par exato elegível; Hit@3 = 3/5, condicionado = 3/4;
- HTTP: nenhum dos sete possui par exato elegível; Hit@3 = 0/7 por construção;
- network/orders recupera par exato e ainda classifica CPU;
- HTTP/user e catalogue acertam sem par HTTP exato.

Logo, separar três problemas:

1. **Cobertura:** há procedimento válido aplicável no pool?
2. **Ranking:** ele chega ao contexto, sem consumir informações privadas?
3. **Uso:** o modelo executa os checks úteis e não copia uma causa histórica?

O qrel serviço/tipo exato é útil, mas não certifica utilidade procedural nem transferência entre serviços. Acrescentar qrel de procedimento aplicável, julgado de forma cega aos resultados dos métodos. Não usar esse julgamento para ajustar o ranking depois de abrir o teste.

Não mudar o limiar v6 com os cinco casos de rede como validação. Se houver ajuste, usar apenas desenvolvimento e registrar o caráter pós-hoc desta coorte.

## 8. Hipóteses científicas testáveis

| Hipótese | Intervenção comum | Resultado a medir | O que a enfraquece |
|---|---|---|---|
| H1: perda de contrato reduz sustentação | emissão restrita de IDs + um reparo simétrico | IDs inválidos, suporte preservado, falsa confirmação | melhora só cosmética ou mecanismo continua sem apoio |
| H2: caminho por aresta recupera sinal discriminativo | CLIENT/endpoint + fases, sucesso lento incluído | cobertura da operação, localização, mecanismo sustentado | novos checks irrelevantes ou maior custo sem ganho |
| H3: corpus válido ajuda além do executor | mesmo engine com/sem retrieval | diferença pareada, uso correto do guia | ganho desaparece no controle sem corpus/orçamento equivalente |
| H4: FI oferece atalho específico do injetor | com/sem indicadores, end-to-end | diferença em localização/mecanismo/falsas confirmações | resultados estáveis e evidência natural suficiente |
| H5: gate semântico evita Bs enganosos | mesma rubrica para candidatos/aceitos | claims não sustentados e direção causal | rejeita tudo por lacuna genérica ou aceita inventar IO |

O objetivo não é forçar todas as hipóteses a produzir melhora. Uma abstenção correta e a rejeição de um B falso podem reduzir a taxa bruta de confirmação e melhorar a validade.

## 9. Protocolo justo proposto

### 9.1 Separar duas perguntas

**Comparação prática:** Skill melhorada versus RAG melhorada, sobre os mesmos novos incidentes e regras comuns. Mede os sistemas completos.

**Contribuição da recuperação:** mesmo executor investigativo com retrieval ligado/desligado. Mede o efeito incremental do conhecimento, não a diferença entre dois engines.

O [runner atual](../../experiment/eval/memory_hog/rag_campaign.py#L510) envia à Skill `max_additional_checks`, modelo e geração, mas não os mesmos limites de tokens/chamadas da RAG. O report lista tetos de 300.000/30 para ambos, porém isso não comprova enforcement desses tetos na Skill.

Na execução atual, Skill usa 3,031 chamadas e 87,6 s/caso; RAG 7,812 chamadas e 178,0 s/caso. Tokens Skill estão indisponíveis, e os tokens RAG são estimados, não medidos. Portanto, **não afirmar equivalência de custo**.

### 9.2 Piloto de desenvolvimento

Usar esta coorte conhecida apenas para depurar:

1. contrato de IDs e erros de estágio;
2. busca dirigida por aresta/operação, inclusive traces sem erro;
3. validação semântica e corroboração HTTP;
4. reauditoria dos Bs e congelamento de corpus revisado.

Não executar todas as mudanças juntas e atribuir o ganho à RAG. Usar ablações para identificar qual etapa mudou cobertura, utilização do guia e diagnóstico.

### 9.3 Avaliação confirmatória

- Pré-registrar mecanismos, alvo de avaliação, métricas primárias, rubrica, exclusões, limites e critério de parada.
- Gerar **novos runs independentes**. Separar treino, desenvolvimento e teste por incidentes e por janelas; não tratar temperatura/seed ou nova inferência no mesmo run como nova replicação da falha.
- Balancear serviço/tipo/intensidade na seleção planejada. Registrar blocked/aborted, sem substituição seletiva após ver resultados.
- Distinguir abort de mesh, atraso de rede e erro natural da aplicação. Acrescentar erro de handler somente se essa população fizer parte da alegação.
- Definir tamanho amostral por efeito mínimo relevante e discordâncias pareadas esperadas, não pelo resultado favorável destes 32 casos.
- Coletar Prometheus/Loki/Tempo online antes da inferência. Selar `live_shared` somente dentro da mesma avaliação; follow-ups podem diferir, mas usam mesmas ferramentas, visibilidade e limites.
- Fixar modelo/checkpoint/runtime, geração, system card, namespace, janelas, prompt, corpus e recuperador, com hashes.
- Intercalar e contrabalancear ordem dos métodos; separar efeito de cache/aquecimento. Fixar a política de cache das sumarizações.
- Aplicar **mesma política de entrada** aos métodos: nenhuma informação privada de alvo/tipo; mesmas regras para FI e equivalentes em texto, logs, ferramentas, revisão e corpus.
- Para isolar retrieval, usar controle com mesmo engine/prompt/planejamento/projeção, mas sem guias. Skill original pode permanecer como referência adicional, identificada como outro executor.
- Medir e aplicar tetos equivalentes de tokens, chamadas, ferramentas, tempo e reparos quando se reivindicar comparação sob orçamento. Igual teto não exige igual consumo; o consumo real é resultado.
- Selar todos os diagnósticos antes de abrir labels ou pontuar. Não escolher a melhor repetição depois de conhecer o gabarito.

### 9.4 Ablação de FI

A condição sem indicadores exige sanitização end-to-end, inclusive autoria/revisão B e conteúdo recuperado. Remover só uma label do prompt não basta.

Preservar bruto privado para auditoria. Se remover FI das labels, reagrupar séries que passam a ter a mesma identidade, sem dobrar tráfego nem misturar reporter. Não apagar outras flags legítimas de transporte indiscriminadamente.

Se os dois corpora forem construídos sob políticas diferentes, documentar e parear essa diferença; não dar guias completos a um braço e guias sanitizados a outro sem tratá-la como fator experimental.

### 9.5 Métricas e análise

Reportar separadamente:

- acerto de serviço, tipo e conjunto, micro e macro por falha;
- direção da cadeia e identificação da dependência afetada;
- mecanismo sustentado, claim não sustentado e falsa confirmação;
- abstenção correta, classificação inconclusiva e cobertura de confirmação;
- cobertura por fase/família/operação, `no_data`, truncamento, gaps e provenance;
- recall da aresta/candidato, guia entregue e checks realmente executados;
- taxas de contrato inválido, reparo, falha de estágio e orçamento esgotado;
- cobertura do corpus, ranking condicionado e utilidade procedural;
- tokens medidos/estimados, chamadas, ferramentas, duração e custo;
- desempenho estratificado por validação privada do efeito.

Usar contraste pareado por run, intervalos de confiança, McNemar exato para endpoints binários e ajuste de multiplicidade nas análises planejadas. Repetições de inferência são aninhadas no mesmo run; não inflar n. Revisores de causalidade devem estar cegos ao método e aos scores; registrar concordância e adjudicação.

## 10. Plano de implementação posterior e critérios de aceite

Nenhum item desta tabela foi implementado nesta revisão.

| Prioridade | Mudança | Critério verificável |
|---|---|---|
| P0 | contrato e reparo de IDs, simétricos | ID puro aceito; prosa/ID inexistente falha explicitamente; uma correção preserva evidência e histórico |
| P0 | corroboração HTTP por observação/aresta/fase | os três probes falsos são rejeitados; erro real da mesma operação/fault é aceito como impacto, sem inventar origem |
| P0 | rubrica semântica B e contexto separado | direção invertida e claims DB/IO sem evidência não passam; guia histórico não pontua candidato atual |
| P1 | CLIENT/endpoint e sucesso lento em rede | shipping/payment usam a operação anômala; ausência de erro/drops não encerra hipótese |
| P1 | cobertura e orçamento por estágio | limite atingido é parcial; prompt excessivo não vira collection_error; síntese/correção têm reserva |
| P1 | controle sem retrieval e custo auditável | mesmos limites efetivamente aplicados; uso Skill contabilizado; nenhuma comparação custo usa null como zero |
| P1 | política FI end-to-end | corpus, ferramentas e prompts sem vazamento; efeito privado separado; reagrupamento não duplica tráfego |
| P2 | nova coorte e teste cego | protocolo congelado antes dos scores; labels só após persistência; denominadores completos |

**Recomendação final:** melhorar os dois métodos pelo mesmo acesso a evidência discriminativa e pelo mesmo rigor de contrato/causalidade. A RAG pode então mostrar valor incremental por procedimentos úteis, e a Skill pode melhorar sem receber o gabarito. Um aumento de acurácia acompanhado de causas inventadas ou aprovação circular não é melhora científica.
