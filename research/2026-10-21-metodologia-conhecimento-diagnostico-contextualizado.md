# Metodologia de conhecimento diagnostico contextualizado para RCA autonoma

**Data:** 21/10/2026.  
**Status:** proposta de pesquisa acordada como direcionamento; nao e um metodo
implementado nem uma contribuicao cuja originalidade ja foi demonstrada.  
**Escopo inicial:** Sock Shop, experimentos de resiliencia e agentes com
ferramentas de observabilidade.  
**Plano associado:** [avaliacoes preliminares](./planos/2026-10-21-avaliacao-preliminar-metodologia.md).

## 1. Sintese da proposta

Investigar uma metodologia para transformar experimentos de resiliencia em
conhecimento diagnostico contextualizado, reutilizavel por agentes autonomos
e validado por evidencias.

A contribuicao pretendida nao e prescrever uma sequencia fixa de consultas,
construir uma equipe especifica de agentes ou demonstrar que RAG deve ser
usado. E estabelecer como produzir, representar, disponibilizar e avaliar
conhecimento que ajude a distinguir explicacoes concorrentes para incidentes.

**Pergunta central:**

> Como construir e utilizar conhecimento sobre a aplicacao e seus mecanismos
> de falha para melhorar a correcao, a sustentacao e a transferencia do
> diagnostico autonomo de incidentes?

A continuidade com a proposta inicial e direta: experimentos de resiliencia
produzem conhecimento operacional reutilizavel. A evolucao e deixar de tratar
esse conhecimento apenas como um conjunto de relatos semelhantes para RAG,
passando a investigar seu valor para discriminar mecanismos e reconhecer
limites de identificacao.

## 2. Motivacao e estado da evidencia

O laboratorio ja permite executar intervencoes, coletar telemetria e avaliar
diagnosticadores. O historico, entretanto, ainda nao demonstra superioridade
causal de RAG, skill ou multiagentes.

Na [comparacao de 20/10](../experiment/eval/memory_hog/out/controlled_campaign/controlled-20261018T221240026903Z/skill_vs_multiagent_evaluation.md),
sobre 29 runs concluidos, a skill acertou servico e tipo de falha,
separadamente, em 21/29; o multiagente acertou em 19/29 e 11/29.
As consultas foram live, houve uma execucao por metodo/run e o efeito da
intervencao foi validado em 21 casos. A skill nao confirmou causas; a unica
confirmacao multiagente errou servico e tipo.

Esses resultados motivam perguntas, nao validam esta metodologia. O material
atual deve ser tratado como exploratorio, inclusive porque ja influenciou
ajustes de prompts, ferramentas e contratos.

## 3. Tres camadas de conhecimento, sem confundir seus papeis

| Camada | Conteudo | Papel no diagnostico | O que nao demonstra |
|---|---|---|---|
| Contexto da aplicacao | Servicos, papeis, dependencias, recursos, timeouts, retries, filas, identidades e versoes | Define restricoes e explicacoes plausiveis | Que uma falha ocorreu ou se propagou |
| Conhecimento experimental | Intervencoes verificadas, mecanismos, sinais discriminativos, contrastes e limites de validade | Sugere hipoteses e observacoes uteis | Que o incidente atual tem a mesma causa |
| Evidencia do incidente atual | Metricas, logs, traces e eventos com janela, origem e cobertura | Sustenta ou enfraquece afirmacoes sobre o incidente | Causalidade apenas por correlacao ou coocorrencia |

O contexto define o que e plausivel; o conhecimento experimental sugere o
que procurar; a evidencia atual permite avaliar a explicacao.

Cada afirmacao contextual deve distinguir se e **declarada**, **observada**
ou **inferida**, com fonte, versao, validade temporal e limitacoes.
Dependencias declaradas e dependencias observadas podem divergir; o metodo
nao deve apagar esse conflito.

## 4. Unidade de conhecimento proposta: mecanismos e contrastes

O produto cientifico desejado nao e somente "um incidente parecido".
Uma unidade de conhecimento deve registrar:

- Condicoes da aplicacao e da carga em que foi produzida.
- Mecanismo efetivamente verificado, separado do nome da ferramenta de caos.
- Sintomas observados e sua ordem temporal, com incerteza de medicao.
- Explicacoes alternativas consideradas.
- Evidencias que ajudaram a distinguir essas explicacoes.
- Evidencias contrarias, sinais ausentes e lacunas de coleta.
- Runs e observacoes de suporte, incluindo controles.
- Condicoes de aplicabilidade, contraexemplos e limites de transferencia.
- Versao do extrator, revisao humana e proveniencia.

