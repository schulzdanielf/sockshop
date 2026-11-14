# Orientacao compartilhada de CPU/memoria: analise e plano experimental

**Data:** 08/11/2026. **Estado:** implementado e validado por testes locais
focados (144 aprovados). Nenhuma coleta MCP, inferencia de modelo, reconstrucao
de B ou nova injecao foi executada.

## 1. Objetivo e decisao recomendada

Introduzir a mesma orientacao discriminativa sobre CPU e memoria nos dois
metodos, Skill e RAG, e medir seu efeito sem mudar simultaneamente o corpus,
os guias B, o recuperador ou os criterios de confirmacao causal.

A primeira intervencao deve ser **um suplemento de prompt compartilhado,
opcional e versionado**. Nao inserir a regra somente nos Bs: isso concede
informacao adicional apenas ao RAG e mistura o efeito da orientacao com o da
reconstrucao dos guias. Nao editar a Skill global nesta etapa: seu hash esta
vinculado ao corpus congelado.

O desenho recomendado e pareado, com quatro condicoes:

| Condicao | Metodo | Orientacao | Corpus/recuperador |
|---|---|---|---|
| S0 | Skill | Ausente | Nao recupera guias |
| S1 | Skill | `cpu-memory-v1` | Nao recupera guias |
| R0 | RAG | Ausente | Corpus atual e recuperador v6 |
| R1 | RAG | `cpu-memory-v1` | Os mesmos de R0 |

Esta proposta nao autoriza inferencias, coleta operacional ou novas injecoes.
Implementacao e execucao experimental sao etapas posteriores separadas.

## 2. Fundamentacao empirica e limites

A comparacao anterior usou os registros B reconstruidos do
[corpus v6](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261107T215646147881Z/rag_base.json).
Os 13 runs de recursos examinados constam em `training_run_ids`: sete
`cpu-hog` e seis `memory-hog`. Nao sao os 14 testes de recursos do relatorio
Skill/RAG, que incluem sete casos de cada falha.

Os valores foram extraidos do resultado persistido de `get_run_golden_signals`
referenciado por N001 em cada B, e nao da interpretacao narrativa do modelo.
Resumo do aumento da media de memoria entre baseline e fault:

| Servico | `cpu-hog`: delta memoria (p.p.) | `memory-hog`: delta memoria (p.p.) |
|---|---:|---:|
| orders | +1,35 | +15,42 |
| payment | +3,04 | +29,21 |
| shipping | +0,76 | +23,72 |
| user | +3,72 | +35,81 |
| front-end | +2,08 | Nao ha B dessa falha nesta comparacao |
| carts | +0,16 | +20,96 |
| catalogue | +1,12 | +32,14 |

Nos 13 registros, a media de CPU tambem aumenta. Portanto, o contraste
observado nao e "memoria cresce versus nao cresce", mas **crescimento
expressivo versus variacao pequena**. A separacao observada nesta amostra nao
estabelece um limiar validado ou um classificador universal.

Identidade das metricas consultadas:

- `cpu_saturation_pct`: uso de CPU por pod, com `rate(...[1m])`, dividido
  pela quota agregada de CPU.
- `memory_saturation_pct`: working set agregado por pod dividido pelos
  limites positivos de memoria.
- Os deltas acima sao pontos percentuais de saturacao, nao bytes nem
  variacao percentual relativa. Exemplo: 50% para 53% representa +3 p.p.
- A agregacao por pod pode incluir outros containers. Nao reinterpretar
  automaticamente esses valores como heap, RSS ou consumo exclusivo da
  aplicacao. Confrontar a identidade realmente disponivel na consulta.
- Working set e quota nao explicam sozinhos leak, GC, OOM ou qual processo
  produziu o aumento. CPU e memoria pertencem a mesma familia de evidencia.

Esses resultados persistidos fundamentam o desenho da intervencao; nao
constituem coleta atual para pontuar uma nova RCA.

Embora a assinatura tenha sido descrita usando Bs de treino, a motivacao
tambem ocorreu depois de inspecionar erros da coorte de teste. Registrar essa
adaptacao: a coorte existente nao recupera independencia por se congelar o
prompt agora. A analise nela deve continuar exploratoria.

