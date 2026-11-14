# Plano de implementacao: elegibilidade de B para http-status-code

**Data:** 08/11/2026. **Estado:** E1-E4 implementados e cobertos por testes
focados. O inventario registra SDK HTTP como `not_queried`, pois ainda nao ha
contrato de consulta executavel. E5 (piloto pareado) e E6 (reauditoria) nao
foram executados; nao houve nova pontuacao, publicacao de corpus ou injecao.

## 1. Objetivo

Converter a telemetria HTTP disponivel em procedimentos B reproduziveis,
factualmente corretos e elegiveis quando houver suporte causal suficiente.
Corrigir rejeicoes indevidas sem transformar acerto de classificacao em
confirmacao causal, nem aprovar casos somente para aumentar a cobertura RAG.

O mecanismo a investigar e o abort no proxy em chamadas destinadas ao servico,
nao necessariamente uma falha interna da aplicacao desse servico.
Preservar a identidade do chamador, proxy, destino pretendido e impacto.

## 2. Fundamentacao e limites

A auditoria online de 08/11 fez 42 consultas MCP historicas nos tres runs abaixo:
27 consultas de cobertura baseline/fault/post, 12 consultas direcionadas e
tres analises detalhadas de traces.

| Run de treino | Destino | Evidencia observada na auditoria |
|---|---|---|
| `run-abc3a42dc026` | shipping | Istio `500/FI`, orders -> FQDN shipping; span do chamador para `shipping/shipping` com 500 |
| `run-73da08fee5d3` | payment | Istio `500/FI`, orders -> FQDN payment; span do chamador para `payment/paymentAuth` com 500 |
| `run-29cf2c69c070` | user | Istio `500/FI`, front-end -> FQDN user; span do chamador para `user/login` com 500 |

Isso demonstra disponibilidade de evidencias antes nao aproveitadas; nao e
uma nova pontuacao de elegibilidade. As consultas atingiram limites de busca
de logs/traces e nao representam cobertura exaustiva.
Metricas SDK recuperadas nessas janelas pertencem ao front-end; instrumentacao
nova nao cria spans SERVER ou access logs ausentes nas execucoes antigas.

Achados de implementacao:

- [_causal_issues](../../experiment/eval/memory_hog/training_b_rebuild.py)
  desconsidera consultas HTTP Istio na contagem de fontes independentes.
  O probe do gate rejeitou Istio + trace e Istio + logs.
- A metrica HTTP especifica passa no gate dos tres casos quando vinculada
  corretamente; shipping nao vinculou a consulta ao suporte do B.
- [_investigation](../../experiment/eval/memory_hog/training_b_rebuild.py)
  pode forcar a consulta discriminante somente depois de encerrar o planejamento.
- O check `http` de
  [rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py)
  consulta Istio; a mencao a metricas SDK no prompt nao oferece, por si so,
  uma consulta SDK executavel.
- Auditoria de payment excedeu o limite de prompt; user repetiu consultas e
  recebeu comentarios positivos em campos reservados a violacoes.
- Os dois guias fornecidos pelo auditor RAG nos tres casos eram de CPU e
  latencia de rede, nao de HTTP.

Usar esses achados para testes de regressao. Toda nova avaliacao exige
consultas MCP novas; arquivos da auditoria nao substituem evidencia atual.

## 3. Sequencia e dependencias

| Etapa | Prioridade | Entrega | Dependencias |
|---|---|---|---|
| E0 | Preparacao | Protocolo, contratos e testes de regressao | Nenhuma |
| E1 | P0 | Independencia por proveniencia, consistente entre B/Skill/RAG | E0 |
| E2 | P0 | Investigacao HTTP dirigida por aresta e coleta do chamador | E0 |
| E3 | P0 | B com suporte vinculado e explicacao factual do mecanismo | E1, E2 |
| E4 | P1 | Auditoria robusta a contexto, duplicatas e contratos invalidos | E0 |
| E5 | Validacao | Piloto pareado dos tres runs, com protocolo unico | E3, E4 |
| E6 | Expansao | Reauditoria dos sete Bs HTTP e avaliacao exploratoria | E5 e revisao humana |

