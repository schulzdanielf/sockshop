# H002 — Agente de IA identifica o MOTIVO da falha melhor que LLM + validadores determinísticos

- **Status:** pausada (2026-09-19 — retomar após avaliação de H001)
- **Criada em:** 2026-09-19 (branch `hibrido`)
- **Testada por:** [Abordagem 002 — Híbrido GAT + Agentes](../abordagens/002-hibrido-gat-agentes.md)
- **Depende de:** [H001](H001-gat-localizacao-falhas.md) (o agente ancora no top-k de serviços do GAT)

## Motivação

- `fault_category` era a métrica mais fraca da abordagem 001 (**17–50%** de acurácia) antes dos validadores determinísticos.
- O `FaultCategoryValidator` resolveu os casos cobertos (mini-run: why 50%→100%), mas:
  - depende de **thresholds manuais** (OOM Δ>0.4, cpu_sat>80, cpu_throttled Δ>0.1) que precisaram de duas rodadas de correção (prioridade OOM sobre CPU; Δ negativo = pod substituído);
  - **só cobre memory/cpu/pod-failure** — as categorias novas do catálogo (network-latency, network-loss, http-error, container-kill) passam sem validação nenhuma;
  - a interação FaultValidator→Localizer já causou regressão (run-82b832839337).
- O servidor MCP já expõe tools investigadoras window-scoped (`check_oom_kills`, `check_pod_restarts`, `get_cpu_saturation_top`, `get_memory_saturation_top`, `check_http_error_rate_top`, `check_request_latency_top`) — um agente pode **investigar ativamente** em vez de depender de regras fixas.

## Hipótese formal

> Um agente de IA (ReAct) com acesso às tools investigadoras do servidor MCP, ancorado no(s) serviço(s) top-k indicados pelo GAT (H001) e na janela temporal do experimento, classifica a `fault_category` e produz a explicação causal com acurácia superior ao pipeline LLM + validadores determinísticos, **incluindo as categorias sem regra manual**.

## Métricas

- Acurácia de `fault_category` por split (S1–S4) e por categoria.
- **Cobertura:** nº de categorias tratadas sem regra manual nova.
- Qualidade da explicação causal (rubrica humana ou LLM-judge, a definir).
- **Custo:** nº médio de chamadas de tool e latência por análise (viabilidade prática).

## Critérios

- **Validada se:** ≥ baseline em S1 e +10 p.p. nas categorias hoje sem validador (network-latency, network-loss, http-error, container-kill), com custo ≤ ~10 chamadas de tool por run.
- **Refutada se:** o agente diverge/alucina chamadas de tool com frequência, ou o custo inviabiliza o ganho de acurácia.

## Riscos / questões abertas

- Backbone do agente: reusar `model/react_agent.py` + Qwen local (OpenAI-compatible) ou o planner MCP do notebook?
- Grounding temporal: o agente precisa receber a janela exata do experimento (lição do validador: lookbacks longos contaminam, ex. `increase(...[1h])`).
- Alucinação de PromQL/queries — o planner do notebook já mostrou esse modo de falha (chamada de tool sem args).
- Fallback: manter validadores determinísticos como guardrails quando a evidência for inequívoca?

## Registro de resultados

_(preencher conforme os experimentos rodarem — ver `research/diario/`)_
