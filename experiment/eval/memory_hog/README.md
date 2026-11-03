# Memory-hog evaluation harness

## Online Skill RCA RAG A/B: 70 runs, 35 train / 35 test

This is a new controlled profile, separate from the retrospective knowledge
evaluation below. It runs the medium 7-service x 5-fault matrix twice: one
independent execution per combination for training, then one for testing.
Existing `small`, `medium` and `large` profiles keep their original matrices.
The test measures new incidents of known scenarios, not unseen scenarios.

From the repository root, inspect the plan without writing files, querying
the cluster or injecting faults:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign --level rag
```

Check infrastructure without injection, then start the campaign only when
operationally authorized:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign --level rag --preflight
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --level rag --rag-top-k 3 --rag-min-score 0.05 --rag-max-rounds 2 --run
```

The API must be restarted with the new `/api/skill-rag/strategy` capability.
Use the existing calibrated CPU profile and controlled recovery/cleanup gates;
this profile does not authorize infrastructure repair or automatic reinjection.
The configured API role must be `chaos_engineer` or `admin`.

For each completed training run, the model generates A from the initial
metrics/logs/traces without labels. In a separate annotation context, it receives
the training service/fault labels, proposes bounded checks against current
telemetry and generates B. Labels never count as telemetry. B is eligible only
when the intervention effect is validated and its diagnosis is corroborated,
matches both labels and passes host validation. There is no mandatory human
approval. Unsupported guides remain pending and are reported, not silently
promoted to fill 35 slots.

New A records use `incident-a-v2`. The model produces only the narrative;
the host extracts structured metric facts from the initial golden-signal
digests. Each fact retains its evidence ID, source JSON pointer, service,
metric, unit, baseline/fault mean, query windows, sample counts and status.
Missing means are never converted to zero; missing coverage, units or windows
are recorded in `fact_limitations`. The links are validated against the
persisted evidence, not against the model's wording. The values retain the
precision of the source digest and do not assert a causal mechanism.

The v2 retrieval text puts metric facts first, omits redundant metric prose
already covered by those facts, normalizes Portuguese accents and whitespace,
and deduplicates observations across groups. Narrative groups remain explicit.
Evidence IDs, timestamps, preliminary hypotheses and experimental labels do
not enter the search text. Raw narrative and provenance remain in A for audit.
The frozen v1 retriever remains available under its original index name.
OpenSearch retriever v2 creates a separate index with field-aware BM25 over
services, symptoms, metric facts, relations, logs, traces, timeline and
limitations; dense retrieval and RRF remain in place. Its history projection
also carries bounded results for executed historical checks, linked to evidence
IDs and explicitly marked as historical, never evidence for the current run.
This does not reconstruct missing telemetry or modify an existing corpus/index.

Build the v2 index explicitly from the installed frozen base before preparing
an OpenSearch test campaign:

```bash
python -m experiment.eval.memory_hog.opensearch_experiments \
  --base-file experiment/platform/data/object_store/.rca_knowledge/<corpus-id>/exploratory_base.json
```

The command verifies the base checksum, creates a separate v2 index, and prints
the index metadata. Reuse the base's SHA-256 for test-only preparation; the v1
index remains untouched.

Frozen bases declare `a_text_version`; bases without it retain the byte-identical
v1 search representation. Queries use the base's version, and mixed-version
indexes are rejected. Existing corpora are not rewritten or automatically
reindexed. To use v2 retrieval, annotate and freeze a new corpus; historical
migration must explicitly rebuild A from its source evidence and rebuild the
index in a separate corpus. A changed representation alone does not establish
better recall or diagnostic accuracy.

After all training cells are terminal and completed runs have annotations,
the backend freezes a hashed base containing only eligible training pairs.
An empty eligible base blocks testing. Blocked/aborted runs remain in the
denominators and are never replaced automatically. One explicitly authorized,
reviewed training `container-kill` retry is allowed only through a versioned
protocol amendment, before base freeze and before any test run. The original
attempt remains in the ledger and is excluded from the training cohort; its
replacement must pass the unchanged run gates. The 70 planned cells and 35/35
split do not change, but this exception can increase submitted attempts to 71.
No second replacement or retry of other RAG cells is permitted.

If a completed training cohort has A/B records but no eligible guides because
the original B annotations did not explicitly preserve the known result, a
separate retroactive B revision can be generated without modifying those
records:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.retroactive_b \
  --campaign experiment/eval/memory_hog/out/controlled_campaign/<campaign-id>
```

The command reads only completed training runs, writes a new
`retro_b_v1/` audit directory and corpus ID, and retains the annotated target
as ground truth distinct from observed evidence. Existing causal/effect gates
remain unchanged; a run with unsupported evidence stays pending. It freezes
the new base only if at least one pair passes those gates. This revision does
not rewrite the saved campaign protocol or select the new corpus for test
execution; test use requires a separately reviewed protocol/configuration
decision.

To recheck failed/empty training queries against their original historical
windows without reinjecting faults:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.recheck_b \
  --campaign experiment/eval/memory_hog/out/controlled_campaign/<campaign-id>
```

Add `--annotate` to generate revised B guides after collection. This writes
immutable evidence, transport requests and annotations in
`retro_b_recheck_v2/`, separate from original A/B and `retro_b_v1/`. Literal
log filters that return nothing are retried unfiltered; corrected local
saturation helpers use MCP `prometheus_range_query`, without requiring an
MCP server restart. Target trace edges and resource comparisons are retained
in a focused model view instead of disappearing in prefix-truncated summaries.
Known labels do not count as evidence; causal/effect gates still apply.
The command neither freezes a base nor starts testing.

Use `--annotate --repair-contracts` to retry only invalid B contracts, storing
one separate repair in `contract_repairs/` without replacing earlier attempts.
When present, `detailed_trace_evidence/<cell-id>.json` supplements are checked
against the source bundle/run and their content hash before use. Their trace
views preserve HTTP status and destination fields. Contract repair must not
upgrade `inconclusive` to `confirmed` without new evidence.
Reported eligibility is an automated structural check, not causal content
approval; review the cited facts and dependency relationships before promotion.
For a narrowly scoped retry of selected failed repair records, add
`--retry-failed-contracts` and one `--cell-id <id>` per run. These attempts and
their summary are written under `contract_revalidations/`; existing v2 files
and the main summary are left unchanged.

### Exploratory RAG campaign: 35 test runs

The full exploratory campaign is isolated from the official 70-run RAG
campaign and its frozen base. It audits the latest v2 B for agreement with the
labels supplied during annotation, then installs label-consistent pairs only as
`pending`/`inconclusive` heuristics. This audit is not independent causal
validation: the labels informed B, and guides must still be checked against
current-run telemetry. In the reviewed v2 source, 32 B records matched their
service/fault labels; 24 had validated intervention effects and 8 did not.
The preparation recomputes these counts and refuses mismatched labels.

Prepare a separate 35-test campaign without injecting faults:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --source-campaign experiment/eval/memory_hog/out/controlled_campaign/<campaign-id> \
  --prepare
