# 001 — LLM + RAG + validadores determinísticos (baseline)

- **Status:** concluida (baseline oficial de comparação)
- **Período:** 2026-04 → 2026-09 (branch `main`, fase F1)
- **Sucessora:** [002 — Híbrido GAT + Agentes](002-hibrido-gat-agentes.md)

## Arquitetura

Pipeline em `experiment/platform/backend/`:

1. **Coleta:** chaos (Litmus) + carga (Locust) + métricas (Prometheus via MCP) + traces (Tempo via MCP), tudo window-scoped por fase do run.
2. **Features:** hotspots de métricas por serviço/fase, `propagation_graph` (cascade_order, edges), eventos de saúde de pods (OOM, restarts).
3. **Resumo L2:** renderização textual das features para o prompt (`summary.py`).
4. **RAG:** recuperação de runs históricos similares — ranker híbrido = similaridade de embedding (cosseno) + similaridade de grafo de propagação (`retrieval.score_graph_similarity`).
5. **LLM verdict:** Qwen local (endpoints OpenAI-compatible) produz JSON com `rca` (serviço) + `fault_category` + raciocínio.
6. **Validadores determinísticos** pós-LLM:
   - `FaultCategoryValidator` — regras sobre `metric_hotspots` (OOM Δ>0.4 → memory-exhaustion; cpu_sat>80 → cpu-exhaustion; restarts Δ>0.5 sem OOM → pod-failure; CPU suprimido quando OOM dispara).
   - `ServiceLocalizerValidator` — ancorado na categoria corrigida, escolhe serviço dominante pela métrica da categoria (tolerância de empate 25%).
   - 1 retry da API em caso de parse error / rca vazio.

## Evolução dentro da abordagem (lições aprendidas)

1. LLM zero-shot → acurácia de `fault_category` fraca (17–50%).
2. + RAG híbrido (embedding + grafo) → identificação de serviço forte no cenário híbrido.
3. + FaultCategoryValidator → mini-run (n=2×2): why 50%→100%.
4. + ServiceLocalizerValidator + retry → where 50%→100%, ambos 50%→100%.
5. Correções v2: prioridade OOM sobre CPU (reclaim de kernel infla CPU em memory-hog); Δ **negativo** em contador cumulativo = pod substituído (assinatura de pod-delete).

## Modos de falha conhecidos (motivadores da abordagem 002)

- **Thresholds manuais frágeis:** run-82b832839337 (user/cpu-hog) — sinal de CPU fraco (throttled Δ≈0.06 < 0.10) não disparou o validador; o localizer ancorou na categoria errada do LLM e **sobrescreveu um RCA correto**.
- **Cobertura parcial:** validadores só cobrem memory/cpu/pod-failure; network-latency, network-loss, http-error e container-kill passam sem validação.
- **Ranker de grafo não aprende:** `score_graph_similarity` é heurística fixa; não melhora com o acúmulo de runs.

## Métricas finais (baseline congelado)

> TODO: congelar aqui a tabela de resultados nos splits S1–S4 (where/why top-1) a partir do `evaluation.csv`, antes dos experimentos da abordagem 002. Ferramenta de apoio: `experiment/eval/memory_hog/audit.py` (audita prompt/contexto sem rodar o LLM).

| Split | where top-1 | why top-1 | n |
|---|---|---|---|
| S1 | — | — | — |
| S2 | — | — | — |
| S3 | — | — | — |
| S4 | — | — | — |
