# Avaliacao isolada: skill, contexto e experiencias do mesmo workflow

> **Politica obrigatoria (03/11/2026):** toda avaliacao de RCA deve obter a
> evidencia do run consultando online os datasources historicos via MCP.
> Evidencia pre-coletada de outra avaliacao, modo `offline` e importacao de
> pacotes de outra execucao sao proibidos para pontuacao. `live_shared` pode
> reutilizar a coleta feita por MCP somente entre condicoes/repeticoes da mesma
> avaliacao; uma nova avaliacao deve coletar novamente. Erros nunca fazem
> fallback para snapshots.

## Objetivo e escopo

[campaign_knowledge.py](./campaign_knowledge.py) implementa uma avaliacao
retrospectiva propria para a matriz media de 35 celulas, sem alterar o
workflow de caos, o banco operacional, o RAG anterior ou a skill em uso.

O treinamento e construcao supervisionada de uma base de experiencias:
**nao altera os pesos do modelo**.

## Historico qualificado e efeito dos rotulos

O modo padrao `validated_only` publica somente experiencias apoiadas pelo
modelo e pelo gate. O modo opt-in `qualified_examples` aceita rascunhos
revisados mesmo quando o gate falha ou o modelo nao apoia o mecanismo, mas
nao os apresenta como causas confirmadas. Cada item preserva separadamente
o servico-alvo/tipo de falha experimental, o resultado do gate e a avaliacao
do modelo; `causal_confirmation` permanece `false`.

Essa base e deliberadamente **rotulada**: a segunda chamada de `train`
recebe o rotulo experimental para confronta-lo com observacoes cegas. Na
condicao `skill_card_history`, os exemplos recuperados tambem expoem a
atribuicao experimental e os estados de qualificacao. Portanto, um ganho
nessa condicao mede o efeito de exemplos historicos rotulados, podendo
incluir priming pelos rotulos; nao isola o efeito do raciocinio causal. Os
rotulos dos casos de teste continuam fora do prompt e sao consultados apenas
apos a resposta para pontuacao. A revisao em lote autoriza incluir o caso
como experiencia qualificada, nao atesta individualmente sua causalidade.

Por caso de teste, o recuperador fornece no maximo tres itens de suporte,
mesmo que os 14 casos de suporte sejam publicados. O diagnostico ainda deve
se apoiar na evidencia atual, nao nos rótulos dos casos passados.

## Protocolo recomendado: comparacao pareada com nova coleta unica

Para isolar o efeito de contexto, use `live_shared`. A tentativa anterior
`linked32-live-v1` fica preservada, inclusive sua falha referencial; ela nao
e retomada com codigo/protocolo novo.

```bash
CAMPAIGN=experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T145517920531Z
CONTINUATION=experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T204641889813Z
OUT=experiment/eval/memory_hog/out/knowledge_evaluation/controlled-20261021T145517920531Z/linked32-shared-v3

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge init \
  --campaign-dir "$CAMPAIGN" --continuation-dir "$CONTINUATION" \
  --output-dir "$OUT" --evidence-mode live_shared --trace-limit 5 --max-repairs 1 \
  --allow-missing-trace-details

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge freeze \
  --output-dir "$OUT"

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge collect \
  --output-dir "$OUT" --allow-telemetry

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge train \
  --output-dir "$OUT" --allow-inference \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --max-input-chars 400000 --max-output-tokens 8192
```

`collect` consulta uma unica vez as janelas historicas de cada um dos 32
runs e salva as respostas brutas e os pacotes. So depois de coletar TODOS
os casos e selar `shared_evidence.json` se permite inferencia. A etapa
retoma coletas interrompidas sem consultar novamente os casos ja salvos.
Nao ha fallback para a telemetria original. Erros de backend ficam
registrados e bloqueiam inferencia; ausencia de dados fica explicita na
cobertura e nao e tratada como ausencia de falha.

Na primeira coleta compartilhada (`linked32-shared-v2`), tres detalhes de
traces de shipping/container-kill (`case-003`, teste) retornaram HTTP 404,
inclusive em verificacao posterior. Nenhuma inferencia foi iniciada nessa
versao. O operador autorizou manter os 32 runs com a lacuna identica nas tres
condicoes; a versao `linked32-shared-v3` registra essa decisao antes dos testes.
`--allow-missing-trace-details` aceita SOMENTE HTTP 404 de
`tempo_analyze_trace` com erro HTTP explicito. A resposta original permanece
salva, e a evidencia recebe status `unavailable` e zero observacoes. Ela nao
corrobora uma causa. Outros erros HTTP, de busca ou de metricas continuam
bloqueantes. Sem essa opcao, o padrao permanece estrito.