```

Inspect `label_audit.json` and `exploratory_manifest.json`; confirm 35 planned
test cells, the expected B audit counts, the corpus hash, and zero test runs
before execution. The source campaign and its test split must remain untouched.
Restart the backend so its RCA/strategy versions include best-guess
classification, confirm both exploratory modes and the expected version via
the strategy endpoint, then run a read-only infrastructure preflight. The
controlled runner repeats its safety gates before each injection.

#### Test-only workload using the existing OpenSearch corpus

When the reviewed exploratory corpus and OpenSearch index already exist, use
the test-only preparation path instead of rebuilding A/B or executing training.
It verifies the corpus, index metadata, document IDs, training provenance, and
the existing label audit before writing a separate one-cell canary or 35-test
campaign. Each completed run waits for historical read-back of metrics, logs,
and traces before comparing Skill with OpenSearch RAG. The read-back is online
through the historical MCP; an S3 bundle or post-run migration Job is not
required in dual-write mode.
Log read-back first checks the target container. If it has no entries and the
query succeeds, validation checks the same run window across the namespace;
the result records the target count and the scope used. Namespace logs can
satisfy the log-family read-back, but are not represented as target-container
logs. The gate remains pending if neither query returns logs.
Historical training cells must be terminal; blocked or aborted cells are
preserved in the source ledger but are not required to appear in the corpus.
Every run referenced by the reused corpus must still be completed and belong to
that historical training split.
The installed corpus was produced with `qwen-14b`; this test workload uses only
`qwen3.5-9b-grafana` for online inference. This model mismatch is allowed only
for exploratory OpenSearch RAG, must be explicitly opted into, and is recorded
alongside the source/current model IDs in the campaign manifest and diagnostics.
To make historical pairs whose intervention effect remains unvalidated eligible
for retrieval as weak analogies, pass
`--include-unvalidated-experimental` during test-only preparation. This does
not validate those effects or guarantee a pair appears in the top-k results;
the option and unvalidated-pair count are recorded in the new manifest. It does
not change standard RAG eligibility or archived campaign results.

Deploy the history-only MCP service and keep its port-forward on loopback:

```bash
make history-mcp-up
make history-mcp-forward
```

Prepare one immutable 35-test campaign using the currently installed corpus
and its frozen OpenSearch SHA-256:

```bash
LLM_MODEL=qwen3.5-9b-grafana \
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --prepare-test-only \
  --source-campaign experiment/eval/memory_hog/out/controlled_campaign/<source-id> \
  --reuse-corpus-id <existing-corpus-id> \
  --opensearch-base-sha256 <existing-index-base-sha256> \
  --allow-legacy-skill-corpus \
  --allow-legacy-model-mismatch \
  --full-test-only \
  --api-base http://127.0.0.1:8012
```

Start the platform API and persistent local OpenSearch on port 8012 with
`HISTORY_MIGRATION_ENABLED=false` and `HISTORY_ARCHIVE_ENABLED=false`; the
existing default platform port remains 8010:

```bash
make experiment-platform-up-bg PLATFORM_PORT=8012 \
  HISTORY_MIGRATION_ENABLED=false HISTORY_ARCHIVE_ENABLED=false
```

The target starts OpenSearch from `opensearch-compose.yml` and waits for its
health endpoint before starting the API. Do not forward SeaweedFS for this mode.
Keep the live MCP on port 18080 unchanged.
After preparation, enable temporary dual-write with the campaign ID printed in
the prepared directory:

```bash
./.venv/bin/python -m experiment.platform.tools.history_dual_write \
  enable --campaign-id <campaign-id>
```

Inspect the immutable manifest and preflight before running. Start the same
35-test campaign with a pause after its first test. Continue only if recovery,
cleanup, historical read-back, Skill, and OpenSearch RAG validations all pass:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --run experiment/eval/memory_hog/out/exploratory_campaigns/<campaign-id> \
  --stop-after-first-test
```

Resume that same campaign for the remaining cells, then turn dual-write off
using the same campaign ID:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --run experiment/eval/memory_hog/out/exploratory_campaigns/<campaign-id> \
  --resume

./.venv/bin/python -m experiment.platform.tools.history_dual_write \
  disable --campaign-id <campaign-id>
```

`--resume --stop-after-first-test` revalidates the first completed test cell and
pauses before submitting another test cell. New test-only configurations pin
`max_total_tokens` to 300000. A diagnostic with `status=error` blocks campaign
progress; at most two bounded diagnostic retries are persisted without
overwriting earlier attempts. `inconclusive` is valid only when all applicable
history, model, and frozen-corpus gates pass.

The toggle filters metric remote-write to `sock-shop`, fans out file logs and
sampled traces to the historical backends, and preserves the live exporters.
Neither preparation nor execution annotates, trains, or reindexes the existing
OpenSearch corpus.

If interrupted, resume the same prepared campaign with `--resume`; the
controlled runner reconciles existing attempts and does not reinject terminal
cells:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --run experiment/eval/memory_hog/out/exploratory_campaigns/<campaign-id> \
  --resume
```

Each test run persists three diagnostics before evaluation reads its labels:
original Skill RCA, exploratory control without corpus retrieval, and
exploratory RAG with paired A/B retrieval. Both exploratory methods can query
online historical observability; only RAG retrieves from the frozen corpus.
All request a known service and one allowed fault
type even if causal evidence remains inconclusive; errors are persisted rather
than replaced by guessed defaults. The report includes all 35 planned cells,
per-method classification completeness/accuracy, run outcomes, retrieved pair
IDs/scores, same-combination recall, timing, model calls, and results split by
fault, service, and intervention-effect validation. This is exploratory
classification evidence, not official validation of B or causal confirmation.

### Diagnostic-only retries after a model mismatch

Use `retry_rag_diagnostics` for completed exploratory runs whose RAG response
failed with `exploratory base hash/version/provider mismatch`. It never starts
experiments or changes the original campaign. It derives a separate base by
changing only the inference model identity, keeping guides/index unchanged and
enforcing the existing integrity, Skill and embedding-provider guards.

```bash
LLM_MODEL=qwen3.5-9b-grafana \
LLM_URL=http://127.0.0.1:8011/v1/chat/completions \
./.venv/bin/python -m experiment.eval.memory_hog.retry_rag_diagnostics \
  --campaign experiment/eval/memory_hog/out/exploratory_campaigns/<campaign-id> \
  --output experiment/eval/memory_hog/out/exploratory_campaigns/<campaign-id>/diagnostic_retries/<attempt-id>
```

The local model health endpoint must report the configured model. Results,
input hashes, source/derived base hashes and an updated comparison are written
only under the output directory. Repeating the command preserves existing
retry records. A diagnostic error is persisted and stops further retries;
use a new attempt directory to investigate it rather than deleting records.
Historical telemetry is queried again for the original windows, so retention
and configuration changes must be reported. The combined comparison mixes
historical answers and new RAG answers; it is not a new controlled paired trial.

### RCA evaluation requires online historical telemetry

Every RCA accuracy evaluation must query the run's historical Prometheus, Loki,
and Tempo datasources through MCP. Start a new `campaign_knowledge` evaluation
in `live_shared` mode for a paired comparison; its fresh collection may be
shared only among conditions and repetitions in that same evaluation. New
evaluations must collect again. Stored snapshots, evidence imports, and the
legacy `frozen_rca_replay` are prohibited for scoring.

The `frozen_rca_replay` entry point now exits with an error. Use the
[campaign knowledge protocol](./CAMPAIGN_KNOWLEDGE.md), which requires an
explicit `collect --allow-telemetry` step and never falls back to stored
telemetry. Missing or partial sources must remain visible in the report.

The v4/v5 frozen replay artifacts remain historical research records only;
they are not current online evaluations and must not be used as evidence that
the current retrieval path works.

Operational control/RAG continuation now preflights concrete checks, reports
available combinations and permits one bounded correction of an unavailable
proposal. Additional fixed, namespace/window-scoped checks cover `lifecycle`,
`cpu_throttling`, JVM `gc`, `http`, and `network`. Discovery of a tool does not
guarantee metric coverage. Saturation is not evidence of GC or thread contention;
missing termination timestamps are explicitly reported. Real tool failures and
unresolved rejected checks retain conservative finalization gates.

See the [correction plan](../../../research/planos/2026-10-31-correcao-telemetria-compactacao-rca.md)
for historical telemetry findings and the per-case follow-up matrix.

### Isolated exploratory RAG pilot

The exploratory pilot is separate from the official 70-run campaign and does
not promote pending or inconclusive B guides into the validated corpus. It uses
the reviewed, boundary-only B from `payment-http-status-code-r0` as an
inconclusive hypothesis for the single test cell
`payment-http-status-code-r1`. Its comparison is `original`,
`exploratory_control` (no retrieved history), and `exploratory_rag` (paired A/B
retrieval). Results are written to a separate campaign directory and report.

