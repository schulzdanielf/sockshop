# Research Lab — Diário de evolução da pesquisa

Este diretório registra o **processo de evolução da pesquisa**: hipóteses, abordagens tentadas, resultados e planos de trabalho. O objetivo é manter um histórico navegável do *porquê* de cada decisão, não apenas do *o quê* foi implementado.

> **Contexto da pesquisa:** localização e explicação de causas raiz (RCA) em microsserviços (Sock Shop), usando chaos engineering (Litmus), observabilidade (Prometheus/Tempo/Loki via servidor MCP) e conhecimento diagnóstico contextualizado para investigação autônoma. LLMs, RAG, skills e multiagentes são alternativas experimentais, não compromissos prévios da contribuição científica. Observabilidade também é um princípio do próprio processo: resultados e decisões precisam ser acompanháveis por evidências.

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

- **Auditoria em 14/11/2026:** [avaliação acadêmica da RAG nos 70 runs](./diario/2026-11-14-auditoria-academica-rag-70-runs.md) — 1.642 consultas MCP históricas novas, matriz por run/fase, testes adversariais e revisão de experimento, corpus, projeção e comparação; plano de correções e ablações, sem novas injeções ou inferências RCA.
- **Complementação em 08/11/2026:** [plano dos seis casos](./planos/2026-11-08-complementacao-seis-casos-treino-teste.md) e [análise de limiares](./planos/2026-11-08-proposta-limiares-estabilidade-complementacao.md) — os dois controles sem falha originais usaram preparação menor que a planejada; `orders`/`user` reprovaram estabilidade. O executor foi alinhado para 180 s de warmup, duas janelas de 60 s e baseline de 180 s; novos controles ainda pendentes. Nenhuma injeção ou inclusão em treino/teste ocorreu.
- **Revisão Skill/RAG em 08/11/2026:** [redes, HTTP e plano científico](./diario/2026-11-08-revisao-skill-rag-redes-plano-cientifico.md) — quatro avaliações históricas, 24 diagnósticos de rede/HTTP, auditoria dos Bs e 209 consultas MCP novas; identifica perda de IDs, seleção de traces e falsos corroboradores, com plano simétrico e controles pareados. Relatório somente, sem alterar executores ou produzir novos scores.
- **Revisão metodológica em 08/11/2026:** [validade do experimento HTTP status code](./2026-11-08-validade-experimento-http-status-code.md) — mecanismo Istio/Envoy, reação da Sock Shop, telemetria, indicador FI, auditoria dos Bs e comparação com literatura; separa validade do abort no mesh de generalização para erros naturais e propõe controles para o artigo.
- **Auditoria em 07/11/2026:** [A, recuperacao RAG e plano de ajustes](./planos/2026-11-07-auditoria-a-recuperacao-rag-plano-ajustes.md) — 64 As auditados, rankings locais reproduzidos, perdas de termos metricos e entrega de guias, decomposicao por servico/falha e ablações de recuperacao; plano versionado, sem alterar producao ou executar novas RCAs.
- **Teste e melhoria da recuperacao em 07/11/2026:** [relatorio da RAG v5](./diario/2026-11-07-relatorio-melhoria-recuperacao-rag.md) — comparacao retrieval-only das estrategias em 32 casos, implementacao do BM25 por campo com selecao IDF e codigos HTTP preservados; acompanhamento testa hipoteses de assinatura por campo e recomenda validar `signature32` prospectivamente.
- **Plano em 03/11/2026:** [evolução da RAG e treino único de 35 experimentos](./planos/2026-11-03-evolucao-rag-treino-35-historico-online.md) — auditoria retrospectiva de A/B, recuperação e saídas Qwen; distinção entre conclusão do run e efeito validado; etapas aprováveis para corpus Qwen 3.5, dual-write histórico, reanotação posterior e avaliação exclusivamente online via MCP. Apenas planejamento, sem execução.
- **Revisão crítica em 31/10/2026:** [projeto, estratégias de RAG e prioridades](../docs/avaliacao-critica-projeto-rag-2026-10-31.md) — síntese da arquitetura, comparação separada dos resultados operacionais e do replay v4, limites do corpus exploratório e recomendações de engenharia, avaliação e documentação. O manifesto v5 foi localizado sem relatório final disponível na leitura; não se presume ganho das correções.
- **Estrategia consolidada em 26/10/2026:** [RAG para RCA com conjuntos A/B](./2026-10-26-estrategia-rag-rca-conjuntos-a-b.md) — Skill RCA, campanha de 70 runs (35 treino/35 teste), A/B gerados pelo modelo sem aprovacao humana obrigatoria, isolamento do gabarito, coleta ao vivo e plano de ajustes do experimento e da avaliacao. Fluxo implementado com testes automatizados; campanha real e validacao de eficacia pendentes.
- **Direcionamento científico em 21/10/2026:** [Metodologia de conhecimento diagnóstico contextualizado](./2026-10-21-metodologia-conhecimento-diagnostico-contextualizado.md) — contexto da aplicação, conhecimento experimental contrastivo e evidência do incidente, preservando autonomia de investigação e avaliação independente. Proposta ainda não validada.
- **Plano de avaliação preliminar em 21/10/2026:** [Experimentos, armazenamento e recuperação](./planos/2026-10-21-avaliacao-preliminar-metodologia.md) — condições comparativas, splits, piloto, corpus imutável, isolamento do gabarito, replay, recuperação do histórico e critérios de aceite. Não implementa nem executa os experimentos.
- **Plano de estratégias configuráveis em 17/10/2026:** [Estratégias configuráveis e comparação reproduzível de RCA](./planos/2026-10-17-estrategias-configuraveis-rca.md) — mapa dos executores, perfis versionados, prioridades de tools/agentes, replay estrito e protocolo de comparação.
- **Correções prioritárias em 17/10/2026:** [Próximos passos](./planos/2026-10-17-proximos-passos.md) — avaliação das sete sugestões por esforço/retorno, reaproveitamento do código e critérios de aceite para melhorar a skill.
- **Avaliação consolidada em 17/10/2026:** [progresso RAG → skill → multiagentes e agenda para artigo](./progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md) — código, resultados locais, dificuldades históricas, workflow de avaliação e bloqueios científicos. Atualiza o diagnóstico sem substituir os registros históricos abaixo.
  - [Resumo dos experimentos realizados](./progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md#a-resumo-dos-experimentos-realizados).
  - [Workflow operacional para avaliar o resultado da skill](./progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md#b-workflow-a-aplicar-para-avaliar-o-resultado-da-skill), com comandos, revisão causal e critérios de decisão.
- **Abordagem de implementação em exploração:** [003 — LLM multiagente para RCA](abordagens/003-llm-multiagente-rca.md), preservada como alternativa e histórico; o novo direcionamento não pressupõe superioridade multiagente.
- **Hipóteses prioritárias propostas:** contexto operacional e conhecimento experimental podem melhorar correção, sustentação e transferência do diagnóstico; seu valor incremental precisa ser testado com evidência comum e orçamento comparável.
- **Plano anterior de exploração:** [Experimentos rápidos multiagente](planos/2026-09-25-plano-llm-multiagente.md), mantido para rastreabilidade.
- **Histórico anterior:** [002 — Híbrido GAT + Agentes](abordagens/002-hibrido-gat-agentes.md), mantido para rastreabilidade
- **▶ Retomar o trabalho:** [PROXIMOS_PASSOS.md](PROXIMOS_PASSOS.md) — estado atual, experimentos pequenos e próximos incrementos

## Princípio de observabilidade

Cada experimento deve tornar visível o que foi executado, quais sinais sustentam a RCA, como os agentes chegaram à decisão e por que uma variante foi mantida, alterada ou descartada. Métricas, logs e traces observam o sistema sob investigação; os mesmos sinais também observam o comportamento e o custo da estratégia de agentes.
