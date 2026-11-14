# Plano de complementação: seis casos de treino e teste

**Data:** 08/11/2026.  
**Estado:** planejamento e auditoria de artefatos persistidos; execução não iniciada.  
**Objetivo:** coletar três novos incidentes de treino e três de teste, sem reinjetar os casos já aproveitáveis, e publicar novas versões dos conjuntos.  
**Limite:** seis intervenções planejadas, uma por célula. Este documento não autoriza carga, injeção, reinício, alteração de probes ou publicação.

## 1. Bases de referência e resultado esperado

Usar o corpus efetivamente empregado na [avaliação v6 pareada](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/), não os IDs de treino herdados do ledger exploratório de outubro:

- [Corpus de referência](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/rag_base.json): 32 pares distintos, todos de treino; SHA-256 declarado `2b6cc672b0a8f92c81abda5a4ce28f3bd955bd2fa69b4c5a69c518d971be12f6`.
- Estados dos pares: 19 `procedurally_validated` e 13 `procedural_pending`. Preservar esses estados; presença no corpus não equivale a confirmação causal.
- [Coorte de referência](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/test_cohort.json): 32 testes concluídos; SHA-256 do arquivo `c192e82f4107c9c86bad32b5405d36cb1892becf3f049d4edfd707eb1690ea17`.
- Os IDs de treino e teste são disjuntos. Há 31 combinações serviço/falha em comum.
- Treino aproveitável de `carts/container-kill`: `run-09acaef3fb15`; teste: `run-68059c9f5d2c`. **Não executar novamente.**
- Treino aproveitável de `user/network-latency`: `run-7571858c2393`. Falta apenas teste.
- Teste aproveitável de `front-end/memory-hog`: `run-da3099a13b0a`. Falta treino aceito.

**Meta condicionada a sucesso:** 32 pares preservados + 3 pares novos = 35 combinações de treino; 32 testes preservados + 3 novos = 35 combinações de teste; interseção de 35 combinações, com incidentes distintos entre os conjuntos. Não prometer 35 guias elegíveis nem 35 efeitos validados: há pendências antigas fora do escopo destes seis casos.

Esta é uma complementação de desenvolvimento de uma coorte já examinada, não um novo teste cego. Os segmentos de novembro não tornam o histórico ambientalmente homogêneo.

## 2. Matriz fechada das seis execuções

| Ordem | Célula lógica | Conjunto | Novas intervenções | Tentativa anterior vinculada |
|---|---|---|---:|---|
| 1 | `front-end-memory-hog-r0` | treino | 1 | `run-5d259c38e864`, concluído, mas excluído pelo gate técnico |
| 2 | `front-end-network-latency-r0` | treino | 1 | `run-06336496e6b3` e repetição `run-b285c310e17b`, bloqueados antes de injetar |
| 3 | `orders-container-kill-r0` | treino | 1 | `run-503646080ca5`, abortado na recuperação |
| 4 | `user-network-latency-r1` | teste | 1 | `run-efa23736624d`, bloqueado antes de injetar |
| 5 | `front-end-network-latency-r1` | teste | 1 | `run-0b3bc77bceb9`, bloqueado antes de injetar |
| 6 | `orders-container-kill-r1` | teste | 1 | `run-9e16c0dacae2`, abortado na recuperação experimental |
| **Total** | | **3 treino + 3 teste** | **6** | |

Executar sequencialmente. A ordem de coleta é pré-fixada; a ordem Skill/RAG da avaliação será contrabalanceada separadamente. Pausar após cada treino para conferir segurança e artefatos; pausar obrigatoriamente após os três treinos para revisar A/B e congelar o corpus.

Manter `replica_idx=0` para treino e `replica_idx=1` para teste como papéis lógicos. Cada novo incidente recebe campanha, experimento e `run_id` próprios. A identidade de tentativa inclui campanha, célula e número da tentativa; não reutilizar o ID antigo nem sobrescrever sua pasta. Nova inferência no mesmo run não é nova réplica de caos.