Prepare the pilot without starting a test run:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_pilot \
  --source-campaign experiment/eval/memory_hog/out/controlled_campaign/<campaign-id> \
  --prepare
```

The preparation validates the reviewed source run and creates a separate
exploratory corpus and campaign. It refuses to proceed if the source test split
already has runs. It imports only each training cell's latest state; the source
ledger and annotations remain untouched, and the complete source ledger is
hash-bound in the exploratory manifest. Historical retries are provenance,
not new pilot attempts. Before executing the pilot, deploy/restart the backend with
the exploratory modes and verify the strategy endpoint advertises
`exploratory_control` and `exploratory_rag`. Then run only the prepared
directory:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_pilot \
  --run experiment/eval/memory_hog/out/exploratory_campaigns/<pilot-id>
```

This is a known-scenario smoke test, not evidence of generalization. The
historical guide and label can suggest checks, but they are explicitly not
evidence for the current run; only current-run telemetry can support its final
diagnosis. The official corpus freeze and its eligibility checks remain
unchanged.

Each completed test run receives three diagnostic attempts against its fixed
windows, using live Prometheus/Loki/Tempo data:

- `original`: unchanged Skill RCA.
- `control`: initial Skill, model-generated A, bounded checks without history.
- `rag`: same extended flow, retrieving A and loading only its paired B.

The method order rotates by test cell. These are up to 105 test RCA attempts,
not 105 additional chaos runs. Persisted answers are scored separately after
collection; current test labels are not included in diagnostic requests.
Control and RAG have separate model state and identical limits.
RAG model prompts retain the current stage and static RCA references rather
than duplicating earlier stage payloads. Embedded evidence is capped at
24,000 characters; complete evidence, query arguments and digests remain in
persisted records for audit and evaluation. Long summaries are explicitly
marked as truncated.
Checks that cite unavailable historical pairs or repeat an executed
verification are rejected and recorded; the diagnosis is marked inconclusive
rather than aborting the campaign.

The initial Skill still makes its two model calls; A uses a separate third
call to preserve the original executor. Each extra-check decision adds one
call, then B/final diagnosis adds one more. With two check rounds this is
six calls per extended attempt, before repairs. There is a total call/token/
time budget and a reserved final-call allowance.

Generated files inside the campaign directory:

- `config.yaml`, `plan.json`, `ledger.jsonl`, `runs.csv`: immutable setup and
  run lifecycle, with explicit train/test roles and unique repetition IDs.
- `rag_protocol.json`: configuration fingerprint checked on resume.
- `rag_reviewed_retry_protocol.json`: one-time, hash-bound amendment when the
  reviewed training-retry exception is explicitly authorized.
- `rag_results/<cell_id>/annotate.json`: training A/B result.
- `rag_results/<cell_id>/{original,control,rag}.json`: persisted test attempts.
- `rag_base.json`: frozen-base acknowledgement and training IDs.
- `rag_evaluation.json`: planned/completed denominators, per-run results,
  paired gains/losses, service/fault/joint accuracy, evidence checks, recall@k,
  costs, breakdowns by service/fault/intervention validation, and separate
  counts for planned cells and submitted attempts.

The authoritative base, embeddings, initial A records and training evidence
live under the platform object store's `.rca_knowledge/<corpus_id>/`.
The existing embedding-provider settings (`RAG_EMBEDDING_PROVIDER`,
`RAG_EMBEDDING_MODEL`, `RAG_EMBEDDING_DIM`) are reused; changing provider,
model or Skill hash invalidates compatibility with a frozen base. The legacy
run-summary index is not used. Retrieval settings and investigation limits
are pinned in `knowledge_campaign` in the saved configuration. CLI overrides
apply only when creating a campaign, never during resume.

Resume without duplicating injections or successful diagnostic attempts:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<campaign-id> --run
```

Annotation failures stop the supervisor and can be retried on the same
completed run through resume. Training A is retained. Failed test attempts
are persisted and scored as failures rather than silently retried. Explicit
injection retries are not allowed by the fixed protocol. The sole exception is
the explicitly reviewed training `container-kill` retry described above:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<campaign-id> \
  --run --retry-reviewed-container-kill orders-container-kill-r0
```

This command requires a prior aborted first attempt with one confirmed restart,
failed recovery, successful cleanup, stopped load and healthy application.
It preserves that attempt, records the protocol amendment, reruns the current
environment preflight and applies every original gate to attempt 2. If the
retry fails, the campaign stops; no further retry is allowed. Do not use the
flag after the training base has been frozen or testing has begun.

Rebuild only the aggregate report for an evaluation that already collected its
evidence online; this command does not start a new evaluation or replace the
required MCP collection:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.rag_campaign \
  --campaign experiment/eval/memory_hog/out/controlled_campaign/<campaign-id>
```

Structural evidence checks and label accuracy do not prove causal correctness.
The report explicitly distinguishes unsupported/incorrect confirmations.
Read the [strategy and protocol](../../../research/2026-10-26-estrategia-rag-rca-conjuntos-a-b.md)
for methodological limitations.

## Isolated knowledge evaluation for the 35-run campaign

The [campaign knowledge evaluator](./CAMPAIGN_KNOWLEDGE.md) compares a
retrospective skill without semantic context, with a system card, and with
reviewed experiences from the same workflow. It uses a separate corpus,
a 17/18 planned support/test split. The recommended `live_shared` mode queries
historical backends via MCP once per case, seals the fresh collection within
that evaluation, and reuses identical evidence across conditions/repetitions.
Every new evaluation must collect again; offline snapshots and evidence imports
are rejected. Linked parent and
continuation datasets preserve segment provenance; the 32 completed runs
currently yield 14 support and 18 test cases. Reference schemas, bounded audited
repairs and per-case failures do not replace human review before publication.
It refuses inference/collection while the
campaign is running and does not modify the operational database or RAG.

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

2. Apply only the Litmus workflow templates. The generated HTTP
   `VirtualService` files are live interventions; the runner applies and
   removes them during each HTTP run:

   ```bash
   find experiment/eval/memory_hog/out/manifests -maxdepth 1 -type f \
     ! -name '*-http-status-code.yaml' -exec kubectl apply -f {} \;
   ```

   Each workflow apply triggers one run; the workflow name persists afterwards.

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
`http-status-code` permanece fora da matriz desta campanha para `front-end`.
Bancos de dados, RabbitMQ, `io-stress` e `dns-error` ficam fora da matriz.

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

Para executar a matriz longa sem supervisao interativa, use o orquestrador com
ledger, retomada, piloto obrigatorio, health checks dos port-forwards, gate de
recuperacao e relatorio: [`AUTONOMOUS_CAMPAIGN.md`](./AUTONOMOUS_CAMPAIGN.md).
O comando legado acima continua disponivel para os fluxos existentes, mas nao
oferece o mesmo contrato fail-closed de retomada.

O gate de efeito registra separadamente `signal_observed`,
`mechanism_signal_observed`, `impact_observed` e `result_status`; `passed` significa
que a intervencao e um sinal-alvo passaram seus gates, nao que a causa esteja
provada. Restart/readiness isolados ficam como sintomas; restart nao aprova
`memory-hog`, nem 5xx generico aprova `network-loss`. HTTP usa 5xx do reporter
cliente na aresta origem->destino e guarda o codigo; resposta agregada do
destino nao basta. Contadores de drops seguem ambiguos ate a regra/direcao
`tc/netem` ser validada. Runs antigos sem a nova serie HTTP nao sao
reinterpretados como se a tivessem.

## Reavaliar uma campanha existente com a Skill RCA

Para avaliar a Skill RCA sem injetar falhas, execute o avaliador sobre uma
campanha já concluída. Se existir `rca_evaluation_retry.json`, ele seleciona
os mesmos runs completos desse relatório; caso contrário, deriva a coorte dos
runs `test` completos em `runs.csv`. Em ambos os casos chama
`POST /api/runs/{run_id}/skill-rca` e grava um relatório separado
`rca_skill_evaluation.json`:

```bash
.venv/bin/python -m experiment.eval.memory_hog.evaluate_skill_rca \
  --api-base http://127.0.0.1:8010
