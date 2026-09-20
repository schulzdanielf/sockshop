# 2026-09-20 — Revisão científica do pipeline (antes do treino)

Auditoria crítica antes de treinar o GAT, para garantir validade científica e
identificar pontos de falha. Pedido explícito do usuário: "garanta que o
experimento está de acordo com as melhores práticas científicas e que não há
pontos de falha".

## Ameaças à validade (checklist)

### Validade interna (leakage) — OK, com uma ressalva

- ✅ **Baseline por run:** cada grafo normaliza pela própria janela baseline
  (log-ratio) — sem vazamento cross-run.
- ✅ **Splits anti-leakage S2/S3:** validado — zero runs de orders/payment como
  treino em S2; zero pod-delete como treino em S3.
- ✅ **Sem IDs de serviço** nos tensores — indutivo para S2.
- ⚠️ **Vazamento topológico (risco aberto):** a topologia é **estática e idêntica**
  em todos os runs. O modelo pode memorizar posição no grafo (front-end = alto
  grau; queue-master = folha) em vez de aprender a assinatura da falha. Registrado
  em H001; mitigação parcial = features sem ID + reportar S2 honestamente.

### Validade de construto — LIMITAÇÃO HONESTA

- ⚠️ **Topologia estática + arestas neutras** significa que o **message passing do
  GAT tem pouco a propagar** que varie por run. O único sinal que muda por run são
  as *node features*. Consequência: o GAT tende a degenerar para "qual nó é
  anômalo" — muito próximo de uma heurística por nó (B1). **Isto não invalida o
  experimento, mas define o teto**: o GAT precisa provar que a estrutura do grafo
  agrega sobre a heurística. Por isso os **baselines são obrigatórios**.

### Dados faltantes — PONTO DE FALHA CORRIGIDO

Auditoria de completude (1536 node-features):

| Feature | % None | Causa |
|---|---|---|
| `error_rate_lr` | **69%** | só front-end/payment/user reportam erro |
| `latency_p95_lr` | 21% | janelas curtas / serviço não chamado |
| `traffic_lr` | 15% | **queue-master (async): 192/192 None** |

- **`queue-master` é um worker assíncrono** (consome RabbitMQ, sem HTTP) →
  golden metrics HTTP não se aplicam. Mas é **label em 17 runs** → localizá-lo
  pelas golden metrics do próprio nó é quase impossível. Registrado como
  limitação: faults em queue-master só seriam detectáveis por efeito downstream
  (aresta shipping→queue-master), que não temos (arestas estáticas/neutras).
- **Correção:** imputação `None → 0.0` (log-ratio neutro = "sem mudança") **+ máscara
  binária por métrica** (1=observado, 0=imputado), para o modelo distinguir "sem
  mudança" de "sem dado". Sem a máscara, 69% de zeros em error_rate seriam
  interpretados como "nenhum erro em lugar nenhum" — falso.

### Validade estatística — A GARANTIR NO TREINO

- Test sets pequenos (S1=22, S2=58, S3=50) → **bootstrap CI 95% obrigatório**;
  nenhuma alegação de Δ sem IC.
- Múltiplos splits → reportar **todos**, sem cherry-picking.
- **Seeds fixas** para reprodutibilidade (numpy + torch).
- Dataset pequeno (192) + GNN → **early stopping em validação** + regularização.

### Validade externa — LIMITAÇÃO REGISTRADA

- Sistema único (Sock Shop), dados limitados por retenção (24h Tempo).
- Generaliza para "microserviços" só como estudo de caso, não população.

## Baselines obrigatórios (ordem científica correta: piso antes do modelo)

| ID | Baseline | O que mede |
|---|---|---|
| **B0** | Random uniforme | Piso absoluto (top-3 esperado = 3/8 = 37.5%) |
| **B1** | Max-anomaly node (maior \|log-ratio\| entre as golden metrics) | Heurística sem grafo — o teto a bater |
| **B2** | Anomaly + difusão 1-hop na topologia | Testa se a **estrutura** ajuda (mini-preview do GAT) |

> **Nota sobre B2 clássico (PageRank reverso):** com topologia **estática e
> idêntica** por run, PageRank puro dá o **mesmo ranking sempre** → inútil como
> preditor por-run. Por isso B2 aqui é anomaly-com-difusão (usa features do run),
> não PageRank estrutural puro. Achado registrado.

## Decisão de sequência (revisada)

1. **Métricas + bootstrap CI** (`metrics.py`) — puro, testável.
2. **Baselines B0/B1/B2** (`baselines.py`) — estabelecem o piso.
3. **Avaliação por split** (`evaluate.py`) — B0/B1/B2 com IC 95%.
4. **Só então o GAT** — com imputação+máscara, seeds, early stopping, comparado
   contra os baselines nos mesmos splits.

Sem os passos 1–3, treinar o GAT seria cientificamente vazio (não haveria com o
que comparar). Esta é a correção metodológica desta revisão.