Exemplo conceitual: aumento da latencia de `orders` pode decorrer de
saturacao local, espera por dependencia ou retries. Um experimento contrastivo
compara mecanismos que produzem sintomas proximos e verifica que observacoes
permitem separa-los. Nao se deve escrever previamente uma regra universal
como "CPU alta implica causa local": ela pode ser efeito secundario.

Uma dependencia arquitetural nao e automaticamente uma aresta causal.
Uma recuperacao posterior a remocao da intervencao tambem nao basta,
isoladamente, para provar a explicacao: pode haver recuperacao espontanea,
mudanca de carga ou interferencia do ambiente.

## 5. Autonomia de investigacao e verificabilidade da conclusao

O agente pode escolher ferramentas, ordem de consultas, hipoteses, revisoes,
aprofundamentos e eventual delegacao. A metodologia nao exige uma sequencia
"metricas, depois logs, depois traces", nem uma topologia multiagente.

Permanecem externos ao agente:

- Permissoes operacionais, isolamento do gabarito e orcamento.
- Identidade e versao dos dados e do conhecimento disponibilizados.
- Registro de consultas, evidencias citadas, custos e erros.
- Criterios independentes para avaliar a conclusao.

Contexto e conhecimento podem ser consultados sob demanda. A comparacao
entre entrega inicial e consulta autonoma e uma extensao experimental, nao
uma obrigacao de implementar ambos antes do primeiro piloto.

Uma contagem de familias de telemetria nao substitui avaliacao de evidencia.
Metricas derivadas de spans e os proprios spans podem compartilhar a mesma
origem. Uma unica observacao especifica pode discriminar mais que varias
observacoes redundantes. A forca da evidencia depende da afirmacao avaliada,
do mecanismo, do alinhamento temporal e da confiabilidade da fonte.

Nao e necessario capturar raciocinio interno do modelo. A auditoria utiliza
acoes observaveis, argumentos das consultas, dados retornados, hipoteses
declaradas e justificativas verificaveis.

## 6. Metodo proposto

### 6.1 Construir contexto com proveniencia

Extrair de manifests, configuracoes, documentacao e topologia observada;
usar codigo seletivamente quando necessario para esclarecer comportamento.
Priorizar informacoes capazes de alterar uma decisao diagnostica:
dependencias, retries, timeouts, recursos e identidade dos componentes.

Comparar contexto revisado manualmente e contexto extraido automaticamente.
O primeiro e uma referencia curada, nao uma verdade perfeita ou um limite
superior garantido. A comparacao separa utilidade do contexto de qualidade
da extracao.

### 6.2 Produzir conhecimento experimental discriminativo

Executar intervencoes com baseline estavel, verificacao do mecanismo,
controle de carga, recuperacao e controles sem falha. Construir contrastes
entre explicacoes plausiveis para sintomas semelhantes.

Extrair unidades de conhecimento apenas de runs elegiveis e revisados.
Uma previsao do LLM nao pode ser promovida automaticamente a fato do corpus.

### 6.3 Disponibilizar o conhecimento ao diagnosticador

Fornecer interfaces para consultar contexto e recuperar conhecimento
experimental. Recuperacao textual, filtros e embeddings sao alternativas
de implementacao, nao a contribuicao cientifica em si.

A recuperacao deve usar somente informacoes acessiveis ao diagnosticador
naquele instante. O tipo de falha injetado no incidente-alvo nao pode
selecionar os exemplos; conhecimento de falhas passadas revisadas pode
conter mecanismos conhecidos, desde que anterior ao corte e fora do teste.

### 6.4 Avaliar diagnostico, sustentacao e limites

Separar localizacao, classificacao de mecanismo, sustentacao das afirmacoes,
capacidade de distinguir alternativas, confirmacao/abstencao e custo.

Quando duas hipoteses forem indistinguiveis sob a evidencia disponivel,
o metodo deve permitir resposta nao identificavel ou conjunto de candidatos.
O gabarito experimental pode conhecer a intervencao, mas isso nao garante
que um diagnosticador sem acesso ao injetor consiga identifica-la.

## 7. Questoes de pesquisa e hipoteses falsificaveis