```

A plataforma deve estar reiniciada/recarregada com a rota Skill RCA. O comando
não executa caos. Para outra campanha, informe `--campaign-dir`; se quiser
reusar uma coorte histórica específica, indique seu relatório com `--baseline`.
O avaliador recusa sobrescrever um relatório já existente. Cada tentativa
salva seu artefato próprio na plataforma e o progresso é persistido após cada run.
O avaliador lê os `chaos_type` possíveis de `test_experiments` e envia a mesma
lista como `possible_fault_types`, sem revelar ao modelo o tipo ou o alvo do
run individual. Para reavaliações parciais de uma campanha maior, `config.yaml`
pode declarar `possible_fault_types` com o conjunto completo de opções; o
avaliador valida que ele cobre todos os tipos presentes na matriz. No benchmark
de cinco causas descrito, a lista esperada é
`container-kill`, `cpu-hog`, `memory-hog`, `network-loss` e
`http-status-code`. A resposta precisa sempre escolher um deles, mesmo quando
inconclusiva. A avaliação pontua somente localização do serviço e `fault_type`
(correspondência exata com `chaos_type`); o campo `category` continua no
diagnóstico RCA, mas não entra no placar. A campanha controlada mais recente
usa `network-latency` no lugar de `network-loss`; para ela, a matriz configura
`possible_fault_types` com `network-latency`, evitando avaliar um rótulo
diferente do caos aplicado.

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

O avaliador multiagente é um fluxo separado do runner e do CSV baseline. Ele
chama `POST /api/runs/{id}/multiagent-rca` apenas para os runs concluídos de uma
campanha existente, preservando as duas avaliações. Antes de iniciar, verifica
que a API carregou as versões esperadas de estratégia e prompt; respostas com
versão divergente não são pontuadas como válidas. Execute baseline e variante
nos mesmos runs e use a comparação pareada por `run_id` para separar ganhos e
regressões.

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

### Workflow controlado (substitui o antigo piloto de cinco runs)

```bash
make experiment-multiagent-rca-evaluation                         # somente plano
make experiment-multiagent-rca-evaluation EXPERIMENT_LEVEL=medium  # somente plano
make experiment-multiagent-rca-evaluation EXPERIMENT_LEVEL=large   # somente plano
```

O modo padrao nao cria arquivos, inicia processos nem consulta API/cluster.
O alvo Make nao restaura port-forwards nem cria templates automaticamente.
A plataforma precisa ser reiniciada pelo operador apos a atualizacao do
codigo para carregar o novo modo do engine. O servidor MCP tambem precisa
carregar a nova tool `prometheus_scrape_targets`; metadados e consultas usam
o mesmo cliente/backend Prometheus, sem presumir equivalencia de URLs.
As campanhas antigas permanecem
intactas; o runner generico e outros workflows mantem sua cronologia legada.

Servicos permitidos: `front-end`, `catalogue`, `carts`, `orders`, `payment`,
`shipping`, `user`. Bancos, RabbitMQ e queue-master nao sao alvos.

| Nivel | Matriz planejada |
|---|---|
| `small` | 5 runs: CPU/payment, memoria/catalogue, latencia/shipping, kill/user, HTTP 500/orders |
| `medium` | 7 servicos x 5 falhas = 35 runs |
| `large` | 35 combinacoes x 5 repeticoes = 175 runs |

#### Resiliencia sem relaxar os gates (protocolo 4)

Novas campanhas persistem `controlled_campaign.resilience` na configuracao:
recuperacao operacional passiva por ate 600s, estabilidade por 120s, limite de
duas recuperacoes consecutivas ou cinco no total e tres retries de consulta
para erros transitorios (conexao, timeout e HTTP 502/503/504). Os retries usam
o mesmo run, com backoff de 5/10/20s; nao repetem submissao nem injecao.
Uma falha `blocked` antes da submissao pode receber uma unica nova tentativa,
somente se a limpeza e a parada da carga estiverem confirmadas. A politica
`pre_intervention_retry` e gravada na configuracao antes da campanha; a nova
tentativa espera 60s, cria outro run/attempt e usa warmup e baseline novos, sem
alterar seed, intensidade ou thresholds. O bloqueio original permanece no
ledger e nos artefatos. Se a politica ou os gates de limpeza faltarem, nao ha
retry automatico; campanhas antigas nao ganham essa autorizacao ao serem
retomadas.

Ao expirar a recuperacao experimental, o run permanece `aborted` e
`recovery.passed=false`. A plataforma pode observar recuperacao operacional
adicional **sob a mesma carga e usando a mesma baseline e os mesmos limites**.
A contagem de estabilidade reinicia quando um check falha ou o intervalo entre
checks excede 60s. Nao ha restart automatico de workloads, troca de intensidade,
remocao de gates nem extensao ilimitada. Os dados operacionais ficam nos
artefatos de checks e no gate `operational_recovery`; a janela experimental
parcial termina em `experimental_recovery_expired`, sem incluir a espera extra.

A campanha so avanca de um aborto por timeout de recuperacao se a intervencao,
o mecanismo, a recuperacao operacional, a limpeza e a parada da carga forem
confirmados. Outros abortos continuam exigindo revisao. Cada proximo run executa
novo warmup e baseline; nenhum aborto vira sucesso. Os eventos de continuidade
e retries ficam em `campaign_events.jsonl`, e o ledger preserva os resultados.
Durante o polling, esse arquivo tambem recebe um `run_heartbeat` a cada 60s
(aproximadamente, conforme a latencia da API), com ID e estado do run.
O circuit breaker tambem e aplicado na retomada. A campanha retorna codigo 1 se
restarem abortos, mesmo quando todas as celulas foram processadas.

Reinicie a API da plataforma para carregar o protocolo 4 antes de iniciar uma
nova campanha; o preflight rejeita uma API com protocolo diferente. O comando
Make e a matriz de 5/35/175 celulas permanecem os mesmos. Campanhas antigas sem
a politica persistida continuam fail-closed: nao edite a configuracao historica
para habilitar recuperacao retroativa.

HTTP 500 e a quinta falha opcional aprovada. Houve sinais HTTP nos logs do
front-end, mas o gate da campanha anterior nao confirmou o efeito na aresta
para orders. Por isso **nao e considerado um mecanismo previamente calibrado**:
exige trafego positivo e baseline de erros na aresta-alvo, e registra o efeito
observado separadamente. Para chamadas entre workloads, usa `reporter=source`;
para `front-end`, cujo caller e o gerador externo a mesh, usa `reporter=destination`
para medir a entrada no servico. A taxa so e zero quando ha trafego positivo e
nenhuma serie 5xx; a serie 5xx bruta
permanece `no_data`. Sem denominador positivo, a taxa continua sem dados e
bloqueia. O mecanismo usa percentual de 5xx na aresta,
nao um limiar arbitrario de RPS (elevacao minima de 1 ponto percentual).
Usa VirtualService Istio isolado e verifica remocao,
nao o proxy Litmus contornado pelo mesh. Conflitos
com VirtualServices existentes bloqueiam a combinacao; nao sobrescreve rotas.
O readback do VirtualService confirma a configuracao submetida no alvo, mas
nao prova propagacao no Envoy. Essa confirmacao independe do resultado HTTP:
5xx/latencia observados ficam registrados separadamente como impacto. Sem
volume de requisicoes ou telemetria valida, o impacto permanece sem dados.

O VirtualService inclui primeiro uma rota sem fault para os caminhos das probes
(`/health` nos servicos HTTP e `/` no front-end); somente as demais rotas
recebem HTTP 500. O gerador valida essa exclusao, e a execucao falha se o
manifesto estiver ausente ou desatualizado. O destino do front-end usa a porta
80 do Service, que encaminha para a porta 8079 do container.

| Fase do modo controlado | Duracao/politica |
|---|---|
| Aquecimento | >=60 s sob carga, seguido de gate de estabilizacao |
| Baseline analisado | 90 s **apos** estabilizar; nao inclui ramp-up |
| CPU / memoria | 180 s configurados no injetor; aguarda workflow terminal |
| Latencia | +200 ms candidatos por 120 s, configuravel por servico |
| Container-kill | Um evento +120 s de observacao; bloqueado enquanto nao houver mecanismo verificado de evento unico |
| Pos-retirada | >=120 s sob carga; janela movel de 60 s para confirmar recuperacao |
| Cooldown | 30 s somente apos recuperacao confirmada |

Se a execucao falhar durante a recuperacao, a janela parcial inclui `post`
desde `post_observation_started` ate o fim da captura, quando esse marcador
existe. Assim, a RCA consegue consultar a degradacao observada apos o restart
sem reescrever os limites baseline/fault.

**O engine legado executa baseline -> warmup -> fault -> post.** Este workflow
opta explicitamente por `controlled_campaign.enabled` e usa outro lifecycle:
warmup/estabilidade -> baseline -> submissao/workflow -> pos-retirada/
recuperacao -> cooldown. Nao basta trocar nomes/duracoes das fases legadas.
A coleta MCP de metricas usa timeout de 30 s por chamada, registrado em
`controlled_campaign.metrics_mcp_timeout_seconds` e em cada `spec.json`;
falhas de coleta continuam explicitas e nao sao aceitas como cobertura valida.

Inicio/fim do workflow Argo, submissao, estimativa da intervencao, primeira
regra netem observada, janela de coleta, inicio amostrado da degradacao e
recuperacao observada sao campos distintos. Sem marcadores efetivos de
injecao, o inicio estimado usa criacao do ChaosResult e o fim soma a duracao
configurada: `duration_verified=false`, com limitacao explicita. Overhead de
workflow nao e somado aos 180/120 s como se fosse intervencao ativa comprovada.
No HTTP, a vida do objeto na API tambem nao comprova o instante de propagacao
no Envoy. Um gate positivo de mecanismo nao torna esses timestamps exatos.
O relatorio tambem guarda timestamps nominais planejados ao iniciar o aquecimento,
com origem e aviso de que gates e overhead podem estender o calendario.

Carga inicial: **30 usuarios**, spawn de **5 usuarios/s**, sem RPS presumido.
O gate exige CPU, memoria, erros, latencia, trafego no alvo e trafego em
orders/payment/shipping para checkout. Sem cobertura, bloqueia a combinacao
com motivo. Estabilidade inicial considera readiness/saude do no, erros
<=1% e variabilidade das metricas na janela de 30 s. Coleta solicitada a
cada 5 s **nao aumenta** scrape/exportacao. Registra metadados reais dos
targets Prometheus, idade da amostra e lookback de 30 s. Idade de amostra nao
e uma medicao isolada do atraso de transporte/ingestao.
CPU, quota, throttling, memoria e OOM sao medidos separadamente por amostragem
de cgroup v2 dentro do container de aplicacao selecionado, usando `kubectl exec`
com o nome do servico, e timestamps e intervalos realmente observados. As series de
recursos cAdvisor agregadas por pod nao sao usadas como substitutas das series
por container. A idade Prometheus usa a serie do pod somente como verificacao
de frescor, nao como medida de recursos da aplicacao. Se cgroup v2, limites,
contadores ou cobertura suficientes faltarem, a combinacao bloqueia.

#### Calibracao e bloqueios de seguranca

- CPU: o codigo revisado do Litmus 3.28.0 passa `CPU_CORES`/`CPU_LOAD` para
  `stress-ng --cpu N --cpu-load P`. P e duty cycle por worker, nao percentual
  da quota do container. Exige configuracao explicita por servico e mede a
  fracao do limite real de CPU no container-alvo. O valor `target_quota_fraction`
  e validado como atingido por pelo menos 60 s, admitindo a tolerancia
  configurada. Baseline ja >=70% bloqueia e pede recalibracao. Consumo/quota
  e throttling sao registrados separadamente; throttling/5xx nao sao
  obrigatorios.
- Memoria: `max(0, .80*limite - p95_cgroup_memory.current_baseline - .05*limite)`,
  arredondado para baixo em MiB, um worker, minimo inicial de 16 MiB.
  O sufixo `M` do parser stress-ng corresponde a 2^20 bytes; um worker evita
  ambiguidade entre orcamento por worker e total.
  `memory.current` e especifico do cgroup do container e pode incluir cache
  reclaimable; as leituras tambem incluem pequeno overhead dos processos
  transitorios de coleta. A politica nao garante ausencia de OOM. Uso ja
  >=85% do limite bloqueia. Monitora tambem usage bytes,
  identidade, reinicios, OOM e saude do no. Em >=90% do limite ou perda de
  telemetria, solicita `engineState=stop` para abort/revert do Litmus e
  interrompe a campanha. Scrapes, consultas e abort podem atrasar: **nao
  promete prevenir OOM**. Restart/OOM inesperado e desvio, nao um caso
  bem-sucedido de pressao sem OOM; nao aumenta intensidade nem repete.
  Exige contador OOM observado: compara o contador absoluto com o baseline
  para detectar inclusive morte de worker sem restart da aplicacao. Repete
  a verificacao ao final da recuperacao/cooldown; ausencia desse contador
  bloqueia, em vez de ser interpretada como zero OOM.
- Rede: exige `sch_netem` confirmado no no do alvo, `tc -j` acessivel no
  container de aplicacao ou no sidecar Istio pronto do mesmo pod, e qdisc sem
  netem preexistente. Registra qual container leu o qdisc; ambos compartilham
  o namespace de rede do pod. Registra interface, direcao
  egress e a regra real, alem de verificar remocao. O namespace de rede e
  compartilhado com o sidecar: o mecanismo afeta a interface do pod,
  **nao apenas uma dependencia**. Mede efeito, degradacao e recuperacao pelo
  P95 `reporter="source"` da aresta que chega ao alvo; o reporter de destino
  pode nao observar atraso aplicado no egress. Sem trafego/latencia da aresta,
  bloqueia antes da injecao.
- Container-kill: nao usa o helper Litmus 3.28.0, que repete por
  duracao/intervalo. A campanha valida UID do pod, identidade/estado do
  container da aplicacao e contador de reinicios; exige `containerID` Docker
  canonico e `docker inspect` com labels de namespace, pod e container que
  coincidam exatamente. Envia um unico `docker stop --signal=9 --time=0` para
  esse ID, sem periodo de graca. Runtimes sem suporte a essa identificacao sao
  bloqueados antes da injecao, sem fallback para PID 1, sidecar ou pod-delete.
  Confirma o evento pela troca de `containerID` e pelo incremento unitario
  do contador de reinicios; a prontidao e registrada separadamente. A janela
  de observacao de pelo menos 120 s comeca apos a confirmacao do restart,
  continua registrando indisponibilidade e nao apaga o evento confirmado.
  Falha de prontidao impede a recuperacao e bloqueia o proximo alvo; o sinal
  nunca e repetido automaticamente. Restarts adicionais sao registrados como
  desvio e interrompem a campanha antes de outro alvo. A amostragem cgroup-v2
  cobre baseline e intervenção, pausa durante o restart e, no protocolo 5,
  retoma ao observar o novo container Running, mesmo antes de readiness.
  Intervalos que atravessam troca de identidade ou reset dos contadores
  não são usados para taxa de CPU.
  Leituras cgroup com timeout têm uma única repetição registrada; falha
  persistente bloqueia o run sem presumir ausência de telemetria. Registra
  estado/identidade e `lastState.terminated`, mas
  codigo 137 isolado nao e classificado como OOM.
- Templates Litmus devem existir; nunca aplica templates para "prime", pois
  isso criaria injecoes adicionais. Verifica a imagem do runner revisada e
  submete o snapshot inspecionado, preservando imagens/status dos helpers.

O arquivo [cpu-calibration.yaml](./cpu-calibration.yaml) configura todos os
servicos com um worker e duty cycle de 100%, como **candidato inicial** para
atingir 100% da quota do container. O runner valida a quota atual, exige
baseline abaixo de 70% e registra se a intervencao realmente sustentou
95-105% da quota por pelo menos 60 s. Isso e uma verificacao em cada run,
nao evidencia pre-existente nem garantia de sucesso. Se nao atingir a faixa,
registra falha de atingimento e nao aumenta automaticamente a intensidade.

Esse arquivo agora e o padrao do CLI e do alvo Make; `--calibration PATH`
substitui o candidato explicitamente. Retomadas sempre leem a configuracao
imutavel salva, sem incorporar o novo padrao.

Antes de uma nova submissao, o executor verifica a matriz inteira: saude dos
nos e deployments (incluindo dependencias), identidade/readiness e limites
cgroup dos containers, perfis CPU, templates/imagens/alvos Litmus, netem/qdisc,
conflitos de VirtualService, isolamento da carga headless e da interface web do
Locust, disponibilidade de Prometheus/Loki/Tempo e do LLM. Confere tambem
readiness real via API proxy Kubernetes (incluindo collector), sem depender
de port-forwards nem confundir busca de traces com prontidao de ingestao.
Repete antes de **cada** nova submissao e salva em `preflight.jsonl`.
Se esse historico ja existir, mudancas de imagem, recursos ou `JAVA_OPTS`
impedem novas submissoes na retomada; exigem outra campanha. Campanhas antigas
sem esse snapshot nao ganham uma identidade ambiental retroativa.
Reconciliar um run ja existente nao exige esse preflight: observar/limpar uma
execucao anterior nao pode depender da saude necessaria para iniciar outra.

Para inspecionar o ambiente **sem criar campanha, gerar carga ou injetar**:

```bash
make experiment-multiagent-rca-evaluation EXPERIMENT_LEVEL=medium RUN_ARGS="--preflight"
```

Preflight aprovado **nao certifica** baseline com 30 usuarios, startup apos
SIGKILL, recuperacao nem cobertura de traces na janela da falha. Esses gates
continuam obrigatorios durante cada run. O aquecimento registra desvio padrao,
limite e resultado por metrica/janela; o bloqueio CPU informa P95, quota e
fracao. Nenhum threshold, prazo de fase, intensidade ou numero de tentativas
foi relaxado. A saida zero agora exige todas as celulas planejadas `completed`;
`blocked` continua registrado e nao impede a avaliacao dos resultados elegiveis,
mas nao e apresentado como campanha completa.

O protocolo **5** exige backend atualizado. Novas campanhas persistem um
orcamento externo de carga/polling derivado dos prazos das fases mais margem
de coleta/controller (900 s) e cleanup/polling (300 s). Com os parametros
padrao, a carga tem teto de 3135 s e o polling 3435 s; o executor para a carga
antes disso ao terminar. Os gates continuam em 300 s experimental, 600 s
operacional e 120 s estaveis, sem extensao. Campanhas anteriores sem orcamento
persistido mantem 1800 s, sem mudanca retroativa.

Depois de um kill confirmado, o cgroup retoma quando o container correto
estiver **Running**, mesmo ainda nao **Ready**. Readiness/recuperacao continuam
independentes. Registra ID por amostra e exclui taxas que atravessem troca
de identidade, mesmo se os contadores do novo container forem maiores.
Falha de leitura/identidade permanece explicita e nao vira consumo zero.

No polling Litmus de `memory-hog`, o controlador coleta uma amostra cgroup
antes de definir o fim da janela recente de 15 s do gate. A submissao e as
consultas de status podem levar mais de 15 s; sem essa leitura, a primeira
verificacao podia ficar sem amostras mesmo com memoria visivel no Grafana.
Falhas nessa leitura continuam abortando a intervencao: nao ha fallback para
amostras antigas, relaxamento do cutoff de 90% ou substituicao por dados
agregados do painel.

O ensaio `--level rag --load-check` nao recebe identificacao de treino/teste
nem registra conhecimento A/B. Ele usa a mesma configuracao operacional, sem
alterar a configuracao original ou exigir uma fase de anotacao.

Antes de campanhas no WSL, verifique tambem a sincronizacao do host Windows
e do Linux. Um servico NTP ativo ou `NTPSynchronized=yes` isoladamente nao
exclui saltos de relogio. Compare UTC com tempo monotonico por varios ciclos
de polling e confira o offset NTP. Descontinuidades podem distorcer as taxas
cgroup calculadas por timestamps UTC e a selecao das janelas recentes.
Preserve os artefatos afetados e investigue-os; nao interprete picos
fisicamente implausiveis como consumo real. Uma correcao do relogio deve
ocorrer sem carga/injecao concorrente e ser seguida de novo ensaio sem falha.
Ela nao aprova retroativamente recovery ou cleanup de runs antigos.

#### Prontidao de orders e continuidade historica

O deployment `orders` dos manifestos simples e do demo completo usa uma
readiness HTTP em `/health`, a cada 3 s e com timeout de 2 s. Sem ela, um
container Running podia ser considerado Ready durante o startup da JVM.
A probe nao reinicia o container, nao certifica estabilidade de recursos e
nao substitui os gates de recuperacao. O chart Helm ja possui readiness HTTP.
Alterar probes durante uma campanha muda o ambiente experimental. A retomada
historica que modificou a readiness de orders foi registrada em segmentos
em `campaign_events.jsonl`; preserve seus abortos e tentativas e nao reporte
essa matriz como ambientalmente homogenea. No protocolo 6, as probes tambem
integram o perfil imutavel: mudancas ou ausencia desse baseline nas campanhas
antigas bloqueiam novas submissoes. Nao edite preflights antigos para contornar
essa verificacao; inicie uma nova campanha.

#### Probes de catalogue e limpeza auditavel (protocolo 6)

Os manifestos simples, demo completo e chart Helm de `catalogue` separam:

- `startupProbe`: TCP em `127.0.0.1` por exec no container, a cada 3 s,
  com ate 60 falhas antes de declarar startup malsucedido.
- `livenessProbe`: a mesma verificacao local de porta, a cada 10 s e com
  tres falhas consecutivas. Usa `nc -z -w 2`, disponivel na imagem atual,
  e timeout de exec de 3 s. Nao depende da rede do pod, banco ou reescrita
  HTTP do Istio. Porta aberta nao comprova ausencia de deadlock da aplicacao.
- `readinessProbe`: HTTP `/health`, a cada 3 s, timeout 2 s e uma falha para
  retirar o pod dos endpoints. Nao tem espera fixa de 180 s; so comeca apos
  startup aprovado. Mantem o contrato HTTP existente do endpoint, sem inferir
  saude por conteudo JSON.

Uma dependencia lenta pode retirar readiness sem causar restart por liveness.
Os limites experimentais de CPU, memoria e recuperacao nao mudaram.

O plugin Litmus agora espera o estado terminal do **workflow Argo**, mesmo se
o ChaosResult ja estiver Completed/Pass. A espera continua limitada a 120 s;
timeout fica explicito, nao vira sucesso. Antes da exclusao, preserva o ultimo
workflow observado e o historico de estados no artefato `chaos_artifacts`.
O supervisor continua exigindo sucesso/Pass, identidade dos helpers, ausencia
de helpers ativos ou de estado desconhecido e ausencia de netem. Registra
helpers e qdisc mesmo quando o estado final impede confirmar limpeza.
Excluir o workflow nao prova reversao.

Falhas de leitura cgroup preservam o erro original, snapshot de identidade,
restart/ultima terminacao e eventos por UID do pod, ou erros explicitos de
captura. Nao recebem amostras zero nem autorizam continuar apos um restart
inesperado.

Reinicie a API para carregar o protocolo 6 antes de uma nova campanha; o CLI
rejeita backend antigo. Aplicar as probes no cluster exige rollout separado,
sem carga/injecao concorrente. Um ensaio sem falha nao certifica recuperacao
sob netem; valide esse comportamento em campanha nova, sem apagar o aborto
anterior.

#### Continuacao em novo ambiente, sem apagar resultados anteriores

Quando uma correcao de probes impede retomar o perfil imutavel original,
`--continue-from` cria um novo segmento com ID e preflight proprios.
Ele preserva carga, politica, intensidades e seed salvas no pai; exclui
somente as combinacoes cujo ultimo resultado era `completed`. Bloqueios,
abortos e combinacoes nao executadas podem ser tentados no novo segmento,
mas nunca viram sucesso no historico do pai.

```bash
.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --continue-from experiment/eval/memory_hog/out/controlled_campaign/controlled-20261021T145517920531Z \
  --validation-cell catalogue-network-latency-r0
