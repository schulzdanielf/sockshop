# Plano: estratégias configuráveis e comparação reproduzível de RCA

**Data:** 17/10/2026  
**Status:** proposta de implementação; nenhum novo executor ou perfil foi implementado neste plano.  
**Objetivo:** variar ferramentas, sequência de investigação, seleção de candidatos,
papéis dos agentes e orçamento sem copiar executores ou alterar o código a cada
experimento. Comparar qualidade, confiabilidade e custo nos mesmos casos.

Este plano complementa o [plano multiagente anterior](./2026-09-25-plano-llm-multiagente.md)
e as [correções prioritárias](./2026-10-17-proximos-passos.md). Não substitui os
experimentos históricos nem autoriza novas injeções.

## 1. Diagnóstico e decisão arquitetural

Hoje é possível escolher **qual endpoint executar**, mas não escolher, de forma
versionada, **como esse endpoint investiga**. Existem mecanismos reutilizáveis;
o principal trabalho é separar política experimental de execução e de segurança.

**Recomendação:** adicionar perfis declarativos tipados e um conjunto pequeno de
políticas registradas no servidor. Reaproveitar o executor multiagente e suas
evidências; manter a Skill RCA e os fluxos antigos como baselines identificados.
Não criar agora um motor genérico de DAGs, linguagem de prompts, plugins
dinâmicos, framework externo ou um executor novo para cada estratégia.

Uma estratégia será uma combinação imutável de:

1. motor (`skill` ou `multiagent`, inicialmente);
2. política de coleta e prioridade das tools;
3. política de seleção e expansão de candidatos;
4. papéis de análise habilitados e política de aprofundamento;
5. orçamento e parâmetros de geração;
6. versões dos prompts, contratos, normalizadores e validação causal.

Alterar uma dessas dimensões gera uma nova versão/hash. As regras de segurança,
isolamento do ground truth e requisitos de corroboração não serão opções do perfil.

## 2. Mapa do projeto atual

Os caminhos desta seção são relativos à raiz do repositório.