E1, E2 e E4 podem ser desenvolvidas separadamente. Integrar e validar o
conjunto antes de iniciar E5. Nao alterar o recuperador para compensar a
ausencia de guias HTTP antes de corrigir sua producao.

## 4. E0 - Congelar o contrato e reproduzir os bloqueios

1. Registrar hashes de codigo, corpus, modelo, prompts, opcoes e consultas.
   Preservar mudancas existentes no worktree e o corpus de referencia.
2. Criar testes pequenos para cada falha comprovada, sem chamadas externas:
   contagem de familias, vinculo HTTP, escolha do chamador, duplicatas,
   overflow de contexto e inconsistencias do revisor.
3. Definir separadamente: coleta concluida, evidencia suficiente,
   classificacao correta, causalidade confirmada e elegibilidade procedural.
4. Registrar que os casos/coortes ja foram examinados: resultados posteriores
   permanecem retrospectivos e exploratorios.

**Aceite:** cada bloqueio tem um teste que reproduz o comportamento atual;
nenhum teste exige elegibilidade por conhecer o rotulo.

## 5. E1 - Independencia por proveniencia, nao somente por ferramenta

Superficies: [training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py),
[rca_skill.py](../../experiment/platform/backend/rca_skill.py),
[rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py) e
[training_b_queries.py](../../experiment/eval/memory_hog/training_b_queries.py).

- Representar a origem da evidencia: Envoy/Istio, SDK, metricas de recursos,
  metricas derivadas de spans, logs ou traces. Reutilizar metadados existentes;
  adicionar somente a proveniencia necessaria, preservada na projecao.
- Compartilhar a regra de independencia entre o rebuild B e os validadores
  Skill/RAG. Hoje, o rebuild usa uma whitelist de ferramentas e Skill conta
  familias sem distinguir a derivacao das metricas.
- Permitir Istio + trace ou Istio + logs como combinacoes independentes
  candidatas; exigir evidencia utilizavel, identidade e fase correspondentes.
- Nunca contar duas metricas como duas familias. Metricas de spans + os
  mesmos traces nao podem ser a unica dupla de suporte.
- Proveniencia desconhecida permanece uma limitacao explicita; nao inferir
  independencia apenas porque a resposta veio de Prometheus.
- A contagem de fontes nao substitui a validacao da cadeia causal e do conteudo.

**Testes/aceite:** combinacoes independentes qualificadas passam no gate de
familias; derivacao duplicada, `no_data`, evidencia de outra aresta/fase e
IDs desconhecidos nao sustentam confirmacao. Preservar os gates dos demais
tipos de falha e a exigencia de explicar restarts relevantes de origem.

## 6. E2 - Investigar proxy -> chamador -> destino

Superficies: [istio_edges.py](../../experiment/platform/backend/analysis/istio_edges.py),
[telemetry_coverage.py](../../experiment/platform/backend/analysis/telemetry_coverage.py),
[rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py),
[training_b_queries.py](../../experiment/eval/memory_hog/training_b_queries.py),
[training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py).

1. Consultar HTTP discriminante antes de encerrar o planejamento. Extrair
   candidatas das arestas observadas, nao do rotulo privado.
2. Preservar codigo, flags, reporter, workload original, FQDN e destino
   resolvido. FQDN ambiguo ou incompatibilidade de namespace bloqueia atribuicao.
3. Para `FI`, consultar logs da aplicacao chamadora e seu proxy; buscar traces
   por servico chamador e erro HTTP, e analisar spans CLIENT candidatos.
4. Vincular erro, endpoint, origem/destino e janela. Nao exigir span SERVER
   do alvo quando a chamada pode ter sido abortada antes de chegar a ele.