`train` reutiliza os 14 pacotes de suporte; `evaluate` reutilizara os 18
pacotes de teste, identicos em A/B/C e em todas as repeticoes. Apos a coleta,
essas etapas nao consultam telemetria. Os hashes por caso sao conferidos
e registrados no relatorio, alem dos resultados por segmento.

O schema fornecido ao modelo restringe as referencias aos IDs exatos do
incidente. O validador local tambem confere as referencias, inclusive nas
alternativas de diagnostico, e as opcoes de classificacao. `--max-repairs 1`
congela uma politica de no maximo UMA tentativa corretiva por resposta,
igual em suporte e nas tres condicoes. Nao se substituem IDs por heuristica.
Resposta original, erro e nova resposta permanecem auditaveis em
`private/contract_attempts/` e `private/model_calls/`. Falhas de primeira
tentativa, reparos e tokens de ambas as chamadas entram nas metricas.
Isso valida contrato, nao a correcao cientifica da explicacao.

Uma falha de contrato apos esgotar o orcamento nao interrompe os demais
suportes: gera `private/training_errors/`, e o comando termina com codigo
nao zero apos processar todos. `training_report.json` registra o denominador,
erros, rascunhos e checklist de revisao. `review_template.json` inclui
somente rascunhos existentes, todos `pending`; nada e aprovado automaticamente.
Retomar `train` nao reinicia falhas terminais nem concede reparos ilimitados.
Uma interrupcao de processo pode ser retomada usando respostas ja auditadas.

**Pare aqui para revisao humana.** Conferir ID existente nao basta: confira
servico, unidade, fase, motivo de restart e corroboracao independente.
Ausencia de OOM nao exclui consumo de memoria; ausencia de drops nao exclui
latencia adicionada. Rejeite explicacoes incorretas, mesmo que o modelo
declare `mechanism_supported=true`.

Somente apos preencher o template com revisor e justificativa:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge publish \
  --output-dir "$OUT" --decisions "$OUT/review_template.json"

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge evaluate \
  --output-dir "$OUT" --allow-inference \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --max-input-chars 400000 --max-output-tokens 8192 --repetitions 3
```

Nao ha publicacao se nenhum rascunho satisfizer os criterios. Nesse caso,
a condicao com experiencias continua indisponivel; nao e substituida por
uma base vazia apresentada como experimento completo.

| Condicao | Conteudo fornecido para analisar o incidente |
|---|---|
| `skill` | Procedimento retrospectivo, contrato comum e evidencia do incidente |
| `skill_card` | Anterior + system card congelada |
| `skill_card_history` | Anterior + ate tres experiencias revisadas do suporte |

Todas usam a [mesma nova skill](../../../.github/skills/rca-evaluation/SKILL.md).
A card nao integra esse procedimento; entra apenas nas condicoes pertinentes.
O contrato comum preserva identidades, categorias e opcoes de falha em todas
as condicoes.

Esta variante nao reproduz o fluxo end-to-end da skill operacional.
O modelo continua analisando um pacote sem executar ferramentas. O runner
oferece tres modos, fixados na inicializacao:

- **`live_shared` (padrao):** consulta os datasources historicos via MCP uma
  vez por caso nesta avaliacao e compartilha essa coleta entre
  condicoes/repeticoes. Exige `collect` antes de inferir.
- **`live_per_condition` (exploratorio):** consulta os backends locais
  separadamente por condicao e repeticao, sempre nas janelas historicas do
  run. Tambem coleta novamente para cada caso de suporte antes de construir
  experiencias. E um ensaio online exploratorio sobre incidentes passados,
  nao uma comparacao causal com evidencia identica.
- **`offline`:** proibido para avaliacao e rejeitado pelo runner.

As consultas MCP do avaliador usam exclusivamente
`historical_observability.mcp_sse_url` (por padrao,
`http://127.0.0.1:18082/sse`); os endpoints live do spec nao sao fallback.
No protocolo de dual write, os dados sao gravados em paralelo e validados por
read-back historico, sem executar uma migracao posterior.