| Superfície | Implementação atual | Reuso / limitação para estratégias |
|---|---|---|
| RCA RAG | [`api.py`](../../experiment/platform/backend/api.py), rota `/llm-analysis`; [`runner.py`](../../experiment/eval/memory_hog/runner.py), `evaluate_strategy` e `phase_eval` | Já percorre runs × estratégias do YAML, variando recuperação, contexto e tokens. Não configura as políticas da Skill ou do multiagente. |
| RCA MCP legada | [`agent_console.py`](../../experiment/platform/backend/agent_console.py), `answer_run_rca_mcp`; rota `/rca` | Tem loop limitado, construção de argumentos, digests e normalização. API usa snapshot preferencial de alguns logs. Saída e tratamento de falhas diferem dos demais motores. |
| Skill RCA | [`rca_skill.py`](../../experiment/platform/backend/rca_skill.py), `answer_skill_rca` | Coleta fixa de métricas/lifecycle, planejamento, logs, traces e diagnóstico. Até três tentativas de JSON por etapa, 4.096 tokens de saída por chamada. Usa constantes para limites de logs e helpers compartilhados. |
| Procedimento Skill | [`SKILL.md`](../../.github/skills/rca/SKILL.md) e referências | Procedimento e contrato causal versionados por hash. A escolha de variantes não deve depender de editar esse arquivo global entre tentativas. |
| Multiagente | [`multiagent_rca.py`](../../experiment/platform/backend/multiagent_rca.py), `answer_multiagent_rca` | Coordenador de plano → coleta → especialistas de métricas/logs/traces → até duas rodadas → síntese → validação. Ordem, papéis e parte das tools são fixos. |
| Contratos/orçamento | `InvestigationContext`, `InvestigationBudget`, `CoordinatorWorkPlan`, `SpecialistReport`, `CausalAssessment`, no mesmo módulo | Pydantic já limita entradas. Defaults: 32 tools, 10 chamadas LLM, 120 mil tokens globais, 180 s, até duas rodadas. API instancia defaults sem aceitar perfil. |
| Evidências e políticas do host | `_EvidenceRegistry`, `_model_json`, `validate_assessment`, no módulo multiagente | Registro, deduplicação e limites são boa base. Classe privada acoplada à execução; extraí-la apenas no trecho que precisar ser compartilhado. |
| Métricas e lifecycle | [`telemetry_coverage.py`](../../experiment/platform/backend/analysis/telemetry_coverage.py), [`mcp_metrics_plugin.py`](../../experiment/platform/backend/plugins/mcp_metrics_plugin.py) | Já distinguem cobertura, terminacões históricas e sinais. Terminação pertence à família metrics; não é fonte independente de Prometheus. |
| Tools disponíveis | [`main.py`](../../mcp-observability-server/main.py) | Golden signals, CPU, memória, readiness, OOM, restart, drops, PromQL, Loki e Tempo. Tool disponível no MCP não implica tool autorizada para qualquer agente. |
| Aprofundamento | `FollowupRequest`, `_EvidenceRegistry.followup` | Hoje aceita Loki, grafo Tempo, CPU e memória. As consultas de CPU/memória são globais por fase; trocar só o serviço não muda os argumentos e pode gerar rejeição por duplicata. |
| Replay atual | [`mcp_evidence_snapshot_plugin.py`](../../experiment/platform/backend/plugins/mcp_evidence_snapshot_plugin.py) | Captura opt-in de logs, limitada; `SnapshotPreferredMCPClient` responde `check_service_logs` compatível e delega o restante ao vivo. Não congela métricas/traces nem atende todo LogQL dos novos fluxos. |
| API | [`api.py`](../../experiment/platform/backend/api.py), `/rca`, `/skill-rca`, `/multiagent-rca` | Skill/multiagente recebem essencialmente `run_id`, criam `attempt_id` e preservam artefato próprio. Instanciam MCP vivo; não compartilham replay estrito. |
| Plano pela API | `/multiagent-rca/plan`, `build_investigation_plan` | Retorna plano determinístico descritivo, `planning_only`; não é um scheduler nem traduz fielmente todas as decisões do fluxo em execução. |
| Cliente/UI | [`rca_cli.py`](../../experiment/platform/rca_cli.py), [`app.js`](../../experiment/platform/frontend/app.js), [`index.html`](../../experiment/platform/frontend/index.html) | CLI só chama Skill. UI tem botões separados e envia `{}`; não há seletor de perfil ou tabela de comparação de tentativas. |
| Persistência | [`sqlite_storage.py`](../../experiment/platform/backend/storage/sqlite_storage.py) | Artefatos e eventos por tentativa já permitem preservar versões. MVP pode usar JSON e manifesto de avaliação, sem migração de banco antecipada. |
| Avaliação Skill | [`evaluate_skill_rca.py`](../../experiment/eval/memory_hog/evaluate_skill_rca.py) | Coorte pareada deriva de baseline legado concluído; não é avaliador geral de variantes e não inclui todos os erros da coorte original. |
| Avaliação multiagente | [`evaluate_multiagent_rca.py`](../../experiment/eval/memory_hog/evaluate_multiagent_rca.py) | Compara serviço/categoria e preserva rodada. Indexa por falha/serviço, rejeitando réplicas; denominador não distingue elegibilidade causal; grava progresso agregado ao final. |
| Diagnóstico do modelo | [`llm_client.py`](../../experiment/platform/backend/analysis/llm_client.py), [`otel.py`](../../experiment/platform/backend/observability/otel.py) | Usage e `finish_reason` já existem em spans. Falta associar sistematicamente esses dados a etapa/tentativa/perfil nos artefatos, inclusive quando parsing falha. Captura de conteúdo é opt-in. |
| Testes existentes | [`test_multiagent_rca.py`](../../experiment/platform/tests/test_multiagent_rca.py), [`test_rca_skill.py`](../../experiment/platform/tests/test_rca_skill.py), [`test_rca_skill_api.py`](../../experiment/platform/tests/test_rca_skill_api.py), [`test_rca_context_optimization.py`](../../experiment/platform/tests/test_rca_context_optimization.py) | Há fixtures para tools, orçamento, follow-up inválido, cobertura e gates causais. São base dos testes de compatibilidade e isolamento entre perfis. |

### 2.1. Restrições que o piloto expôs

Nas duas avaliações multiagentes recentes dos cinco runs:

| Indicador | Primeira rodada | Segunda rodada |
|---|---:|---:|
| Respostas finais válidas | 3/5 | 5/5 |
| Serviço correto | 0/5 | 1/5 |
| Categoria correta | 0/5 | 0/5 |
| Diagnósticos confirmados | 0 | 0 |

Fontes: [primeira avaliação](../../experiment/eval/memory_hog/out/quick_campaign/five-skill-quality-20261017T121726176281Z/multiagent_rca/20261017T125847134677Z/evaluation.json)
e [segunda avaliação](../../experiment/eval/memory_hog/out/quick_campaign/five-skill-quality-20261017T121726176281Z/multiagent_rca/20261017T175945182050Z/evaluation.json).
Esses artefatos locais podem não existir em outro checkout.

- O mesmo run não garante a mesma evidência: as consultas são feitas ao vivo
  para janelas antigas. No caso CPU, a primeira RCA tinha observações de traces;
  a segunda não tinha. Não atribuir toda diferença à aleatoriedade do LLM.
- Plano com JSON válido não garante candidatos semanticamente válidos. Houve
  IDs de evidência e textos de hipóteses no campo `candidates`; o fallback atual
  percorre métricas e suas linhas em ordem, sem ranking causal balanceado.
- Logs dos primeiros candidatos podem consumir o orçamento antes de investigar
  a origem. Reordenar ferramentas sem controlar esse efeito muda a evidência.
- `followup` de CPU/memória pede uma tool global; uma segunda solicitação com
  outro serviço pode ser a mesma consulta. Expor granularidade real ao planner.
- O campo geral de aprovação do efeito não equivale a confirmação causal.
  Memória teve falha de probe; HTTP não mostrou o 5xx esperado; lifecycle ainda
  tem sinais ambíguos. Não usar os cinco como dataset causal já validado.

## 3. Dois experimentos diferentes, não um único ranking

### A. Qualidade do raciocínio, com evidência fixa

Todos recebem o mesmo pacote sanitizado e versionado de métricas, logs,
lifecycle e traces, com os mesmos limites de contexto. Comparar:

- sintetizador único;
- especialistas por família + sintetizador;
- mesma decomposição com uma etapa adicional de revisão de contradições, depois.

Não executar novas consultas nesse modo. Assim é possível perguntar se os
agentes melhoram a interpretação, em vez de apenas coletarem mais dados.
Desabilitar um especialista não precisa remover aquela família do pacote do
sintetizador: são ablações distintas, a serem identificadas separadamente.

### B. Qualidade da investigação, com catálogo congelado e orçamento igual

Cada política escolhe ferramentas/candidatos/ordem a partir do mesmo universo
de respostas disponíveis. Medir também o custo das consultas e o que deixou de
ser visto. Comparar, inicialmente, métricas genéricas contra prioridade de
recursos/lifecycle e planejamento LLM contra seleção determinística.

Uma política não recebe de graça os achados que não consultou. O replay cobra
o orçamento lógico de consulta; a latência de replay é reportada separadamente
da latência de coleta original. Não comparar replay em milissegundos com
consultas vivas e concluir ganho operacional.

**Não misturar ranking A com ranking B.** Um replay da trilha de uma estratégia
também não basta para B: outra pode pedir consultas que a primeira nunca fez.

## 4. Arquitetura proposta

```text
UI / CLI / benchmark
        |
perfil registrado + versão + modo de evidência + run_id
        |
resolver de perfil / contexto sem ground truth / orçamento
        |
cliente MCP controlado (live_record OU replay_strict)
        |
política de coleta -> seleção de candidatos -> interpretação
        |                                      |
        +---------------- síntese --------------+
                              |
           validação causal comum, não configurável
                              |
       tentativa imutável + manifesto + observabilidade
                              |
     avaliador offline junta diagnóstico ao ground truth
```

### 4.1. Perfis tipados, pequenos e versionados

Criar um contrato `RcaStrategyProfile`, próximo aos contratos existentes, e
arquivos YAML em `experiment/platform/rca_strategies/`. Caminhos novos são
propostas, não arquivos já disponíveis.

Exemplo conceitual; o comando de execução e o schema ainda serão implementados:

