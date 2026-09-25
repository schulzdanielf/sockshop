# Research Lab — Diário de evolução da pesquisa

Este diretório registra o **processo de evolução da pesquisa**: hipóteses, abordagens tentadas, resultados e planos de trabalho. O objetivo é manter um histórico navegável do *porquê* de cada decisão, não apenas do *o quê* foi implementado.

> **Contexto da pesquisa:** localização e explicação de causas raiz (RCA) em microsserviços (Sock Shop), usando chaos engineering (Litmus), observabilidade (Prometheus/Tempo/Loki via servidor MCP) e uma estratégia experimental 100% baseada em LLMs e multiagentes. Observabilidade também é um princípio do próprio processo: resultados e decisões precisam ser acompanháveis por evidências.

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

- **Abordagem ativa:** [003 — LLM multiagente para RCA](abordagens/003-llm-multiagente-rca.md) — exploração incremental
- **Hipóteses em exploração:** arquitetura especializada, investigação iterativa e MCP enriquecido melhoram RCA sem fixar ainda uma topologia de agentes
- **Plano vigente:** [Plano de experimentos rápidos multiagente](planos/2026-09-25-plano-llm-multiagente.md)
- **Histórico anterior:** [002 — Híbrido GAT + Agentes](abordagens/002-hibrido-gat-agentes.md), mantido para rastreabilidade
- **▶ Retomar o trabalho:** [PROXIMOS_PASSOS.md](PROXIMOS_PASSOS.md) — estado atual, experimentos pequenos e próximos incrementos

## Princípio de observabilidade

Cada experimento deve tornar visível o que foi executado, quais sinais sustentam a RCA, como os agentes chegaram à decisão e por que uma variante foi mantida, alterada ou descartada. Métricas, logs e traces observam o sistema sob investigação; os mesmos sinais também observam o comportamento e o custo da estratégia de agentes.