## 3. Texto candidato da politica

Congelar o texto completo antes das novas inferencias:

> Compare CPU e memoria do mesmo componente entre baseline, fault e, quando
> disponivel, post, respeitando identidade de servico/pod/container, unidade,
> limites, agregacao e cobertura. Neste ambiente experimental, aumento
> expressivo de memoria acompanhado de aumento de CPU favorece a hipotese
> `memory-hog`; aumento expressivo de CPU com memoria aproximadamente estavel,
> ou com variacao pequena frente ao baseline, favorece `cpu-hog`. Pequenos
> aumentos de memoria nao excluem `cpu-hog`. Essa orientacao e uma hipotese
> discriminativa, nao uma regra deterministica: confronte carga, alocacao/GC,
> throttling e outras explicacoes com evidencias do incidente atual. Ausencia
> de dados de memoria nao significa estabilidade. Nao infira leak, GC ou OOM
> apenas desses sinais. CPU e memoria sao uma unica familia de evidencia e
> nao bastam para confirmar causalidade. Use somente os tipos de falha
> permitidos no contexto; mantenha `inconclusive` quando faltar corroboracao.

Nao incluir no prompt os valores desta amostra, IDs dos runs, servicos
injetados, resultados B, ground truth, elegibilidade ou respostas anteriores
do teste. O suplemento deve ser identico para todos os casos e metodos.

Nao estabelecer agora uma regra como "delta memoria > X significa
memory-hog", nem escolher X no intervalo que separou os 13 exemplos.
Uma versao quantitativa futura requer conjunto de desenvolvimento separado,
variabilidade baseline, unidades/limites comparaveis, tratamento explicito
de dados ausentes e congelamento anterior a um teste independente.

## 4. Pontos de integracao encontrados

| Superficie atual | Achado | Mudanca planejada |
|---|---|---|
| [rca_options.py](../../experiment/platform/backend/rca_options.py) | `KnowledgeOptions` herda `RCABenchmarkOptions` | Declarar a politica comum, com enum estrito e padrao desativado |
| [rca_skill.py](../../experiment/platform/backend/rca_skill.py) | Carrega Skill e referencias; conserva instrucoes estaticas nas chamadas | Adicionar o suplemento ao prompt de sistema antes do planejamento |
| [rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py) | RAG inicia com Skill, mas `ModelSession.generate` monta outro sistema para etapas posteriores | Passar a politica a Skill inicial e ao sistema das etapas RAG |
| [api.py](../../experiment/platform/backend/api.py) | Endpoint Skill encaminha parametros individualmente | Encaminhar politica explicitamente e persistir sua identidade |
| [rag_campaign.py](../../experiment/eval/memory_hog/rag_campaign.py) | O corpo de `original` e reconstruido antes de chamar `skill-rca` | Preservar a politica nos dois corpos HTTP |
| [test_base_rag_evaluation.py](../../experiment/eval/memory_hog/test_base_rag_evaluation.py) | Prepara dois metodos, valida capacidade da API e sela resultados antes do scoring | Congelar politica por braco; validar paridade e completar matriz antes do scoring |
| [README do harness](../../experiment/eval/memory_hog/README.md) | Documenta pinagem e coleta historica online | Documentar intervencao, desenho, limites e comandos implementados |

Alterar apenas a Skill inicial nao e suficiente: a sintese RAG possui um
prompt distinto. Inserir a orientacao em um guia recuperado tambem nao garante
acesso simetrico: a recuperacao pode nao retornar esse guia.

`load_skill()` calcula hash sobre a Skill e suas referencias. A preparacao
da avaliacao rejeita corpus com hash diferente da Skill ativa. Por isso,
preservar esses arquivos e registrar um hash separado do suplemento.

## 5. Plano de implementacao tecnica

### Etapa 1 - Politica comum, sem modificar o comportamento legado

1. Adicionar `resource_signature_policy` a `RCABenchmarkOptions`, com valores
   `none` e `cpu-memory-v1`, e padrao `none`.