5. Oferecer consulta SDK explicita no inventario executavel, reutilizando
   metricas realmente disponiveis, labels, unidades e filtros de probes.
   Nao misturar histogramas SDK com taxas de requisicoes Istio.
6. Usar uma orientacao HTTP versionada comum a Skill, RAG e rebuild, sem
   reescrever a Skill global ou mudar silenciosamente seu hash. Manter o
   comportamento anterior selecionavel para a comparacao.
7. Registrar truncamento e retornar ao planejamento apos a nova evidencia;
   preservar limites de chamadas, tempo e consultas unicas.

**Testes/aceite:** `unknown` com FQDN valido resolve a aresta; o chamador correto
e consultado; probes e erros comerciais de baseline nao confirmam injecao;
ausencia de servidor nao e tratada como prova; consulta SDK disponivel e
executavel, ou ausencia registrada explicitamente.

## 7. E3 - Construir B factual, vinculado e auditavel

Superficie principal:
[training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py).

- Exigir passo observado para a metrica discriminante e para cada evidencia
  usada no suporte, contradicao, hipotese ou alternativa.
- O passo de confirmacao cita o mesmo ID HTTP presente no diagnostico.
  Corrigir omissoes por revisao explicita; nao inserir causa ou suporte
  automaticamente a partir do rotulo.
- Explicar: proxy respondeu -> chamada para o destino falhou -> chamador
  propagou impacto. Nao descrever automaticamente erro interno no alvo.
- Comparar pelo menos duas alternativas: erro da aplicacao/dependencia e
  problema de conexao/latencia. Descartar somente com evidencias discriminantes.
- Tratar `FI` como sinal de abort do proxy, nao prova de RabbitMQ, banco,
  OOM ou uma configuracao especifica nao observada.
- Considerar a janela de `rate` e sua persistencia no post; nao declarar
  falha ainda ativa ou recuperacao instantanea so por esse sinal.

**Aceite:** retirar uma evidencia critica invalida seu suporte; um B com
erro propagado generico, mecanismo inventado, `no_data` usado como refutacao
ou cadeia invertida continua pendente, mesmo acertando servico/falha.

## 8. E4 - Robustez da auditoria e revisao

Superficies: [training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py),
[training_b_queries.py](../../experiment/eval/memory_hog/training_b_queries.py) e
[evidence_projection.py](../../experiment/platform/backend/evidence_projection.py).

- Medir o prompt completo serializado, incluindo schema, historico e feedback,
  antes da chamada. Reservar espaco para as tentativas de correcao.
- Reutilizar a compactacao estruturada; reduzir redundancia e historico
  opcional antes de remover fatos discriminantes. Preservar IDs, flags,
  identidades, fases, valores e unidades; registrar omissoes.
- Quando fatos protegidos nao couberem, dividir a revisao em escopos
  justificaveis ou falhar explicitamente. Nao apenas elevar o limite,
  cortar JSON ou substituir resultados por resumos antigos.
- Remover consultas ja executadas do inventario. Uma tentativa duplicada
  deve apontar para a evidencia existente, nao bloquear todas as consultas
  novas nem ser contabilizada como nova execucao.
- Validar coerencia do revisor: `pass` com `unsupported_claims` ou
  `unsupported_evidence_references` nao vazios exige correcao explicita.
  Nao limpar listas pelo sentido aparente de uma frase.
- Separar erro de coleta, erro de contrato, limite de contexto e evidencia
  insuficiente nos artefatos e no resumo da rodada.

**Testes/aceite:** reproduzir o prompt de 153.185 caracteres; nenhum envio
ultrapassa 150.000, inclusive apos feedback. Fatos protegidos permanecem
intactos ou ha erro explicito. Duplicatas nao encerram uma investigacao que
tenha consultas novas validas. O revisor nao aprova contratos contraditorios.

