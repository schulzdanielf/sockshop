# 2026-09-20 — Adaptação ao Istio: edge features reais (e a implicação honesta)

## O que foi implementado

- **`graph_builder.py` estendido** para aceitar métricas Istio além das
  `traces_service_graph_*`. Novo `edge_source` com 3 níveis: `full` (Istio, arestas
  reais source→destination), `user_only` (traces service-graph), `static` (neutras).
  - `_edge_features_istio`: req rate, latência média (sum/count), erro% (5xx/total)
    por aresta, em log-ratio vs baseline.
  - `_detect_edge_source` + `_edge_series_istio` (labels `source_workload`/
    `destination_workload`/`response_code`).
- **`istio_edges.py`** — monta as range queries Prometheus por janela.
- **4 testes novos** (`test_graph_builder_istio.py`). Suíte: **121 passando**.

## A implicação honesta (decisiva)

**Os runs históricos não têm dados Istio** — confirmado: `istio_requests_total`
em 2026-09-16 (janela de um run antigo) = **0 séries**. O Istio foi instalado hoje.

Logo: o dataset existente (192 grafos, todos `edge_source: static`) **não muda**.
Edge features reais só existirão em **runs novos** coletados com o mesh ativo.

## O caminho cientificamente correto

1. **Coletar runs novos com o mesh ativo** — cada run novo terá `edge_source: full`.
2. **C3 (janela efetiva) + graph builder** agora com edge features reais por aresta.
3. **Re-treinar o GAT** e comparar honestamente: GAT(full) vs GAT(static) vs MLP vs B1.

Isso testa H001 de forma justa: a mesma arquitetura, mas agora com arestas que
variam por run — a variável que estava faltando quando H001 foi refutada com
topologia estática.

## Bloqueante resolvido que vale registrar

O fix da porta 15001 (`front-end-envoy` → 15002) foi o que desbloqueou o mesh no
serviço mais importante. Sem isso, a aresta front-end→catalogue nunca existiria.

## Decisão para o usuário

Gerar runs novos com o mesh ativo agora (custam tempo de máquina: cada run ~5–10
min), ou primeiro revisar quantos runs/células precisamos para um teste com poder
estatístico suficiente (dado que S1/S2/S3 têm poucos runs por célula)?

---

## Teste pequeno de validação (pedido do usuário) — PASSOU ✅

Antes de qualquer experimento longo, o usuário pediu um teste pequeno para
validar que todas as métricas necessárias estão disponíveis. Resultado: o teste
**revelou e corrigiu 2 problemas** antes de gastar tempo de máquina:

1. **Validador consultava métricas erradas** (`traffic`/`error_rate`/`latency_p95`
   como métricas do Prometheus — mas são nomes de query nossos). Corrigido para
   usar métricas Istio reais por destination (nó) e source→destination (aresta).
2. **Janela de validação:** instant query pegava entre scrapes do Prometheus
   (~15s). Corrigido para **range query** na janela — robusto.

**Validação final: `READY`.**
- Node metrics: **8/8** serviços com golden metrics (Istio por destination).
- Edge metrics: **7/7** arestas da topologia cobertas (front-end→catalogue/carts/
  orders/user, orders→payment/shipping, shipping→queue-master).
- 43 séries por métrica Istio.

Novo artefato: `experiment/eval/gat/validate_metrics.py` — checklist de
disponibilidade (`validation_check.json`, exit 0/1). Roda antes de qualquer
experimento para garantir que não falta métrica.

**Grafana:** o acesso que falhava era o port-forward 3000 (quebrado). O NodePort
**31300 funciona**: http://localhost:31300. Reiniciar o port-forward se quiser
3000: `kubectl port-forward -n monitoring svc/grafana 3000:80 &`.

## Próximo passo (desbloqueado)

Coletar runs novos com o mesh ativo → `edge_source: full` → re-treinar o GAT e
comparar honestamente GAT(full) vs GAT(static) vs MLP vs B1.