Consultar agora nao significa observar o estado atual do cluster: a janela
historica do incidente e preservada. Dados que expiraram nao sao substituidos
por telemetria atual nem por um fallback silencioso para arquivos. Uma nova
avaliacao exige uma nova coleta MCP, ainda que outra avaliacao tenha coletado
as mesmas janelas anteriormente.

## Protecao da execucao atual

- `init` apenas le o plano e congela protocolo/contexto em diretorio novo.
  Pode ser executado enquanto a campanha roda.
- `freeze`, `collect`, `train` e `evaluate` recusam operar se o lock da campanha estiver
  ocupado ou houver celulas nao terminais no CSV.
- O lock e adquirido sem espera. O avaliador nao sinaliza, encerra ou
  reinicia processos e nao modifica arquivos da campanha.
- Durante essas etapas, o lock compartilhado impede iniciar uma retomada
  concorrente da mesma campanha; execute-as apenas apos encerrar a coleta.
- Nao ha consultas a Kubernetes ou a API da plataforma. Somente `live_shared`
  ou `live_per_condition` sao aceitos; ambos consultam MCP/Prometheus/Loki/Tempo
  historicos. `collect` (live_shared), ou `train` e `evaluate`
  (live_per_condition), consultam esses backends apos verificar que a campanha
  terminou e com `--allow-telemetry` explicito.
  Nao ha fallback para telemetria armazenada se os backends falharem.
- Inferencia exige `--allow-inference`, modelo explicito e endpoint local.
  Nao e iniciada pelo `init`.
- Banco SQLite, flag `is_training` e corpus RAG operacional permanecem intactos.
- O diretorio novo e criado com permissoes de acesso apenas para seu dono.
  Nao publique os pacotes privados ou prompts sem revisao de dados sensiveis.

O avaliador nao agenda trabalho em background. O operador escolhe quando
iniciar inferencias, inclusive para evitar concorrencia com outras tarefas
que usem o modelo/GPU.

## Divisao dos experimentos

O plano deve conter exatamente sete servicos x cinco intervencoes, uma
celula por par. A ordem de servicos e falhas e permutada com seed e a matriz
e dividida alternadamente:

- 17 celulas de suporte, para construir experiencias.
- 18 celulas de teste, nunca usadas para produzir a base.
- Todos os servicos e tipos aparecem nos dois conjuntos planejados.

Celulas bloqueadas, abortadas ou falhas permanecem no balanco, mas nao
entram como incidentes concluidos. Nao ha redistribuicao para obter numeros
mais favoraveis ou preencher lacunas depois de observar os resultados.

Este desenho testa novas combinacoes de servico/intervencao na mesma
aplicacao; nao testa mecanismo desconhecido, aplicacao desconhecida ou
repetibilidade da intervencao. A campanha nao contem controles sham.
Como a campanha e aleatorizada, casos de suporte podem ocorrer depois de
casos de teste no relogio. Isso e permitido neste ensaio retrospectivo,
mas impede interpreta-lo como reutilizacao online de conhecimento passado.

## Uso

Execute da raiz do repositorio. O diretorio sugerido e separado da campanha:

```bash
CAMPAIGN=experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T091929101290Z
OUT=experiment/eval/memory_hog/out/knowledge_evaluation/controlled-20261021T091929101290Z/v2-live
```

### Variante exploratoria anterior: continuacao vinculada, 32 runs concluidos

Para a campanha de 21/10 e sua continuacao, use:

```bash
CAMPAIGN=experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T145517920531Z
CONTINUATION=experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T204641889813Z
OUT=experiment/eval/memory_hog/out/knowledge_evaluation/controlled-20261021T145517920531Z/linked32-live-v1

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge init \
  --campaign-dir "$CAMPAIGN" --continuation-dir "$CONTINUATION" \
  --output-dir "$OUT" --evidence-mode live_per_condition --trace-limit 5

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge freeze \
  --output-dir "$OUT"
```

