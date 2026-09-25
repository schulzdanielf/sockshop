# 2026-09-19 — Avaliação de viabilidade: como obter arestas para o GAT

**Contexto:** o usuário confirmou que o Grafana mostra arestas **centradas no `user`** (Locust), e restringiu: as imagens do projeto não são dele — só pode mexer no Kubernetes. Pediu uma estratégia de telemetria forte, se viável.

## A pergunta que o usuário fez (e a resposta)

> "Se eu vejo o grafo no Grafana, por que você não consegue mapear as arestas?"

**Resposta:** consigo — as arestas existem no **Prometheus-v3** (de onde o Grafana lê), não nos traces brutos do Tempo (onde eu estava olhando). O `metrics_generator` do Tempo gera `traces_service_graph_request_total` com 8 arestas. **Mas todas partem de `user`** (o load generator): `user→front-end`, `user→catalogue`, etc. **Zero arestas internas** (`front-end→catalogue`, etc.).

## Diagnóstico estrutural (evidências ao vivo)

| Camada | Achado |
|---|---|
| Service graph no Prometheus-v3 | 8 arestas, todas `user → X`; métricas completas (req_total, failed_total, client_seconds, spanmetrics) |
| Arestas internas (`client=front-end`) | **0** |
| Spans do front-end | 9 spans: **1 SERVER + 8 INTERNAL, zero CLIENT** |
| Spans de carts/orders/catalogue | 1 SERVER cada, zero CLIENT |
| OTel Operator | instalado (v0.148.0); annotations: front-end(nodejs), carts/orders/shipping/queue-master(java); **catalogue/payment/user SEM annotation** |
| catalogue | usa **Zipkin** via env var (não OTel) |
| Go auto-instrumentation | **`enable-go-auto-instrumentation: false`** no Operator |
| Envoy sidecar | **só no front-end**, e com tracing **explicitamente desabilitado** ("com OTel SDK no app, o Envoy não recebe mais tráfego via HTTP_PROXY") |

## Causa raiz (dupla)

1. **Cobertura de instrumentação insuficiente:** nenhum serviço reporta spans **CLIENT** (as chamadas de saída). Sem spans CLIENT, o Tempo não tem como construir arestas — mesmo com propagação W3C perfeita.
2. **Propagação de contexto quebrada:** stacks heterogêneas (OTel nativo, Zipkin, nada) não compartilham `traceparent`.

Ambas decorrem da restrição: **as imagens são fixas, não dá para rebuildar com instrumentação melhor.**

## Opções avaliadas (viabilidade Kubernetes-only)

### Opção A — Service mesh (Istio/Linkerd) — arestas via sidecar

Injetar Envoy em todos os serviços; o sidecar captura todo tráfego L4/L7 e gera traces/telemetria de quem chama quem, **sem tocar nas imagens**.

- **Viabilidade:** alta tecnicamente; não precisa rebuildar nada.
- **Custo:** instalar e operar um service mesh; overhead de recursos (cada pod ganha um sidecar); o Sock Shop roda com containers já restritos de recurso — risco de interferir no experimento.
- **Conflito potencial:** o front-end já tem um Envoy próprio; sidecar adicional pode conflitar.
- **Veredito:** **viável mas pesado.** É a solução "certa" de telemetria forte, mas é um projeto de infraestrutura em si.

### Opção B — eBPF (Pixie/Cilium Hubble) — arestas sem instrumentação

eBPF observa syscalls de rede no kernel e reconstrói o mapa de chamadas L7 **sem nenhum sidecar nem instrumentação de app**.

- **Viabilidade:** alta; Kubernetes-only de verdade (DaemonSet).
- **Custo:** instalar Pixie ou Cilium; overhead baixo-moderado; dados de aresta são de *rede* (conexões TCP/HTTP), não spans — precisam ser agregados em golden metrics por aresta.
- **WSL2/docker-desktop:** eBPF funciona, mas requer kernel com suporte (o host é WSL2 — verificar).
- **Veredito:** **a opção mais elegante** para a restrição. Telemetria forte sem tocar em nada.

### Opção C — Auto-instrumentação Go no Operator — cobrir catalogue/payment/user

Ligar `enable-go-auto-instrumentation` no Operator e anotar os 3 serviços sem cobertura.

- **Viabilidade:** parcial. A auto-instrumentação Go (eBPF-based, v0.23.0) captura spans, mas: (1) catalogue usa Zipkin nativo, pode conflitar; (2) não garante spans CLIENT se o app usa HTTP clients não instrumentados; (3) é beta.
- **Veredito:** **complemento**, não solução. Pode ajudar, não resolve a cobertura CLIENT.

### Opção D — Topologia estática + arestas `user→X` + node features (pragmático)

Aceitar o que existe: topologia conhecida da Sock Shop (arestas estáticas), features de aresta das métricas `user→X` (que existem), node features do `metrics_raw` (que existem).

- **Viabilidade:** imediata, zero infra.
- **Custo científico:** o GAT não aprende a cascata interna real — aprende "qual serviço o user percebe degradado" + sinais de nó. Limitação registrada na tese.
- **Veredito:** **destrava hoje**, e o dataset pode ser regenerado quando a telemetria forte existir.

## Recomendação

**Viabilidade eBPF confirmada:** kernel 6.6.87.2-microsoft-standard-WSL2 com BTF presente (`/sys/kernel/btf/vmlinux`) — eBPF CO-RE funciona, logo Pixie seria viável no WSL2 *tecnicamente*.

