# Plano de avaliacao preliminar da metodologia

**Data:** 21/10/2026.  
**Status:** plano proposto; nao implementa schemas, migracoes, replay ou ferramentas.  
**Proposta cientifica:** [conhecimento diagnostico contextualizado](../2026-10-21-metodologia-conhecimento-diagnostico-contextualizado.md).  
**Objetivo:** obter uma primeira comparacao auditavel do valor de contexto e
conhecimento experimental, sem ampliar prematuramente a arquitetura de agentes.

## 1. Resultado esperado do primeiro ciclo

Entregar um pacote que permita responder:

1. Contexto da aplicacao melhora diagnostico alem de telemetria e vocabulario?
2. Conhecimento de experimentos anteriores acrescenta valor ao contexto?
3. Conhecimento contrastivo ajuda mais que relatos historicos convencionais?
4. Quais erros decorrem de dados, extracao, recuperacao ou diagnosticador?

A primeira entrega e um piloto metodologico, nao uma demonstracao de
superioridade geral ou uma estimativa definitiva de efeito.

## 2. O que ja existe e o que ainda falta

Estado observado no codigo em 21/10; nao confundir com a versao que executou
cada campanha historica.

| Superficie | Base reutilizavel | Limite relevante |
|---|---|---|
| [SQLite](../../experiment/platform/backend/storage/sqlite_storage.py) | Experimentos versionados, runs, eventos, artefatos, features, embeddings e flag `is_training` | Nao constitui manifesto imutavel de corpus nem isolamento completo do gabarito |
| [Object store local](../../experiment/platform/data/object_store/) | Arquivos por run, incluindo observacoes e tentativas de diagnostico | Identidade por caminho nao garante imutabilidade, integridade ou portabilidade |
| [System card](../../experiment/platform/backend/analysis/system_card.py) | Contexto estatico e recorte de servicos/vizinhos | Mistura semantica e vocabulario; nao implementa extracao automatica historica |
| [Recuperacao](../../experiment/platform/backend/analysis/retrieval.py) | Tags, embeddings e similaridade de propagacao | Precisa de auditoria do payload; tags de fenomeno podem conter `chaos:*` |
| [Sumarizacao](../../experiment/platform/backend/analysis/summary.py) | Resumos e tags de runs | Pode incorporar `chaos_type`; nao usar diretamente como consulta do alvo sem sanitizacao |
| [Snapshot MCP](../../experiment/platform/backend/plugins/mcp_evidence_snapshot_plugin.py) | Captura limitada de logs e cliente snapshot-first | Consultas nao atendidas podem ir ao vivo; nao e replay offline estrito de todas as fontes |
| [Campanha controlada](../../experiment/eval/memory_hog/controlled_campaign.py) | Planejamento, calibracao, execucao e ledger de intervencoes | O piloto proposto acrescenta controles sham, contrastes e splits proprios |
| [Avaliador skill](../../experiment/eval/memory_hog/evaluate_skill_rca.py) e [multiagente](../../experiment/eval/memory_hog/evaluate_multiagent_rca.py) | Tentativas, placares e artefatos por run | Adaptar coortes, conhecimento fornecido, metricas e avaliacao semantica comuns |

Preferir SQLite e arquivos locais no primeiro ciclo. Nao adicionar banco
vetorial, banco de grafos, framework de agentes ou servico distribuido antes
de demonstrar necessidade.

## 3. Separar quatro conjuntos antes de ajustar o metodo

| Conjunto | Uso permitido | Restricao |
|---|---|---|
| Historico exploratorio | Depurar ferramentas, definir rubrica e estudar erros | Os 29 runs de outubro nao sao teste novo e intocado |
| Corpus de suporte | Produzir e revisar conhecimento recuperavel | Somente dados elegiveis e anteriores ao corte do teste |
| Desenvolvimento | Ajustar extracao, prompts, recuperacao e limiares | Nao publicar como teste final |
| Teste preliminar reservado | Comparar condicoes congeladas | Sem ajuste com base nas respostas antes de encerrar a coorte |

Fixar `split_group_id` para agrupar o mesmo incidente, suas reavaliacoes,
recapturas, derivados e unidades de conhecimento. Nenhuma derivacao de
incidente de teste pode entrar no indice de suporte.

Replica independente e uma nova intervencao com baseline e recuperacao
verificados. Repetir inferencia sobre o mesmo snapshot nao aumenta o numero
de incidentes. Um split por replicas da mesma celula mede repeticao naquele
dominio, nao generalizacao a mecanismo ou aplicacao desconhecidos.

