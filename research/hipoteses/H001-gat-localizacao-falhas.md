# H001 — GAT localiza o serviço raiz melhor que ranker híbrido / LLM

- **Status:** **não suportada** — refutada para topologia estática (2026-09-20) e não suportada com edge features reais Istio (2026-09-20, indeterminada por insuficiência de dados full: 25/218). Aguarda expansão do dataset para veredito definitivo.
- **Criada em:** 2026-09-19 (branch `hibrido`)
- **Testada por:** [Abordagem 002 — Híbrido GAT + Agentes](../abordagens/002-hibrido-gat-agentes.md)
- **Abordagem baseline de comparação:** [001 — LLM + RAG + validadores](../abordagens/001-llm-rag-validadores.md)
- **Setup decidido:** PyTorch + PyTorch Geometric; modelo inicial **GAT** (GATv2 como variação posterior)

## Motivação

A abordagem 001 resolve o WHERE (qual serviço) via LLM sobre resumo L2 + RAG (ranker híbrido: similaridade de embedding + similaridade de grafo de propagação em `retrieval.score_graph_similarity`). Limitações observadas:

- A similaridade de grafo atual é uma **heurística de comparação**, não um modelo aprendido — não melhora com o volume de runs acumulados no `platform.db`.
- O `ServiceLocalizerValidator` já sobrescreveu um RCA **correto** do LLM (run-82b832839337) ao ancorar numa `fault_category` não validada — evidência de que regras determinísticas com thresholds manuais são frágeis como mecanismo principal de localização.
- O pipeline **já produz** `features.propagation_graph` (cascade_order, edges, coletado por fase via traces do Tempo) — estrutura pronta para alimentar um modelo de grafo sem custo adicional de instrumentação.

## Hipótese formal

> Um Graph Attention Network (GAT) treinado sobre o grafo de propagação do run — nós = serviços, features de nó = hotspots de métricas por fase, arestas = chamadas observadas nos traces — ranqueia o serviço raiz da falha com acurácia top-1/top-3 superior ao ranker híbrido atual e ao LLM, **especialmente nos splits anti-leakage S2 (held-out service) e S3 (held-out fault)** definidos em `experiment/docs/experimento_validacao_phase_f1.md`.

## Esquema do grafo (decidido 2026-09-19)

- **Nós = serviços da Sock Shop.** Features de nó: as **3 golden metrics por serviço** (request rate, tempo de resposta, % de erro). *Sem métricas de recurso (cpu/mem/oom/restarts)* — decisão consciente de generalização: resource metrics medem o sintoma da máquina específica, não do serviço; além disso os containers têm poucos recursos e alta variância.
- **Arestas = chamadas observadas** (service graph derivado dos traces; fonte: métricas agregadas de span, não spans brutos). Features de aresta: as mesmas 3 golden metrics **por chamada A→B**. **Bidirecionais com edge_attr marcando a direção real** (caller→callee) — a atenção aprende qual sentido carrega o sinal de propagação.
- **Encoding:** **log-ratio** $\log((x+\epsilon)/(baseline+\epsilon))$ — resolve baseline de erro ≈ 0, centraliza em 0 (sem mudança = 0) e simetriza subida/queda. Direção sempre período/baseline.
- **Granularidade temporal:** **um grafo por run** (snapshot único, razão fault/baseline) — sem modelo temporal na v0.
- **Janela de fault com recorte encurtado:** falhas transientes (container-kill, pod-delete) têm estímulo de segundos e sintoma breve dentro de uma fase de fault longa; calcular a razão sobre a fase inteira **dilui o pico** e cria ruído de label (label diz "falhou", features dizem "normal"). Decisão: janela = primeiros ~2–3 min após o disparo do chaos (com cap no fim real da fase), ancorada na duração configurada no chaos manifest quando disponível (transiente → curta; persistente → até o fim da injeção).
- **Agregador robusto em janelas curtas:** manter **count** (nº de spans/requisições) como feature auxiliar de confiança junto de cada razão — error% com 3 spans não vale o mesmo que com 20.000.
- **Sem IDs de serviço** nas features (indutividade para S2).

## Métricas

- **Top-3 accuracy (primária):** acerto = serviço raiz entre os 3 primeiros do ranking.
- **Top-1 accuracy** e **MRR** (secundárias, para comparabilidade com a literatura).
- Comparadores: (a) LLM + validadores (abordagem 001), (b) heurística trivial de centralidade/grau do grafo, (c) ranker híbrido atual isolado.

## Critérios

- **Validada se:** **top-3** ≥ baseline + 10 p.p. em S2 e S3, sem regressão em S1.
- **Refutada se:** não supera a heurística trivial de centralidade, ou overfita (S1 alto, S2/S3 no nível do acaso) — indício de leakage ou dataset insuficiente.