```yaml
schema_version: "1.0"
id: multiagent.resource-first
version: 1
engine: multiagent
collection_policy: resource_first_v1
candidate_policy: balanced_metrics_v1
reasoning_policy: family_specialists_v1
specialists: [metrics, logs, traces]
followup_policy: bounded_coordinator_v1
budget:
  max_tool_calls: 32
  max_model_calls: 10
  max_total_tokens: 120000
  timeout_seconds: 180
  max_followup_rounds: 2
generation_policy: compact_json_v1
```

Registrar `*_v1` em allowlists de funções locais; validar combinações antes de
consultar ou chamar o LLM. Não aceitar import Python, código, shell, caminhos
de arquivo arbitrários ou consultas livres vindos do perfil/API.

Preferir presets nomeados de coleta a dezenas de flags booleanas. Só expor
parâmetros adicionais quando houver uma pergunta experimental específica.
Políticas de geração resolvem limites de saída por estágio e retries; isso não
deve ficar implícito em constantes diferentes entre motores.

### 4.2. O que pode variar e o que é fixo

| Variável experimental | Exemplos | Restrição |
|---|---|---|
| Prioridade das tools | Recursos antes de RED; lifecycle antes da expansão de dependências; traces antes dos logs detalhados | Triagem mínima de métricas permanece; não ignorar motivo de restart. |
| Escolha dos candidatos | LLM, ranking determinístico por família, vizinhos de dependência | Nunca usar alvo/falha/resultado de caos; registrar critérios, empates, candidatos descartados. |
| Profundidade por candidato | Contagens de logs antes das amostras; quotas por serviço/fase | Identidade exata, limites, cobertura e truncamento sempre explícitos. |
| Agentes | Síntese única, métricas+logs+traces, futura revisão crítica | Agentes não contam como fontes independentes. |
| Aprofundamento | Desabilitado, coordenador com até duas rodadas | Mesma allowlist e deduplicação; reservar orçamento para síntese. |
| Família disponível | Ablação sem traces, sem especialista de logs | Ausência explícita, mesmo validador; não confirmar causa só por métricas. |
| Orçamento | Tiers pequeno/padrão, saída e retry limitados | Comparar dentro do mesmo tier; contabilizar tentativas que falham. |
| Modelo/prompt | Segundo eixo experimental, posterior ao MVP | Fixar inicialmente; não mudar modelo, coleta e agentes simultaneamente. |

Campos invariantes: sanitização, somente leitura, namespace/janelas controlados
pelo host, identidade do workload, isolamento do ground truth, referências de
evidência válidas, separação evento/mecanismo/impacto e abstenção causal.

### 4.3. Reuso incremental dos executores

1. Congelar `skill.current.v1` e `multiagent.current.v1` como perfis de
   compatibilidade. Nenhuma mudança silenciosa dos botões existentes.
2. Fazer o multiagente receber perfil resolvido pelo servidor. Substituir os
   blocos fixos de coleta/candidatos/papéis pelas políticas pequenas propostas.
3. Reutilizar `_EvidenceRegistry`, normalizadores e `InvestigationBudget`.
   Extrair registro compartilhado apenas quando um segundo executor precisar
   dele; não reescrever todos os helpers de `agent_console.py`.
4. Preservar respostas brutas e pós-validação separadamente para auditoria.
   A projeção normalizada de comparação é derivada, não substitui o artefato.
5. Manter RAG e RCA MCP legada como baselines históricos com adaptadores
   explícitos. Seus post-processadores não são equivalentes ao gate causal:
   publicar raw e final, sem apresentar ganho do corretor como ganho do LLM.

Os especialistas atuais executam sequencialmente, embora tenham papéis
separados. Paralelismo não será parte do primeiro incremento: afeta latência,
competição no modelo local, ordem do registro e orçamento global.

## 5. Reprodutibilidade e diagnóstico antes do benchmark

### 5.1. Pacote de evidência e replay

Evoluir o mecanismo de snapshot, sem tratá-lo como replay completo já pronto:

- congelar janelas e sua origem, namespace, cartão do sistema, catálogo e schemas
  das tools, dados/respostas sanitizados, normalizadores e hash do pacote;
- armazenar chave canônica de consulta: tool + argumentos + versão da tool +
  identidade da fonte. Ordenar chaves JSON, não reordenar listas sem semântica;