`is_training` continua util para compatibilidade, mas a allowlist imutavel
do manifesto deve definir a elegibilidade do corpus. Runs antigos cujo
split nao foi auditado ficam em quarentena; o default historico da flag nao
e prova de que podem entrar na recuperacao.

## 4. Experimento preliminar proposto

### 4.1 Perguntas e condicoes

Usar inicialmente um unico agente com ferramentas, sem forcar ordem de
consulta. Fixar modelo, parametros, contrato de saida, ferramentas de
telemetria e limites globais.

| Condicao | Informacao disponivel |
|---|---|
| C0 | Evidencia do incidente, catalogo minimo de identidades e contrato de saida |
| C1 | C0 + contexto estrutural e operacional revisado |
| C2 | C1 + relatos de experimentos passados elegiveis |
| C3 | C1 + unidades de conhecimento sobre mecanismos e contrastes |

C0 e C1 devem receber os mesmos nomes validos de servicos e mecanismos,
incluindo `unknown` e ausencia de incidente. Nao retirar o vocabulario de
uma condicao. Mapear nomes de injetores para mecanismos no avaliador, com
taxonomia congelada e sem entrega do gabarito ao diagnosticador.

Para C2 e C3, usar o mesmo corpus de suporte e a mesma revisao de fatos.
Controlar orcamento de contexto e registrar tokens efetivos; C3 nao pode
receber evidencias extras ocultas. O custo de produzir e revisar as unidades
tambem deve ser medido. Assim, compara-se a representacao e disponibilizacao,
nao apenas quantidade ou curadoria desigual.

Primeiro comparar C0 e C1. Adicionar C2/C3 somente apos validar corpus e
recuperacao. Contexto automatico versus revisado, consulta sob demanda versus
entrega inicial e modelos maiores sao extensoes posteriores, uma por vez.

### 4.2 Matriz pequena, mas informativa

**Proposta inicial ajustavel no desenvolvimento:** dois servicos com perfis
distintos, tres mecanismos e tres blocos independentes.

- Candidatos: `catalogue` e `orders`, sujeitos a calibracao e cobertura.
- Candidatos a mecanismos: pressao de CPU, pressao de memoria e atraso em
  dependencia/aresta de rede. Escolher a direcao e a aresta atingida.
- Cada bloco: seis intervencoes e dois controles sham, um por servico.
- Total planejado: 18 intervencoes + seis sham = 24 runs de teste.

Um sham preserva trafego, agenda e coleta, sem aplicar a perturbacao; nao e
simplesmente uma janela de baseline retirada de um run com caos.
Nao afirmar que o harness atual ja implementa esse desenho.

Os mecanismos so formam um contraste util se produzirem sintomas comparaveis
no desenvolvimento. Se nao produzirem, revisar a selecao antes de congelar
o teste, sem perseguir um resultado favoravel ao metodo. Estabelecer bandas
de carga e de degradacao e registrar a intensidade realmente atingida.

Produzir separadamente um corpus de suporte pequeno, por exemplo 8-16 runs
novos/revisados, ou recuperar historicos que tenham dados suficientes.
Essa faixa e um orcamento inicial, nao garantia de cobertura dos contrastes.
Nao usar os 24 runs reservados para produzir esse conhecimento.

Randomizar a ordem em blocos com seed e registrar restricoes operacionais.
Esperar estabilidade e recuperacao, nao apenas um cooldown fixo. Registrar
host, recursos, versoes, carga e interferencias. O bloco captura variacao do
ambiente; seu tamanho nao equivale a poder estatistico previamente calculado.

### 4.3 Gates por run

Registrar estados distintos:

1. Baseline e carga estaveis.
2. Intervencao aplicada ou sham executado conforme protocolo.
3. Mecanismo atingido e intensidade verificados por auditoria externa.
4. Impacto no servico/usuario medido, inclusive quando nao houver degradacao.
5. Cobertura por fonte, servico e fase, com atraso de ingestao.
6. Recuperacao verificada e ausencia de sobreposicao com o proximo run.

Nao condensar tudo em `valid=true`. Mecanismo atingido sem impacto e um
resultado diferente de intervencao falha. Definir criterios numericos no
desenvolvimento, por mecanismo, e versiona-los antes de coletar o teste.

