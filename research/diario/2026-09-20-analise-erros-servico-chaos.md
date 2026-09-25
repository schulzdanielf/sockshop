# 2026-09-20 — Análise de erros por serviço e por chaos (GAT / MLP / B1)

Auditoria de acertos top-3 sobre o **teste de todos os splits** (S1+S2+S3), por
serviço-alvo e por tipo de chaos. B1 é determinístico; GAT e MLP com seed 0
(referência — GAT tem jitter CUDA). Pergunta: **há serviço ou chaos nunca
acertado?** E o teste está medindo corretamente?

## Top-3 por SERVIÇO (acertos/total)

| serviço | B1 | GAT | MLP | leitura |
|---|---|---|---|---|
| queue-master | 0/2 (0.00) | 2/2 (1.00) | 0/2 (0.00) | n=2 — ruído, inconclusivo |
| shipping | 7/23 (0.30) | **1/23 (0.04)** | 18/23 (0.78) | **GAT falha sistemático** |
| payment | 13/34 (0.38) | 7/34 (0.21) | 24/34 (0.71) | held-out S2; GAT baixo |
| carts | 8/20 (0.40) | **3/20 (0.15)** | 14/20 (0.70) | GAT baixo |
| catalogue | 1/2 (0.50) | 1/2 | 2/2 | n=2 |
| user | 10/15 (0.67) | 13/15 (0.87) | 14/15 (0.93) | ok |
| front-end | 8/11 (0.73) | 10/11 (0.91) | 11/11 (1.00) | bem localizável |
| orders | **28/38 (0.74)** | 17/38 (0.45) | 14/38 (0.37) | B1 acha, GAT/MLP não |

**Nunca acertados (sistemático, n≥15):** `shipping` e `carts` pelo **GAT**
(0.04 / 0.15). queue-master tem 0% em B1/MLP mas n=2 → não dá para concluir.

## Top-3 por CHAOS (acertos/total)

| chaos | B1 | GAT | MLP | leitura |
|---|---|---|---|---|
| memory-hog | **7/24 (0.29)** | 10/24 | 19/24 (0.79) | **B1 fraco**; modelos aprendem |
| pod-delete | 30/62 (0.48) | 21/62 | 51/62 (0.82) | MLP forte; GAT fraco |
| network-loss | 1/2 | 1/2 | 1/2 | n=2 |
| network-latency | 8/13 (0.62) | 4/13 (0.31) | 9/13 | GAT fraco |
| container-kill | 5/8 | 3/8 | 5/8 | n=8 |
| cpu-hog | 18/28 (0.64) | 14/28 (0.50) | **10/28 (0.36)** | **MLP fraco** (surpresa) |
| http-status-code | **6/8 (0.75)** | **1/8 (0.12)** | **2/8 (0.25)** | **GAT/MLP falham; B1 acha** |

**Nunca acertados (sistemático, n≥13):** `http-status-code` por GAT (0.12) e MLP
(0.25) apesar de B1=0.75; `network-latency` por GAT (0.31).

## Hipóteses dos motivos

**H-A · Viés de posição topológica (confirmada antes, reaparece).**
`shipping` e `queue-master` são folhas / assíncronos (via fila RabbitMQ). No S2
e S3 o GAT já mostrou memorizar posição. `shipping` GAT=0.04 com n=23 é o caso
mais gritante: o modelo aprendeu que "shipping não é onde se aponta" (shipping
aparece como label quase só em pod-delete no corpus). B1 (features puras) e MLP
(sem posição) localizam shipping bem (0.30 / 0.78). → a falha é de **estrutura**,
não de sinal.

**H-B · `orders`: sinal anômalo fraco mas consistente.** B1 acha orders (0.74)
porque o serviço **é** o mais anômalo quando falha; mas GAT/MLP, treinados sem
orders como label (held-out S2), não aprenderam a apontar para aquela posição.
Reforça H-A: o problema é a posição held-out, não a detectabilidade.

**H-C · `http-status-code`: o sinal existe nas features mas o modelo não o usa.**
B1=0.75 prova que a anomalia (error_rate_lr alto no serviço-alvo) está lá e é
localizável por heurística. GAT/MLP falharem (0.12/0.25) com só 8 exemplos de
teste e poucos de treino sugere **escassez de dados + o modelo priorizando
latência/tráfego sobre error_rate**. É o chaos com menos runs full — exatamente
o que a campanha de expansão tenta corrigir (adiciona http-status-code em 3
serviços).

**H-D · `cpu-hog` fraco no MLP (0.36) mas não no B1 (0.64):** cpu-hog manifesta
em latência/tráfego indiretamente (sem erro explícito). O MLP pode estar
super-ajustando a faults com erro direto. Inconsistente entre modelos → mais
dados ajudam a decidir se é real ou variância.

## O teste está medindo corretamente?

Sim, com uma ressalva de **dados**, não de método:
- O ranking usa `rank_of_label` com empate conservador — sem inflar.
- Os serviços com 0% (queue-master, catalogue) têm **n=2** — não são "nunca
  acertados", são sub-amostrados. A métrica está certa; o volume é o problema.
- Os fracassos sistemáticos reais (n≥15) são **shipping/carts no GAT** e
  **http-status-code em GAT/MLP** — todos explicáveis por H-A (posição) ou H-C
  (escassez), não por bug de medição.

## Implicação para a campanha de expansão

A matriz de expansão ([config_istio_expansion.yaml](../../../experiment/eval/gat/config_istio_expansion.yaml))
já cobre os pontos fracos: adiciona **http-status-code** (catalogue, carts,
payment, user), **container-kill** e **network-*** em vários serviços, e aumenta
a cobertura de **shipping/carts** (hoje falham no GAT). queue-master e catalogue
seguem sub-representados por serem held-out/escassos — limitação aceita.

**Validação recomendada pós-coleta:** re-rodar esta auditoria e confirmar que
(1) `http-status-code` e `network-*` sobem no GAT com mais dados full, e
(2) `shipping/carts` no GAT — se continuarem baixos mesmo com dados, confirma
H-A (posição) como causa estrutural.