> **Revisão dos splits (decisão 2026-09-21):** S2 (held-out service) mede algo
> **artificial** — na prática é viável injetar falha em orders/payment e treinar.
> A generalização operacionalmente relevante é **falha nova em serviço conhecido**
> (S3) e **combinação falha × serviço não vista** (um possível S2'). **Decisão:**
> S2 atual permanece como **diagnóstico** (detector de viés de posição — foi ele
> que expôs o defeito do GAT), **não** como critério de validação. O peso da
> validação desloca-se para S3 e, se adotado, um futuro S2'. Mudança S2→S2'
> (altera `build_dataset`) fica **adiada** para depois da coleta. Detalhes em
> [research/diario/2026-09-21-hipoteses-mlp-supera-gat.md](../diario/2026-09-21-hipoteses-mlp-supera-gat.md).

## Riscos / questões abertas

- **Tamanho do dataset:** quantos runs rotulados existem hoje? (inventário é a tarefa 0 do plano). GNN pode exigir augmentation ou grafos sintéticos.
- **S2 (held-out service)** implica nó nunca visto no treino → features devem ser relativas (log-ratio vs baseline), nunca IDs de serviço, para o modelo ser indutivo.
- **Vazamento topológico:** mesmo sem IDs, a topologia fixa da Sock Shop identifica serviços (front-end = alto grau de saída, queue-master = folha). Em S2 o modelo pode memorizar posição no grafo em vez de aprender a assinatura da falha. Sem correção barata na v0 — registrar na discussão.
- **Cobertura de golden metrics:** cpu-hog/memory-hog manifestam-se indiretamente (latência ↑, erro pode nem subir). Risco aceito conscientemente: se H001 falhar só nessas categorias, vira achado delimitador da tese.
- **Falhas transientes (container-kill, pod-delete):** sinal intrinsecamente mais fraco e breve; o recorte de janela maximiza o sinal existente, mas se S1 mostrar essas categorias sistematicamente abaixo das persistentes, é achado (natureza da falha), não bug.
- **Fase ≠ sintoma:** as fases do run são definidas pelo schedule do experimento, não pelo sintoma observado. Em falhas persistentes coincidem; em transientes divergem. A v0 ancora no disparo + duração do manifest, não em detecção de anomalia.
- **Papel do GAT:** substituir o LLM no WHERE ou votar com ele (ensemble)? Decisão adiada para a Fase 3.
- Desbalanceamento de categorias no catálogo de faults (7 tipos hoje: cpu-hog, memory-hog, pod-delete, network-latency, network-loss, http-status-code, container-kill).

## Registro de resultados

**2026-09-20 — REFUTADA para a Opção D (topologia estática + arestas neutras).**

Detalhes em [research/diario/2026-09-20-gat-resultado.md](../diario/2026-09-20-gat-resultado.md).

| Split | GAT top-3 (3 seeds) | B1 max-anomaly | Veredito |
|---|---|---|---|
| S1 | 0.50 ± 0.10 | 0.59 | GAT < B1 |
| S2 | 0.14 ± 0.06 | 0.60 | GAT << B1 (abaixo do acaso) |
| S3 | 0.43 ± 0.08 | 0.46 | GAT < B1 |

- Critério de validação (top-3 ≥ B1 + 10 p.p. em S2/S3 sem regressão em S1): **não atingido em nenhum split**.
- **Mecanismo:** viés de posição topológica. Com topologia estática, o GAT memoriza identidade de nó; o split S2 (held-out orders/payment) o expõe — B1 localiza orders em 87%/payment em 32%, mas o GAT colapsa para 0.14 porque nunca treinou a apontar essas posições.
- **Baselines já anunciavam:** B2 (difusão na topologia) < B1 (sem topologia) — a estrutura estática é ativamente prejudicial.
- **Em aberto:** a hipótese de que um GAT com **edge features reais** (telemetria forte, Pixie/mesh) supere B1 permanece não testada.
- **Ablação recomendada:** MLP por-nó (sem grafo) sobre as mesmas features — se ≥ GAT, confirma que a estrutura estática é o problema.

### Ablação executada (2026-09-20) — conclusão definitiva

`experiment/eval/gat/mlp_ablation.py`: MLP por-nó (sem grafo, mesmas features).

| Split | GAT (grafo) | MLP (sem grafo) | B1 |
|---|---|---|---|
| S1 | 0.50 | 0.53 | 0.59 |
| S2 | 0.14 | **0.48** | 0.60 |
| S3 | 0.43 | **0.86** | 0.46 |

O MLP supera o GAT (S2/S3 dramaticamente) e bate B1 no S3. Prova isolada de que a
**topologia estática é prejudicial** (injeta viés de identidade de nó). O sinal
está nas node features, aprendível por-nó e transferível para faults novos
(S3=0.86). Para um GNN agregar valor aqui, edge features precisam variar por run.

---

**2026-09-20 — Re-teste com edge features reais (Istio): NÃO SUPORTADA.**

Detalhes em [research/diario/2026-09-20-gat-resultado-istio-full.md](../diario/2026-09-20-gat-resultado-istio-full.md).

Com 25 grafos full coletados via Istio, o GAT foi re-treinado (GPU, 3 seeds):

| Split | GAT top-3 | MLP | B1 | GAT-full subset (seed 0) |
|---|---|---|---|---|
| S1 | 0.48 ± 0.14 | 0.61 | 0.45 | **0.59 (n=22)** > B1 0.45 |
| S2 | 0.24 ± 0.07 | 0.56 | 0.58 | 0.00 (n=7) |
| S3 | 0.40 ± 0.03 | 0.63 | 0.47 | 0.14 (n=7) |

- **Critério (top-3 ≥ B1+10 p.p. em S2/S3): não atingido** — GAT < B1 em S2/S3.
- **Sinal fraco a favor em S1-full** (0.59 vs 0.45): quando treinado/testado com
  edges reais, o GAT superou B1. Mas o treino S1 tinha só **3 grafos full** (vs
  193 static) — o modelo mal aprende a usar arestas.
- **S2 permanece bloqueado mesmo com edges reais** (full = 0.00): o viés de
  identidade de serviço held-out não é resolvido por edge features.
- **MLP ≥ GAT em todos os splits** — a estrutura continua subtraindo valor.
- **Causa raiz do resultado baixo:** (a) fome de dados reais (25/218; treino S1
  com 3 full), (b) generalização S2 estruturalmente bloqueada.
- **Status atualizado:** hipótese **não suportada** na configuração atual;
  indeterminada por insuficiência de dados com mesh. Plano de expansão:
  [research/planos/2026-09-20-expansao-dataset-gat.md](../planos/2026-09-20-expansao-dataset-gat.md).