- escolher catálogo finito, comum a todas as políticas do piloto: métricas
  globais, CPU/memória por fase, lifecycle, logs com quotas por serviço/fase,
  contagens e traces. Registrar falhas e `no_data` como respostas congeladas;
- para logs filtrados: capturar respostas exatas das consultas permitidas, ou
  derivar apenas de um corpus completo para o escopo. Amostra truncada não
  permite simular consulta de logs completa nem ausência de um padrão;
- `replay_strict` jamais chama fonte viva em cache miss: registrar
  `evidence_unavailable_in_replay`, distinta de `no_data` da fonte;
- `live_record` preserva tudo consultado, mas rodadas assim não entram como
  comparação de entrada idêntica. Um futuro modo híbrido deve se identificar;
- nenhum pacote entregue ao modelo contém alvo, tipo de caos, verdict Litmus,
  tags ou nomes de arquivos que revelem os rótulos.

Os dados que já desapareceram das fontes não podem ser reconstruídos por um
perfil. Usar o piloto antigo para regressão técnica; obter nova captura apenas
em campanha explicitamente autorizada, depois de validar a instrumentação.

### 5.2. Observabilidade por chamada de modelo

Antes de tentar curar JSON aumentando indiscriminadamente tokens:

- correlacionar `run_id`, `attempt_id`, perfil/hash, etapa, agente e request ID;
- registrar limite solicitado, tokens usados, duração, `finish_reason`,
  schema/hash, hash do prompt/resposta, parsing e validação semântica;
- preservar resposta inválida sanitizada em artefato local com captura de
  conteúdo explícita e acesso controlado; não ampliar conteúdo sensível nos
  spans/logs por padrão. Hash/contagem não permitem reconstruir JSON truncado;
- distinguir truncamento, JSON malformado, schema inválido, candidatos
  inexistentes, timeout e erro de tool;
- retry/reparo é uma política explícita, limitada e contabilizada. Não mascarar
  falha da primeira tentativa com um diagnóstico final aparentemente perfeito;
- reutilizar spans/auditoria existentes; persistir também falhas do plano/final
  que hoje podem não aparecer em `model_attempts` com todos esses metadados.

## 6. Primeiro conjunto de variantes

Começar com poucos pares, sem produto cartesiano de todas as opções.

| Perfil proposto | Referência | Única pergunta principal |
|---|---|---|
| `multiagent.current.v1` | Implementação atual congelada | Qual é o baseline real do executor, inclusive falhas? |
| `multiagent.balanced-candidates.v1` | `current` | Seleção balanceada por métricas/lifecycle melhora localizar a origem e cobertura dos candidatos? |
| `multiagent.resource-first.v1` | `balanced-candidates` | Investigar CPU/memória/lifecycle antes de escolher candidatos melhora discriminação? |
| `multiagent.no-followup.v1` | `balanced-candidates` | O coordenador de aprofundamento traz ganho suficiente para seu custo? |
| `single.fixed-evidence.v1` | Pacote de evidências comum | Qual o resultado com um único sintetizador, sem especialistas? |
| `specialists.fixed-evidence.v1` | Mesmo pacote do perfil anterior | A decomposição por família melhora interpretação/citações? |

Os dois últimos são o experimento A; os quatro primeiros, o B. Não comparar
seus números como se a entrada e o custo fossem equivalentes. Para reproduzir
`current` na primeira migração, manter suas particularidades de janela,
limites e prompts; normalizações comuns posteriores são uma nova versão.

Somente depois: logs por contagem antes de amostras, traces após triagem e antes
do plano de candidatos, expansão por dependências, revisor de contradições,
paralelismo e modelo alternativo. Uma mudança por par e justificativa registrada.
Não escolher `resource-first` só porque o avaliador sabe que o caso é CPU/memória.

## 7. API, CLI, UI e artefatos

### Contrato proposto

- `GET /api/rca-strategies`: perfis válidos, versões, descrição, motor e hash.
- Nas rotas Skill/multiagente, aceitar body opcional com `strategy_id`,
  `strategy_version`, `evidence_mode` e `evidence_bundle_id` quando necessário.
  `{}` ou body ausente preserva o default do endpoint.