# Adicione --run somente para autorizar carga e injecao.
```

Sem `--run`, imprime o plano sem criar arquivos ou consultar API/cluster.
O pai deve estar sem lock exclusivo e com todas as entradas existentes em
estado terminal. A nova configuracao guarda caminho/ID do pai, hashes de
configuracao/plano/ledger, resultados anteriores e contagem herdada de
conclusoes. Os runs antigos nao sao copiados para o novo ledger ou avaliacao.

A celula de validacao passa a ser a primeira. A continuacao so autoriza
outras celulas se ela terminar `completed`, com submissao, intervencao,
mecanismo, recuperacao, limpeza e parada da carga comprovados. Bloqueio de
warmup tambem interrompe, sem executar as demais. A retomada do novo segmento
usa `--resume` normalmente e repete esse gate, sem dispensar validacao.
Analise os ambientes separadamente: completar a cobertura de combinacoes
entre segmentos nao produz uma matriz ambientalmente homogenea.

#### Executar ensaio sem falhas

```bash
make experiment-multiagent-rca-evaluation EXPERIMENT_LEVEL=medium RUN_ARGS="--load-check"
```

Gera uma unica carga com 30 usuarios e spawn 5/s; observa os sete servicos
com os mesmos gates de cobertura/variabilidade, 60 s minimos de aquecimento,
ate nove verificacoes, baseline de 90 s, folga CPU e memoria. Uma thread
periodica por servico mantem a coleta cgroup durante consultas MCP, com
snapshots sincronizados; espera/encerra as threads antes da limpeza. Exige tambem busca
de traces por servico restrita a baseline atual. Falha de consulta ou ausencia
de traces nao e convertida em cobertura positiva.

Registra em `out/load_checks/load-check-<UTC>/`: `report.json`, preflight,
metricas e consultas de traces, separados do corpus da campanha. Baseline
coletada apos aquecimento instavel e marcada inelegivel; serve somente para
diagnostico. O ensaio nao chama injecao nem `ControlledRun.run()`, nao e uma
celula experimental concluida e **nao certifica recuperacao apos falha**.
O handle remoto e persistido antes da coleta e `finally` confirma parada da
carga inclusive em interrupcao; SIGKILL/perda do processo ainda exigem
reconciliacao manual pelo handle persistido e TTL da carga.

#### Capacidade candidata para revisao (nao aplicada automaticamente)

A campanha `controlled-20261021T091929101290Z` teve baseline CPU P95 de
0.3729/0.5 cores em front-end e 0.2759/0.3 em carts (74.6% e 92.0%).
Shipping iniciou em cerca de 400 s com Java 8u111, OTel e quota de 300m.
O JVM enxerga 16 CPUs; excesso de paralelismo/throttling e uma hipotese,
nao uma causa comprovada. Nao houve amostragem cgroup durante o startup,
pois ela pausa ate readiness apos o kill.

Os patches em [experiment-capacity](../../../deploy/kubernetes/experiment-capacity/)
preparam CPU limite de 1 core para esses tres containers e requests maiores,
sem mudar memoria, replicas, imagens, carga ou sidecars. Nos dois JVMs,
limitam threads de GC/JIT; flags verificadas no Java 8u111 do ambiente.
O patch Tempo adiciona readiness real: um pod `Running` sem probe chegou a
responder HTTP 503 em `/ready`, depois voltou a `ready`.
Nada disso e aplicado pelo harness. Sob autorizacao operacional separada:

```bash
# Acrescente --dry-run=server para revisar sem alterar o cluster.
kubectl -n sock-shop patch deployment front-end --type=strategic --patch-file deploy/kubernetes/experiment-capacity/front-end-patch.yaml
kubectl -n sock-shop patch deployment carts --type=strategic --patch-file deploy/kubernetes/experiment-capacity/carts-patch.yaml
kubectl -n sock-shop patch deployment shipping --type=strategic --patch-file deploy/kubernetes/experiment-capacity/shipping-patch.yaml
kubectl -n monitoring patch deployment tempo --type=strategic --patch-file deploy/kubernetes/experiment-capacity/tempo-readiness-patch.yaml
```

Os patches provocam rollout quando aplicados. Espere todos os rollouts,
restabeleca port-forwards explicitamente se necessario e execute o preflight.
Meça novamente estabilidade/baseline a 30 usuarios e recuperacao/startup
antes de aceitar a capacidade como validada. Uma quota de 1 core e candidata,
nao garantia de atingimento sustentado em no compartilhado. Se o gate falhar,
investigue e pare; nao repita injecoes automaticamente.
Inicie **nova** campanha apos mudar capacidade/JVM; nao retome plano antigo
sob condicoes diferentes nem misture seus agregados. Preserve os artefatos
anteriores, incluindo bloqueados/abortados e ausencia de traces.
A Metrics API ausente afeta `kubectl top` e HPA do Istio, mas nao e fonte dos
gates cgroup/Prometheus; instalar metrics-server requer revisao separada.

```yaml
policy:
  sustained_ratio: 1.25
  sustained_seconds: 60
  minimum_cpu_delta_cores: 0.05
