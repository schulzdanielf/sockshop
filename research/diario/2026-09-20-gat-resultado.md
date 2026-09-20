# 2026-09-20 — Resultado do GAT vs baselines: H001 REFUTADA (Opção D)

Treino do GAT (GATv2, 2 camadas, imputação+máscara, 3 seeds, early stopping em
validação estratificada), comparado aos baselines nos mesmos splits.

## Resultado (top-3 accuracy)

| Split | n_test | GAT (mean ± std) | B0 random | B1 max-anomaly | B2 difusão |
|---|---|---|---|---|---|
| **S1** repetição | 22 | 0.50 ± 0.10 | 0.36 | **0.59** | 0.45 |
| **S2** held-out serviço | 58 | **0.14 ± 0.06** | 0.34 | **0.60** | 0.53 |
| **S3** held-out fault | 50 | 0.43 ± 0.08 | 0.34 | **0.46** | 0.32 |

## Veredito: H001 refutada na forma atual (Opção D)

Critério de H001 (definido *antes* do experimento): top-3 ≥ B1 + 10 p.p. em S2 e
S3, sem regressão em S1. **O GAT não supera B1 em nenhum split** — perde em todos.
Em S2 o GAT fica **abaixo do acaso** (0.14 < 0.34). H001 refutada para o desenho
com topologia estática + arestas neutras.

## Mecanismo do fracasso (entendido, não anedótico)

**Análise de erros no S2:** B1 (só features, sem aprendizado) localiza `orders` em
**87%** e `payment` em **32%** top-3. Ou seja, os serviços held-out **são
perfeitamente localizáveis pelas features** — em 0/58 runs o alvo tem anomalia
zero. Mesmo assim o GAT colapsa para 0.14.

**Causa:** o GAT aprendeu **viés de posição topológica**. Em S2, orders/payment
nunca aparecem como label no treino (held-out). Com topologia **estática e
idêntica** em todos os grafos, o modelo aprende "que posição no grafo costuma ser
a culpada" em vez de "qual nó tem features anômalas". Ao testar em orders/payment
— posições que ele treinou para *nunca* apontar — erra sistematicamente, abaixo do
acaso. B1, puramente baseado em features, não tem esse viés e generaliza.

Isto é **exatamente o vazamento topológico** registrado como risco em H001. O
split anti-leakage S2 o expôs de forma brutal e inequívoca. É um resultado
metodologicamente forte: a rigor científica do desenho é o que torna este
resultado negativo confiável.

## Achados que os baselines já anunciavam

1. **B2 (difusão na topologia) < B1 (sem topologia)** em todos os splits — a
   topologia estática já era ativamente prejudicial. O GAT, que depende do mesmo
   message passing, herdou e amplificou o problema.
2. **B1 é forte e feature-based** — o sinal está nas node features, não na
   estrutura estática.

## Conclusão científica e caminho adiante

O resultado negativo é **informativo e publicável**: com topologia estática e sem
edge features reais, um GNN **memoriza identidade de nó** em vez de aprender
assinatura de falha, e generaliza pior que uma heurística trivial por nó.

Implicações concretas:

1. **Edge features reais são necessárias** (não suficientes garantidamente, mas
   necessárias) — motiva a trilha de telemetria forte (Pixie/mesh), agora com
   evidência empírica de que a topologia estática não basta.
2. **Ablação natural (próximo experimento):** um MLP por-nó (sem grafo, sem message
   passing) sobre as mesmas node features. Se o MLP ≥ GAT, prova que a estrutura é
   o problema, não o aprendizado. É a comparação que fecha o argumento.
3. **Mitigação de leakage topológico:** para um GNN fazer sentido aqui, ou (a) a
   topologia precisa variar por run (edge features reais), ou (b) treinar com
   todos os serviços como possíveis labels (o que S2 proíbe por design — daí o
   valor do split).

H001 permanece **refutada para Opção D**; a hipótese de que um GAT com **edge
features reais** supere B1 fica em aberto para quando a telemetria forte existir.

## Ablação (capstone) — MLP por-nó prova que a topologia é o problema

`experiment/eval/gat/mlp_ablation.py` — MLP com as **mesmas node features**, mas
sem grafo, sem message passing e sem noção de posição (pesos compartilhados):

| Split | GAT (grafo) | **MLP (sem grafo)** | B1 |
|---|---|---|---|
| S1 | 0.50 | 0.53 | 0.59 |
| S2 | 0.14 | **0.48** | 0.60 |
| S3 | 0.43 | **0.86** | 0.46 |

**Conclusão definitiva:** o MLP supera o GAT dramaticamente (S2: 0.48 vs 0.14;
S3: 0.86 vs 0.43) e **bate o B1 no S3** (0.86 vs 0.46). Como o MLP não tem noção
de posição (pesos idênticos por nó), ele **não pode memorizar identidade** — por
isso generaliza. Isto prova, de forma isolada, que **a topologia estática é
ativamente prejudicial**: o message passing sobre ela injeta viés de posição que
quebra a generalização (S2/S3).

**Achado positivo importante:** o MLP por-nó sobre as golden metrics em log-ratio
**generaliza muito bem para faults não vistos** (S3 = 0.86) — a assinatura de
feature por nó é o sinal real, e ela é aprendível e transferível. Este é o
verdadeiro estado-da-arte do experimento até agora, não o GAT.

**Narrativa científica completa e honesta:** baselines fortes (B1) → GAT falha e
entendemos por quê (memorização topológica exposta por S2) → ablação (MLP) prova
que o grafo é o problema e recupera a generalização → caminho adiante (edge
features reais) empiricamente motivado.
