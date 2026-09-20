# 2026-09-20 — Resultado dos baselines (piso científico) — ACHADO CRÍTICO

Baselines avaliados por split, top-3 (primária) com IC 95% via bootstrap.
Este é o **piso** que o GAT precisa superar para H001 ter valor.

## Resultado (top-3 accuracy [IC 95%])

| Split | n | B0 random | B1 max-anomaly (sem grafo) | B2 anomaly+difusão |
|---|---|---|---|---|
| **S1** (repetição) | 22 | 0.41 [0.23, 0.64] | **0.59 [0.36, 0.77]** | 0.45 [0.27, 0.68] |
| **S2** (held-out serviço) | 58 | 0.36 [0.24, 0.50] | **0.60 [0.47, 0.74]** | 0.53 [0.40, 0.66] |
| **S3** (held-out fault) | 50 | 0.26 [0.14, 0.38] | **0.46 [0.34, 0.60]** | 0.32 [0.20, 0.44] |

top-1 muito baixo em todos (0.06–0.22) → **top-3 é a métrica certa**; localizar o
serviço exato com só golden metrics é intrinsecamente difícil.

## Achados críticos (impactam a tese)

### 1. B2 (difusão na topologia) é PIOR que B1 (sem grafo) em todos os splits

Este é o achado mais importante. **Propagar a anomalia pela topologia estática
piora a localização** — a estrutura, como está (estática + arestas neutras), não
carrega sinal útil; na prática dilui a anomalia do próprio nó.

**Consequência direta para o GAT:** o message passing do GAT opera sobre a mesma
topologia. Se difundir sinal pela topologia piora (B2<B1), há forte indício de
que o GAT terá dificuldade em superar B1. **Isto valida a preocupação de construto
da revisão:** sem edge features reais (Pixie/telemetria forte), o grafo agrega
pouco. O experimento agora tem uma **hipótese-nula empírica forte**.

### 2. B1 (max-anomaly) é o bar real, e ele é modesto

0.46–0.60 top-3. O GAT precisa superar isso **além dos ICs** para H001 se
sustentar. Dado que B2<B1, o caminho é o GAT extrair das node features padrões que
o `max` simples não captura (ex.: combinação traffic+latency aprendida), não a
topologia.

### 3. ICs largos (n=22–58) exigem cautela

Em S1, B1 [0.36,0.77] **sobrepõe** B0 [0.23,0.64] — nem B1>random é claramente
significativo em S1. Só em S2 (n=58) B1 [0.47,0.74] está claramente acima de B0
[0.24,0.50]. **Nenhuma alegação de superioridade sem checar sobreposição de IC.**

## Implicação metodológica

O GAT será treinado e comparado honestamente contra estes baselines. Três
desfechos possíveis, todos cientificamente informativos:

1. **GAT > B1 além do IC** → H001 sustentada; as node features têm estrutura
   aprendível que o grafo ajuda a explorar.
2. **GAT ≈ B1** → o grafo estático não agrega; confirma que edge features reais
   (telemetria forte) são necessárias. Motiva a trilha Pixie/mesh.
3. **GAT < B1** → overfitting no dataset pequeno; registrar e regularizar.

Qualquer um dos três é um resultado publicável e honesto. O experimento está
metodologicamente blindado: baselines primeiro, ICs, splits anti-leakage, seeds.