2. Centralizar texto, identificador e SHA-256 em um unico helper compartilhado.
   Nao aceitar texto arbitrario fornecido por caso ou construir a orientacao
   a partir do rotulo do incidente.
3. Para `none`, nao adicionar nenhuma mensagem nem alterar o texto existente.
   A semantica de classificacao e os gates causais continuam iguais.
4. Auditar os usos de `options.model_dump()` em fingerprints de API, pares
   e persistencia. Um novo campo com valor padrao pode mudar hashes e impedir
   leitura de resultados antigos. Preservar a representacao legada quando a
   politica estiver ausente/desativada; incluir campo e hash quando ativada.
   Reusar a serializacao comum, sem excecoes dispersas.
5. Rejeitar versao desconhecida e incompatibilidade de hash explicitamente.
   Nao continuar silenciosamente sem a orientacao solicitada.

### Etapa 2 - Entrega simetrica e auditavel

1. Encaminhar a opcao nos endpoints e no runner, inclusive no corpo reduzido
   de `original`.
2. Entregar exatamente o mesmo suplemento na Skill autonoma e na Skill
   inicial do RAG.
3. Entregar o suplemento nas etapas subsequentes de `ModelSession.generate`,
   incluindo selecao de checks e diagnostico final. Assim ele nao desaparece
   depois da recuperacao.
4. Validar que compactacao, reparo de JSON e retries preservam o suplemento.
   Nao inserir copias cumulativas a cada rodada.
5. Persistir, quando ativada, `resource_signature_policy`, seu texto/hash
   congelado e auditoria dos prompts efetivamente enviados por etapa. Reusar
   mecanismos de persistencia/redacao existentes; nao registrar credenciais.
6. Nao permitir transferencia de inicial/resultados/cache de um braco para
   outro. Identidade da condicao deve participar do fingerprint e dos
   identificadores de avaliacao externa.
7. Nao alterar filtros de elegibilidade, validacao do diagnostico, corpus,
   limites de evidencia, top-k ou pesos do recuperador nesta intervencao.

### Etapa 3 - Preparacao e orquestracao das quatro condicoes

Reusar os metodos existentes `original` e `exploratory_rag`, sem criar copias
de seus executores. Representar a politica como uma dimensao adicional:
dois bracos, cada um com os mesmos dois metodos.

1. Estender a preparacao para selecionar explicitamente a politica e gerar
   uma comparacao com dois bracos imutaveis. Manter a preparacao antiga
   compativel quando essa comparacao nao for solicitada.
2. Criar um manifesto pai que vincule configs/manifestos dos bracos, coorte,
   politica, hashes e plano de analise. Cada braco recebe ID externo unico.
3. Antes da inferencia, validar via capacidade/OpenAPI os dois caminhos
   da API, nao apenas a existencia do campo no schema base. Um servidor
   antigo ou um endpoint que descarte a opcao deve bloquear a execucao.
4. Separar orquestracao de diagnostico e scoring usando os passos atuais.
   Nao executar o fluxo completo de scoring de um braco antes de concluir
   os diagnosticos dos demais.
5. Congelar uma ordem balanceada por caso e condicao, com seed de
   agendamento distinta da seed de geracao. Registrar a ordem antes de
   executar; evitar sempre realizar todo o controle antes do tratamento.
   Manter execucao sequencial das inferencias para nao adicionar um
   confundidor de concorrencia/carga do servidor.
6. Selar a lista de respostas e hashes das quatro condicoes antes de acessar
   rotulos para scoring. A execucao nao pode reajustar o texto no meio.
7. Na coorte atual, planejar 128 diagnosticos (32 x 4), sem contar retries.
   Nao reaproveitar respostas da v6 como S0/R0 da nova comparacao.
8. Preservar artefatos anteriores e limitar retomadas a respostas completas
   da mesma avaliacao/condicao, verificadas por hash e config.

### Etapa 4 - Telemetria e gates experimentais

1. Consultar novamente os datasources historicos via MCP para Prometheus,
   Loki e Tempo em baseline, fault e post de todos os runs.