| Questao | Hipotese a testar | Resultado que a enfraquece |
|---|---|---|
| Q1. Contexto operacional acrescenta valor? | Contexto pertinente melhora acerto ou sustentacao sob orcamento comparavel | Nao ha ganho consistente frente a telemetria e contrato de saida identicos |
| Q2. Conhecimento contrastivo supera relatos semelhantes? | Contrastes ajudam a distinguir mecanismos com sintomas proximos | Recuperacao de relatos ou agente sem historico oferece resultado equivalente ou melhor |
| Q3. O metodo resiste a contexto imperfeito? | Proveniencia e confronto com telemetria reduzem adesao a contexto incorreto | Contexto desatualizado induz erros sem deteccao ou abstencao util |
| Q4. O conhecimento transfere? | Parte do conhecimento ajuda fora dos servicos/configuracoes de origem | O ganho desaparece quando nomes, configuracoes ou aplicacao mudam |

No primeiro ciclo, priorizar Q1 e um teste limitado de Q2. Q3 e Q4 ampliam
a validacao; nao exigir um fatorial completo de todas as dimensoes.

## 8. Contribuicoes pretendidas e limites de originalidade

1. Metodo de construcao de conhecimento diagnostico contextualizado a
   partir de artefatos da aplicacao e experimentos controlados.
2. Representacao e disponibilizacao desse conhecimento para investigacao
   autonoma, sem depender de uma ordem fixa ou de um modelo especifico.
3. Evidencia empirica sobre quando o conhecimento ajuda, prejudica ou nao
   acrescenta valor, incluindo confiabilidade, custo e transferencia.

A plataforma e o corpus sao instrumentos e artefatos reprodutiveis que
sustentam essas contribuicoes. Sua integracao, isoladamente, nao garante
novidade de doutorado.

A revisao de literatura precisa confrontar RCA com agentes, conhecimento
arquitetural, diagnostico baseado em modelos, case-based reasoning,
experimentos contrastivos, proveniencia e avaliacao de explicacoes.
RCAgent, mABC e RCAEval sao pontos de partida, nao uma revisao exaustiva.
E necessario demonstrar que aspecto os metodos existentes nao representam,
nao discriminam ou nao validam.

## 9. Criterios de qualidade e ameacas a validade

- Preservar o mesmo contrato de saida ao variar contexto. A system card atual
  mistura vocabulario permitido e semantica; remove-la integralmente altera
  ambos e nao isola compreensao arquitetural.
- Distinguir acertar o alvo de caos, acertar o mecanismo e explicar a
  degradacao observada. Intervencao sem impacto e um resultado possivel.
- Nao reduzir mecanismo ao nome exato de uma falha Litmus.
- Avaliar risco de confirmacao e cobertura juntos. Abstencao universal nao
  e sucesso; ausencia de confirmacoes torna sua precisao indefinida.
- Evitar vazamento por nomes de arquivos, tags, resumos, embeddings,
  eventos do injetor e registros do avaliador.
- Separar variabilidade do modelo de variabilidade de incidentes.
- **Proposta supersedida em 03/11/2026:** nao pontuar RCA com evidencia
  congelada. Toda avaliacao deve consultar via MCP os datasources historicos do
  run. Dados coletados podem ser compartilhados apenas entre condicoes e
  repeticoes da mesma avaliacao; a proxima avaliacao exige nova coleta.
- Considerar familiaridade dos modelos com Sock Shop; testar renomeacao
  consistente como controle complementar, nao como substituto de outra aplicacao.
- Nao usar latencia do agente como MTTR sem avaliar resolucao operacional.
- Nao inferir causalidade da explicacao textual ou fidelidade ao raciocinio
  interno apenas porque as citacoes existem.

## 10. Regra de decisao cientifica

Se um agente mais capaz resolver os incidentes sem conhecimento adicional,
isso deve ser um resultado legitimo. A metodologia so deve ser mantida como
melhoria se demonstrar valor incremental em correcao, sustentacao, custo,
robustez ou transferencia, e nao apenas em um placar escolhido depois.

O objetivo e produzir conhecimento sobre as condicoes de utilidade do metodo,
inclusive resultados negativos, e nao defender previamente RAG, skill,
multiagentes ou um extrator particular.

## Referencias e rastreabilidade

- [Plano de avaliacao preliminar](./planos/2026-10-21-avaliacao-preliminar-metodologia.md).
- [Historico RAG e validadores](./abordagens/001-llm-rag-validadores.md).
- [Consolidacao ate 17/10](./progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md).
- [Comparacao com RCAEval](../docs/comparacao-cientifica-rcaeval-microservices-demo-2026-10-17.md).
- [RCAgent, 2024](https://doi.org/10.1145/3627673.3680016).
- [mABC, 2024](https://aclanthology.org/2024.findings-emnlp.232/).
- [RCAEval](https://github.com/phamquiluan/RCAEval).

Links para artefatos gerados localmente podem nao existir em um clone limpo.