## 9. E5 - Piloto pareado e criterio de promocao

Executar os tres runs de treino da secao 2, com saidas novas e sem publicar
corpus parcial. Usar `--pending-only` e os mesmos `--source-run-id` para
ambas as condicoes.

- Controle: politica/codigo anterior identificavel.
- Intervencao: E1-E4 integradas.
- Mesmo modelo Qwen 3.5, corpus e recuperador, parametros de geracao,
  budgets e modo de auditoria RAG. Nao usar `factual-a-only` somente em um
  dos casos e agrega-lo como se fosse o mesmo experimento.
- Fixar explicitamente temperatura/seed quando suportadas, encaminha-las
  pelo fluxo e registra-las; nao presumir que o rebuild herda as opcoes
  do avaliador held-out.
- Coletar MCP historico novo nas tres fases/fontes para cada condicao, ou
  usar `live_shared` somente se o fluxo garantir suporte, selo e paridade.
  Nao reutilizar cases salvos de outra avaliacao.
- Backend com erro bloqueia pontuacao; `no_data` e truncamento sao limitacoes,
  nao respostas substituidas por snapshots. Consultas dinamicas adicionais
  tambem ficam registradas e respeitam o mesmo budget.

Medir por caso: cobertura, consultas HTTP/chamador executadas, analise de
trace, vinculo dos IDs, contrato/revisao concluida, suporte independente,
factualidade, status causal, elegibilidade, tempo, tokens e gates restantes.

**Promocao:** testes locais passam, coleta tem comprovantes verificaveis,
nenhuma nova afirmacao causal sem suporte e revisao humana confirma os
procedimentos. Melhorar elegibilidade e um resultado a medir, nao uma meta
obrigatoria de 3/3. Se algum caso permanecer pendente, apresentar o gate e
a evidencia faltante antes de expandir.

## 10. E6 - Expansao e avaliacao do beneficio

1. Reauditar os sete Bs HTTP de treino depois de revisar o piloto.
2. Preservar pendentes e falhas; nao publicar subset como corpus completo.
   Seguir o fluxo completo existente para congelar novo corpus, com
   proveniencia verificavel e indexacao somente apos aprovacao.
3. Medir efeito no teste com quatro condicoes se houver avaliacao Skill/RAG:
   Skill antiga/nova orientacao e RAG antiga/nova orientacao no mesmo corpus.
   Avaliar o novo corpus em etapa separada para nao misturar os efeitos.
4. Consultar MCP novo e manter budgets iguais. Reportar elegibilidade do
   treino separada de Hit@3 e acuracia conjunta do teste.
5. A coorte existente continua exploratoria. Novos incidentes independentes,
   caso necessarios, exigem autorizacao separada para execucao de falhas.

## 11. Validacao local e documentacao

Usar primeiro os testes existentes das superficies alteradas:

```bash
./.venv/bin/python -m pytest \
  experiment/eval/memory_hog/tests/test_training_b_rebuild.py \
  experiment/platform/tests/test_rca_skill.py \
  experiment/platform/tests/test_rca_knowledge.py \
  experiment/platform/tests/test_evidence_projection.py
```

Selecionar testes diretamente relacionados durante o desenvolvimento; executar
o conjunto acima na integracao e ampliar somente se houver regressao ou mudanca
em API/evaluador. Incluir novos testes nos arquivos existentes quando couber.

Atualizar [README do rebuild](../../experiment/eval/memory_hog/README.md) com
proveniencia, estrategia HTTP, limites e novo protocolo.
Se houver novas opcoes de API ou avaliador, atualizar sua documentacao e
testar encaminhamento, defaults e persistencia.

**Fora do escopo:** migrar novamente os dados, inventar telemetria historica,
relaxar o numero de fontes, promover Bs por rotulo, trocar modelo/recuperador,
modificar a politica CPU/memoria ou reiniciar/injetar falhas no cluster.