2. Caminho inicial mais simples: pre-coleta nova para cada braco, usando os
   gates atuais, e consultas historicas online durante os diagnosticos.
   Nao atribuir a pre-coleta o papel de evidencia congelada da inferencia.
3. Manter janelas, namespace e ferramentas disponiveis iguais. Os metodos
   podem escolher follow-ups distintos; registrar essas diferencas e a
   cobertura, sem alegar que receberam amostras identicas.
4. Se no futuro houver coleta `live_shared`, ela so pode ser compartilhada
   apos coleta MCP nova e selagem dentro desta mesma avaliacao. Nao usar
   snapshots ou bundles de outra avaliacao como fallback.
5. Erros de backend ou falta de comprovante MCP bloqueiam pontuacao.
   `no_data`, truncamento e cobertura parcial sao limitacoes explicitas,
   nao evidencias de que o recurso permaneceu estavel.
6. Manter rastreabilidade por avaliacao/braco/condicao/caso na telemetria
   de progresso. Publicar escores finais apenas apos relatorio validado.

## 6. Controles e plano de analise a congelar

### Variaveis fixas

- Mesmo modelo Qwen 3.5, temperatura 0.1 e seed 20261107 nas quatro condicoes.
  Registrar tambem identidade do modelo/runtime; seed nao garante
  determinismo entre ambientes diferentes.
- Mesmo corpus e hashes, elegibilidade procedural, v6, top-3 e parametros de
  retrieval em R0/R1. O suplemento pode mudar A ou consultas escolhidas pelo
  modelo; nao declarar que o ranking resultante sera necessariamente igual.
- Mesma coorte e janelas historicas; sem IDs de treino/teste ou rotulos como
  evidencias do modelo.
- Mesmo orcamento para S0/S1 e mesmo orcamento para R0/R1. Skill e RAG ja tem
  pipelines/orcamentos diferentes: declarar isso, nao alegar equivalencia de
  custo nem mudar simultaneamente os limites para iguala-los.
- Mesmos gates de confirmacao, criterio de acerto e politica de retries.
  O custo adicional do suplemento faz parte do tratamento; nao compensar
  aumentando o teto somente nos bracos informados.

### Desfechos e contrastes

Desfecho primario: acerto conjunto de servico e falha na coorte completa,
preservando a metrica principal do experimento atual.

Contrastes primarios pareados: S1-S0 e R1-R0. Reportar contagens
melhorou/piorou/ambos corretos/ambos errados e delta em pontos percentuais.
Usar teste exato de McNemar para os dois contrastes, com ajuste de Holm;
registrar convencao e nivel de significancia antes da execucao. A amostra
pequena e a natureza adaptativa limitam a interpretacao dos p-valores.

Desfechos secundarios, previamente definidos:

- Acerto conjunto e matriz de confusao nos tipos `cpu-hog`/`memory-hog`,
  mantendo os 14 casos de teste atuais, e nao os 13 Bs de treino, como
  denominador desse subconjunto.
- Acuracia de servico e falha separadamente, macro-F1 e resultados por tipo,
  sem selecionar apenas os casos que melhoraram.
- Diferenca RAG-Skill sem orientacao e com orientacao; interacao descritiva
  `(R1-R0) - (S1-S0)`. Nao confundir ganho da orientacao com ganho de retrieval.
- Status confirmado/inconclusivo, erros, consultas, cobertura, tokens,
  tempo e esgotamento de orcamento por condicao.
- Hit@3/MRR do RAG como desfechos secundarios, nunca como prova de acerto.

Reportar intervalos de confianca pareados de 95% para os deltas, por
bootstrap de runs com seed e numero de reamostragens pre-registrados.
Reamostrar o run com suas quatro respostas juntas; nao tratar respostas,
spans ou rodadas do mesmo incidente como unidades independentes.

Classificacao correta nao prova causalidade. Afirmar "confirmacao indevida"
exige revisao das evidencias por protocolo causal, idealmente cega quanto
ao braco e orientacao; nao usar apenas acerto de rotulo como essa revisao.
Registrar isso como desfecho de seguranca adicional, se a revisao for feita.