- Perfil de motor incompatível, versão desconhecida, pacote de outro run ou
  combinação inválida: erro explícito antes de chamar tools/LLM. Sem fallback
  silencioso para o perfil padrão. Servidor resolve pacote e caminho.
- O plano pela API deve mostrar o perfil resolvido e as políticas reais; não
  prometer consultas/etapas que o executor não usa.
- Adaptar a CLI existente para escolher motor/perfil. Primeiro entregar comandos
  curtos por arquivos de benchmark; seletor de UI vem depois.
- UI apresenta estratégia, versão, fonte/hash de evidência, tentativa, estado e
  histórico. Comparação de tentativas não sobrescreve diagnóstico anterior.

Cada tentativa preserva: perfil completo resolvido/hash, git revision e estado
dirty, hashes dos arquivos executados relevantes quando dirty, versões do
modelo/runtime/prompts/skill/cartão/validador, janelas, snapshot, parâmetros de
geração, eventos de etapas, orçamento, chamadas, erros e diagnósticos raw/final.
Seed/temperatura só são declaradas controladas se realmente suportadas e enviadas
ao backend; não prometer determinismo do servidor/modelo.

## 8. Avaliador único e protocolo de comparação

Criar `experiment/eval/memory_hog/evaluate_rca_strategies.py` como entrada comum
e migrar a pontuação duplicada gradualmente. Manter wrappers dos scripts atuais.

### Manifesto de benchmark

O manifesto fixa `run_id`, réplica da campanha, serviço/categoria de referência,
elegibilidade por tarefa, checks de injeção/sinal, pacote e seu hash. Ground
truth fica no processo avaliador, não na requisição do agente.

Chave do resultado: `(benchmark_id, run_id, strategy_hash, repetition)`.
Não indexar apenas por `(chaos_type, service)`: isso perde ou rejeita réplicas.
Salvar progresso após cada tentativa; retomar sem repetir sucessos ou apagar
erros. Retry de tentativa é novo registro vinculado ao anterior.

O benchmark executa `runs × perfis × repetições`, sem dependência de baseline
antigo bem-sucedido. Inicialmente cinco repetições de modelo por caso/perfil
para medir instabilidade; reduzir matriz, não esconder custo. Essas repetições
não substituem novas execuções independentes de caos.

### Métricas obrigatórias

1. **Confiabilidade:** tentativas previstas/executadas, resposta válida, erros
   por etapa, timeout, JSON/schema/semântica inválida, retry e motivo de parada.
2. **Localização:** acerto exato do serviço de origem; quando houver referência,
   serviço impactado separado. Nunca usar serviço-alvo e serviço impactado como
   sinônimos. Hipótese localizada e causa confirmada são resultados diferentes.
3. **Categoria:** acerto exato contra rótulo revisado; não chamar isso de
   equivalência da causa raiz em texto. Avaliar mecanismo/cadeia causal por
   revisão humana cega e rubricada, separadamente.
4. **Conjunta e abstenção:** serviço+categoria, taxa de `inconclusive`,
   confirmações corretas e confirmações indevidas. Publicar também acerto das
   hipóteses inconclusivas; `inconclusive` não implica automaticamente serviço
   errado, nem significa acerto confirmado.
5. **Evidência:** famílias e serviços cobertos em fault, faltantes, truncamento,
   precisão das referências, contradições, uso indevido de baseline/no_data.
   Métricas e lifecycle derivado delas continuam uma única família.
6. **Custo:** tools lógicas e físicas (cache/replay), modelo por etapa/agente,
   tokens incluindo retries, latência total/p95, coleta original e replay
   separados, diagnóstico correto por orçamento.

Publicar dois denominadores: (a) toda a coorte, para taxa de sucesso end-to-end,
contando erros como não sucesso; (b) subconjunto elegível para cada tarefa,
definido antes de ver os diagnósticos, para qualidade causal. Não filtrar
somente respostas válidas. Falha de injeção, ambiguidade de identidade e fonte
ausente são estratos visíveis; não são licença para remover casos difíceis.