## 3. Auditoria das falhas anteriores

### 3.1 Natureza desta auditoria

Foram examinados ledgers, specs efetivos, reports controlados, amostras cgroup e janelas métricas originais. Os números abaixo são cálculos descritivos sobre esses artefatos, **não consultas MCP novas, novos scores ou aprovação retroativa**. Eles sustentam os motivos dos gates; não comprovam a causa de cada lentidão, instabilidade ou startup da aplicação. Uma RCA causal adicional exigirá nova consulta histórica, logs e traces correlacionados.

### 3.2 Front-end / memory-hog: duração marginal e janela estimada

Fonte: [report original](../../experiment/platform/data/object_store/run-5d259c38e864/controlled_report.json), [amostras cgroup completas](../../experiment/platform/data/object_store/run-5d259c38e864/cgroup_v2_samples.json) e [métrica da intervenção](../../experiment/platform/data/object_store/run-5d259c38e864/controlled_metrics_0041_intervention.json).

- Recuperação e limpeza passaram; `intervention_confirmed_on_target=false` e mecanismo reprovado. Não era uma falha de recuperação.
- Baseline de memória: média **195.665.488,84 bytes**. Limiar: **244.581.861,05 bytes**, equivalente a 1,25 × média baseline.
- Sustentação exigida: **60 s**; observada na janela usada: **58,511 s**. O déficit é **1,489 s**.
- A janela estimada foi **01:58:43–02:01:43 UTC**, derivada da criação do ChaosResult + 180 s. O maior trecho acima do limiar nela foi **02:00:42,451–02:01:40,963 UTC**.
- No sampler completo, com o mesmo limiar e limite de gap de 20 s, esse trecho continua até **02:02:14,190 UTC**, somando **91,739 s**. Parte está fora da janela original e não pode ser incorporada silenciosamente ao seu gate.
- O step de caos terminou às **02:02:46 UTC** e o workflow às **02:03:18 UTC**. Isso sugere desalinhamento entre janela estimada e pressão medida; o step inteiro também não é prova de atividade contínua do stressor.
- A intensidade calculada foi **545 MiB adicionais**, um worker, limite do container de **1.000 MiB**. Média da memória na intervenção: **616.327.168 bytes**; pico: **847.990.784 bytes**.

**Conclusão operacional:** houve pressão mensurável, mas o gate aplicado não passou. Não atribuir tudo a intensidade insuficiente, não arredondar 58,51 para 60 e não converter o run antigo em treino.

**Mitigação:** obter marcadores reais de início/fim do stressor e conservar a coleta durante toda a execução. Corrigir o recorte antes de reduzir a duração exigida. Manter 60 s, exclusão dos primeiros 30 s e gap máximo de 20 s. A janela deve vir de eventos do injetor/processo, não ser escolhida depois de observar o trecho favorável da memória.

### 3.3 Front-end / network-latency: cobertura da entrada externa

Fontes: reports [primeiro treino](../../experiment/platform/data/object_store/run-06336496e6b3/controlled_report.json), [segundo treino](../../experiment/platform/data/object_store/run-b285c310e17b/controlled_report.json) e [teste](../../experiment/platform/data/object_store/run-0b3bc77bceb9/controlled_report.json).