payment:
  cpu:
    cores: 1
    load: 20
    target_quota_fraction: 0.80
    evidence: "" # vazio bloqueia; preencher com referencia da calibracao real
shipping:
  interface: eth0
  latency_ms: 200
```

O gate de elevacao sustentada exige, por padrao, >=25% acima do baseline e,
para CPU, tambem >=0.05 core adicional, durante >=60 s sem gaps >20 s,
descartando os primeiros 30 s de lookback. Memoria preserva bytes e CPU
cores/quota, numero de amostras e duracao do aumento. Ausencia de amostra
nao vira zero; ausencia de mecanismo mensuravel pede calibracao, nao
escalonamento automatico. Intervencao submetida, confirmada no alvo,
mecanismo, impacto, cobertura, recuperacao e limpeza sao gates separados.
Um servico pode resistir sem erros HTTP ou perda de disponibilidade.

#### Execucao, retomada e evidencias

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --level small --seed 20261018 --calibration ./calibracao.yaml --run
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<id> --run
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<id> --run \
  --retry-blocked payment-cpu-hog-r0
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<id> --run \
  --retry-unconfirmed-container-kill user-container-kill-r0
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<id> --run \
  --retry-invalid-window catalogue-http-status-code-r0
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<id> --run \
  --retry-reviewed-container-kill carts-container-kill-r0
```

