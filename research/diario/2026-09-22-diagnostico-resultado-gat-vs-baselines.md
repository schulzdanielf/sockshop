# Diagnóstico do resultado GAT vs. baselines (2026-09-22)

## 1. Resumo executivo

O resultado reportado (`gat_vs_baselines.json` pós-reset, 115 grafos) é ruim,
mas **a causa dominante não é o GAT em si — é o dataset e o desenho dos
splits**. Em ordem de impacto:

1. **S3 está com `n_test=0` — bug de dados, não de modelo.** O único chaos
   held-out de S3 (`pod-delete`) não existe mais no dataset atual (foi
   substituído por `container-kill` na campanha `istio_expansion`). O split
   nunca teve exemplos de teste, então toda métrica de S3 é `NaN` por
   definição — nem GAT, nem baselines, nem MLP têm o que avaliar.
2. **S2 é estruturalmente hostil por desenho** (já era um veredito conhecido,
   ver `/memories/repo/gat-h001.md`): o split remove TODO exemplo de
   `orders`/`payment` do treino (`train_n=0` para ambos os serviços). O GAT
   (e o MLP) têm que apontar para uma classe nunca vista — não é
   "aprender causa raiz", é generalização zero-shot para uma classe nova. O
   baseline B1 (heurística sem aprendizado) vence os dois modelos aqui porque
   não precisa "conhecer" a classe, só olha a métrica mais anômala.
3. **22/115 grafos (19%) usam `http-status-code` sob Istio com label
   provavelmente falso** (ERRO 2/5, já documentado): o chaos não produz efeito
   observável no alvo porque o proxy do Litmus é contornado pelo sidecar
   Envoy. Esses exemplos ensinam o modelo a associar "sem sintoma" ao rótulo
   errado, tanto no treino quanto no teste.
4. **10/115 grafos (9%) têm `queue-master` como alvo com anomalia ZERO em
   100% dos casos** — é fila AMQP, não observável via Istio (ERRO 3,
   limitação conhecida). Esses exemplos são irrecuperáveis para qualquer
   scorer baseado nas golden metrics: não há sinal a ser aprendido.
5. Removendo (3) e (4), o **piso baseline sobe de top-3=0.52 para 0.60** só
   por tirar ruído — evidência direta de que boa parte do problema é dado
   contaminado/sem sinal, não falta de capacidade do modelo.
6. Com dataset "limpo", 94% dos alvos têm alguma anomalia > 0 (sinal
   existe); no dataset bruto, apenas 87%. Ou seja, mesmo no pior caso o
   problema não é "não há sinal nenhum" — é "há ruído sistemático
   concentrado em duas categorias identificáveis".

**Conclusão da triagem experimento vs. avaliação:** é principalmente um
problema de **desenho experimental / composição do dataset** (splits e
exemplos com label falso ou sem sinal), não um bug na lógica de avaliação
(`metrics.py`/`baselines.py` foram auditados e estão corretos — ver seção 5).
O comportamento do GAT (memorizar identidade do nó ao invés de usar o grafo)
permanece uma hipótese válida e não descartada, mas não dá para separar esse
efeito do ruído estrutural acima até o dataset ser corrigido.

---

## 2. Onde está falhando, caso a caso

### 2.1 S3 — falha total, `n_test=0` (bug de dados)

```json
"S3": {"n_train": 115, "n_test": 0, "gat_top3_mean": NaN, ...}
```