**Achado ao testar o Pixie (2026-09-19):** o CLI `px` (baixado de `release/cli/v0.8.8`) exige **login no Pixie Cloud para qualquer operação** — inclusive `px deploy --check_only` e `--extract_yaml`. Mesmo o Vizier self-hosted precisa de uma `deploy_key` obtida via conta. Essa dependência de conta SaaS adiciona fricção e telemetria externa que conflita com o ambiente controlado do experimento. Teste abortado antes de instalar qualquer coisa no cluster; **nenhum recurso Pixie foi criado** (verificado: sem namespace/pods).

**Decisão do usuário (2026-09-19): Opção D.** Destrava o GAT imediatamente com o que já existe, sem nova infraestrutura. A telemetria forte (Pixie ou service mesh) fica registrada como melhoria futura — o dataset marca `edge_source` por amostra, então o delta dela é mensurável quando existir.

## Decisão (tomada)

**Opção D — topologia estática + arestas `user→X` + node features.** Dataset v1 construído com o que já existe; `edge_source` por amostra permite medir o delta da telemetria forte no futuro.

## Reavaliação Pixie (2026-09-20) — checagem de viabilidade antes de investir

Após H001 ser refutada na Opção D (topologia estática prejudicial), o usuário
decidiu investir na telemetria real via Pixie, mas pediu verificação de
bloqueadores futuros antes de gastar tempo. Checagem empírica no cluster:

| Requisito | Resultado |
|---|---|
| Recursos do node | 16 CPU, 36GB RAM, ~11GB livres — suficiente (Pixie ~3-4GB) |
| DNS Pixie Cloud (`withpixie.ai`) | resolve (34.102.151.106) |
| HTTPS ao Pixie Cloud | 307 (alcançável, TLS ok) |
| Pod privilegiado + hostPID + hostPath | **agenda e roda** no docker-desktop |
| BTF (`/sys/kernel/btf/vmlinux`) | presente (CO-RE, kernel-agnóstico) |
| tracefs, kprobes | 62.978 funções kprobe-áveis |
| Flags kernel | `CONFIG_BPF/BPF_SYSCALL/BPF_JIT/PERF_EVENTS/KPROBES/UPROBES/DEBUG_INFO_BTF` **todas =y** |

**Veredito: luz verde técnica.** Todos os requisitos de eBPF do Pixie estão
presentes e testados. `perf_event_paranoid=2` e `unprivileged_bpf_disabled=2` não
afetam — os PEMs rodam privilegiados e contornam essas restrições.

**Risco residual (baixo, honesto):** o kernel WSL2 (`6.6.87.2-microsoft-standard`)
não está na lista oficialmente testada do Pixie. Mitigado por BTF+CO-RE (funciona
em qualquer kernel com BTF). **Falha, se ocorrer, é rápida e barata:** o PEM ou
carrega o eBPF e flui dado em ~5 min, ou falha no load — saberíamos em ~15 min, e
como o Pixie **não toca nas imagens nem nos manifests do sock-shop**, não há nada a
reverter. É a opção de menor arrependimento exatamente por isso.

**Requisito operacional:** conta gratuita no Pixie Community Cloud (o CLI `px`
exige login para deploy). É o único cadastro necessário.

## Deploy do Pixie e resultado go/no-go (2026-09-20) — NÃO viável no WSL2

Deploy executado (conta criada, deploy key + API key). Fluxo:
- `px auth login --api_key --cloud_addr=withpixie.ai:443` (o default de cloud_addr estava errado — community cloud é `withpixie.ai:443`).
- Check de kernel falhou por bug cosmético de parse (`patch number "87.2"`), contornado com `--check=false`. Kernel 6.6 >> 4.14 exigido.
- Deploy completou: Vizier `CS_HEALTHY`, todos os pods Running, **PEM (agente eBPF) carregou** — perf_profiler capturou 40k stack traces.

**Bloqueador definitivo:** o `socket_tracer` do Pixie (fonte de `http_events`/`conn_stats` = o service graph) **não instancia**:
```
Source Connector (socket_tracer) not instantiated:
  Unable to attach kprobe for __x64_sys_connect using syscall__probe_entry_connect
```
Reproduzido 2× (deploy + PEM restart), determinístico. Diagnóstico:
- `/sys/kernel/debug/tracing/kprobe_events` **é gravável** (testado: escrita manual de kprobe em `__x64_sys_connect` → WRITE_OK) e `CONFIG_KPROBE_EVENTS=y`.
- Mas o Pixie/BCC anexa via `perf_event_open(PERF_TYPE_KPROBE)` — mecanismo **diferente** do write-legado do tracefs — e **esse caminho não funciona no kernel WSL2**. Prova: o `perf_profiler` (perf sampling comum) sobe; o `socket_tracer` (kprobes em syscalls) falha no 1º attach.
- Não corrigível sem rebuildar o kernel WSL2; o Pixie não expõe o caminho legado de attach.

**Custo real: ~40 min, zero toque no app.** Pixie removido 100% (`pl`, `px-operator`, `olm`, CRDs, clusterroles — todos deletados; sock-shop intacto; chaves temporárias apagadas). Rotacionar deploy/API keys no painel.

**Conclusão:** telemetria eBPF de protocolo (edge features reais) **não é viável neste docker-desktop/WSL2**. Alternativas para o futuro, todas fora do WSL2: cluster Linux real (cloud/VM com kernel padrão) onde o Pixie funciona; ou service mesh (Istio) que gera métricas de aresta via sidecar sem depender de perf-kprobe. Por ora, **consolidar o MLP por-nó como resultado WHERE** (S3=0.86); a hipótese do GNN com edge features fica parada até um ambiente com kernel padrão.
