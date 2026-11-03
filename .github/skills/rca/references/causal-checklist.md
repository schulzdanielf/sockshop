# Checklist causal: nao parar no sintoma

| Sinal inicial | Hipoteses a confrontar | Evidencia robusta a buscar |
|---|---|---|
| Restart | OOM, crash, probe, rollout, kill, dependencia/rede | Log anterior, erro do processo, spans de dependencia, motivo de terminacao |
| OOM/pressao de memoria | Limite baixo, carga, leak, GC | Evolucao memoria, log OOM/GC, operacoes lentas, motivo da terminacao |
| CPU alta | Carga, throttling, loop, GC | Uso vs quota, logs de GC, spans lentos com operacao identificada |
| Readiness baixa | Probe, dependencia, rede, startup | Timeouts antes da transicao, operacao e dependencia nos spans |
| 5xx | Erro local, abort Envoy, dependencia, regra de negocio | Log do chamador e destino, span CLIENT/SERVER, resposta/flags do proxy |
| Latencia/perda | Rede, DNS, recurso, dependencia | RTT/timeouts/retransmissao, UnknownHostException, spans e logs correlacionados |

Traces sem spans de erro ainda podem indicar regressao: compare duracao fault
com o P95 baseline da mesma operacao. Use-os para aprofundamento quando nao
houver trace de erro, mas nao atribua causa apenas por duracao ou por delta
agregado do grafo.

Para cada restart observado:

1. Identifique o servico e, quando disponivel, pod/container e intervalo entre
   scrapes. Nao use o estado atual do pod como prova do que aconteceu ontem.
2. Examine memoria/CPU/OOM e readiness antes e durante a mudanca.
3. Procure logs anteriores ao evento em referencia/fault, e depois em post.
4. Confronte spans: dependencia falhou antes do restart? O reinicio explica
   a falha ou e resposta a uma falha anterior?
5. Se dados historicos de terminacao, probes ou eventos Kubernetes nao estiverem
   disponiveis, declare essa lacuna e solicite esse check. Nao invente exit code,
   `OOMKilled`, rollout ou liveness failure.
6. Corrobore o motivo em mais de uma familia. Um contador de restart confirma
   o evento; nao confirma o mecanismo inicial.

Inspecione contradicoes: um restart pode ocorrer sem OOM; `no_data` nao refuta
OOM; drops cAdvisor zerados nao refutam perda netem; dependencia no cartao
nao comprova propagacao. Nao afirme causa confirmada apenas para cumprir formato.

O pipeline busca logs mesmo quando o planejamento nao explica o restart,
depois consulta traces. Se o orçamento deixar um servico sem logs ou os
dados forem insuficientes, preserve a pendencia na analise de restart.