Substitua `./calibracao.yaml` pelo caminho real do arquivo; um caminho de
exemplo nao e criado automaticamente. Sem evidencias reais de calibracao CPU,
a combinacao CPU e bloqueada pelo gate. Arquivo ausente ou YAML invalido e
reportado como erro de argumento antes de criar diretorio ou iniciar runs.
`--retry-blocked CELL_ID` e opcional e exige `--resume`; aceita somente uma
celula cujo ledger confirme `submitted: false`, limpeza aprovada e carga parada.
Em campanhas novas, esse mesmo retry e automatico uma unica vez sob a politica
pre-registrada `pre_intervention_retry`; uma segunda falha permanece bloqueada.
Cada nova tentativa ganha identificador, diretorio e linha de ledger proprios.
Runs completos ou cuja injecao foi submetida nunca sao repetidos; o historico
anterior permanece. A excecao explicita
`--retry-unconfirmed-container-kill CELL_ID` so aceita um aborto container-kill
cujo relatorio prove que nao houve submissao ou que um unico envio nao causou
restart, com leitura de pods posterior sem mudanca. A nova preflight exige o
mesmo UID de pod, `containerID` e contador de reinicios do alvo anterior.
`--retry-invalid-window CELL_ID` permite repetir somente uma celula HTTP
concluida cujo artefato persistido prove a sobreposicao positiva entre os
limites de `fault` e `post`, com fonte `virtualservice_api_bracket_estimated`
e gates anteriores de recuperacao, limpeza e parada da carga aprovados.
`--retry-reviewed-container-kill CELL_ID` e uma excecao explicita para um
aborto container-kill com um restart confirmado, recuperacao reprovada e
limpeza, parada de carga e estabilidade da aplicacao comprovadas. Ela exige
revisao/autorizacao explicita e repete o preflight atual; nao relaxa nenhum
gate da nova tentativa. As tentativas anteriores ficam preservadas, cada
nova tentativa tem artefatos e evento de auditoria proprios, e `runs.csv`
mostra a tentativa mais recente por celula. Outros abortos continuam exigindo
investigacao manual.

