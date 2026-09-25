# 2026-09-21 — Hipóteses: por que o MLP supera o GAT?

**Contexto.** No re-teste da H001 com edge features reais (Istio), o **MLP por-nó**
(sem grafo, sem posição) superou o GAT em todos os splits:

| Split | GAT | MLP | B1 |
|---|---|---|---|
| S1 | 0.48 | 0.61 | 0.45 |
| S2 | 0.24 | 0.56 | 0.58 |
| S3 | 0.40 | **0.63** | 0.47 |

O MLP — que não usa estrutura nenhuma — é o melhor modelo aprendido. Isso indica
que **a estrutura do grafo, como usada, subtrai valor**. Este documento registra
as hipóteses causais, as **previsões falseáveis** de cada uma e **como testá-las**.
Será revisitado após a campanha de expansão (`gat-istio-v2`) gerar mais dados full.

---

## H1 — Topologia estática injeta viés de posição (mais provável)

**Mecanismo.** A topologia é idêntica em todos os grafos → cada serviço ocupa uma
posição fixa e reconhecível. A atenção do GAT, para propagar features entre
vizinhos, internaliza a **posição/identidade** do nó. O MLP (pesos
compartilhados, sem arestas) é cego à posição: vê apenas "um nó com estas
features".

**Evidência já existente.** Em S2 (orders/payment nunca são causa no treino), o
GAT colapsa abaixo do acaso (0.24 < 0.375) mesmo com o alvo visivelmente anômalo
— aprendeu a nunca apontar para aquelas posições.

**Previsão falseável.** Embaralhar/aleatorizar as arestas por run (remover a
posição fixa) → o GAT em S2 deve **subir** ao nível do MLP.

**Como testar.** Ablação "GAT com topologia embaralhada": permutar as arestas
(mantendo o grau) por grafo. Barato, sem nova coleta. **Confirma H1 se S2 subir.**

## H2 — Message passing dilui o sinal local (smoothing)

**Mecanismo.** A falha é localizada (alvo = nó mais anômalo). O GAT mistura
features de cada nó com a dos vizinhos, **borrando** o contraste alvo-vs-resto.
Em S2/S3 (alvo nunca visto), o modelo depende desse contraste — e o smoothing o
destrói. MLP preserva o contraste intacto.

**Previsão falseável.** Reduzir propagação (menos camadas de atenção / mais
dropout nas arestas) → GAT aproxima do MLP. Atenção aprendida concentra-se na
**self-loop** quando o modelo acerta.

**Como testar.** Inspecionar pesos de atenção por camada; ablação com 1 camada;
dropout de aresta alto. **Confirma H2 se menos propagação → melhor S2/S3.**

## H3 — Fome de dados reais + dominação dos static

**Mecanismo.** Treino com ~193 static (arestas zeradas/mascaradas) vs poucos full.
Máscaras de aresta quase sempre zeradas → o GAT aprende a **ignorar arestas**,
ficando com a topologia fixa (a parte prejudicial). O MLP não usa arestas, logo
não sofre.

**Previsão falseável.** Treinar o GAT **só com grafos full** → deve melhorar.

**Como testar.** **É o que a expansão `gat-istio-v2` testa.** Após a coleta,
treinar GAT full-only. **Se full-only não passar o MLP, H3 é refutada** e sobram
H1/H2.

## H4 — Overfitting estrutural (mais parâmetros, poucos dados)

**Mecanismo.** GAT (2 camadas, multi-head) tem capacidade de sobra para **decorar**
~150–200 grafos de 8 nós — inclusive o viés de posição. O MLP, mais simples, é
forçado a aprender a regra geral.

**Previsão falseável.** Curva train-vs-val do GAT mostra gap grande
(memorização); reduzir hidden/heads → gap diminui e aproxima do MLP.

**Como testar.** Plotar train_loss vs val_top3 por época; ablação de capacidade.
**Confirma H4 se modelo menor → melhor generalização.**

## H5 — Heterogeneidade de réplicas (especulativa)

**Mecanismo.** Réplicas da mesma célula variam (ruído de cluster); o GAT capta
idiossincrasias via arestas; o MLP é mais robusto. Só testável com mais dados.

---

## Leitura atual e plano de verificação

**H1 e H2 são a mesma moeda** (a estrutura fixa é o problema) e já têm evidência
forte. A expansão de dados testa **H3** (o mais acionável agora). Ordem de
verificação proposta, do mais barato ao mais caro:

1. **H4** — curvas train/val (grátis, já temos o history).
2. **H1** — GAT com arestas embaralhadas (barato, sem coleta).
3. **H2** — inspeção de atenção + ablação de propagação (barato).
4. **H3** — GAT full-only (depende da coleta em curso).

**Critério de encerramento:** se, após dados full suficientes, o GAT ainda não
superar o MLP/B1 com folga em S2/S3, a conclusão é que **a estrutura de grafo não
agrega valor para localização nesta configuração** — achado cientificamente
válido que redireciona a tese (features por-nó + modelos mais simples, ou grafos
com arestas genuinamente dinâmicas por run).

---

## Nota — decisão sobre S2 (registrada em 2026-09-21)

Discussão com o autor: **S2 (held-out service) mede algo artificial** — na prática
é possível injetar falha em orders/payment e treinar com isso. A generalização
**operacionalmente relevante** é:
- **falha nova em serviço conhecido** (não treinável — o mais realista), e
- **combinação falha × serviço não vista** (vi a falha em outros nós e outras
  falhas neste nó, mas nunca juntas) — um possível **S2'**.

**Decisão:** S2 atual **permanece, mas como diagnóstico** (detector de viés de
posição — foi ele que expôs o defeito do GAT), **não como critério de validação
da H001**. O peso da validação desloca-se para S3 (falha nova) e, se adotado, um
futuro S2' (combinação held-out). A mudança de S2 → S2' altera `build_dataset`
(regra de split) e fica **adiada** para depois da coleta, a decidir com dados.