Esta opcao exige ambos os segmentos inativos. O avaliador verifica o vinculo
salvo pela continuacao, os hashes originais do pai, a matriz pendente exata,
os CSVs e os ledgers terminais. Preserva os 23 concluidos do pai e seleciona
os nove concluidos da continuacao: 32 runs unicos, sem copiar ou alterar
resultados das campanhas. Os dois bloqueados e o abortado finais permanecem
nas exclusoes. Nao se recuperam tentativas antigas no lugar de resultados
finais desfavoraveis.

A divisao suporte/teste continua sendo a da matriz original de 35 celulas,
com a mesma seed; nao e recalculada para favorecer os 32 disponiveis.
Portanto, os 32 participam do dataset, mas apenas o subconjunto de teste
recebe os diagnosticos A/B/C. Suporte constroi a base e nunca e pontuado como
teste. A identidade do segmento fica no snapshot, na proveniencia privada,
nas tentativas e no relatorio, sem entrar como pista no prompt do incidente.
No snapshot `linked32-live-v1`, sao 14 casos de suporte e 18 de teste:
nove suportes e 14 testes no pai; cinco suportes e quatro testes na
continuacao. As tres exclusoes pertencem ao suporte, sem rebalanceamento.

O manifesto congela hashes de config, plano, ledger e CSV dos dois segmentos.
Alteracoes posteriores sao rejeitadas nas etapas que leem a campanha.
Cada etapa de coleta/inferencia segura os dois locks compartilhados.
O relatorio inclui denominadores do dataset e `summary_by_segment`, alem do
agregado exploratorio. A recuperacao de experiencias pode cruzar esses dois
segmentos explicitamente vinculados, nunca workflows arbitrarios. Isso nao
elimina o confundimento pela mudanca de ambiente: nao apresente o agregado
como uma campanha homogenea ou estimativa causal de generalizacao.

Se `init` e `freeze` ja foram executados nesse diretorio, nao os repita:
continue em `train` abaixo. A inferencia nao e iniciada por esses comandos.
`--continuation-dir` so e usado em `init`; as demais etapas leem o manifesto.

Para esse dataset, os comandos de inferencia com os orcamentos usados no
piloto anterior sao:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge train \
  --output-dir "$OUT" --allow-inference --allow-telemetry \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --max-input-chars 400000 --max-output-tokens 8192

# Nova versao: coleta MCP nova obrigatoria para cada avaliacao.
OUT_QUALIFIED=experiment/eval/memory_hog/out/knowledge_evaluation/controlled-20261021T145517920531Z/linked32-qualified-v5

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge init \
  --campaign-dir "$CAMPAIGN" --continuation-dir "$CONTINUATION" \
  --output-dir "$OUT_QUALIFIED" --evidence-mode live_shared --trace-limit 5 \
  --max-repairs 1 --allow-missing-trace-details \
  --history-policy qualified_examples

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge freeze \
  --output-dir "$OUT_QUALIFIED"

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge collect \
  --output-dir "$OUT_QUALIFIED" --allow-telemetry

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge train \
  --output-dir "$OUT_QUALIFIED" --allow-inference \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --max-input-chars 400000 --max-output-tokens 8192

# Registrar revisao/aprovacao em lote no template da v4, mantendo qualificacoes.
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge publish \
  --output-dir "$OUT_QUALIFIED" --decisions "$OUT_QUALIFIED/review_template.json"

./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge evaluate \
  --output-dir "$OUT_QUALIFIED" --allow-inference \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --max-input-chars 400000 --max-output-tokens 8192 --repetitions 3
```

Cada avaliacao precisa executar `collect` para consultar novamente Prometheus,
Loki e Tempo via MCP nas janelas historicas originais. A evidência coletada e
compartilhada apenas dentro desse diretorio de avaliacao. Importar os pacotes
da v3 ou de qualquer avaliacao anterior e proibido.

Os limites nao garantem que o servidor comporte todo prompt: estouros ou
respostas invalidas sao falhas explicitas. `freeze` nao valida inferencia.

### 1. Inicializar uma unica vez

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge init \
  --campaign-dir "$CAMPAIGN" --output-dir "$OUT" \
  --evidence-mode live_per_condition --trace-limit 5
```

O comando recusa reutilizar diretorio existente. Se o manifesto ja foi
inicializado, comece na etapa seguinte; nao apague dados para repetir.

