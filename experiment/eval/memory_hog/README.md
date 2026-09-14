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
* Local Qwen LLM is up at `localhost:8001/generate`.
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

## Troubleshooting

* **`HTTP 400` on `/api/runs/start`** — likely the Argo workflow template
  `<service>-memory-hog` doesn't exist yet. Re-apply the manifests.
* **Run sits at `running` forever** — check `kubectl get workflows -n litmus`
  for stuck workflows and `kubectl get pods -n sock-shop` for crash-looping
  target pods. The harness has a `run_timeout_seconds` (30 min default) cap.
* **LLM 502 in eval phase** — Qwen at `localhost:8001/generate` is down or
  out of context. Lower `budget_tokens` in the failing strategy or restart
  the model.