Mesma coorte e pacote entre pares; mesmo modelo e tier de orçamento. Fixar ou
contrabalancear ordem das variantes; executar serialmente no modelo local no
piloto. Reportar diferenças pareadas, estabilidade entre repetições e contagens
por falha. Em coorte maior, intervalos/re-amostragem por run independente, não
tratando várias gerações do mesmo run como incidentes independentes.

Os cinco runs atuais servem para smoke/reprodução de falhas. Não selecionar
perfil vencedor para artigo usando apenas esses mesmos cinco: separar
desenvolvimento e avaliação held-out, incluir controle sem caos e réplicas
independentes após corrigir instrumentação e efeitos de injeção.

## 9. Entrega em fases, esforço e dependências

Estimativas relativas de esforço, não promessa de prazo.

| Fase | Entrega | Esforço | Depende de | Aceite verificável |
|---|---|---|---|---|
| 0 | Diagnóstico por chamada e captura de defaults atuais | Pequeno/médio | Nenhuma | Fixtures de resposta truncada, schema/candidatos inválidos e timeout geram artefatos completos; primeira tentativa não some após retry. |
| 1 | Contrato, catálogo e perfis `current` | Médio | 0 | Perfil default mantém comportamento; inválidos falham antes de consultas; hash muda com conteúdo/prompt/política. |
| 2 | `replay_strict` e catálogo de evidências do piloto | Médio/alto | 1 | Todos os motores comparados usam o mesmo pacote; cache miss nunca acessa rede; nenhum rótulo chega aos agentes. |
| 3 | Coleta/candidatos/agentes/follow-up extraídos em políticas | Médio | 1 | Variar política realmente muda sequência/consumo; invariantes e contabilidade global permanecem; isolamento entre tentativas. |
| 4 | Avaliador comum, manifesto e primeiros pares | Médio | 0, 2, 3 | Runs com réplica e erros entram no relatório; retomada não sobrescreve; comparação usa hashes iguais de evidência. |
| 5 | Seletor UI e histórico de comparação | Pequeno/médio | 1, 4 | Perfil da tela é o persistido; defaults/erros dos botões antigos preservados. |
| 6 | Coorte held-out, ablações e relatório científico | Alto | 4 e validação dos experimentos de caos | Resultados pareados, custo, abstenções, controles e limitações publicados sem confundir label de injeção com prova causal. |

Fases 2 e 3 são independentes entre si depois de 1; não é necessário terminar a
UI para iniciar avaliação por CLI. Capturar evidência disponível pode ser
urgente por retenção, mas não autoriza mudanças no cluster.

### Testes mínimos de regressão

- Fixtures dos perfis atuais preservam ordem de chamadas, janelas e limites.
- Allowlist protege todas as chamadas do host e do coordenador, não só a lista
  apresentada ao LLM; priorizar não pode contornar orçamento ou segurança.
- Candidatos são serviços válidos; falhas semânticas ficam auditadas, sem
  trocar silenciosamente hipótese por ID de evidência.
- Pacote errado, tool não gravada, janela divergente, conteúdo truncado e fonte
  indisponível produzem estados distintos; replay não tem cliente vivo oculto.
- Síntese mantém orçamento reservado; retries debitam custo; especialista
  desligado não é executado e o agente final recebe lacunas explícitas.
- Sucesso bruto e rebaixamento do validador são preservados separadamente;
  métricas/lifecycle não passam por duas famílias independentes.
- API com `{}` continua funcionando; perfil incompatível e override não
  permitido retornam erro explícito; UI/CLI mostram versão real.
- Avaliação suporta múltiplas réplicas, erros, inconclusivos e interrupção;
  denominadores não mudam conforme o sucesso do modelo.

## 10. Próximo incremento recomendado

Implementar primeiro **fase 0 + fase 1**, preservando o fluxo atual e permitindo
seleção explícita de perfil; em seguida replay estrito e apenas o par
`current`/`balanced-candidates`. Executar sobre fixtures e casos existentes
antes de autorizar campanha nova.

Isso entrega uma base para mudar estratégias sem editar o pipeline e, antes
de aumentar agentes ou tools, permite responder três perguntas mensuráveis:

1. O perfil alterou de fato o comportamento previsto?
2. O ganho permaneceu quando a evidência foi a mesma?
3. O ganho de serviço/categoria e evidência compensou erros, abstenções e custo?