O manifesto registra seed, splits, card, procedimento, contrato, modo online e
hash do avaliador. Alterar o codigo do avaliador depois exige um novo diretorio
de avaliacao; nao se misturam tentativas de implementacoes diferentes.
O manifesto v1 anterior deve ser preservado. Esta versao corrigida exige
uma nova inicializacao; nao edite o hash do manifesto antigo.
O modo offline e rejeitado pelo runner; nao inicialize uma avaliacao apenas com
dados armazenados.

No modo live, `--trace-limit` congela um teto de resultados da busca Tempo
por fase (padrao 5; intervalo 1 a 50), limitado tambem pelo teto da spec
original. O mesmo teto vale para suporte e todas as condicoes/repeticoes.
O valor e as consultas efetivas ficam registrados; os resultados selecionados
pelo backend nao sao amostra aleatoria ou cobertura completa dos traces.
Ausencia de erros nessa amostra nao demonstra ausencia de erros no incidente.
Este limite nao autoriza usar snapshots de telemetria congelados nem trunca
silenciosamente prompts:
entradas acima de `--max-input-chars` continuam falhando antes da inferencia.
Na campanha historica de 29 runs, o piloto com teto 5 foi aprovado antes de
qualquer inferencia, apos a coleta com teto 50 exceder o orcamento de entrada.

### 2. Apos a campanha terminar, congelar os artefatos

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge freeze \
  --output-dir "$OUT"
```

Este passo prepara janelas, identidades e metadados privados da campanha. Os
pacotes que alimentam o diagnostico e a pontuacao devem vir da etapa `collect`,
por consultas MCP novas nesta avaliacao; este snapshot nao substitui essa
coleta.

Sao lidos artefatos ja existentes no object store para construir o protocolo:

- Janelas observadas, sem metadados do experimento no payload do modelo.
- Metricas gerais resumidas por serie e por fase: contagem, media, extremos
  e primeiro/ultimo valores; rotulos sao selecionados por allowlist.
- Amostras de logs por servico e fase, com cobertura/truncamento, rejeitando
  linhas fora da janela e containers identificados como injetores.
- Features deterministicas de traces: caminho bruto, hotspots e erros.
  Resumos LLM e hipoteses RCA de traces nao sao utilizados. So traces
  inteiramente contidos em uma fase sao atribuidos a ela; schemas
  desconhecidos e traces que cruzam limites sao explicitamente marcados.
- Relatorio de intervencao somente na area privada para rotulos e gates.
- Spec efetiva privada, cuja identidade precisa concordar com a celula.
  No modo live, somente a configuracao de consultas de observabilidade e
  reutilizada; alvo, tipo de injecao e metadados do experimento nao entram
  nas consultas nem no payload do diagnosticador.

O congelamento confere `run_id` da janela e do snapshot de logs, quando
presente, e o ID exato do experimento na janela/spec contra campanha,
servico, falha e replica do ledger. Arquivos de outro run ou workflow sao
rejeitados antes de produzir o pacote.
Retentativas com sufixo `attempt-N` sao aceitas apenas quando a ultima entrada
da celula no ledger confirma exatamente o numero da tentativa, run e estado
concluido. O hash do ledger e registrado no snapshot.
Logs ausentes geram `missing_artifact` para cada servico/fase; servico ou
fase omitidos geram `missing_coverage`. Isso nao e `no_data`.

Nao entram no contexto `effective_spec`, nomes do experimento, manifestos de
caos, logs do injetor ou diagnosticos anteriores. `controlled_report` e fontes
brutas ficam privados. Ausencia, erro, schema nao suportado e truncamento
nao sao convertidos em ausencia de falha.

O congelamento nao reconstrui telemetria que nao foi preservada. Se faltarem
artefatos obrigatorios, falha explicitamente. Nao se consulta o cluster para
preencher lacunas nesta etapa. No modo live, as consultas posteriores
fornecem a telemetria utilizada pelo modelo, sem misturar com esse snapshot.
Nao e necessario modificar o CSV original.

### 3. Construir rascunhos da base de suporte

Confirme que o modelo esta livre e use seu identificador local exato.
O exemplo abaixo usa o nome registrado nas avaliacoes anteriores:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge train \
  --output-dir "$OUT" --allow-inference --allow-telemetry \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions
```

Para cada caso de suporte concluido:

0. No modo live, consulta metricas, logs e traces nas janelas historicas e
   persiste a coleta. As duas chamadas de treinamento usam esse mesmo pacote.