Falha operacional nao vira `inconclusive` nem e removida silenciosamente.
Aplicar o gate de completude atual, bloquear o escore da matriz incompleta
e registrar falhas/retries/custo. Politica de abandono ou exclusao deve ser
definida antes de observar resultados.

## 7. Testes e criterios de aceite

Reusar as suites existentes:

- [testes do executor Skill](../../experiment/platform/tests/test_skill_rca_evaluation.py);
- [testes da API Skill](../../experiment/platform/tests/test_rca_skill_api.py);
- [testes do RAG](../../experiment/platform/tests/test_rca_knowledge.py);
- [testes do runner](../../experiment/eval/memory_hog/tests/test_rag_campaign.py);
- [testes da avaliacao](../../experiment/eval/memory_hog/tests/test_test_base_rag_evaluation.py).

Casos obrigatorios:

1. Politica ausente/desativada preserva prompts, fingerprints legados e
   leitura de resultados antigos; configs da v6 continuam validas.
2. Opcao ativada chega a ambos os endpoints e a todas as etapas pertinentes.
   Texto e hash efetivamente entregues sao iguais, mesmo com compactacao,
   reparo e retry. Versao desconhecida ou paridade ausente bloqueia.
3. Nenhum rotulo/servico injetado e utilizado para construir o suplemento.
   A orientacao nao expande `possible_fault_types`.
4. Dados ausentes de memoria nao sao convertidos em zero ou estabilidade.
   CPU+memoria nao burlam o gate de familias independentes ou de restart.
5. Cache/fingerprint separa as condicoes; corpus, hash da Skill e guias B
   permanecem intactos.
6. Manifesto da matriz valida quatro respostas por run, ordem congelada,
   mesma coorte/config invariantes e comprovantes MCP novos.
7. Scoring nao ocorre antes da selagem completa; pareamento usa run, nao
   posicao na lista; testes sinteticos verificam deltas, denominadores,
   discordancias, confusao e matriz incompleta.
8. Preparacao e testes locais nao chamam modelo, MCP ou injecao real.

Na implementacao, os seletores relacionados foram executados:

```bash
./.venv/bin/python -m pytest -q \
  experiment/platform/tests/test_rca_skill_api.py \
  experiment/platform/tests/test_rca_knowledge.py \
  experiment/platform/tests/test_resource_signature.py \
  experiment/eval/memory_hog/tests/test_rag_campaign.py \
  experiment/eval/memory_hog/tests/test_test_base_rag_evaluation.py
```

Resultado: **144 testes aprovados**. As regioes alteradas do harness passaram
na verificacao Black com `--line-ranges`, e `git diff --check` passou. A
checagem global de isort ainda aponta ordenacao anterior a esta mudanca em
`rca_skill.py`, `rca_knowledge.py` e `api.py`; os imports novos foram mantidos
sem reordenar esses blocos preexistentes.

## 8. Limites de publicacao e etapas futuras

Na coorte atual, publicar como **ablacao exploratoria pareada de uma
orientacao desenvolvida apos analise dos resultados**, sem alegar avaliacao
cega independente, generalizacao ou melhoria ja comprovada.

Para teste confirmatorio: congelar politica, parametros e analise; definir
amostra/tamanho antes da coleta; reservar novos runs nao inspecionados,
separados por episodio de injecao e sem janelas sobrepostas de incidentes;
executar as quatro condicoes sem reajustes guiados pelos resultados.
Repeticoes em sementes adicionais, se previstas, devem ser identicas nos
bracos e analisadas sem pseudo-replicacao.

Reconstruir Bs com uma nova metodologia e uma etapa separada: requer novo
corpus/versionamento, nova validacao procedural e outro contraste. A
intervencao compartilhada aqui nao transforma guias pendentes em elegiveis.

Nao atribuir automaticamente efeitos de memoria a GC/leak, nem generalizar
a assinatura de um injetor de recursos para todos os incidentes reais.
O resultado esperado desta primeira implementacao e **paridade verificavel
da informacao e mensuracao auditavel de seu efeito**, nao acerto forcado.
