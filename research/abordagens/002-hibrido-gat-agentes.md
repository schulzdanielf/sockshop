# 002 — Híbrido: GAT localiza (WHERE) + agentes de IA explicam (WHY)

- **Status:** ativa — **escopo atual: apenas o WHERE (H001)**; H002 pausada
- **Branch:** `hibrido` (criada em 2026-09-19)
- **Antecessora / baseline:** [001 — LLM + RAG + validadores](001-llm-rag-validadores.md)
- **Hipóteses testadas:** [H001](../hipoteses/H001-gat-localizacao-falhas.md), [H002](../hipoteses/H002-agentes-ia-motivo-falha.md)
- **Plano de trabalho:** [2026-09-19-plano-hibrido](../planos/2026-09-19-plano-hibrido.md)

## Decisões (2026-09-19)

- **Foco:** WHERE primeiro; WHY (agente) só depois de H001 avaliada.
- **Stack:** PyTorch + PyTorch Geometric.
- **Modelo inicial:** GAT (GATv2 como variação).
- **Métrica primária:** **top-3 accuracy** (top-1 e MRR secundárias).

## Ideia central

Separar explicitamente as duas perguntas do RCA e dar a cada uma o mecanismo mais adequado:

```
run (features + propagation_graph)
        │
        ▼
┌─────────────────────────┐     top-k serviços      ┌──────────────────────────┐
│  GAT — WHERE            │ ──────────────────────► │  Agente de IA — WHY      │
│  ranqueia serviço raiz  │   + janela do experimento│  ReAct + tools MCP       │
│  (aprende com runs)     │                          │  fault_category + explicação│
└─────────────────────────┘                          └──────────────────────────┘
                                                              │
                                            validadores 001 como guardrails (opcional)
```

- **WHERE (GAT):** modelo aprendido sobre o `propagation_graph` + hotspots por nó — substitui a heurística fixa de similaridade e o julgamento do LLM sobre texto.
- **WHY (agente):** investiga ativamente com as tools window-scoped do servidor MCP, ancorado nos top-k do GAT — substitui os thresholds manuais por evidência coletada sob demanda.
- **Guardrails:** validadores da abordagem 001 podem permanecer como rede de segurança, mas com a lição do run-82b832839337: **nunca sobrescrever quando a categoria âncora não for validada**.

## O que se reaproveita da abordagem 001

- Coleta (Litmus/Locust/MCP), features, `propagation_graph`, resumo L2, storage — **intocados**.
- `evaluation.csv` + `runner.py` como harness de avaliação (mesmos splits S1–S4, mesmas métricas).
- `audit.py` para inspeção offline do que foi enviado ao modelo.
- Infra de LLM local (`model/`: `react_agent.py`, `openai_compat.py`, Qwen via endpoints OpenAI-compatible).

## O que é novo

- Exportador de dataset de grafos (platform.db → amostras de treino do GAT).
- Modelo GAT (stack a definir: PyTorch Geometric é o candidato default).
- Agente WHY (ReAct) com as tools investigadoras do MCP.
- Integração em `api.run_llm_analysis`: GAT (WHERE) → agente (WHY) → guardrails.

## Critérios de sucesso da abordagem

- H001 validada (top-1 ≥ baseline + 10 p.p. em S2/S3 sem regressão em S1).
- H002 validada (≥ baseline em S1; +10 p.p. nas categorias sem validador; custo viável).
- Nenhum modo de falha novo sem guardrail correspondente.

## Registro de andamento

_(ver `research/diario/` — entradas datadas)_