Manter todos os runs no balanco operacional. Reportar qualidade diagnostica
no subconjunto com gabarito adjudicavel e, separadamente, no subconjunto com
cobertura suficiente. Exibir as exclusoes por motivo e por celula; preservar
a mesma coorte ao comparar condicoes.

## 5. Armazenamento: pacote imutavel e indice reconstruivel

### 5.1 Responsabilidades

- **SQLite:** catalogo operacional e indice pesquisavel; nao unica fonte
  de verdade de uma avaliacao publicada.
- **Arquivos de evidencia:** conteudo bruto e derivados versionados,
  referenciados por caminhos relativos e SHA-256.
- **Manifesto do corpus:** lista exata de runs, splits, versoes e hashes.
- **Area privada do avaliador:** intervencoes, gabarito e adjudicacao;
  inacessivel as ferramentas do agente.
- **Indice de recuperacao:** derivado reconstruivel de uma versao do corpus;
  nao atualizar silenciosamente durante a avaliacao.

Separar fisicamente ou por permissoes o pacote do agente e o do avaliador.
Remover o gabarito do prompt nao basta se a ferramenta pode abrir todo o
object store. Usar identificadores opacos que nao codifiquem servico/falha.

### 5.2 Organizacao logica sugerida

Estrutura proposta para exportacao; nao mover o object store atual nem
sobrescrever artefatos historicos para adota-la.

```text
dataset-version/
  manifest.json
  protocol.json
  agent/
    runs/<opaque_run_id>/
      windows.json
      environment.json
      coverage.json
      evidence/<content_hash>.json
    contexts/<context_version>/
      assertions.jsonl
    knowledge/<knowledge_version>/
      episodes.jsonl
      contrasts.jsonl
    retrieval/index_manifest.json
  evaluator-private/
    interventions.jsonl
    ground_truth.jsonl
    adjudication.jsonl
```

Identidades reais de servicos podem aparecer na evidencia; o identificador
opaco evita codificar a resposta no nome do run. O material exportavel deve
ser sanitizado e inspecionado para credenciais e dados pessoais.

### 5.3 Registros minimos propostos

| Registro | Campos essenciais |
|---|---|
| Manifesto | `dataset_version`, `schema_version`, `created_at`, `cutoff_at`, protocolo/hash, runs e grupos por split, arquivos/hashes, revisao do codigo e diff/imagens identificados |
| Evidencia | `evidence_id`, `run_id`, fonte original, consulta/argumentos, servico, fase, janela pedida e retornada, `event_time`, `collected_at`, status, truncamento/sampling, unidade, payload/hash |
| Contexto | `assertion_id`, sujeito, relacao/atributo, valor, natureza declarada/observada/inferida, fonte/hash, validade temporal, extrator/revisor, conflitos |
| Episodio publico de suporte | `episode_id`, `support_run_ids`, contexto/hash, sintomas observados, mecanismo revisado, referencias de evidencia, limitacoes e data de disponibilidade |
| Unidade contrastiva | `knowledge_id`, versao, mecanismos alternativos, condicoes, sinais discriminativos/contrarios, grupos de suporte, contraexemplos e validade |
| Gabarito privado | Alvo pretendido/atingido, intervencao, mecanismo verificado, impacto, grau de certeza, suporte externo, casos nao identificaveis |
| Recuperacao | `attempt_id`, consulta sanitizada, versoes do corpus/indice, filtros, candidatos/scores, itens retornados, bytes/tokens e motivo de indisponibilidade |
| Tentativa | Run/snapshot, condicao, contexto/conhecimento, modelo e hash quando disponivel, parametros, prompts/tools versionados, respostas, citacoes, custos, tempos e erros |

Exemplo reduzido de uma unidade contrastiva, sem afirmar um resultado real:

```json
{
  "schema_version": 1,
  "knowledge_id": "contrast-example-v1",
  "status": "hypothesis_pending_review",
  "support_run_ids": [],
  "context_version": "context-example-v1",
  "alternatives": ["local_cpu_pressure", "dependency_delay"],
  "shared_symptom": "request_latency_increase",
  "candidate_discriminators": [
    "local_cpu_throttling_during_fault",
    "client_server_span_latency_relation"
  ],
  "evidence_refs": [],
  "limitations": ["No empirical support in this illustrative record"]
}
```

