# Relatório do experimento completo de caos e avaliação de RCA

## 1. Objetivo deste relatório

Este documento foi atualizado para responder, de forma operacional e auditável, às perguntas centrais sobre o pipeline:

- quais etapas são executadas em cada fase do experimento;
- como os dados são coletados, enriquecidos e sumarizados;
- quais as diferenças entre os baselines/estratégias;
- quais dados chegam ao modelo de linguagem;
- como funciona a etapa determinística pós-LLM;
- onde estão os gargalos atuais e em que vale investir tempo.

Fontes principais no código:

- [experiment/eval/memory_hog/runner.py](../eval/memory_hog/runner.py)
- [experiment/eval/memory_hog/config.yaml](../eval/memory_hog/config.yaml)
- [experiment/platform/backend/engine.py](../platform/backend/engine.py)
- [experiment/platform/backend/api.py](../platform/backend/api.py)
- [experiment/platform/backend/analysis/prompt.py](../platform/backend/analysis/prompt.py)
- [experiment/platform/backend/analysis/fault_category_validator.py](../platform/backend/analysis/fault_category_validator.py)
- [experiment/platform/backend/analysis/service_localizer.py](../platform/backend/analysis/service_localizer.py)
- [experiment/platform/backend/storage/sqlite_storage.py](../platform/backend/storage/sqlite_storage.py)
- [experiment/eval/memory_hog/out/runs.csv](../eval/memory_hog/out/runs.csv)
- [experiment/eval/memory_hog/out/evaluation.csv](../eval/memory_hog/out/evaluation.csv)

---

## 2. Visão de alto nível: fases e fluxo de execução

O experimento segue 3 macrofases:

1. `TRAIN`:
	popula o corpus histórico (is_training=1) com execuções de caos em células serviço x falha.
2. `TEST`:
	executa células held-out (is_training=0), sem permitir vazamento dessas linhas para recuperação.
3. `EVAL`:
	roda múltiplas estratégias de RCA para cada run de teste e grava métricas de acerto/custo.

Sequência operacional por célula (service, chaos_type):

1. geração do spec do experimento;
2. criação da versão do experimento via API;
3. disparo do run;
4. execução da timeline (baseline, warmup, fault, post);
5. coleta de métricas/traces;
6. construção de features L1;
7. sumarização L2 + tags;
8. persistência em SQLite e object store;
9. etapa de análise LLM + pós-processamento determinístico (na fase eval ou consulta sob demanda).

---

## 3. Detalhamento fase a fase

## 3.1 Fase TRAIN

Objetivo:
criar exemplos históricos úteis para RAG, cobrindo múltiplos serviços e famílias de falha.

O que acontece:

1. O harness lê [experiment/eval/memory_hog/config.yaml](../eval/memory_hog/config.yaml).
2. Para cada célula de treino, gera `experiment_id` único e define `is_training=true`.
3. A plataforma executa o caos no cluster e grava artefatos.
4. As linhas entram no corpus recuperável para estratégias com RAG.

Por que importa:
se uma família de falha tem pouca cobertura ou exemplos muito homogêneos, o RAG tende a recuperar vizinhos enviesados.

## 3.2 Fase TEST

Objetivo:
medir generalização em células separadas do treino.

O que acontece:

1. O harness dispara as células de teste com `is_training=false`.
2. Essas linhas são avaliadas como alvo de diagnóstico.
3. Essas linhas não entram como vizinhas de recuperação.

Garantia importante:
o filtro por `is_training` evita auto-leakage de teste no RAG.

## 3.3 Fase EVAL

Objetivo:
comparar estratégias de análise RCA por acurácia e custo (tokens).

O que acontece:

1. Para cada run de teste, o harness chama `POST /api/runs/{id}/llm-analysis` em cada estratégia.
2. A API monta contexto, chama LLM, faz parse e executa pós-processadores determinísticos.
3. O resultado final e o resultado bruto são gravados em [experiment/eval/memory_hog/out/evaluation.csv](../eval/memory_hog/out/evaluation.csv).

---

## 4. Enriquecimento e sumarização de dados (L0 -> L1 -> L2)

## 4.1 L0: dados brutos

Fontes principais:

- métricas Prometheus via MCP;
- traces Tempo via MCP;
- eventos de execução (timeline/status);
- artefatos do caos/load.

Persistidos em object store por run:

- metrics_raw.json
- traces_raw.json
- load_summary.json
- chaos_artifacts.json

## 4.2 L1: features estruturadas

Construídas no engine com campos como:

- verdict de resiliência (resilient/degraded_recoverable/degraded_persistent);
- phase_metrics por baseline/warmup/fault/post;
- slo_violations e recovery_time_seconds;
- affected_services;
- metric_hotspots por métrica/serviço;
- top_failure_signatures e rca_hypotheses;
- propagation_graph (quando disponível).

Essas features alimentam tanto a etapa LLM quanto a etapa determinística.

## 4.3 L2: resumo para recuperação e prompt

Do L1, o sistema deriva:

- summary_text textual padronizado;
- tags estruturadas (verdict, serviço, violação, etc.);
- embedding vetorial do resumo anonimizado.

As entradas L2 são usadas para ranking de vizinhos e montagem de prompt.

---

## 5. O que cada baseline/estratégia muda

Estratégias avaliadas:

- S0_no_rag: sem vizinhos (limit=0), usa apenas target + system card.
- S1_rag_tags: vizinhos por similaridade de tags.
- S2_rag_embeddings: vizinhos por similaridade vetorial.
- S3_rag_hybrid: combinação embedding + semelhança de grafo de propagação.
- S4_rag_embeddings_no_card: embedding sem system card.
- S5_rag_short_budget: similar ao embedding com orçamento de prompt reduzido.

Interpretação funcional:

- S1 testa valor de metadados simbólicos.
- S2 testa valor semântico do resumo.
- S3 testa se topologia/cascata agrega sinal além da semântica.
- S4 mede impacto da remoção de conhecimento estrutural explícito.
- S5 mede trade-off custo vs qualidade.

---

## 6. Quais dados são apresentados ao modelo

Pipeline de montagem em [experiment/platform/backend/analysis/prompt.py](../platform/backend/analysis/prompt.py):

1. System prompt de papel técnico.
2. System card (se habilitado) com:
	- serviços válidos de RCA;
	- categorias de falha permitidas;
	- contexto arquitetural.
3. Bloco do run alvo:
	- run_id;
	- tags filtradas para reduzir leakage;
	- summary_text com máscara de campos que entregam ground truth.
4. Blocos de runs recuperados:
	- até K vizinhos;
	- score e verdict;
	- citation id para rastreabilidade.
5. Schema JSON obrigatório da resposta.

Mecanismos anti-leakage relevantes:

- remoção de `chaos_type` explícito do target summary;
- remoção de `experiment_id` do target summary;
- remoção de lista de affected_services no target summary;
- remoção de seções do target que expõem diretamente epicentro.

---

## 7. Etapa determinística pós-LLM

Ordem em [experiment/platform/backend/api.py](../platform/backend/api.py):

1. parse do JSON do LLM;
2. `validate_fault_category`;
3. `localize_service`;
4. gravação de `decision_provenance` com origem de cada campo.

## 7.1 FaultCategoryValidator (o porquê)

Em [experiment/platform/backend/analysis/fault_category_validator.py](../platform/backend/analysis/fault_category_validator.py), regras de override por sinal de hotspot:

- memory-exhaustion;
- cpu-exhaustion;
- pod-failure;
- network-latency;
- http-error.

Política de segurança:

- sem sinal forte: não sobrescreve;
- múltiplas regras fortes em conflito: não sobrescreve;
- exatamente uma regra forte: sobrescreve e registra evidência.

## 7.2 ServiceLocalizer (o onde)

Em [experiment/platform/backend/analysis/service_localizer.py](../platform/backend/analysis/service_localizer.py), ancoragem por categoria:

- memory: oom_killed/memory saturation;
- cpu: cpu_throttled/cpu saturation;
- pod-failure: pod_restarts_total;
- network-latency: maior delta de latência;
- http-error: maior spike de error_rate.

Também segue política conservadora (empate/ambiguidade não sobrescreve).

---

## 8. Ajustes recentes incorporados

Atualizações já aplicadas no código:

1. suporte explícito a network-latency e http-error no validador determinístico;
2. localização de serviço ajustada para usar delta do sinal causal (e não apenas valor absoluto em fault window);
3. isolamento de recuperação RAG por campanha:
	- `experiment_id`;
	- `experiment_version`;
	- `is_training_only`.

Efeito prático:

- reduz contaminação entre campanhas/versões;
- reduz risco de vizinhos historicamente irrelevantes;
- mantém separação treino/teste na recuperação.

---

## 9. Resultado mais recente: leitura diagnóstica

Resumo final (pós-override) informado no último ciclo:

- S0_no_rag: where 10%, why 10%, both 10%, avg_tok 2608
- S1_rag_tags: where 30%, why 20%, both 20%, avg_tok 4610
- S2_rag_embeddings: where 30%, why 20%, both 20%, avg_tok 4610
- S3_rag_hybrid: where 30%, why 20%, both 20%, avg_tok 4612
- S4_rag_embeddings_no_card: where 30%, why 0%, both 0%, avg_tok 3613
- S5_rag_short_budget: where 30%, why 10%, both 10%, avg_tok 3011

