# Memory-hog evaluation harness

Automated harness to (1) inject `pod-memory-hog` chaos in a chosen subset of
Sock Shop services to seed the RAG corpus, then (2) inject memory-hog in a
held-out subset of services and (3) evaluate multiple LLM analysis strategies
on the same held-out runs.

The goal is to measure how RAG and summarisation choices affect top-1 RCA
accuracy when the ground truth (`chaos_target`) is known a priori.

## Layout

```
experiment/eval/memory_hog/
├── config.yaml                    ← experimental matrix (services, strategies)
├── generate_chaos_manifests.py    ← renders per-service Argo manifests
├── runner.py                      ← orchestrator (train + test + eval)
├── README.md                      ← this file
└── out/
    ├── manifests/<svc>-memory-hog.yaml   ← one Argo workflow per service
    ├── runs.csv                          ← one row per (service, replica)
    └── evaluation.csv                    ← one row per (run_id, strategy)
```

## Prerequisites

* The platform is running and reachable at `http://127.0.0.1:8010`
  (`uvicorn experiment.platform.backend.main:app --host 0.0.0.0 --port 8010`).
* The MCP observability server is up at `127.0.0.1:18080`.
* Local Qwen LLM is up at `localhost:8001/v1/chat/completions`.
* Litmus + Argo are installed in the `litmus` namespace, with the
  `controller-instanceid` `f21787ba-08ee-4649-ab66-c880492636dc`.
* `kubectl` context points to the cluster running Sock Shop in the
  `sock-shop` namespace.
* `PyYAML` is installed in the active venv (already a transitive dep of the
  platform).

## One-time setup — register chaos workflow templates

The platform's Litmus plugin uses Argo's "clone the latest run" trick to
resubmit named workflows (`<service>-memory-hog`). For that to work, each
workflow template name must already exist in Litmus.

1. Generate one Argo workflow YAML per target service from the existing
   `catalogue-memory-hog.yaml` baseline:

   ```bash
   python experiment/eval/memory_hog/generate_chaos_manifests.py
   ```

   Output goes under `experiment/eval/memory_hog/out/manifests/`.

2. Apply them once so Litmus learns the templates (each apply triggers one
   workflow run — expected; the workflow name persists afterwards):

   ```bash
   kubectl apply -f experiment/eval/memory_hog/out/manifests/
   ```

3. Verify:

   ```bash
   kubectl get workflows.argoproj.io -n litmus | grep memory-hog
   ```

Tweaks to memory consumption, chaos duration, or the service list go in
`config.yaml`; just re-run `generate_chaos_manifests.py` afterwards.

## Catálogo de falhas estendido (network / dns / io / http / container)

Além de `memory-hog`, `cpu-hog` e `pod-delete`, o gerador suporta seis falhas
adicionais. Os baselines do serviço `catalogue` ficam em
`deploy/kubernetes/manifests-chaos/` e o catálogo completo (parâmetros,
`fault_category` e ressalvas) está documentado em
[`deploy/kubernetes/manifests-chaos/README.md`](../../../deploy/kubernetes/manifests-chaos/README.md).

| `chaos_type` | ChaosExperiment | `fault_category` | Na matriz? | Status docker-desktop/WSL2 |
|---|---|---|---|---|
| `network-latency` | `pod-network-latency` | `network-latency` | ✅ sim | ✅ roda (requer `sch_netem`) |
| `network-loss` | `pod-network-loss` | `network-loss` | ✅ sim | ✅ roda (requer `sch_netem`) |
| `http-status-code` | `pod-http-status-code` | `http-error` | ✅ sim | ✅ roda |
| `container-kill` | `container-kill` | `pod-failure` | ✅ sim | ✅ roda |
| `io-stress` | `pod-io-stress` | `io-exhaustion` | ❌ não | ❌ não roda (overlayfs/O_DIRECT) |
| `dns-error` | `pod-dns-error` | `dns-failure` | ❌ não | ❌ não roda (dns_interceptor) |

As **4 falhas marcadas ✅** já estão nas listas `train_experiments` /
`test_experiments` (cada tipo aparece em treino e em teste, em serviços
diferentes). `io-stress` e `dns-error` continuam declaradas em `chaos_types`
mas **fora da matriz**, porque não rodam neste cluster (ver abaixo). Para
habilitá-las em outro ambiente, adicione células `{service, chaos_type}` nas
duas listas e re-rode `generate_chaos_manifests.py`.

> ⚠️ **Falhas de rede:** rode `make chaos-enable-netem` (`sudo modprobe
> sch_netem`) **uma vez por boot** antes de qualquer run `network-*`. Sem isso
> elas abortam com `Specified qdisc kind is unknown`. O carregamento não é
> persistente no WSL2.
>
> ❌ **Não rodam aqui:** `io-stress` falha porque `stress-ng --hdd` usa
> `O_DIRECT` (sem suporte no overlayfs do WSL2); `dns-error` falha porque o
> `dns_interceptor` sai com `exit status 1` no netns do alvo (mesmo com os
> módulos NFQUEUE carregados). Ambas devem funcionar em nós de cluster reais.
>
> ⚠️ **RCA:** as categorias novas já estão no `allowed_fault_categories` do
> system card, mas o validador/localizador só têm regras dedicadas para
> `memory/cpu/pod-failure`. Veja a seção "Integração com o pipeline de RCA" no
> README dos manifestos.