Uma unidade sem suporte e revisao nao entra no corpus factual de C3.
Versionar correcoes por novos IDs/hashes, preservando o anterior. Guardar
dados brutos uma vez, referenciados pelos derivados; nao duplicar traces
inteiros em cada embedding ou prompt.

### 5.4 Importar o historico com seguranca metodologica

1. Inventariar runs e artefatos existentes sem altera-los.
2. Confrontar janelas, IDs, cobertura, versoes e origem das consultas.
3. Classificar: reutilizavel congelado, parcialmente reutilizavel ou
   exploratorio sem evidencia suficiente.
4. Criar vistas publicas sanitizadas; manter originais na area de auditoria.
5. Calcular hashes e publicar manifesto de importacao com lacunas.
6. Nao preencher historico ausente com estado atual do cluster.

Um diagnostico antigo pode ser recuperado como relato nao verificado apenas
em uma condicao que declare isso. Nao promovê-lo a mecanismo confirmado.
Por padrao, C2 e C3 usam fatos revisados equivalentes.

### 5.5 Persistencia e recuperabilidade

Guardar protocolos, schemas e pequenos manifestos no Git, sem grandes
payloads ou dados sensiveis. Preservar exportacao consistente do catalogo
e artefatos em armazenamento autorizado com backup; copiar o arquivo SQLite
aberto, isoladamente, nao garante um snapshot consistente.

Antes de declarar o corpus pronto, restaurar uma exportacao em diretorio
isolado, verificar hashes e reconstruir o indice. O teste de restauracao
e parte do aceite, nao apenas a existencia de uma copia.

## 6. Recuperar experimentos passados sem vazar o teste

### 6.1 Pipeline minimo

1. Receber consulta derivada apenas dos sintomas observados e contexto
   permitido no instante da tentativa.
2. Aplicar allowlist do manifesto, split/grupo, corte temporal e requisitos
   de revisao antes do ranking.
3. Buscar episodios/unidades por termos e atributos observaveis.
4. Acrescentar embeddings como variante apenas se o baseline simples justificar.
5. Retornar poucos itens, proveniencia, validade, contraexemplos e limites.
6. Registrar consulta, candidatos e resposta exata por tentativa.

Nao filtrar pelo `chaos_type` real do alvo nem gerar seu embedding a partir
de um resumo que contenha esse campo. As tags `chaos:*` podem existir no
suporte conhecido, mas nao podem ser produzidas com gabarito do alvo.
E necessario auditar tambem textos, grafos e chamadas que constroem a consulta.

O agente pode pesquisar uma hipotese que ele proprio formulou, inclusive
por nome de mecanismo. Isso e diferente de o host inserir a resposta real.
Registrar a origem da consulta permite distinguir as duas situacoes.

Conhecimento historico elegivel pode conter mecanismos e conclusoes revisadas
de suporte. O que e proibido e conhecimento derivado do incidente-alvo,
de suas replicas agrupadas indevidamente ou de informacao posterior ao corte.

### 6.2 Avaliar recuperacao antes de avaliar o diagnostico

Preparar consultas pequenas revisadas com itens relevantes e contraexemplos.
Medir precision/recall em k sobre esse conjunto rotulado, proveniencia,
diversidade de mecanismos e tokens. Relevancia nao e apenas "mesmo tipo
de falha": um contraste que refuta uma hipotese tambem pode ser relevante.

Esses rotulos pertencem ao desenvolvimento ou a um teste de recuperacao
separado. Nao ajustar ranking com gabaritos do teste diagnostico reservado.

### 6.3 Interfaces conceituais, ainda nao implementadas

- Consultar contexto por componente, fluxo e instante.
- Buscar episodios por sintomas e restricoes.
- Buscar contrastes entre hipoteses.
- Abrir evidencia de suporte por ID.

Nao impor que o agente use todas as interfaces. Respostas vazias devem
explicitar ausencia de resultado elegivel; erros de acesso ou indice sao
erros, nao respostas vazias de sucesso.

## 7. Replay justo e disponibilidade temporal

Congelar metricas, logs, traces, identidades, contexto e conhecimento antes
da comparacao. Adaptar ferramentas ao snapshot, sem fallback live.

Para autonomia, nao basta reproduzir apenas as consultas feitas pelo primeiro
metodo. Definir um universo comum de dados e operacoes suportadas, com filtros
por servico/janela, para todas as condicoes. Nao e necessario emular PromQL
ou LogQL irrestritos: comecar com ferramentas tipadas sobre o pacote.
Consulta fora desse universo retorna `unsupported_query` e e contabilizada.

