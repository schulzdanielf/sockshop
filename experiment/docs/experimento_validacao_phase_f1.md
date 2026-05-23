# Experimento de validação — Phase F1 (estado atual)

Antes de gastar tempo otimizando seleção de features (Phase H) ou treinando GNN, você precisa de **uma evidência empírica honesta** de que o pipeline atual (SLO L1 + temporal + propagation graph + RAG + LLM verdict) **funciona melhor que baselines triviais e generaliza além do conjunto de treino**. Esse experimento é exatamente isso.

---

## 1. Pergunta de pesquisa

Três perguntas, em ordem de importância:

- **Q1 (utilidade):** o LLM verdict + RAG acerta o root cause com mais frequência que heurísticas triviais sobre o mesmo dado?
- **Q2 (valor do RAG):** runs históricos no contexto melhoram a acurácia vs. zero‑shot (mesmo LLM, sem vizinhos)?
- **Q3 (generalização):** o desempenho cai quanto entre (a) repetir um experimento conhecido, (b) variar o serviço alvo do mesmo fault type, (c) introduzir um fault type novo, (d) sistema novo?

Q3 é o coração do experimento. Sem ele você só mede memorização.

---

## 2. Design fatorial

Você tem 3 fault types × N microserviços. Vou usar Sock Shop (8 serviços relevantes: `front-end, catalogue, carts, orders, payment, shipping, user, queue-master`) como exemplo. Ajuste para os seus.

### 2.1 Eixos do fatorial

| Eixo | Níveis | Comentário |
|---|---|---|
| Fault type | `cpu-hog`, `memory-hog`, `pod-delete` | 3 |
| Serviço alvo | 8 serviços | 8 |
| Intensidade | low / high (ex.: `cpu-hog 50%` vs `90%`; `memory 60% vs 90%`; `pod-delete 1 vs N`) | 2 |
| Repetição | 3 réplicas por célula | controla variância |

**Total:** 3 × 8 × 2 × 3 = **144 runs**. Em torno de 5–10min por run (warmup+fault+post+coleta), são ~15–24h de máquina — viável em 2–3 dias com paralelização mínima ou rodando à noite.

> Se 144 for muito, comece com **3 × 8 × 1 × 2 = 48 runs** (uma intensidade só, 2 réplicas). É o mínimo viável.

### 2.2 Cargas (importante)
Mantenha a **mesma carga sintética** (ex.: `locust` com perfil fixo) em todas as runs. Sem isso, variação de carga vira variável de confusão e afunda o experimento.

### 2.3 Baseline (steady‑state)
Antes do bloco fatorial, rode **10–20 runs sem chaos** para alimentar o `baseline_means` por sistema. Isso já é feito hoje por run, mas ter um pool externo dá um baseline mais estável.

---

## 3. Splits anti‑leakage (o passo que define se o resultado vale algo)

Defina **quatro splits encaixados**, cada um respondendo um ângulo diferente do Q3:

| Split | Treino (vai pro RAG) | Teste | Mede |
|---|---|---|---|
| **S1 — Repetition** | 2 réplicas de cada célula | 3ª réplica | Reprodutibilidade. Teto superior. |
| **S2 — Held‑out service** | Todas as runs *exceto* `orders` e `payment` | Runs em `orders` e `payment` | Generalização para serviço novo, fault conhecido. |
| **S3 — Held‑out fault** | Todas as runs *exceto* `pod-delete` | Runs `pod-delete` | Generalização para fault novo, serviços conhecidos. |
| **S4 — Held‑out intensity** | Só `low` | Só `high` | Generalização para severidade não vista. |

S1 é o piso de sanidade; se você não acerta S1, há bug. S2/S3/S4 são os splits que de fato dizem se o método generaliza ou se está só memorizando.

Use time‑split dentro de cada célula (réplicas mais antigas no treino) para evitar leakage temporal trivial.

---

## 4. Modelos a comparar

Compare **4 sistemas** no mesmo conjunto de teste:

1. **B0 — Random:** chuta um serviço aleatório uniforme. Piso absoluto.
2. **B1 — Max error rate:** retorna o serviço com maior `error_rate` durante a fault window. Heurística trivial sem topologia.
3. **B2 — PageRank reverso ponderado por error_count nas arestas do `propagation_graph`.** Estado da arte clássico (MicroRCA‑like) sem LLM.
4. **B3 — RAG + LLM zero‑neighbors:** seu pipeline atual mas com `k_neighbors=0` (só features L1+temporal+graph da run atual no prompt). Mede Q2.
5. **M1 — RAG + LLM completo:** seu pipeline com `k_neighbors=3` (configuração atual). Modelo "produto".

Variante opcional:
6. **M2 — RAG + LLM hybrid retrieval:** Phase F1 com `mode=hybrid`. Compara contra M1 que usa `mode=embeddings`.

---

## 5. Métricas

Para cada (split, modelo), calcule:

- **Top‑1 RCA accuracy:** `verdict.rca == chaos_target` (label barato, automático).
- **Top‑3 RCA recall:** o `chaos_target` está nos 3 primeiros candidatos do verdict.
- **Confiança calibrada (Brier score)** do `confidence` do verdict.
- **Abstention rate:** % de runs em que o verdict diz "unknown/insufficient_data".
- **Latência end‑to‑end** (engine → verdict) e **tokens de prompt** — para custo.
- **Por sistema:** matriz de confusão `chaos_type × predicted_service` para entender onde quebra.

Reporte **médias com IC 95%** via bootstrap (1000 resamples) — sem isso, com 144 runs, qualquer Δ de 2pp é ruído.

**Teste estatístico:** McNemar pareado (M1 vs B2, M1 vs B3) — runs são pareadas, exatamente o caso do McNemar.

---

## 6. Pipeline de execução (sem código, só os blocos)

```
1. Setup
   - Define matriz (fault, serviço, intensidade, réplica) → CSV manifesto
   - Garante locust rodando, baseline coletado, MCP servers ativos
   - Limpa SQLite ou marca tag de experimento (run_features.experiment_tag)

2. Coleta (loop)
   Para cada linha do manifesto:
     a. Reseta cluster (espera baseline estável: error_rate < ε, p95 < β)
     b. Inicia janela baseline (30s)
     c. Dispara chaos via litmus (chaos_target = serviço da linha)
     d. Janela fault (60s)
     e. Para chaos
     f. Janela post (120s)
     g. Coleta tudo via plataforma → run_features salvo
     h. Persiste chaos_target/chaos_type/intensity como ground truth na run

3. Splits
   - Script gera 4 CSVs (S1.csv ... S4.csv) com colunas: run_id, split_role ∈ {train,test}

4. Avaliação
   Para cada split, para cada modelo:
     - Reconstrói RAG só com run_ids do treino
     - Para cada run_id do teste, invoca o modelo
     - Compara verdict.rca vs chaos_target
     - Persiste em eval_results(split, model, run_id, predicted, correct, confidence, tokens, latency_ms)

5. Relatório
   - Tabelas: accuracy por (split, model) com IC 95%
   - Heatmaps: matriz de confusão por modelo
   - Quebras: accuracy por chaos_type, por serviço, por intensidade
```

A camada de **avaliação não precisa rerodar chaos** — ela só replays sobre `run_features` já coletado. Isso torna o experimento iterável (mudou o prompt? roda só a etapa 4).

---

## 7. O que conta como "resultado promissor"

Defina os critérios **antes** de rodar, pra evitar p‑hacking. Sugestão:

| Pergunta | Critério mínimo | Critério forte |
|---|---|---|
| Q1 (utilidade) | M1 > B1 em ≥3 dos 4 splits, ΔTop‑1 ≥ +10pp em S1 com p<0.05 (McNemar) | M1 > B2 também, ΔTop‑1 ≥ +5pp |
| Q2 (valor RAG) | M1 > B3 em S2 e S3, ΔTop‑1 ≥ +5pp | ΔTop‑1 ≥ +10pp e Brier melhor |
| Q3 (generalização) | Acurácia em S2/S3 fica em ≥70% da acurácia em S1 | ≥85% |

