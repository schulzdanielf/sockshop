# 2026-09-20 — Istio: viável no WSL2 (supera o bloqueio do Pixie)

Após o Pixie falhar no WSL2 (socket_tracer não anexa perf-kprobe), avaliamos o
service mesh Istio — que usa **sidecar Envoy** (proxy L4/L7), mecanismo diferente
que **não depende de perf-kprobe**.

## Resultado da avaliação

**Istio FUNCIONA no docker-desktop/WSL2.** Evidência:

- Control plane (`istiod`, profile=minimal, sidecar slim) instalado e saudável.
- Sidecars injetados e `Running 2/2` em: catalogue, carts, orders, payment,
  shipping, user, queue-master.
- **Arestas reais capturadas** no Prometheus-v3 via `istio_requests_total`:
  `catalogue → otel-collector`, `payment → otel-collector`, etc. — prova de que o
  mesh mede tráfego cliente→servidor por workload.

## Por que o Pixie falhou e o Istio não

| | Pixie (eBPF) | Istio (Envoy sidecar) |
|---|---|---|
| Mecanismo | perf-kprobe em syscalls | proxy L4/L7 por pod |
| WSL2 | **falha** (perf_event_open(PERF_TYPE_KPROBE) não anexa) | funciona |
| Toca app? | não (DaemonSet) | sim (injeta container) |

## Problemas operacionais a resolver (não bloqueadores)

1. **front-end + `front-end-envoy` customizado** usa a porta **15001**, a mesma do
   interceptador Istio (`virtualOutbound`) → o listener é rejeitado (lds rejected),
   `istio-proxy` nunca fica pronto. Porta hard-coded na imagem. **Ajuste:** excluir
   o front-end da injeção (`sidecar.istio.io/inject: "false"`) OU o front-end-envoy
   custom é só proxy Zipkin — possivelmente o tráfego do app não passa por ele.
2. **front-end + OTel auto-instrumentation (Node.js)** já injeta agente via
   Operator — dois proxies no mesmo pod. Precisa de cuidado na ordem/injeção.
3. **Recursos:** 1.1Gi livre no host; sidecar slim (10m/64Mi req) obrigatório.
   O `payment` (100m/100Mi) ficou pronto, mas é o mais apertado.
4. **Tráfego de teste:** o front-end restartado perdeu a sessão (curl Exit 7) —
   gerar carga válida (sessão ou endpoint sem auth) antes de medir as arestas.

## Próximo passo

Resolver o front-end (excluir da injeção ou ajustar ordem), gerar tráfego válido,
e confirmar se a aresta **`front-end → catalogue`** (a que faltava) aparece. Se
sim: o Istio vira a fonte de edge features (`edge_source: full`) e re-testamos o
GAT com arestas reais — a hipótese que ficou aberta.

---

## Resolução e go/no-go — PASSOU ✅

### Problemas encontrados e resolvidos

1. **front-end + `front-end-envoy` na porta 15001** colidia com o interceptador
   Istio (`virtualOutbound`) → listener rejeitado (lds rejected), `istio-proxy`
   nunca pronto. **Resolvido:** o listener na 15001 é o egress proxy que o próprio
   config documenta como *desabilitado* ("com OTel SDK no app, o Envoy não recebe
   mais tráfego via HTTP_PROXY"). Colisão era de **porta, não de tráfego**. Fix:
   mover o listener para **15002** (`front-end-envoy-config`), mantendo o cluster
   Zipkin (9411) intacto. Depois: `front-end`, `front-end-envoy`, `istio-proxy`
   **todos `true`** — coexistem.

2. **front-end + OTel auto-instrumentation (Node.js)** já injetava agente via
   Operator — coexistiu com o istio-proxy sem conflito após o fix da porta.

### Go/no-go (a aresta que faltava)

Tráfego válido de dentro do cluster (`kubectl exec front-end wget catalogue:80/health`):
**`front-end → catalogue` capturada** no `istio_requests_total`. Mapa observado:

```
front-end → catalogue   (2 reqs)
netgen    → catalogue   (28)
ordgen    → carts/orders/user (19 cada)
```

### Métricas por aresta disponíveis (golden metrics para o GAT)

`istio_requests_total` (req), `istio_request_duration_milliseconds_sum/_bucket`
(latência), `istio_request_bytes_*`, `istio_response_bytes_*` — todas com labels
`source_workload`, `destination_workload`, `response_code` (para erro%). Exatamente
as 3 golden metrics por chamada A→B que o desenho do grafo exige.

### Conclusão

**O Istio fornece edge features reais no WSL2** — o que o Pixie não conseguiu. A
hipótese do GAT com edge features reais volta a ser testável. Próximo passo:
adaptar o `graph_builder` para consumir `istio_requests_total` +
`istio_request_duration_milliseconds_*` (arestas reais com `edge_source: full`),
regenerar o dataset e re-treinar o GAT para testar H001 de forma justa.