Separar `no_data`, `partial`, `tool_error`, `unsupported_query` e evidencia
existente mas ainda indisponivel naquele instante. O snapshot deve preservar
truncamento e limites, nao inventar observacoes ausentes.

O primeiro piloto sera **retrospectivo**, com janela de corte comum apos o
run e ingestao. Dados post podem ser usados se isso estiver declarado para
todas as condicoes. Nao chamar esse teste de diagnostico em tempo real.

Um ensaio posterior online deve respeitar `available_at`, ocultar dados
futuros e medir atraso de ingestao e coleta. Nao misturar seus resultados
com o teste retrospectivo.

## 8. Execucao e avaliacao

### 8.1 Controle das tentativas

Executar C0-C3 em contexto novo e independente por run, sem memoria entre
condicoes. Randomizar/intercalar sua ordem; congelar prompts, modelo,
contratos e ferramentas antes do teste. Registrar limites e uso real de
tokens, chamadas e tempo; comparabilidade de custo nao exige consumo identico.

Comecar com uma passagem para validar integridade; depois executar tres
tentativas independentes por condicao/run, sem selecionar a melhor.
Com 24 runs e quatro condicoes, isso corresponde a 288 investigacoes,
nao a 288 incidentes nem a 288 chamadas LLM. O agente pode fazer varias
chamadas por investigacao.

Usar um modelo viavel no laboratorio. Depois, se houver acesso autorizado,
comparar com pelo menos um modelo mais capaz para verificar valor incremental.
Nao enviar codigo, credenciais ou telemetria privada a servicos externos.

### 8.2 Gabarito e rubrica

Revisar o mecanismo com artefatos de intervencao e verificacao independente,
fora das ferramentas do diagnosticador. Marcar certeza insuficiente em vez
de forcar uma unica classe.

Dois avaliadores, cegos para a condicao quando possivel, revisam as
afirmacoes atomicas e citacoes; registrar discordancias e adjudicacao.
Se houver apenas um avaliador, declarar a limitacao e nao alegar concordancia.
LLM-as-judge pode auxiliar, mas nao ser o unico juiz do efeito proposto.

Para cada afirmacao relevante, marcar:

- Suportada pela evidencia citada, com servico e tempo alinhados.
- Contradita.
- Sem suporte suficiente.
- Nao verificavel com o pacote.

Avaliar explicitamente se a explicacao distingue alternativas ou apenas
repete sintomas. A rubrica e o que conta como afirmacao relevante precisam
ser definidos com exemplos no desenvolvimento.

### 8.3 Metricas e denominadores

**Desfecho primario proposto:** acerto conjunto de servico e mecanismo por
run com gabarito adjudicavel, com erros de execucao e ausencia de hipotese
pontuavel contados como incorretos. Uma hipotese marcada inconclusiva ainda
pode ser pontuada como hipotese; isso nao a transforma em confirmacao.

Reportar separadamente:

- Acerto de servico, mecanismo e conjunto; controle sham e falsa deteccao.
- Fracao de afirmacoes suportadas, contraditas e nao verificaveis.
- Confirmacoes corretas / confirmacoes emitidas e confirmacoes erradas /
  confirmacoes emitidas; se nao houver confirmacao, ambas indefinidas.
- Confirmacoes emitidas / tentativas elegiveis, abstencoes e falhas, para
  impedir que abstencao universal pareca sucesso.
- Confirmacoes corretas / tentativas elegiveis e erros por run.
- Custo online de investigacao e custo offline de extracao/revisao/indexacao,
  separados; custo amortizado depende do numero de reutilizacoes assumido.
- Tokens, ferramentas, tempos, truncamentos e indisponibilidades.

Apresentar fluxo desde runs planejados ate gabaritos adjudicaveis, alem da
analise end-to-end de disponibilidade. Estratificar por mecanismo, servico,
cobertura e validacao; nao trocar o denominador para melhorar o placar.

### 8.4 Analise preliminar

Agregar repeticoes do modelo dentro de cada run e preservar pareamento entre
condicoes. Exibir resultados individuais e diferencas com intervalos de
incerteza, respeitando grupos/blocos; nao tratar afirmacoes ou inferencias
repetidas como incidentes independentes.

