# Research Lab — Diário de evolução da pesquisa

Este diretório registra o **processo de evolução da pesquisa**: hipóteses, abordagens tentadas, resultados e planos de trabalho. O objetivo é manter um histórico navegável do *porquê* de cada decisão, não apenas do *o quê* foi implementado.

> **Contexto da pesquisa:** localização de falhas (WHERE) e identificação da causa raiz (WHY) em microsserviços (Sock Shop), combinando chaos engineering (Litmus), observabilidade (Prometheus/Tempo/Loki via servidor MCP), LLMs e — na branch `hibrido` — Graph Attention Networks + agentes de IA.

## Estrutura

| Diretório | Conteúdo | Convenção de nomes |
|---|---|---|
| `hipoteses/` | Hipóteses formais da pesquisa, com critérios de validação | `H###-slug.md` |
| `abordagens/` | Linha do tempo das abordagens tentadas (uma por branch/fase) | `###-slug.md` (ordem cronológica) |
| `diario/` | Entradas datadas do diário de pesquisa (o que foi feito, achados, bloqueios) | `YYYY-MM-DD-slug.md` |
| `planos/` | Planos de trabalho | `YYYY-MM-DD-slug.md` |

## Convenções

- **Status de hipótese:** `ativa` → `em-teste` → `validada` | `refutada` | `pausada`
- **Status de abordagem:** `ativa` | `concluida` | `abandonada` (com motivo registrado)
- Toda hipótese referencia a(s) abordagem(ns) que a testam, e vice-versa.
- Toda entrada de diário linka as hipóteses/abordagens tocadas naquele dia.
- Resultados quantitativos vão primeiro no diário; quando conclusivos, são resumidos na hipótese.
- Documentos de experimento detalhados (setup, design fatorial, splits) continuam em `experiment/docs/`.

## Estado atual

- **Abordagem ativa:** [002 — Híbrido GAT + Agentes](abordagens/002-hibrido-gat-agentes.md) (branch `hibrido`)
- **Hipóteses ativas:** [H001 — GAT localiza falhas](hipoteses/H001-gat-localizacao-falhas.md), [H002 — Agentes identificam o motivo](hipoteses/H002-agentes-ia-motivo-falha.md)
- **Plano vigente:** [Plano de trabalho da abordagem híbrida](planos/2026-09-19-plano-hibrido.md)
- **Mapeamento de mudanças:** [2026-09-19-mapeamento-evolutivo](planos/2026-09-19-mapeamento-evolutivo.md) (coleta, gates/checklist, avaliação, observabilidade)