1. Solicita observacoes e alternativas sem fornecer a resposta.
2. Persiste essa extracao cega.
3. Revela alvo, tipo e gate experimental numa segunda chamada.
4. Solicita confronto, discriminadores, contradicoes e limitacoes.
5. Salva um rascunho pendente e um modelo de decisoes para revisao.

Ha ate 34 chamadas de treinamento se todos os 17 suportes forem concluidos.
Nenhuma resposta de teste entra nessa etapa. A rotulacao experimental nao
e tratada como prova de mecanismo na telemetria.

Falhas de modelo e respostas invalidas ficam na auditoria privada.
Rascunhos concluidos nao sao sobrescritos. Uma interrupcao na chamada
supervisionada pode ser retomada sem refazer a extracao cega.

### 4. Revisar e publicar experiencias

Revise os rascunhos e seus IDs de evidencia em `drafts/`, `observations/`
e no campo `bundle` de cada rascunho. No modo live, esse campo corresponde
a coleta real usada, nao a telemetria original de `snapshot.json`.
Edite `review_template.json`, substituindo `pending`
por `approve` ou `reject`, com nome do revisor e justificativa.

Formato de uma entrada, usando um ID que realmente seja de suporte:

```json
{
  "case-002": {
    "decision": "approve",
    "reviewer": "identificador-do-revisor",
    "note": "Conferidos discriminadores, referencias, temporalidade e limites."
  }
}
```

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge publish \
  --output-dir "$OUT" --decisions "$OUT/review_template.json"
```

Publicacao exige mecanismo aprovado pelo gate experimental, sustentacao
declarada no rascunho e ao menos um discriminador com referencia. Esses
checks nao substituem revisao semantica humana. Se nenhum caso puder ser
aprovado, a publicacao falha: nao fabrica uma base de sucesso.

Casos rejeitados ou nao revisados nao sao recuperaveis. O registro de
decisoes e preservado. O corpus publicado e imutavel; para alterar seu
conteudo, crie uma nova avaliacao, nao edite hashes para forcar aceitacao.

### 5. Executar a comparacao pareada

```bash
./.venv/bin/python -m experiment.eval.memory_hog.campaign_knowledge evaluate \
  --output-dir "$OUT" --allow-inference --allow-telemetry \
  --model qwen3.5-9b-grafana \
  --llm-url http://127.0.0.1:8001/v1/chat/completions \
  --repetitions 3