Pre-especificar contrastes C1-C0, C2-C1 e C3-C2. Com poucos runs, enfatizar
tamanho de efeito, estabilidade e analise de erros; testes multiplos ou
intervalos instaveis nao sustentam conclusoes fortes. Dimensionar uma
campanha confirmatoria posterior a partir do piloto, sem alegar poder
estatistico garantido para n=24.

## 9. Sequencia de implementacao e criterios de aceite

| Ordem | Entrega | Dependencia | Aceite verificavel |
|---|---|---|---|
| P0.1 | Protocolo, taxonomia, rubrica e splits | Nenhuma | Condicoes, desfecho e regras de exclusao congelados antes do teste |
| P0.2 | Inventario e importacao do historico | P0.1 | Cada run classificado; originais preservados; nenhum historico preenchido com live |
| P0.3 | Exportador sanitizado e manifesto | P0.2 | Hashes validos; agente nao acessa gabarito; restauracao em ambiente isolado |
| P0.4 | Replay estrito e contrato comum | P0.3 | Execucao sem rede para telemetria; consultas equivalentes retornam os mesmos dados |
| P0.5 | Contexto revisado separado do vocabulario | P0.1, P0.3 | C0/C1 compartilham identidades e saida; fontes/versoes das afirmacoes registradas |
| P1.1 | Corpus de suporte e unidades contrastivas | P0.1, P0.3 | Conhecimento revisado aponta para evidencia elegivel; nenhum grupo de teste |
| P1.2 | Recuperacao simples e auditoria | P1.1 | Consulta do alvo sem gabarito; candidatos reconstituiveis; teste rotulado separado |
| P1.3 | Calibracao e coleta dos runs reservados | P0.1-P0.5 | Gates por run, ledger completo e snapshots; falhas/exclusoes preservadas |
| P1.4 | Avaliacao C0/C1 e depois C2/C3 | P0.4, P0.5, P1.2, P1.3 | Mesmos snapshots e coorte; tentativas independentes; todas as respostas preservadas |
| P1.5 | Revisao cega e relatorio do piloto | P1.4 | Rubrica aplicada, denominadores e custos publicados; conclusoes limitadas ao piloto |
| P2 | Extracao automatica, contexto imperfeito e transferencia | P1.5 | Uma nova dimensao por ensaio, sem reutilizar teste para ajuste |

P0.4 e P0.5 podem avancar em paralelo; a coleta P1.3 nao precisa esperar
o ranking final, mas precisa de universo de captura e splits definidos.
Nao executar novas injecoes apenas por este documento; faze-lo quando o
protocolo estiver aprovado e o ambiente disponivel.

## 10. Testes necessarios antes de inferencia em lote

- Integridade de hashes e restauracao de pacote/indice.
- Separacao de suporte, desenvolvimento e teste, incluindo derivados.
- Bloqueio de tags, caminhos, resumos e embeddings que revelem o alvo.
- Gabarito inacessivel por ferramentas e sistema de arquivos do agente.
- Replay com acesso live indisponivel, sem fallback silencioso.
- Mesma consulta, mesmo resultado e mesmo status em todas as condicoes.
- Erro, vazio, parcial e truncado preservados como estados distintos.
- Disponibilidade temporal e contexto valido no corte declarado.
- Correcao dos denominadores, sham, falhas, abstencao e precisao indefinida.
- Isolamento de memoria entre tentativas e persistencia sem sobrescrita.

Esses sao testes a implementar; a criacao deste plano nao os executa nem
declara os recursos prontos.

## 11. Regras para decidir o proximo investimento

- Se C1 nao ajudar, verificar relevancia, qualidade e tamanho do contexto
  antes de automatizar a extracao em larga escala.
- Se contexto revisado ajudar e automatico nao, priorizar extracao/atualizacao.
- Se C2 ajudar e C3 nao, nao adicionar estrutura contrastiva por principio.
- Se C3 melhorar apenas com mais tokens ou curadoria, controlar esse fator
  antes de atribuir ganho a representacao.
- Se dados insuficientes explicarem os erros, corrigir captura antes de
  adicionar agentes.
- Se um agente mais capaz sem historico igualar o metodo, estudar custo e
  cenarios de transferencia sem alegar ganho universal.
- Se contexto desatualizado induzir erro, investigar proveniencia e revisao,
  nao endurecer a obediencia ao contexto.

O primeiro produto cientifico esperado e um resultado reproduzivel sobre
utilidade e limites do conhecimento fornecido, incluindo a possibilidade
de nenhum ganho. Uma campanha maior so se justifica apos validar o instrumento.