O plano/configuracao sao persistidos antes de iniciar. A ordem e
pseudoaleatoria com seed e evita o mesmo servico consecutivo quando possivel.
A execucao e sequencial; runs de terceiros ativos bloqueiam. O ledger
JSONL e append-only, com chave por servico/falha/repeticao; retomada reusa
run/idempotency key e nao reinjeta concluidos/bloqueados sem uma das excecoes
de retry explicitamente autorizadas acima. Falha de limpeza,
recuperacao ou aborto interrompe antes de outro alvo e exige investigacao
manual. Nao existe retry automatico de uma intervencao submetida; o retry
automatico pre-injecao nao repete dados experimentais nem converte bloqueio
em sucesso.
Ha lock exclusivo por diretorio de campanha. Se a resposta do start se
perder, a retomada procura o run pelo ID deterministico do experimento
antes de submeter qualquer coisa.

Antes de submeter um run, o runner exige que a API anuncie o protocolo da
campanha controlada; uma API antiga (sem o endpoint de capabilities) e
bloqueada sem nova submissao. Reinicie a plataforma depois de atualizar o
codigo para carregar esse suporte. Se uma resposta terminal nao contiver
`summary.controlled_status`, a tentativa e registrada como aborto de protocolo,
a resposta bruta e preservada em `run.json` e a campanha para; nao retome nem
submeta outra intervencao antes de investigar o run e confirmar recuperacao e
limpeza.

A carga controlada resolve um unico pod/container Locust e registra UID,
containerID, PID remoto e instante de criacao do processo. Para somente
esse PID, confirma termino por identidade no `/proc`, sem confundir
interrupcao do `kubectl` local com parada do Locust remoto. Saida vai para
log remoto por run, sem pipes locais que possam bloquear runs longos;
o resumo captura cauda de 200 kB e sinaliza truncamento. Mudanca de
identidade ou falha de parada aborta a campanha. O caminho legado da
carga nao muda.

Em `out/controlled_campaign/<id>/`: `plan.json`, `config.yaml`, `ledger.jsonl`,
`campaign_events.jsonl` (interrupcoes/erros do executor),
`runs.csv`, `effect_validation.json`, specs/manifests e respostas por tentativa.
Os artefatos do run na plataforma preservam metricas brutas por coleta,
logs snapshot/traces com status e limites, pods/eventos, configuracao efetiva,
workflow submetido/observado, injetor, timestamps/fontes, gates e limpeza.
Dados parciais de abortos sao capturados quando disponiveis e erros ficam
explicitos. Nao sobrescreve runs/campanhas historicas.

Apos a campanha, avalia os runs concluidos com a **RCA multiagente** e
preflight de versao, separadamente dos resultados da intervencao.
Repeticoes sao identificadas por `replica_idx`; falhas/bloqueios permanecem
no denominador global do relatorio, nao desaparecem do resultado.

Para avaliar a RCA multiagente sobre uma campanha já concluída, passe o
diretório da campanha. Essa avaliação é somente leitura para os runs de caos e
grava respostas e relatórios versionados em `multiagent_rca/`:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.evaluate_multiagent_rca \
  --campaign-dir experiment/eval/memory_hog/out/quick_campaign/<campaign-id>
```

Opcionalmente, informe um `evaluation.json` anterior com
`--compare-evaluation <caminho>`. O relatório compara somente os runs em comum e
inclui acurácia de serviço e tipo de falha, recall do alvo no ranking métrico e
escopo, cobertura de métricas/logs/traces em fault, revisões/violações de
escopo, reparos/falhas dos agentes, confirmações sem suporte independente,
tokens/chamadas/latência e acurácia estratificada pelos runs cuja injeção teve
efeito validado. A
correspondência de serviço e `fault_type` usa os rótulos de ground truth do
ledger/configuração apenas na avaliação; eles não são enviados ao modelo.

## Comparação pareada: Skill RCA vs multiagentes

Execute os dois avaliadores na mesma campanha. Cada um usa o conjunto de
`possible_fault_types` derivado da matriz da campanha; o comparador valida que
os conjuntos são iguais e compara apenas `run_id`s em comum. Respostas
ausentes, inválidas ou com erro contam como incorretas no denominador pareado.
O relatório compara somente serviço e causa (`fault_type`, correspondência
exata com `chaos_type`), com detalhamento do tipo por causa. `category` é
mantida na resposta RCA, mas não é pontuada:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.evaluate_skill_rca \
  --campaign-dir experiment/eval/memory_hog/out/quick_campaign/<campaign-id>

./.venv/bin/python -m experiment.eval.memory_hog.evaluate_multiagent_rca \
  --campaign-dir experiment/eval/memory_hog/out/quick_campaign/<campaign-id> \
  --compare-skill-evaluation experiment/eval/memory_hog/out/quick_campaign/<campaign-id>/rca_skill_evaluation.json
```

`fault_type` registra a causa mais provável escolhida entre as opções da
campanha, inclusive quando o diagnóstico é `inconclusive`; esse estado indica
falta de confirmação causal, não ausência de uma hipótese. `category` registra
a categoria causal mais ampla (por exemplo, `cpu-hog` / `cpu-exhaustion`).
Os relatórios JSON preservam os dois campos separadamente, e o log Skill exibe
ambos. Uma resposta sem `fault_type` ou fora das opções é registrada como
erro/resposta inválida, não recebe uma causa inventada e conta como incorreta.

O JSON e o Markdown multiagente registram os pares, denominadores, acertos
exclusivos de cada método e a variação líquida. O resultado da injeção
(`effect_validation.json`) permanece uma dimensão separada e não é mostrado ao
modelo como ground truth.

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
`out/diverse_campaign/<campaign-id>/`, gera nesse diretório os manifestos de
chaos incluindo os VirtualServices que preservam as rotas das probes, e não
executa avaliação LLM. Os manifestos são gerados mesmo com `--skip-prime`.
Com `--wait-for-idle`, o script aguarda outras execuções da plataforma
terminarem antes de injetar faults; esse tempo de espera não entra nas 10 horas
estimadas. Sem essa opção, o script falha antes de iniciar se houver outro run
ativo.

## Troubleshooting

* **`HTTP 400` on `/api/runs/start`** — likely the Argo workflow template
  `<service>-memory-hog` doesn't exist yet. Re-apply the manifests.
* **Run sits at `running` forever** — check `kubectl get workflows -n litmus`
  for stuck workflows and `kubectl get pods -n sock-shop` for crash-looping
  target pods. The harness has a `run_timeout_seconds` (30 min default) cap.
* **LLM 502 in eval phase** — Qwen at `localhost:8001/v1/chat/completions` is down or
  out of context. Lower `budget_tokens` in the failing strategy or restart
  the model.