Resumo bruto (pré-override):

- overrides determinísticos dispararam em 23/60 resultados;
- S1 teve melhor where bruto (40%);
- S2/S3 não superaram S1 no bruto de forma consistente;
- S4 sem system card colapsou no eixo de categoria (why 0%).

Leitura principal:

1. Houve ganho real de localização (`where`) com RAG versus baseline sem RAG.
2. O gargalo persiste em categoria da falha (`why`), especialmente rede/http.
3. Sem system card, o modelo perde taxonomia e tende a rótulos genéricos.
4. Custo maior de tokens (S1-S3) ainda não converteu em ganho robusto de `both` além de 20%.

---

## 10. Validação recente dos cenários de rede

Execuções de verificação operacional realizadas:

- run-57ccc0712872: payment-network-latency, status completed, verdict degraded_recoverable;
- run-b56cd312d35b: shipping-network-loss, status completed, verdict degraded_recoverable.

Evidência de injeção:

- ChaosResult no Litmus com verdict Pass/Completed para os dois experimentos de rede.

Evidência de impacto métrico:

- network-latency: aumento relevante em latency_p95/p99 e subida de error_rate em serviços de borda;
- network-loss: aumento de latency_p95 e error_rate com efeito de cascata entre serviços.

Conclusão operacional:
as falhas de rede estão executando e impactando sinal observável; o problema atual está mais na interpretação/classificação RCA do que na injeção em si.

---

## 11. Onde investir tempo para corrigir

Prioridade alta:

1. Fortalecer taxonomia de falha para rede e HTTP:
	- regra explícita para network-loss no validador;
	- normalização de rótulos sinônimos (ex.: resource_exhaustion -> cpu/memory por evidência).
2. Reduzir respostas nulas (`rca=None`, `fault=None`):
	- endurecer parse/contrato de JSON;
	- retry com instrução de reparo de formato.
3. Melhorar sinal de comparação dos vizinhos:
	- aumentar diversidade de exemplos de rede no treino;
	- evitar dominância de vizinhos de memory/cpu quando o caso é rede.

Prioridade média:

1. Ajustar budget e compressão de prompt para preservar sinais críticos de rede.
2. Rebalancear pesos no híbrido para não favorecer casos semanticamente parecidos, mas causalmente diferentes.

Prioridade de validação estatística (após correções estruturais):

1. aumentar número de réplicas por célula;
2. aplicar teste pareado entre estratégias;
3. medir intervalo de confiança por família de falha.

Mensagem central:
agora o melhor retorno de engenharia está em corrigir cobertura causal e robustez de extração, não em apenas aumentar volume de execuções.

---

## 12. Checklist de revisão do fluxo

Use este checklist em cada iteração:

1. Injeção:
	ChaosResult Pass/Completed e janela de fault correta.
2. Observabilidade:
	spikes esperados em latency/error/cpu/memory conforme tipo de caos.
3. L1/L2:
	features e summary consistentes com os sinais reais.
4. Recuperação:
	vizinhos do mesmo experiment_id + experiment_version + is_training=1.
5. LLM:
	resposta JSON válida e com categoria em vocabulário permitido.
6. Determinístico:
	override apenas quando evidência unívoca; conflito auditável quando ambíguo.
7. Resultado final:
	comparar where/why/both e custo de token por estratégia.

---

## 13. Referências internas do repositório

- [experiment/docs/fluxo_experimento.md](./fluxo_experimento.md)
- [experiment/eval/memory_hog/README.md](../eval/memory_hog/README.md)
- [experiment/eval/memory_hog/config.yaml](../eval/memory_hog/config.yaml)
- [experiment/eval/memory_hog/runner.py](../eval/memory_hog/runner.py)
- [experiment/eval/memory_hog/out/runs.csv](../eval/memory_hog/out/runs.csv)
- [experiment/eval/memory_hog/out/evaluation.csv](../eval/memory_hog/out/evaluation.csv)
- [experiment/platform/backend/api.py](../platform/backend/api.py)
- [experiment/platform/backend/engine.py](../platform/backend/engine.py)
- [experiment/platform/backend/storage/sqlite_storage.py](../platform/backend/storage/sqlite_storage.py)
- [experiment/platform/backend/analysis/prompt.py](../platform/backend/analysis/prompt.py)
- [experiment/platform/backend/analysis/fault_category_validator.py](../platform/backend/analysis/fault_category_validator.py)
- [experiment/platform/backend/analysis/service_localizer.py](../platform/backend/analysis/service_localizer.py)