- Os três ficaram `blocked`, com `submitted=false`. Não houve uma injeção de rede malsucedida.
- Cada tentativa teve **nove janelas de estabilização**, todas sem amostras de `target_edge_requests` e `target_edge_latency_ms`; nenhuma chegou ao registro de variabilidade `stabilization_checks`.
- Havia tráfego no reporter de destino. Médias por janela: **61,12–64,92 req/s** no primeiro treino; **24,41–62,85 req/s** na repetição; **56,19–59,71 req/s** no teste.
- A repetição também apresentou latência média por janela de até **6.255 ms**. Corrigir a cobertura não prova que todas essas janelas eram estáveis.
- O [seletor controlado](../../experiment/platform/backend/controlled_experiment.py#L926) usa `reporter="source"` para network-latency. O [helper Istio](../../experiment/platform/backend/analysis/istio_edges.py#L36) exclui `source_workload="unknown"` nessa rota. Existe exceção para front-end/HTTP, mas não front-end/network-latency.

**Conclusão operacional:** a mensagem genérica “load did not stabilize” mascara um bloqueio de cobertura; o código e os dados são compatíveis com um seletor inadequado para Locust externo ao mesh. As labels brutas ainda devem ser verificadas no preflight, sem presumir um chamador interno inexistente.

**Mitigação:** definir explicitamente a medição da entrada externa, sem esperar que Locust produza uma aresta mesh source inexistente:

1. Descobrir e registrar labels reais de entrada do front-end durante ensaio sem falha.
2. Usar um reporter declarado por cenário, fixo em baseline/fault/post, preservando namespace, destino, origem externa e labels brutas. Reusar o helper existente quando compatível.
3. Para entrada externa, avaliar `reporter="destination"` e latência das requisições no Locust por operação. A latência server-side pode não capturar toda a espera egress no pod: validar a sensibilidade da medição antes de aceitá-la como gate do mecanismo.
4. Manter separado o read-back de qdisc/netem, que confirma a intervenção privada, do impacto observado por Istio/Locust. Se a métrica escolhida não for discriminativa, o caso fica pendente; não passar apenas porque o workflow concluiu.
5. Não somar reporters, converter `no_data` em zero, remover o gate de cobertura ou inserir um sidecar no Locust durante a campanha para fabricar labels.
6. Emitir o motivo específico: cobertura ausente, instabilidade, saúde ou limite de espera esgotado.

### 3.4 User / network-latency: instabilidade real e margens estreitas

Fonte: [report do teste anterior](../../experiment/platform/data/object_store/run-efa23736624d/controlled_report.json).

- `submitted=false`; nenhuma intervenção foi feita.
- Nos checks 1 e 2, a saúde estava reprovada: primeiro `front-end` e `catalogue`, depois `front-end`.
- Houve desvios substanciais de latência: desvio-padrão **108,73 ms** versus limite **38,53 ms**, e **82,79 ms** versus **26,66 ms**.
- Nos dois últimos checks, já com saúde aprovada, restou CPU marginal: **0,0224 core** versus **0,0210**, e **0,0214 core** versus **0,0211**.
- O [gate atual](../../experiment/platform/backend/controlled_experiment.py#L89) usa `stddev <= max(20% × |média|, piso)`, com piso CPU de **0,02 core**, janela de **30 s** e nove oportunidades após warmup mínimo de 60 s.

**Conclusão operacional:** há um componente marginal no fim, mas não foi somente excesso de rigor. Relaxar a saúde ou tolerar a grande oscilação de latência seria um erro.

**Mitigação:** aumentar a preparação temporal e validar estabilidade em janelas mais representativas, sem reduzir carga. Manter inicialmente os limites de variabilidade. Se ensaios sem falha demonstrarem bloqueios recorrentes apenas por CPU marginal, propor piso **0,025 core** como variante prospectiva; só adotar após aprovação e congelamento, para todas as seis células. Não aplicá-lo seletivamente a um teste depois de falhar.

### 3.5 Orders / container-kill: startup lento, prazo e recuperação

Fontes: [treino original](../../experiment/platform/data/object_store/run-503646080ca5/controlled_report.json), [teste original](../../experiment/platform/data/object_store/run-9e16c0dacae2/controlled_report.json) e respectivas specs efetivas.

- A intervenção foi confirmada: um SIGKILL no container exato, contador de restart +1 e mudança de identidade. Terminação `Error`, exit code 137; **não é prova de OOM**.
- Tempo entre submissão do sinal e primeira confirmação Ready: aproximadamente **248 s no treino** e **249 s no teste**. O processo reiniciou em poucos segundos, mas isso não equivale a aplicação pronta.
- O prazo efetivo de recuperação experimental era **300 s após 120 s de observação post**, não os 600 s declarados no bloco SLO genérico. Usar a [spec efetiva](../../experiment/platform/data/object_store/run-503646080ca5/effective_spec.json), não apenas o config de alto nível.

| Último check experimental | CPU P95 / teto, core | Latência P95 / teto, ms | Resultado |
|---|---:|---:|---|
| Treino `run-503646080ca5` | 0,3324 / 0,1338 | 391,67 / 366,00 | ambos reprovados |
| Teste `run-9e16c0dacae2` | 0,2496 / 0,1384 | 595,00 / 410,00 | ambos reprovados |

- CPU excedeu o teto em aproximadamente **2,48×** e **1,80×**. A falha original não pode ser descrita como apenas quatro milissegundos de latência.
- Na observação operacional seguinte, o teste acumulou **120,756 s** de estabilidade e passou; o treino original acumulou somente **21,407 s** dos 120 s e não passou nessa etapa.
- Houve checagens passivas posteriores do treino: a tentativa 2 teve lacuna de CPU/memória; a 3 habilitou cgroup exato; a 4 passou após emenda explícita do teto de latência de aproximadamente **366 para 370 ms**. O [registro da emenda](../../experiment/eval/memory_hog/out/controlled_campaign/controlled-20261103T234452388707Z/campaign_events.jsonl) e o [report tardio](../../experiment/eval/memory_hog/out/controlled_campaign/controlled-20261103T234452388707Z/late_recoveries/run-503646080ca5/attempt-4/report.json) preservam `aborted` e exclusão do treino.

**Conclusão operacional:** a lembrança do limiar apertado procede na checagem tardia. Porém, startup demorado e CPU acima da baseline também foram relevantes. JVM/GC, instrumentação, probes e contenção do host são hipóteses, não causas comprovadas por estes reports.

**Mitigação:** manter um SIGKILL, mesmos recursos/probes e carga; ampliar prospectivamente o prazo experimental para **600 s após post de 120 s**, com o mesmo gate métrico e saúde. Não substituir recuperação experimental por operacional tardia. Registrar logs de startup/JVM, probes/eventos e CPU do container para explicar as próximas esperas.

Não copiar um teto absoluto de 370 ms para novos runs. Se a calibração sem caos demonstrar falsos bloqueios marginais de latência, uma variante candidata é elevar o piso aditivo de **50 para 60 ms**, mantendo o fator 1,2. Exige aprovação prévia, política comum e registro dos resultados sob ambas as regras; não serve para aceitar CPU 2,48× acima do teto.

## 4. Política candidata para a próxima execução

Todos os itens novos desta seção são **propostas a implementar, validar e aprovar antes da primeira injeção**, não opções já disponíveis por simples edição de YAML.

### 4.1 Calibração sem caos

1. Fazer dois ensaios fixos de **10 min**, sem intervenção, com o mesmo perfil de 30 usuários, spawn 5/s, mesmas jornadas e recursos. Esses ensaios não entram no treino/teste nem contam entre as seis intervenções.
2. Coletar métricas dos três serviços envolvidos, saúde da aplicação inteira, cobertura por família, scrapes, UTC versus relógio monotônico e carga do host. Não executar inferência concorrente nem manter outro Locust ativo.
3. Reproduzir a regra antiga e a candidata em todas as janelas elegíveis dos controles; registrar taxa de bloqueio por cobertura, saúde e métrica, sem escolher só a melhor baseline.
4. Se houver saúde reprovada, deriva persistente, clock jump ou latência instável, interromper para diagnóstico. Alterações de infraestrutura/probes precisam de autorização separada e novo ensaio.
5. Fixar o perfil escolhido antes das seis células. Se os dois controles não permitirem escolher um perfil estável, não avançar; não fazer controles ilimitados até obter uma janela favorável.

### 4.2 Parâmetros recomendados

| Superfície | Regra anterior | Candidata principal | O que não relaxar |
|---|---|---|---|
| Carga | 30 usuários; spawn 5/s | igual; iniciar/parar por run | não diminuir usuários para conseguir aprovação |
| Warmup mínimo | 60 s | 180 s; prazo total de preparação até 600 s desde início da carga | saúde, ausência de erro de backend e identidade do pod |
| Estabilização | janela 30 s; até nove checks | duas janelas sucessivas de 60 s, sem sobreposição, ambas aprovadas; acompanhamento a cada 15 s | registrar todos os checks; sem declarar independência de fontes |
| Variabilidade | 20% + pisos por métrica | inicialmente igual | saúde e oscilação substancial; piso CPU 0,025 só na variante aprovada |
| Baseline | 90 s após estabilidade | 180 s após estabilidade | não incluir ramp-up nem selecionar baseline pelo resultado da falha |
| Sustentação memória | 60 s, após exclusão inicial de 30 s; gap <=20 s | igual, com janela ancorada no stressor | sem arredondamento, soma de trechos ou empréstimo de samples post |
| Memória/injetor | um worker; 180 s; dose derivada da folga | igual; recalcular dose da baseline nova | limite vivo, cgroup exato, OOM e cutoff 90% |
| Rede | +200 ms, 120 s, jitter 0, egress | igual; corrigir medição da entrada externa | qdisc anterior limpo, netem confirmado e cleanup |
| Kill | um SIGKILL na aplicação | igual | não matar pod inteiro, sidecar ou banco |
| Recuperação experimental orders | post 120 s + prazo 300 s | post 120 s + prazo 600 s | CPU/memória, saúde, tráfego e erros; não aprovar apenas Ready |
| Recuperação dos demais quatro | post 120 s + prazo 300 s | igual | mesma regra e cobertura |
| Segurança operacional após expiração | prazo 600 s; estabilidade 120 s | igual, separada da janela experimental | não transformar `aborted` em `completed` |
| Cooldown | 30 s após recuperação | mínimo 30 s + preflight do próximo run | não atravessar casos com carga contínua |

O teto métrico de recuperação permanece: tráfego médio >=80% da baseline; CPU/memória/erros/latência P95 <= `max(1,2 × P95 baseline, P95 baseline + margem)`. Margens atuais: CPU 0,05 core; memória 16 MiB; erros 1 ponto percentual; latência 50 ms. Cada check precisa de pelo menos quatro amostras; cobertura baseline/fault continua exigindo pelo menos 80% do intervalo. Não confundir o P95 temporal de uma série com P95 de todas as requisições.

O aumento de duração não pode depender só de `timeline.warmup_seconds`: o executor contém esperas de 60/90/120 s e nove checks codificados. Implementar a política versionada no executor e recalcular os tetos de carga/polling pela soma dos estágios, incluindo overhead. Timeout do cliente não altera o timeout do datasource.

### 4.3 Segurança e tentativas

- Preflight sem reparo automático: namespace/container/UID, limites, probes e imagens fixados; aplicação saudável; relógios coerentes; MCP disponível; ausência de injetor ou carga concorrente.
- Para rede: qdisc anterior conhecido, sem netem preexistente, módulo confirmado; congelar interface/direção e verificar regra e retirada. Não usar ausência de drops como refutação de atraso.
- Para memória: calcular dose pela política de folga existente; não reutilizar cegamente 545 MiB. Exigir contadores OOM/OOM-kill e cutoff 90%, com interrupção e limpeza comprovadas se atingido.
- Para kill: runtime/container exatos, evento único e read-back de identidade; amostragem cgroup deve pausar/retomar sem calcular taxas atravessando o reset do contador.
- Se um run falhar após submissão, preservar a tentativa e pausar. **Não reinjetar automaticamente.**
- Permitir, apenas se pré-aprovado, uma repetição de bloqueio anterior à intervenção: no máximo duas tentativas de preparação por célula, com submissão comprovadamente ausente, backoff >=60 s, carga parada, cleanup e novo preflight aprovados.
- Essa repetição pode elevar o número de tentativas, mas não o teto de seis intervenções. Após o segundo bloqueio, parar para revisão; não saltar a lacuna como se estivesse completa.
- Transporte e inferência têm retries próprios e limitados, sem novo caos. Retomada reconcilia os IDs existentes; estado ambíguo bloqueia.

## 5. Trabalho necessário antes de coletar

### F0 — Manifesto complementar e proveniência

Criar um novo perfil/protocolo explícito de complementação com allowlist das seis células. Não usar `--resume` nas campanhas antigas nem o script genérico de treino direcionado, que executa outra matriz.

O manifesto, gerado pelo executor, deve vincular:

- protocolo, seed e ordem; três papéis `train` e três `test`;
- hashes dos conjuntos-fonte, specs, executor, calibração, probes, imagens, modelo/Skill e política de observabilidade;
- `logical_cell_id`, `source_campaign_id`, `prior_run_ids`, `new_run_id`, `attempt`, `split`, `replacement_reason` e gates;
- diferença de ambiente/protocolo em relação às execuções antigas;
- fases UTC efetivas, estimadas e respectivos limites de incerteza;
- seleção por regras congeladas, nunca por acerto Skill/RAG.

No conjunto derivado, a célula usa o novo run; o histórico conserva o anterior excluído. `carts/container-kill` continua apontando para o treino de novembro, sem trazer o abortado de outubro.

### F1 — Contratos e gates

Reusar [controlled_campaign.py](../../experiment/eval/memory_hog/controlled_campaign.py) e [controlled_experiment.py](../../experiment/platform/backend/controlled_experiment.py), com suporte explícito a matriz complementar e política temporal. As validações atuais da matriz 35/70 não devem ser removidas: protocolos legados mantêm seu contrato.

Antes de qualquer carga, testar:

- dry-run gera exatamente seis células únicas e 3/3; não expande para 35/70;
- retomada não duplica submissões nem aceita alteração de perfil;
- frontend externo: série destination presente e source ausente não vira “sem tráfego”; reporter e operação são fixos entre fases; não há dupla contagem;
- `no_data`, backend error, saúde ruim e instabilidade têm motivos distintos;
- 58,511 s continua reprovando uma exigência de 60 s; gaps >20 s quebram sustentação; janelas não podem ser escolhidas pelo sinal;
- duas janelas de estabilização completas, baseline separada e prazo de warmup mensurável;
- recuperação orders usa o prazo efetivo de 600 s; expirado permanece abortado; operational recovery não altera o score;
- budget de carga/polling cobre estágios novos; retorno de erro e cleanup continuam explícitos.

Começar pelas suítes existentes de [campanha controlada](../../experiment/eval/memory_hog/tests/test_controlled_campaign.py) e [executor controlado](../../experiment/platform/tests/test_controlled_experiment.py). Esta entrega documental não executa esses testes nem modifica Python.

### F2 — Controles sem falha e aprovação

Os dois controles executados em `supplemental-20261108T214200871008Z` usaram a implementação antiga de preparação (warmup de 60 s, janelas retrospectivas de 30 s e baseline de 90 s), não o protocolo descrito aqui. Seus resultados de estabilidade em 600 s são evidência diagnóstica, mas não aprovam limites nem substituem novos controles com a preparação alinhada.

O executor agora aplica a política persistida no manifesto: warmup de 180 s, duas janelas sucessivas não sobrepostas de 60 s, timeout de preparação de 600 s e baseline de 180 s. Cada janela verifica a aresta de rede quando aplicável; falha quebra a sequência consecutiva. Os dois controles precisam ser repetidos sob essa política após aprovação da carga. Revisar relatórios de cobertura/estabilidade antes de qualquer injeção. Variante CPU/latência, se necessária, exige nova identidade de política antes de injetar, com comparação descritiva às regras antigas.

O executor [supplemental_campaign.py](../../experiment/eval/memory_hog/supplemental_campaign.py) pausa em `awaiting_calibration_review` depois dos dois controles e do preflight. Somente após revisar e aprovar ambos, iniciar treino com `--continue-training --resume <campaign_dir>`; controle ou preflight reprovado mantém as injeções bloqueadas.

## 6. Coleta e inclusão no treino

1. Executar ordens 1–3, uma carga por incidente, parando a carga ao final. Sem Qwen ou anotação concorrendo com a coleta.
2. Persistir specs, fases, séries brutas, limites/identidade cgroup, injetor/qdisc ou SIGKILL, eventos/probes, logs e traces. Dados privados do controlador ficam separados da evidência enviada ao modelo.
3. Fazer read-back MCP histórico de Prometheus, Loki e Tempo por run e fase, com argumentos, UTC, contagens, status e SHA-256. Erro de transporte bloqueia; `no_data` registra lacuna, não efeito ausente.
4. Exigir gates técnicos, recuperação experimental, cleanup e carga parada antes de selecionar cada novo run de treino. B pode permanecer pendente mesmo com coleta válida.
5. Depois da coleta e com carga parada, produzir A factual e B supervisionado para **somente os três treinos novos**, com Qwen 3.5 identificado, geração/Skill fixadas e orçamento auditável. O A não recebe alvo/falha privados; B recebe supervisão em contexto separado.
6. Reusar o procedimento de [training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py), acrescentando uma rota versionada para novos runs. A rota atual verifica 32 pares e uma adjudicação histórica específica; `--source-run-id` seleciona pendências existentes, não importa um incidente novo.
7. Revisar fidelidade serviço/unidade/fase, referências reais, duas famílias independentes e alternativas. Para rede, distinguir atraso de processamento; para restart, investigar motivo. Métricas derivadas de spans e os spans não viram duas provas independentes do mesmo fato.
8. Publicar um corpus derivado novo: preservar os 32 pares originais com seus IDs, hashes de conteúdo e estados; adicionar os três novos pares. Registrar `parent_base_sha256`, evidências e decisões novas; não recalcular conteúdo dos 32 para parecer recém-validado.
9. Recalcular metadados globais/hash do corpus e criar índice físico separado, compatível com a representação A existente. Conferir documentos, IDs, status e exclusão de testes. Não trocar o alias operacional sem aprovação.

**Aceite do treino:** 35 chaves serviço/falha distintas, 35 IDs fonte distintos e todos `split=train`; nenhum ID antigo excluído entra como substituto; cada par adicional tem proveniência e status fiel. Se B não for elegível, reportar a diferença entre cobertura bruta e cobertura recuperável; não liberar pendentes só para chegar a 35.

## 7. Coleta e inclusão no teste

1. Só após corpus congelado, executar ordens 4–6 com as mesmas políticas aprovadas. Os dois orders compartilham política de recuperação; os dois front-end/rede compartilham reporter/instrumentação.
2. Os três recebem `is_training=false` e `split=test`; não gerar pares de treino com eles, nem usar suas respostas para corrigir B dentro desta avaliação.
3. Criar manifesto de coorte derivada: preservar os 32 testes originais e adicionar os três novos. Os blocked/aborted antigos permanecem no histórico e denominadores de tentativa.
4. Resolver a coorte de múltiplas campanhas por manifesto validado, não copiando linhas para um CSV antigo.
5. Adaptar [test_base_rag_evaluation.py](../../experiment/eval/memory_hog/test_base_rag_evaluation.py) para o protocolo novo: a rota atual exige 32 pares e, por padrão, 32 testes de uma campanha. Manter a validação legada; o novo protocolo verifica exatamente os IDs/chaves declarados e, neste plano, 35/35.
6. Validar ausência de interseção entre todos os IDs de treino e teste, inclusive fontes dos pares. Igual combinação serviço/falha não é vazamento quando são incidentes diferentes.

**Aceite do teste:** 35 chaves únicas, 35 incidentes concluídos selecionados e papéis corretos, sem inclusão baseada na qualidade do diagnóstico. Validação privada do efeito deve ser reportada por caso; pendências antigas não são aprovadas por esta complementação.

## 8. Avaliação após complementação, sem reinjetar os 32 testes antigos

- Comparação global com o corpus ampliado exige **nova avaliação dos 35 testes**, não a soma de scores antigos de 32 com scores novos de três.
- Consultar novamente MCP histórico para baseline/fault/post de todos os runs da avaliação. Não importar respostas de outra avaliação como evidência atual.
- O contrato de precollection test-base usa **três consultas por fase** (Prometheus, Loki, Tempo), três fases: **35 × 9 = 315 consultas lógicas iniciais**, antes de follow-ups e retries. Confirmar esse contrato na implementação nova.
- Skill e RAG recebem a mesma política de evidência visível e modelo/geração fixados; nenhum ground truth antes de persistir a resposta. Contrabalancear ordem, contabilizar chamadas, tempo e tokens com limites explícitos.
- Duas condições sobre 35 incidentes geram 70 diagnósticos primários, **não 70 injeções**. Repetições de inferência são opcionais e devem ser pré-registradas; não selecionar a melhor resposta.
- Reportar execução, efeito privado, cobertura do corpus, elegibilidade, Hit@3, diagnóstico e sustentação causal separadamente. A comparação é desenvolvimento pós-hoc, estratificada por segmento/protocolo antigo versus complementar.
- Se quiser primeiro um smoke apenas dos três testes novos, registrá-lo separadamente; ele não atualiza o placar global de 35.

## 9. Entregáveis futuros e critério de encerramento

1. Perfil/protocolo complementar aprovado, dry-run e testes dos contratos.
2. Dois reports sem caos e decisão de calibração anterior à primeira injeção.
3. Ledger dos seis casos, incluindo bloqueios, abortos, submissões, cleanup e referências às tentativas anteriores.
4. Três A/B novos com evidências e decisões; corpus derivado e índice novo.
5. Manifestos derivados de treino/teste com 35 combinações cada, se todos os requisitos passarem.
6. Nova avaliação com coleta MCP própria, hashes e métricas por segmento, sem sobrescrever reports antigos.

**Parada:** não existe obrigação de produzir seis sucessos. Falha de segurança, saúde, medição, recuperação ou contrato interrompe o avanço. Se alguma célula continuar inelegível, preservar o déficit e voltar à revisão, sem alterar critérios após observar seu resultado.

**Decisão recomendada:** corrigir primeiro cobertura de front-end/rede e o recorte temporal de memória; ampliar preparação/baseline e prazo de recuperação orders; só ajustar pisos de variabilidade/latência com evidência dos controles sem falha e aprovação prospectiva. Não começar relaxando todos os limiares.

**Aditivo prospectivo autorizado:** para uma campanha nova, aplicar a política
`supplemental-user-latency-25pct-v1`, com `max(25% da média absoluta, 20 ms)`
somente em `user.latency_ms` nos controles de calibração e respectivas janelas
de preparação. Os controles já observados permanecem imutáveis e inelegíveis
para aprovação retroativa. A nova campanha deve executar ambos os controles
sob essa política; só poderá pausar para revisão se ambos e o preflight
passarem. A política não altera pisos de outras métricas, critérios de efeito,
recuperação, cobertura ou segurança, e não inicia injeções automaticamente.

**Aditivo posterior:** a política ativa para a próxima campanha passa a ser
`supplemental-all-stability-25pct-v1`: fator relativo de 25% para todas as
métricas de estabilidade nos controles de calibração e nas janelas de
preparação, mantendo os pisos de cada métrica e todos os gates não relacionados
à estabilidade. Isso substitui a política restrita a `user.latency_ms` somente
para a nova execução; os reports anteriores e seus estados não são
reclassificados. São necessários dois controles novos, seguidos de revisão
explícita antes de qualquer injeção.