Se M1 ≈ B2 em todos os splits, o LLM não está agregando — útil saber, e aí o próximo passo vira "melhorar prompt/features" em vez de "comprar GPU melhor". Negativo é resultado.

---

## 8. Generalização para sistema novo (passo extra opcional)

Quando você quiser ir além do Sock Shop, **repita o mesmo design** num segundo sistema (ex.: Online Boutique / hipster-shop do Google) e meça:

- **Cross‑system zero‑shot:** RAG construído só com Sock Shop, testado em Online Boutique. Acurácia provavelmente cai forte — é o número honesto da generalização cross‑system.
- **Cross‑system few‑shot:** adiciona N runs do sistema novo ao RAG e mede como acurácia escala com N (curva de aprendizado). Útil para responder "quantas runs eu preciso pra adaptar a um sistema novo".

Esse é o gancho natural para defender que a abordagem é "plataforma" e não "modelo treinado num sistema".

---

## 9. Riscos práticos a planejar agora

1. **Litmus flakiness:** `pod-delete` pode falhar em recriar; adicione retry e descarte runs onde a fault não foi detectada nas métricas (sanity check: `error_rate` ou `p95` realmente subiu).
2. **Carga inconsistente:** monitore `request_rate` durante baseline; rejeite runs com desvio >20%.
3. **Cluster ruidoso:** intercale runs (não rode todo `cpu-hog` em sequência) — drift de cache/JIT vira artefato.
4. **Ground truth ambíguo:** `pod-delete` em `front-end` derruba todo mundo; o `chaos_target` ainda é `front-end` mas o LLM pode responder o que falhou primeiro. Defina a regra **antes**: acerto exige nomear o serviço onde o chaos foi injetado, não onde o sintoma apareceu.
5. **Tempo de LLM:** 144 runs × 5 modelos = 720 invocações. Com Qwen 14B local em ~15s/invocação, ~3h só de inferência. Cache obrigatório por `(run_id, model_id, prompt_hash)`.
6. **Determinismo:** seed fixa no LLM (`temperature=0`), seed no locust, seed no plugin de embedding hash. Sem isso você não reproduz nada.

---

## 10. Entregáveis mínimos do experimento

1. **CSV de runs** com `(run_id, chaos_type, chaos_target, intensity, replica, started_at)` + `experiment_tag`.
2. **CSV de resultados** com `(split, model, run_id, predicted_rca, correct, confidence, tokens, latency_ms)`.
3. **Notebook de análise** (`experiment/notebooks/eval_phase_f1.ipynb`) que gera:
   - Tabela principal (accuracy por split × modelo, IC 95%).
   - Heatmap de confusão por modelo.
   - Quebra por fault type e por serviço.
   - Resultado dos McNemar pareados.
4. **Seção no README** "Validation results" com a tabela principal e a interpretação (positiva ou negativa — ambas valem).

---

## Resumo em 5 linhas

Rode **48–144 runs num fatorial (3 faults × N serviços × intensidade × réplicas)** num único sistema, com carga e baseline padronizados. Avalie **5 modelos** (random, max‑error, PageRank, RAG zero‑shot, RAG completo) sobre **4 splits encaixados** (repetition, held‑out service, held‑out fault, held‑out intensity), usando **top‑1/top‑3 RCA com IC bootstrap e McNemar pareado**. Critérios de sucesso definidos *antes* de rodar. A etapa de avaliação reusa `run_features` já coletado, então variar prompt/modelo é barato. Esse é o teste mínimo viável para dizer "Phase F1 tem resultado promissor" com honestidade — e a base sobre a qual Phase H (otimização de features) e GNN (Etapa 4) ficam justificáveis.
