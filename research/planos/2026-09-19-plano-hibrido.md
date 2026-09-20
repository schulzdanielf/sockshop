# Plano de trabalho — Abordagem híbrida (GAT + agentes)

- **Criado em:** 2026-09-19 · **Branch:** `hibrido`
- **Abordagem:** [002](../abordagens/002-hibrido-gat-agentes.md) · **Hipóteses:** [H001](../hipoteses/H001-gat-localizacao-falhas.md), [H002](../hipoteses/H002-agentes-ia-motivo-falha.md)
- **Status geral:** ativo — **escopo atual: apenas o WHERE (H001)**
- **Mapeamento detalhado de mudanças:** [2026-09-19-mapeamento-evolutivo](2026-09-19-mapeamento-evolutivo.md) (experimento, coleta, gates de validação, avaliação, observabilidade — com o que já existe e as lacunas)

## Decisões tomadas (2026-09-19)

1. **Foco no WHERE** — H002 (agente/WHY) fica pausada até H001 ser avaliada.
2. **Stack:** PyTorch + **PyTorch Geometric**.
3. **Primeiro modelo avaliado:** **GAT** (GATv2 como variação posterior, se necessário).
4. **Critério de sucesso: top-3** — a resposta conta como correta se o serviço raiz estiver entre os 3 primeiros do ranking (top-1 registrado como métrica secundária).

## Princípios

1. **Mesma régua:** avaliar com os mesmos splits S1–S4 e métricas da abordagem 001 — sem isso, comparação não vale.
2. **Incremental:** um componente novo por vez (GAT primeiro, agente depois), medindo o delta — mesma disciplina que funcionou com os validadores.
3. **Reuso máximo:** coleta, features, `propagation_graph`, `evaluation.csv`, `audit.py` e infra de LLM local não mudam.

---

## Fase 0 — Inventário e dataset de grafos

> Detalhamento completo no [mapeamento evolutivo](2026-09-19-mapeamento-evolutivo.md) — inclui gates de validação (`run_quality.json`), janela efetiva de fault, construtor de grafo, dataset builder com splits persistidos e plano de testes por etapa.

- [ ] Contar runs rotulados no `platform.db` por célula (fault × serviço) — responde: dados bastam para treinar um GNN?
- [ ] Definir esquema do dataset (**decisões 2026-09-19**):
  - Nó = serviço; features de nó = **3 golden metrics por serviço** (request rate, latência, % erro); **sem métricas de recurso** (decisão de generalização); **sem IDs** (indutivo para S2).
  - Aresta = chamada A→B do service graph (fonte: métricas agregadas de span); features de aresta = mesmas 3 golden metrics por chamada; **bidirecionais com edge_attr marcando direção real**.
  - Encoding: **log-ratio** com ε vs baseline do período normal.
  - **Um grafo por run** (snapshot único) — sem modelo temporal na v0.
  - **Janela de fault encurtada** (decidido 2026-09-19): primeiros ~2–3 min após o disparo do chaos, cap no fim real da fase; ancorar na duração do chaos manifest (transiente → curta; persistente → até o fim da injeção). Motivo: em container-kill/pod-delete a fase de fault é muito mais longa que o sintoma; razão sobre a fase inteira dilui o pico e gera ruído de label.
  - **Count como feature auxiliar** de confiança junto de cada razão (janelas curtas → métricas esparsas).
  - Labels = serviço raiz + `fault_category` (ground truth do chaos manifest).
- [ ] Exportador: `platform.db` → JSONL/`.pt` (reusar leitura do `audit.py`, sem subir o servidor).

**Gate:** ≥ N runs/célula (definir N após inventário); senão, planejar augmentation ou rodadas extras de chaos.

## Fase 1 — Baseline congelado

- [ ] Fechar a tabela de métricas da abordagem 001 (where/why top-1 por split S1–S4) em [001-llm-rag-validadores.md](../abordagens/001-llm-rag-validadores.md).

## Fase 2 — GAT v0 (WHERE) ← foco atual

- [ ] Setup: PyTorch + **PyTorch Geometric** no ambiente.
- [ ] Modelo: **GAT** 2 camadas (heads=4, concat) + readout; saída = score por nó (ranking de serviço).
- [ ] Treino/val em S1; **teste cego** em S2/S3/S4.
- [ ] Comparadores: heurística de centralidade, ranker híbrido atual, LLM (tabela da Fase 1).
- [ ] **Avaliação por top-3:** acerto = serviço raiz entre os 3 primeiros; registrar top-1 e MRR como métricas secundárias.

**Gate:** top-3 ≥ heurística trivial de centralidade; senão, parar e diagnosticar (dataset? features? leakage?).

## Fase 3 — Avaliação H001

- [ ] Tabela **top-3** (primária) + top-1/MRR (secundárias) por split; análise de erros por categoria.
- [ ] Registrar resultado em H001 (validada/refutada) + entrada no diário.

## Fase 4 — Agente WHY (H002) — **pausado**

> Retomar somente após a Fase 3. O agente consumirá o **top-3 do GAT** como âncora de investigação.

- [ ] Backbone: `model/react_agent.py` + Qwen local (OpenAI-compatible).
- [ ] Tools: investigators window-scoped do MCP (`check_oom_kills`, `check_pod_restarts`, `get_cpu_saturation_top`, `get_memory_saturation_top`, `check_http_error_rate_top`, `check_request_latency_top`).
- [ ] Entrada: top-k do GAT + janela exata do experimento; saída: `fault_category` + explicação + evidências.
- [ ] Avaliar acurácia de categoria por split, com foco nas 4 categorias sem validador; medir custo (chamadas/run).

## Fase 5 — Integração híbrida

- [ ] `api.run_llm_analysis`: GAT (WHERE) → agente (WHY) → validadores 001 como guardrails (nunca sobrescrever com âncora não validada — lição do run-82b832839337).
- [ ] Campos novos no `runner.py` EVAL_FIELDS para A/B (gat_topk, agent_tool_calls, guardrail_fired).

## Fase 6 — Análise e escrita

- [ ] Tabela comparativa 001 vs 002 (4 splits); análise de falhas; atualizar hipóteses e diário.
- [ ] Redigir seção de resultados (doc em `experiment/docs/`).

---

## Decisões em aberto

1. ~~Stack GNN~~ → **PyTorch Geometric** (decidido 2026-09-19).
2. **Papel do GAT no WHERE:** substitui o LLM, ou ensemble (voto GAT + LLM)? — decidir na Fase 3, com os números na mão.
3. ~~Backbone do agente~~ — pausado junto com H002.
4. **Dados:** se o inventário (Fase 0) mostrar poucos runs, augmentation vs mais rodadas de chaos?
5. **Métrica primária** → **top-3** (decidido 2026-09-19); top-1/MRR secundárias.
