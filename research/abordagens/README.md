# Linha do tempo das abordagens

Cada abordagem representa uma tentativa de evolução do pipeline de RCA. Numeradas em ordem cronológica; uma por branch/fase.

| # | Abordagem | Branch / fase | Período | Status | Hipóteses |
|---|---|---|---|---|---|
| [001](001-llm-rag-validadores.md) | LLM + RAG + validadores determinísticos | `main` (fase F1) | 2026-04 → 2026-09 | concluida (virou baseline) | — |
| [002](002-hibrido-gat-agentes.md) | Híbrido: GAT (WHERE) + agentes de IA (WHY) | `hibrido` | 2026-09 | concluida como histórico | [H001](../hipoteses/H001-gat-localizacao-falhas.md), [H002](../hipoteses/H002-agentes-ia-motivo-falha.md) |
| [003](003-llm-multiagente-rca.md) | LLM multiagente especializado para RCA | exploração incremental | 2026-09 → | ativa | em definição experimental |

## Regras

- Uma abordagem só vira `concluida` quando suas métricas finais estão registradas no próprio arquivo.
- `abandonada` exige motivo registrado (ex.: custo, dataset insuficiente, refutação de hipótese).
- A abordagem vigente como **baseline de comparação** deve ter sua tabela de métricas congelada e referenciada pelas abordagens seguintes.