Todos os 115 grafos caem em `train`. Motivo: `S3_HELD_OUT_FAULT = "pod-delete"`
em [build_dataset.py](../../experiment/eval/gat/build_dataset.py#L59), mas o
chaos `pod-delete` **não existe** na campanha atual — o tipo equivalente foi
renomeado/substituído por `container-kill` (19 runs). O split S3 ficou órfão.

Isso não é "o GAT falhou em generalizar para falha nova" — é "não existe
nenhuma falha nova no dataset para testar". `merge_split_results.py` e
`train.py` calculam a média sobre uma lista vazia → `NaN` propaga para toda a
tabela. **Não há nada a diagnosticar no modelo aqui até o split ser
redefinido** (trocar `S3_HELD_OUT_FAULT` para `container-kill`, ou reintroduzir
`pod-delete` na coleta).

### 2.2 S2 — GAT e MLP abaixo do baseline B1 (desenho hostil, não bug)

```
GAT  S2: 0.44 ± 0.06   MLP S2: 0.60 ± 0.03   B1: 0.67 [0.50, 0.83]
```

`S2_HELD_OUT_SERVICES = ("orders", "payment")` remove os 30 grafos com esses
alvos do treino inteiramente:

```
S2: labels do TESTE presentes no TREINO?
   orders    test_n=15  train_n=0
   payment   test_n=15  train_n=0
```

Qualquer classificador paramétrico (GAT ou MLP) nunca viu essas classes no
treino — o "aprendizado" possível é só via features genéricas (não índice do
nó). B1 (heurística "maior anomalia absoluta") não depende de ter visto a
classe antes, então tem vantagem estrutural. O resultado GAT/MLP < B1 aqui já
era esperado e está documentado como **diagnóstico de viés de posição**, não
como veredito de H001 (ver `/memories/repo/gat-h001.md`, decisão de
2026-09-21). Persiste no dataset novo.

Adicionalmente, 5 dos 30 casos de teste de S2 são `payment ×
http-status-code` — rótulo suspeito (ver 2.3) — o que piora ainda mais o
baseline "ver estatísticas de payment", pois metade dos exemplos de payment
tem sinal artificial.

### 2.3 `http-status-code` sob Istio — label sistematicamente falso

22 grafos (7 "http-status-code" + 15 com o path do manifesto YAML como
`chaos_type_raw`, não normalizado — ver seção 4.2 sobre bug de normalização).
Amostra do teste manual (`error_rate_lr` do próprio alvo):

```
run-f931cb7c8a5f  catalogue  err=None  lat=+0.66  traf=+0.49
run-6f91513c7f17  payment    err=None  lat=-0.04  traf=+0.04
run-542766ee8337  payment    err=None  lat=-0.00  traf=-0.03
run-22ee9cd9b11e  payment    err=None  lat=-0.00  traf=+0.07
```

Em nenhum desses casos o `error_rate_lr` do serviço-alvo está preenchido
(sempre `None`) e latência/tráfego oscilam perto de zero — exatamente o
padrão já documentado no ERRO 2/5 de
[erros-experimentos.md](../../experiment/docs/erros-experimentos.md#🔴-erro-2--http-status-code-não-injeta-efeito-sob-istio-label-falso):
o Envoy sidecar contorna o proxy do Litmus, então o chaos não produz sintoma
observável no alvo rotulado. B1 nesses casos acerta por acaso ou erra sem
recurso — não há métrica correlacionada ao rótulo.

### 2.4 `queue-master` — alvo sem sinal em 10/10 casos

```
queue-master   n=10  alvo_sem_sinal=10  B1_top3=0.00  a_gt_mediana=0.00
```

Confirma ERRO 3 (fila AMQP/RabbitMQ, invisível ao Istio): toda vez que
`queue-master` é o rótulo, as três golden metrics do próprio nó são `None`/0.
Não existe scorer possível (nem humano) que acerte esses casos a partir dessas
features — falta de dado, não falta de modelo.

### 2.5 Outros alvos problemáticos (dentro do dataset "limpo")

```
carts       alvo_sem_sinal=3/15   B1_top3=0.40
shipping    alvo_sem_sinal=1/9    B1_top3=0.11
front-end   alvo_sem_sinal=0/10   B1_top3=0.30
```

`shipping` e `front-end` têm sinal presente mas o baseline ainda falha
bastante — aqui sim há espaço para o GAT ajudar (usar topologia para
diferenciar sintoma difuso vs. causa raiz), mas com `n=9` e `n=10` no
dataset inteiro a variância domina qualquer conclusão.

---

## 3. Onde estão os resultados do MLP (pergunta do usuário)

Estão em
[experiment/eval/gat/out/gat/mlp_ablation.json](../../experiment/eval/gat/out/gat/mlp_ablation.json),
gerados por `python -m experiment.eval.gat.mlp_ablation --save`. O arquivo
citado no resumo do usuário estava **desatualizado** (rodado em
2026-09-20, antes do reset de dados de 2026-09-21) — os números antigos
(MLP S1=0.63, S2=0.63, S3=0.59) vinham do dataset pré-reset (216–181 grafos,
outra composição). Reexecutei a ablação no dataset atual (115 grafos,
pós-reset) para termos comparação honesta:

| Split | GAT top-3 | MLP top-3 (3 seeds) | B1 (heurística) |
|---|---|---|---|
| S1 (test=11) | 0.42 ± 0.15 | 0.36 ± 0.15 | 0.27 [0.09, 0.55] |
| S2 (test=30) | 0.44 ± 0.06 | 0.60 ± 0.03 | 0.67 [0.50, 0.83] |
| S3 (test=0)  | NaN | NaN | NaN |

Achados:
- Em S1, GAT ≳ MLP ≳ B1 — os três próximos, alta variância (n=11).
- Em S2, MLP > GAT, mas ambos < B1 — reforça a hipótese de viés de posição
  do GAT (H1 do diário de 2026-09-21) só parcialmente: mesmo o MLP
  (sem topologia) perde para a heurística sem aprendizado, sugerindo que o
  problema em S2 é mais "zero-shot de classe nunca vista" do que
  especificamente "GAT decora nó via grafo".
- **Recomendação:** rodar `mlp_ablation.py` sempre junto de `train.py` (ou
  incluir no `merge_split_results.py`) para essa comparação nunca ficar
  desatualizada — hoje são scripts/artefatos desacoplados e ninguém percebeu
  a defasagem até esta análise.

---

## 4. Experimento vs. avaliação — onde está o problema

### 4.1 Avaliação (`metrics.py`, `baselines.py`, `merge_split_results.py`)

Auditados nesta sessão — **sem bugs encontrados**:
- `_rank_of_label` trata empates de forma conservadora (não infla métrica).
- `bootstrap_ci` reamostra por *run* (unidade experimental correta), não por
  nó.
- `evaluate_scorer`/`bootstrap_ci` retornam `NaN` corretamente quando não há
  grafos com label — isso é o que se propaga em S3, é o comportamento
  esperado dado `n_test=0`, não um bug de cálculo.
- B0/B1/B2 implementam exatamente o que a docstring descreve.

### 4.2 Dado que a avaliação processa (achado extra: normalização de chaos_type)

`chaos_type_normalized` deveria remover o prefixo de serviço
(`normalize_chaos_type` em
[build_dataset.py](../../experiment/eval/gat/build_dataset.py#L64-L82)), mas a
lista `known` não cobre o valor bruto quando ele é o **path do manifesto YAML**
(ex.: `.../catalogue-http-status-code.yaml`) em vez do nome do workflow — 15
dos 22 grafos de `http-status-code` carregam esse path inteiro como
`chaos_type_normalized`, poluindo qualquer agregação por `chaos_type` (inclui
`merge_split_results`/relatórios de auditoria que agrupam por esse campo).
Não afeta o cálculo de métricas (que usa `label.root_service_idx`, correto),
mas prejudica qualquer diagnóstico futuro por tipo de chaos — vale corrigir
`normalize_chaos_type` para extrair o sufixo mesmo quando vem de um path de
arquivo.

### 4.3 Experimento (coleta/dataset) — aqui está o grosso do problema

Resumo dos 4 problemas identificados, todos já com causa raiz conhecida:

| # | Problema | Runs afetados | Efeito |
|---|---|---|---|
| A | S3 sem exemplos de teste (`pod-delete` ausente da campanha atual) | 0 test / 115 total | métrica NaN, split inutilizável |
| B | S2 remove 100% do treino de 2 das 8 classes | 30/115 (teste) | zero-shot forçado, GAT/MLP < B1 por desenho |
| C | `http-status-code` sob Istio sem efeito observável (label falso) | 22/115 (19%) | ruído sistemático correlacionado ao rótulo errado |
| D | `queue-master` sem sinal observável (fila AMQP) | 10/115 (9%) | alvo irrecuperável para qualquer scorer baseado em métricas |

Juntos, B+C+D tocam **~55% dos grafos** (com sobreposição entre B e C em
`payment`). Isso por si só explica a maior parte da distância entre o
resultado "péssimo" e o que seria um resultado limpo — como mostrado na
seção 1, tirar C+D já eleva o piso baseline de 0.52→0.60 top-3.

---

## 5. Hipóteses para o resultado ruim (ordenadas por probabilidade)

1. **H-DADO (alta confiança, evidência direta acima):** o dataset pós-reset
   ainda carrega os 3 problemas de coleta já documentados em
   `erros-experimentos.md` (ERRO 2/3/5) e um problema de configuração de
   split (S3 órfão). O "resultado péssimo" é majoritariamente reflexo disso,
   não do modelo.
2. **H-SPLIT-S2 (confirmada, já era veredito anterior):** S2 mede
   generalização zero-shot para classe nunca vista, não localização de causa
   raiz — não deveria ser comparado ao B1 como se fosse um teste justo de
   "o modelo aprendeu a task". Ver decisão de 2026-09-21 já registrada em
   memória.
3. **H-AMOSTRA (confiança média):** com `n_test` de 11 e 30, os intervalos de
   confiança são largos (ex.: S1 GAT seeds variam de 0.18 a 0.64). Boa parte
   da diferença GAT vs B1 em cada split individual pode não ser
   estatisticamente significativa — os CIs do B1 e do GAT se sobrepõem em S1.
4. **H-POSIÇÃO (não refutada, não teve teste novo nesta sessão):** GAT
   memoriza identidade de nó via topologia estática, como já apontado em
   sessões anteriores (H1 do diário 2026-09-21). Esta sessão não conseguiu
   isolar esse efeito do ruído de dado (H-DADO/H-SPLIT) porque ambos afetam
   os mesmos splits. Teste pendente: rodar GAT só no subconjunto "limpo"
   (sem http-status-code, sem queue-master) e comparar contra MLP no mesmo
   subconjunto.
5. **H-FEATURE-GAP (secundária):** `error_rate_lr` falta em 78% dos nós
   (719/920) no dataset inteiro — bem mais que `traffic_lr` (14%) ou
   `latency_p95_lr` (18%). Isso reduz o sinal disponível especificamente para
   falhas de erro HTTP, reforçando o problema C acima mesmo fora dos casos
   diretamente rotulados como falsos.

---

## 6. Próximos passos recomendados

1. **Corrigir o split S3** — trocar `S3_HELD_OUT_FAULT` para `container-kill`
   (o transiente disponível na campanha atual) ou reintroduzir `pod-delete`
   na coleta, e então re-rodar treino/merge.
2. **Excluir os 22 grafos de `http-status-code`** do treino/teste (ou
   completar a migração para `VirtualService fault.abort` nativo do Istio e
   recoletar) — está pela metade: o runner já usa `fault.abort`
   ([ver ação corretiva de 2026-09-21](../../research/diario) /
   `/memories/repo/gat-h001.md`), mas os 22 grafos antigos com label falso
   ainda estão no `dataset.jsonl`.
3. **Decidir o tratamento de `queue-master`**: excluir da task WHERE (não
   observável via Istio) ou aceitar como "não localizável" e reportar
   separadamente — hoje ele só derruba a métrica sem ensinar nada.
4. **Rodar GAT + MLP + baselines apenas no subconjunto limpo** (83 grafos, sem
   C e D) para isolar H-POSIÇÃO de H-DADO — é o teste mais barato e decisivo
   pendente.
5. **Corrigir `normalize_chaos_type`** para não vazar paths de arquivo (item
   4.2) — barato e evita poluir relatórios de auditoria futuros.
6. **Manter `mlp_ablation.py` sincronizado** com `train.py`/
   `merge_split_results.py` (mesmo dataset, mesmo momento) para a comparação
   GAT vs MLP nunca ficar desatualizada como aconteceu aqui.

---

## Apêndice — números brutos usados nesta análise

- Dataset avaliado: `experiment/eval/gat/out/dataset.jsonl` (115 grafos,
  gerado 2026-09-22 04:30).
- `gat_vs_baselines.json` (2026-09-22 06:03) — resultado citado pelo usuário.
- `mlp_ablation.json` — **regenerado nesta sessão** (2026-09-22) sobre o
  mesmo dataset, pois a versão anterior (2026-09-21 06:44) era de antes do
  reset e não é comparável.
- Baselines B0/B1/B2 no dataset "limpo" (sem http-status-code, sem
  queue-master, n=83): B0=0.45, B1=0.60, B2=0.58 top-3 — vs. dataset bruto
  (n=115): B0=0.45, B1=0.52, B2=0.51.
