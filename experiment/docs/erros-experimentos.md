# Erros e Falhas dos Experimentos — Registro de Auditoria

**Propósito:** documentar os problemas encontrados na coleta de experimentos,
com o tipo de erro, a causa raiz e as possíveis soluções. Serve de referência
para corrigir o pipeline **antes** de confiar no dataset para treinar o GAT.

> **Leitura recomendada junto com:** [docs/guia-completo-experimento.md](../../docs/guia-completo-experimento.md).

---

## 🔴 ERRO 1 — `label` ausente em TODOS os runs (CRÍTICO, bloqueia o treino)

**Tipo:** label faltante / ground-truth não propagado.

**O que foi verificado:** **50/50** runs completados (com `gat_graph`) têm
`label.root_service_idx = None`, `root_service = None`, `fault_category = None`.
Além disso, **20 runs completados não têm `gat_graph.json`**.

**Consequência:** **nenhum grafo do novo corpus pode ser usado para treinar ou
avaliar** — o GAT precisa do índice do serviço raiz (o "gabarito") para computar
top-k. Sem label, os runs são inúteis para o WHERE. **A coleta atual está gerando
dados sem gabarito.**

**Causa raiz (revisada após verificação rigorosa):** o ground-truth **está
presente** nas tags do experimento (`target:<service>`, `fault_category:<cat>`,
corretos por versão — provado) e a lógica de extração do engine
([engine.py](../platform/backend/engine.py#L418-L431)) **funciona** quando
testada isoladamente. **Mas o valor gravado em `run_features` é None.** O bug
está na **execução**, não nos dados: ou a plataforma rodou com código antigo em
memória (sem a extração), ou `_build_run_features` falhou silenciosamente (exceção
best-effort engolida) antes de persistir o campo — e o `build_gat_graph`
([engine.py](../platform/backend/engine.py#L357-L365)) leu `features` ainda sem
`ground_truth_service`, gerando o label None.

**Onde investigar:**
- `experiment/platform/backend/engine.py` → `_build_run_features` (extrai e persiste
  ground truth) e a chamada a `build_gat_graph` (lê de `features`).
- Verificar se a extração ocorre **antes** do `upsert_run_features` e se nenhuma
  exceção está sendo engolida entre elas.

**Soluções possíveis:**
1. **Backfill pós-coleta (rápido, recupera os runs já coletados):** ler as tags da
   versão do experimento (`target:`/`fault_category:`) ou o
   `chaos_artifacts.workflow_template` → preencher o label. Reconstrói o gabarito
   sem recoletar. **Recomendado** — os runs têm o dado, só falta propagá-lo.
2. **Corrigir a origem (definitivo):** garantir que a plataforma roda o código com
   a extração (reiniciar após deploy) e que a ordem extração→persistência→grafo é
   respeitada, sem exceção silenciosa.

---

## 🔴 ERRO 2 — http-status-code não injeta efeito sob Istio (label falso)

**Tipo:** chaos sem efeito observável → label diz "falhou", features dizem "normal".

**O que foi verificado:** **14/14** runs de `http-status-code` (catalogue, carts,
payment, user) têm o nó-alvo com `error_rate_lr = None` (sem erro), tráfego e
latência normais. Ex.: `run-d389432d8ad8` (payment) — durante o "fault", o
payment continuou **autorizando pagamentos (200)**, sem nenhum 5xx.

**Causa raiz:** o `pod-http-status-code` do Litmus sobe um **proxy na porta 20000**
(`PROXY_PORT`) e redireciona o tráfego da porta 80 via **iptables** no pod. Com
**Istio**, o tráfego chega pelo **sidecar Envoy (mTLS)**, que usa **suas próprias
regras iptables e está antes na cadeia** — então o tráfego real passa pelo
Envoy → app **sem passar pelo proxy do chaos**. O chaos injeta, mas nada passa
por ele.

**Consequência:** label falso sistemático. O GAT aprenderia "http-error = sem
sintoma", corrompendo o aprendizado.

**Soluções possíveis:**
1. **Remover `http-status-code` da campanha** (não funciona sob Istio).
2. **Substituir pelo mecanismo nativo do mesh:** `VirtualService` com
   `fault.abort` (HTTP 500) ou `fault.delay` — o próprio Envoy injeta o erro,
   garantindo que o tráfego real é afetado. **É a correção correta sob Istio.**
3. Marcar os 14 runs afetados para exclusão (mesmo com backfill do label, o
   sinal é falso).

---

## 🟡 ERRO 3 — Aresta `shipping → queue-master` nunca é observável (limitação)

**Tipo:** limitação estrutural de observabilidade (não é bug).

**O que foi verificado:** `validate_metrics` reporta 6/7 arestas cobertas; falta
sempre `shipping→queue-master`. O shipping **publica** na fila ("Adding shipment
to queue..."), mas a comunicação é **AMQP para o RabbitMQ** — o queue-master
**consome da fila**, não recebe chamada HTTP/mTLS do shipping. O Istio (mesh L7
HTTP) **não captura AMQP**.

**Consequência:** essa aresta sempre terá features zeradas/mascaradas. Afeta 1
aresta entre 8 nós — impacto limitado.

**Soluções possíveis:**
1. **Aceitar** como limitação documentada (recomendado agora).
2. Ajustar `validate_metrics` para **não exigir** essa aresta (6/7 = OK).
3. (Overkill) instrumentar o RabbitMQ com métricas de fila para inferir a aresta.

---

## 🟡 ERRO 4 — Falhas intermitentes por port-forward do MCP cair

**Tipo:** dependência de infra local (port-forward) instável.

**O que foi verificado:** 4 runs falharam com `httpx.ConnectError` /
`ReadTimeout` ao abrir o SSE do MCP (127.0.0.1:18080). Causa: o **port-forward
do MCP caía** no meio da coleta. Não é falha do experimento nem do cluster —
é a ponte local → cluster.

**Solução aplicada:** watchdog [experiment/eval/gat/ensure-port-forwards.sh](../eval/gat/ensure-port-forwards.sh)
que mantém MCP (18080) e Prometheus (9091) sempre vivos com restart automático.
**Testado:** matar o port-forward → watchdog recria em ~10s.

**Prevenção:** rodar o watchdog antes de coletar
(`nohup bash experiment/eval/gat/ensure-port-forwards.sh &`).

---

## 🟢 RESOLVIDO — otel-collector OOM derrubava serviços (2026-09-21)

**Tipo:** OOM do componente de observabilidade → cascata de falsos erros.

**O que foi verificado:** otel-collector em CrashLoopBackOff (OOM, exitCode 137)
derrubava o Zipkin (9411) → os serviços Go (user/payment/catalogue) faziam
timeout nos spans → falhavam liveness → restart → front-end propagava 503/401/500
("erro 50%"). **Não era** recurso de nó nem o chaos do run.

**Solução aplicada:** config do collector (remover `debug/logs` verbose,
`start_at: end`, `memory_limiter`) + bump de memória (512Mi→1Gi) + bumps em
payment/user/catalogue + alertas Grafana. Detalhes na memória do repo.

**Lição:** antes de culpar um chaos por "não gerar sinal", descartar falha de
infra com [experiment/eval/gat/check_infra.py](../eval/gat/check_infra.py).

---

## Comportamento esperado vs problema — guia rápido

| Observação no Grafana | É esperado? | Interpretação |
|---|---|---|
| Latência sobe no **alvo** e nos **chamadores** (network-latency) | ✅ sim | a falha de rede propaga para quem chama o alvo |
| Alvo sem dado (`None`) esporadicamente | 🟡 às vezes | nós com pouco tráfego podem não ter ponto na janela |
| **Alvo sem anomalia em http-status-code** | 🔴 não | proxy do chaos contornado pelo Istio (ERRO 2) |
| **label None** | 🔴 nunca | ground-truth não propagado (ERRO 1) |
| Buracos de métrica em **todos** os serviços | 🟡 verificar | pode ser lacuna de scrape do Prometheus OU instabilidade — rodar `check_infra` |

---

## Checklist antes de usar o dataset para treino

- [ ] **ERRO 1 resolvido:** todos os grafos têm `label.root_service_idx` válido.
- [ ] **ERRO 2 resolvido:** runs de http-status-code corrigidos (VirtualService) ou excluídos.
- [ ] `check_infra` reportou **LIMPA** na janela de coleta.
- [ ] `audit_run` passa nos runs (inclui gate de estabilidade futura).

---

## Auditoria de impacto nas métricas (2026-09-21, pós-backfill)

Auditoria **somente-leitura** dos runs completados após o backfill de labels
(ERRO 1 resolvido) — para cada tipo de chaos, se o **serviço-alvo** apresentou
anomalia real nas golden metrics (|log-ratio| acima de limiar: erro >0.1,
latência/tráfego >0.3). Critério: a métrica anômala deve ser **coerente com o
tipo de falha**.

| chaos | n | c/ sinal | s/ sinal | métrica que anomala no alvo | avaliação |
|---|---|---|---|---|---|
| **network-latency** | 11 | **11** | 0 | latência (9), tráfego (5) | ✅ **funcionando** — latência sobe no alvo, coerente |
| **network-loss** | 6 | 4 | 2 | latência (3), tráfego (2) | ✅ funcionando (maioria) |
| **memory-hog** | 10 | 6 | 4 | latência (6) | 🟡 parcial — anomalia é **indireta** (latência, não erro), coerente com a natureza da falha |
| **container-kill** | 8 | 4 | 4 | tráfego (4), latência (2) | 🟡 parcial — falha **transiente** (pod morre e renasce); sinal breve, pode cair fora da janela |
| **cpu-hog** | 2 | 2 | 0 | tráfego (1), latência (1) | ✅ ok, mas **n=2** (sub-amostrado) |
| **http-status-code** | 0* | — | — | — | ⚠️ ver abaixo |

\* **http-status-code:** os 20 runs antigos (label falso) foram apagados. Após a
correção (VirtualService), **nenhum run novo completou com grafo ainda** — o
primeiro (`run-f931cb7c8a5f`, catalogue) completou mas **sem** `error_rate` no
alvo (None), e o `istio_edges` mostrou **zero 5xx em →catalogue** (os únicos 5xx
foram `unknown→front-end`, o Locust recebendo 503). **A correção via
VirtualService ainda NÃO está confirmada** — o 500 injetado pelo Envoy não
apareceu como erro no serviço-alvo. Ver ERRO 5.

### Leituras da auditoria

1. **network-latency e network-loss são os mais confiáveis** — o alvo anomala de
   forma coerente (latência) na maioria dos runs.
2. **memory-hog e container-kill são parciais por natureza** — memory-hog
   manifesta em latência (indireto), container-kill é transiente (sinal breve).
   Não é bug; é a natureza da falha. Mas reduz o contraste alvo-vs-resto.
3. **http-status-code permanece sem sinal confirmado** mesmo após VirtualService.

---

## 🔴 ERRO 5 — VirtualService fault.abort não gera erro observável no alvo

**Tipo:** correção do ERRO 2 implementada, mas **sem confirmação de efeito**.

**O que foi verificado:** o run `run-f931cb7c8a5f` (http-status-code × catalogue)
usou `manifest_path` (VirtualService) corretamente, completou, mas:
- `gat_graph` do alvo (catalogue): `error_rate_lr=None`, `traffic_lr=0.81`,
  `latency_lr=1.16` — **sem erro**.
- `istio_edges` tinha 500/503 registrados, **mas nenhum em `→catalogue`** — os
  5xx eram `unknown→front-end` (Locust recebendo 503) e 2 req espúrias.
- Testes manuais (aplicar VS + carga) foram **inconclusivos** (janela/carga não
  alinharam para uma prova limpa).

**Hipóteses da não-observação:**
1. **Atribuição da métrica:** o `fault.abort` age no **sidecar do cliente**; o
   500 abortado pode não ser reportado com `destination_workload=catalogue` (o
   pedido nunca chega ao servidor). O GAT mede erro no **destino** — se o abort é
   no cliente, o alvo nunca mostra erro.
2. **Host mismatch:** o front-end pode chamar o catalogue por um host que não
   casa com o `hosts: [catalogue]` do VirtualService.
3. **Janela curta:** fault de 90s com scrape de 15–30s pode perder o pico.

**Soluções possíveis:**
1. **Validação controlada (recomendado):** 1 run isolado http-status-code ×
   catalogue, com carga contínua garantida, medindo `error_rate` do catalogue
   **durante** o fault. Decide se o VS funciona.
2. Se o problema for atribuição (hipótese 1): medir o erro **na origem**
   (`source_workload`) ou usar `fault.abort` com `override` no gateway, ou trocar
   por **HTTP route real que devolve 500** na app (não abort no mesh).
3. Se for host: ajustar `hosts` do VirtualService para o FQDN exato usado pelo
   front-end (`catalogue.sock-shop.svc.cluster.local`).

**Status:** aberto. **Não** usar runs http-status-code no treino até confirmar
sinal no alvo.