```

As configuracoes de modelo e orcamentos devem permanecer iguais as do
treinamento; a mudanca e recusada nesse diretorio. O cliente compartilhado
nao oferece override de temperature/seed: sao utilizados defaults do
servidor, fato registrado em `model_settings.json`. O operador deve manter
o mesmo servidor/modelo carregado e registrar sua versao; o nome configurado
nao comprova o digest dos pesos efetivamente servidos.

O limite padrao de entrada e 220.000 caracteres e a saida 4.096 tokens.
Nao sao unidades equivalentes. O servidor precisa suportar a entrada real;
estouro de limite gera erro explicito, nao truncamento silencioso.
Se precisar alterar os limites, use os mesmos valores em `train` e
`evaluate` e decida-os antes do teste. Nao ajuste pelo placar de teste.

Sao ate 162 investigacoes: 18 incidentes x tres condicoes x tres repeticoes.
Cada investigacao faz uma chamada de analise final, sem ferramentas.
Condicoes e repeticoes sao intercaladas em ordem deterministica com seed.
No modo live, cada investigacao faz antes sua propria coleta: consultas
metricas da spec, captura limitada de logs e busca/analise deterministica
de traces por fase, sem resumo por LLM. Isso adiciona carga e latencia;
execute apenas apos a campanha e quando os backends estiverem disponiveis.
Nao ha tentativa de recuperar automaticamente dados expirados.

Tentativas existentes com os mesmos inputs sao reutilizadas, inclusive
falhas, sem selecionar a melhor resposta. Para uma nova rodada independente,
use outro diretorio/manifesto; nao apague seletivamente tentativas ruins.
Na retomada, a coleta ja persistida para aquela tentativa tambem e reutilizada,
nao consultada novamente para melhorar o resultado. Novas condicoes e
repeticoes recebem coletas separadas.

## Recuperacao e isolamento do workflow

A base e `knowledge.json`, sem reutilizar SQLite ou embeddings anteriores.
Somente experiencias de suporte do mesmo manifesto, campanha e snapshot
sao aceitas. Hashes identificam os inputs e denunciam alteracoes acidentais.

A consulta e extraida deterministicamente de mudancas nas metricas e
mensagens de erro observadas. O ranking inicial e Jaccard entre assinaturas
observaveis, sem usar o tipo real ou servico-alvo do teste. Sao retornados
ate tres itens com score positivo, desempate por ID, proveniencia e evidencias
de suporte citadas. Uma busca sem correspondencia retorna lista vazia.
As assinaturas de suporte sao as do pacote realmente usado no treinamento,
inclusive quando ele foi coletado live, e nao de um snapshot diferente.

Esse ranking e um baseline simples, nao uma medida validada de relevancia.
Se a recuperacao falhar, isso deve ser analisado separadamente da utilidade
do conhecimento. Publicar so casos com gate aprovado pode enviesar a base;
reportar sua composicao por servico e mecanismo e as rejeicoes.

IDs historicos sao prefixados, por exemplo `K-case-002:E0001`. Eles nao
podem ser aceitos como referencias de evidencia do incidente atual.
A verificacao estrutural reaproveita os gates da skill operacional; ela
nao comprova alinhamento semantico ou causalidade.

## Arquivos produzidos

```text
manifest.json                 protocolo, divisao e hashes
snapshot.json                 pacotes iguais para A/B/C e exclusoes
private/labels.json           gabaritos e proveniencia, fora do payload
private/sources/              copias seladas dos artefatos originais
private/model_calls/          chamadas, schemas, respostas, erros e tempos
private/live_evidence/        coleta por suporte/condicao/repeticao, consultas e dados brutos
model_settings.json           configuracao congelada de inferencia
observations/                 extracoes cegas de suporte
drafts/                       experiencias supervisionadas pendentes
review_template.json          decisoes humanas editaveis antes da publicacao
knowledge.json                base revisada imutavel do workflow
attempts/                     inputs, itens recuperados, respostas e placares
report-r3.json                resultados pareados agregados por incidente
```

O relatorio apresenta acertos separados e conjuntos, erros contados como
incorretos, placares por incidente, diferencas pareadas, cobertura e precisao
de confirmacao por rotulo, estrato com mecanismo validado, tempo e tokens
reportados. Sem confirmacoes, a precisao e `null`, nao 100%.
Respostas invalidas com usage retornado continuam contabilizadas no custo.

O CLI retorna erro se houver tentativas de avaliacao falhas, preservando o
relatorio e todas as tentativas para auditoria.
O relatorio registra modo, desenho e se a evidencia e identica entre
condicoes. Cada tentativa registra o hash de seu pacote. Em modo live,
nao atribuir diferencas de placar exclusivamente ao conhecimento: retencao,
indexacao e disponibilidade dos backends sao fatores de confusao.

## Limites e proximos checks cientificos

- Acerto e correspondencia com rotulos; nao confirma explicacao causal.
- Gates automaticos de mecanismo e revisao humana podem errar.
- Revisao cega das explicacoes de teste e concordancia entre avaliadores
  ainda precisam ser realizadas externamente ao placar automatico.
- As repeticoes do LLM nao sao novos incidentes; a unidade primaria e o caso.
- A card contem notas interpretativas alem de topologia. O efeito observado
  e do pacote contextual completo, nao somente de compreensao arquitetural.
- O contexto foi congelado no inicio da avaliacao, nao necessariamente
  observado durante cada intervencao. Revisar sua compatibilidade.
- Nao ha estimativa automatica de significancia, confianca calibrada,
  curva risco-cobertura ou transferencia externa.
- Tempo de resposta nao e MTTR. Custos de construcao/revisao da base sao
  separados dos custos de diagnostico.
- Um resultado positivo exige examinar exemplos recuperados e erros; um
  resultado negativo nao prova que conhecimento experimental seja inutil.

## Validacao de software

```bash
./.venv/bin/python -m pytest \
  experiment/eval/memory_hog/tests/test_campaign_knowledge.py -q
```

Os testes usam dados sinteticos e modelo falso, sem acesso a cluster,
servidor LLM ou banco operacional. Nao sao resultados experimentais de RCA.