## Run the experiment

### 40 execucoes variadas, com falhas de 3 minutos

A campanha usa 40 combinacoes distintas de servico/falha da matriz suportada:
sete servicos de aplicacao e sete tipos de falha executaveis. Nenhum par se
repete e cada execucao consecutiva muda tanto o servico quanto a falha.
`http-status-code` nao e aplicado ao `front-end` (porta 8079). Bancos de dados,
RabbitMQ, `io-stress` e `dns-error` ficam fora da matriz.

O comando sem `--run` apenas imprime a matriz e a estimativa, sem criar
arquivos, consultar o cluster ou aplicar falhas:

```bash
.venv/bin/python -m experiment.eval.memory_hog.run_forty_faults
.venv/bin/python -m experiment.eval.memory_hog.run_forty_faults --run
```

Execute na raiz do repositorio, com a plataforma em `:8010` e os componentes
de carga/observabilidade dos pre-requisitos ativos.

A duracao da injecao e 180 s; nao garante indisponibilidade continua, pois
reinicios e recuperacao dependem do Kubernetes. Cada run tem 60 s de baseline,
30 s de warmup, margem de 90 s para inicializacao/finalizacao do workflow,
120 s de observacao posterior e 60 s de cooldown. Reserve cerca de 8 h 20 min
para as 40 execucoes, incluindo o overhead medido da plataforma; essa
estimativa nao inclui as 40 chamadas RCA feitas depois que todos os runs
terminarem.

O script verifica a disponibilidade do Sock Shop, limites de CPU/memoria,
`sch_netem`, ausencia de runs ativos e os templates Litmus necessarios.
Execute `make chaos-enable-netem` antes, se o modulo ainda nao estiver ativo.
Nao ha cadastramento automatico de templates: aplicar um template ausente
dispararia workflows adicionais. Se faltar algum, o preflight aborta com os
nomes para cadastramento separado.

O harness existente gerencia carga, injecao, limpeza e coleta de telemetria.
A campanha nao altera a configuracao principal. Depois dos 40 runs, o script
chama a mesma rota online usada pelo botao de RCA,
`POST /api/runs/{run_id}/rca`, para cada run concluido. Cada campanha grava
configuracao, manifestos, `runs.csv`, `effect_validation.json` e
`rca_evaluation.json` em `out/forty_faults/<campaign_id>/`. O relatorio RCA
preserva a resposta e a cobertura por ferramenta e resume a precisao de
localizacao do servico e de classificacao da categoria de falha contra o
ground truth da matriz. O codigo de saida so e zero se os runs, as verificacoes
de efeito e as chamadas RCA forem concluidos com sucesso.

## Reavaliar uma campanha existente com a Skill RCA

Para comparar a Skill RCA com o endpoint RCA anterior sem injetar falhas,
execute o avaliador sobre uma campanha já concluída. Por padrão, ele seleciona
exatamente os runs completos do relatório `rca_evaluation_retry.json`, chama
`POST /api/runs/{run_id}/skill-rca` para cada run e grava um relatório separado
`rca_skill_evaluation.json`, preservando os relatórios da RCA antiga:

```bash
.venv/bin/python -m experiment.eval.memory_hog.evaluate_skill_rca \
  --api-base http://127.0.0.1:8010
```

A plataforma deve estar reiniciada/recarregada com a rota Skill RCA. O comando
não executa caos. Para outra campanha, informe `--campaign-dir`; o resultado
exige o relatório baseline `rca_evaluation_retry.json` correspondente e
recusa sobrescrever um relatório já existente. Cada tentativa salva seu
artefato próprio na plataforma e o progresso é persistido após cada run.

End-to-end (train → test → eval):

```bash
python -m experiment.eval.memory_hog.runner
```

Re-evaluate strategies on previously collected runs (skip re-injecting chaos):

```bash
python -m experiment.eval.memory_hog.runner --phase eval
```

Dry-run the spec assembly without touching the cluster:

```bash
python -m experiment.eval.memory_hog.runner --dry-run
```

A final summary table prints to stdout:

```
─── Summary: top-1 RCA accuracy per strategy ────────────────────
strategy                            n      acc    avg_tok
S0_no_rag                           8   12.50%       1280
S1_rag_tags                         8   62.50%       2120
S2_rag_embeddings                   8   75.00%       2180
S3_rag_hybrid                       8   75.00%       2210
S4_rag_embeddings_no_card           8   50.00%       1900
S5_rag_short_budget                 8   62.50%       1610
```

`out/evaluation.csv` is the raw input for any further statistical analysis
(McNemar paired test, per-service breakdown, confidence calibration, etc.).

## Experimental matrix

Defaults in `config.yaml`:

| Dimension | Values | Cells |
|---|---|---|
| Train services | `catalogue, carts, orders, payment` | 4 |
| Test services  | `user, shipping, queue-master, front-end` | 4 |
| Replicas       | `2` per service | — |
| Strategies     | 6 (no-RAG / tags / embeddings / hybrid / no-card / short-budget) | — |

→ 16 chaos runs (~2h wall clock) + 8 × 6 = **48 evaluation rows**.

DBs and rabbitmq are excluded on purpose: a memory-hog on a database makes
every consumer fail, so the ground truth becomes ambiguous (the LLM can
legitimately name the symptomatic service rather than the injected one).

## Adding new strategies

Append an entry to `strategies` in `config.yaml`. Recognised fields map 1:1
to the `POST /api/runs/{id}/llm-analysis` query params:

```yaml
- name: "S6_custom"
  mode: "embedding"     # embedding | tags | hybrid
  limit: 5              # 0 disables RAG retrieval
  system_id: "sock-shop"  # "__none__" forces system-card fallback
  budget_tokens: 2400   # smaller → forces shorter summary
```

Re-run with `--phase eval` to backfill results on existing runs.

## Escopo do runner e comparação multiagente

O runner atual chama somente `POST /api/runs/{id}/llm-analysis`. Cada célula
`(run_id, strategy)` mede o baseline de LLM único com RAG e os validadores
determinísticos ativos; registra WHERE/WHY/full accuracy, confiança, tokens,
timings, retries e metadados de validator/localizer em `evaluation.csv`.
Ele não avalia `telemetry-chat` nem agentes especializados/paralelos.

Para o primeiro piloto multiagente, preserve o runner e o CSV atuais como
baseline. Implemente um adapter/runner separado que produza os mesmos campos
de avaliação e acrescente chamadas por agente, cobertura, evidências citadas,
latência e tokens por agente. Execute baseline e variante nos mesmos runs
concluídos com ground-truth e quality gates aprovados; não use runs sem
cobertura de traces para pontuar o especialista de traces. O plano está em
[`research/planos/2026-09-25-plano-llm-multiagente.md`](../../../research/planos/2026-09-25-plano-llm-multiagente.md).

## Campanha rápida de avaliação

`run_quick_campaign.py` executa uma amostra de cada falha compatível com o
cluster, em sete serviços diferentes: memory-hog, cpu-hog, pod-delete,
network-latency, network-loss, http-status-code e container-kill. A estimativa
é de cerca de 60 minutos, incluindo margem para templates Litmus ausentes e
finalização dos ChaosResults. A fase de fault dura 150 s para absorver o
start-up do ChaosResult e preservar os 60 s configurados de injeção. DNS e IO ficam de fora por incompatibilidade
conhecida com WSL2.

Prévia sem tocar no cluster:

```bash
python -m experiment.eval.memory_hog.run_quick_campaign
```

Para executar, carregue `sch_netem` e use `--run`:

```bash
make chaos-enable-netem
python -m experiment.eval.memory_hog.run_quick_campaign --run
```

Os sete runs são held-out (`test`), não entram no corpus RCA. O script salva
ledger, configuração, manifests e `effect_validation.json` em
`out/quick_campaign/`; o relatório combina o veredito de injeção com sinais
de efeito no serviço-alvo. A execução só começa com `--run`.

## Campanha Longa de Aproximadamente 10 Horas

`run_diverse_campaign.py` prepara 48 células: os sete tipos de falha que rodam
neste cluster, variando entre sete serviços. Cada fault é configurado para
300 s; HTTP fica fora do `front-end`, cujo tráfego usa a porta 8079. O cronograma
reserva margem para o start-up do Litmus e estima 10h10–10h22, usando o tempo
medido na primeira execução quick. `dns-error` e `io-stress` ficam excluídas
porque falham no WSL2 atual (`dns_interceptor`/netns e `O_DIRECT`/overlayfs).

Inspecione a matriz sem tocar no cluster:

```bash
python experiment/eval/memory_hog/run_diverse_campaign.py
```

Para executar, confirme antes que os deployments estão saudáveis e que
`sch_netem` está carregado; o preflight também verifica os limites de CPU e
memória dos alvos:

```bash
make chaos-enable-netem
python experiment/eval/memory_hog/run_diverse_campaign.py --run --wait-for-idle
```

A execução usa ID, configuração e ledger exclusivos em
`out/diverse_campaign/<campaign-id>/`, sem avaliação LLM. Com
`--wait-for-idle`, aguarda outras execuções da plataforma terminarem antes de
injetar faults; esse tempo de espera não entra nas 10 horas estimadas. Sem essa
opção, o script falha antes de iniciar se houver outro run ativo.

## Troubleshooting

* **`HTTP 400` on `/api/runs/start`** — likely the Argo workflow template
  `<service>-memory-hog` doesn't exist yet. Re-apply the manifests.
* **Run sits at `running` forever** — check `kubectl get workflows -n litmus`
  for stuck workflows and `kubectl get pods -n sock-shop` for crash-looping
  target pods. The harness has a `run_timeout_seconds` (30 min default) cap.
* **LLM 502 in eval phase** — Qwen at `localhost:8001/v1/chat/completions` is down or
  out of context. Lower `budget_tokens` in the failing strategy or restart
  the model.
