# Memory-hog evaluation harness

## Autonomous second test block (35 to 70)

The [replication plan](../../../research/planos/2026-11-10-replicacao-autonoma-35-testes.md)
records the authorized overnight policy and scientific limitations.
`test_replication_campaign` prepares a frozen seven-service/five-fault test-only
matrix, waits for a verified successful evaluation, runs two fresh no-fault
controls, and collects up to 35 new accepted incidents. Preparation and `--check`
only inspect/write local files; `--run` explicitly arms autonomous execution.

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --prepare <evaluation-directory> --output <new-campaign-directory>
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --check <new-campaign-directory>
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --run <new-campaign-directory>
```

Training/RAG remain frozen and no diagnostic evaluation is started. At most two
attempts per cell are allowed; reinjection requires an explicit technical failure,
confirmed cleanup and recovery. A negative measured effect alone is not a retry
reason. The 24-hour post-release budget never turns missing runs into successes.
Published original/new/combined cohorts include truthful counts and completeness,
alongside all failed attempts. A complete combined cohort has 70 distinct held-out
run IDs. This uses a prospective protocol amendment, not identical historical
replication. Terminal `blocked`/`partial` campaigns require review, not blind restart.

## Six-case supplemental campaign

Check readiness without starting load, changing dual-write, creating a campaign
or injecting faults:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.supplemental_campaign --preflight
```

This checks the API's **loaded implementation**, the exact six-case environment
and 600 seconds of wall/monotonic clock agreement. The API captures a SHA-256
fingerprint of the controlled lifecycle, engine, load/injection plugins and
resource-intensity code at import. A matching protocol number alone is not
sufficient: restart the API from the same checkout after changing those files.
The fingerprint, measurement policy and memory-worker signature must match
before controls or experiment submission.

The clock check preserves the 1-second drift limit and detects both individual
steps and accumulated drift. Passing it is a prerequisite, not a guarantee of
future clock stability. Keep Windows/WSL and the Kubernetes node synchronized,
avoid suspend/resume and manual clock changes during a campaign, and investigate
recurring host clock steps instead of enlarging the tolerance. A dedicated Linux
host avoids the additional WSL/Windows clock boundary but still needs validation.
Do not treat `NTPSynchronized=yes` alone as proof that no step occurred.

Run the frozen three-train/three-test complement with:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.supplemental_campaign --run
```

The command first persists the same readiness checks in `initial_readiness.json`.
A failed check blocks before dual-write or load. It then enables historical
dual-write under the campaign's shared
`history_campaign_id`, runs two 600-second no-fault controls, performs the
six-case preflight, and pauses for calibration review before any injection.
Each control must cover at least 600 seconds by both wall and monotonic clocks;
it may extend by at most 30 monotonic seconds to account for clock-rate drift.
The versioned `supplemental-all-stability-25pct-v1` policy uses a 25% relative
limit for every stability metric during calibration preparation and no-fault
control windows, while preserving each metric's existing floor. Coverage,
health, effect, recovery and safety gates are unchanged. This policy applies
only to new controls: prior reports are never reclassified.

New supplemental campaigns additionally pin
`no-fault-control-diagnostic-v1` in `control_acceptance_policy`. The long
600-second window's variability checks are **diagnostic**, not acceptance
gates. The same 25% limits and floors are still computed, individual
`stability.*.passed` values remain truthful, and a failed variability check
sets `no_fault_window.stability_passed=false` with explicit `warnings`.
The report's top-level `passed` means acceptance under its recorded policy,
not that every variability check passed. The two controls must still pass
preparation, baseline, coverage/error-rate, health, container identity,
clock integrity and load-cleanup checks. Missing/invalid telemetry or an
exception never becomes a warning-only result.

This is a prospective protocol amendment, not identical-protocol replication.
Existing campaigns without this policy keep blocking variability checks,
including on historical revalidation. A continuation verifies the control
policy against its manifest. Generic load rehearsals default to the original
blocking mode. No old failed report is reclassified or reused as an accepted
new control.

Per-case gates remain unchanged: the local preparation still requires two
consecutive 60-second windows within 600 seconds, using the existing **20%**
relative stability limit (not the calibration's 25%). Each case still checks
its own baseline, effect, recovery, cleanup and evidence. Long-window variance
is a heuristic noise diagnostic, not proof that samples are corrupt or that
the injected mechanism is unidentifiable. Conversely, diagnostic acceptance
does not guarantee an interpretable chaos run; the per-case checks can still
block or abort for a concrete integrity/safety/effect problem. Both accepted
controls are reviewed, including their warnings, before training starts.
Before starting load, it waits up to the configured visibility timeout for
fresh history-labeled samples from `front-end`, `orders`, and `user`; failure
blocks the campaign and disables dual-write without starting load.
Training/test continuations repeat readiness checks and preserve the reports
as `training_readiness.json` / `test_readiness.json`. A readiness failure disables
the campaign's dual-write and prevents submission.
Dual-write stays enabled across the review and training-base pauses so the
later cases use the same history label; it is disabled when the campaign
blocks or completes. Continue only after reviewing the saved reports:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.supplemental_campaign \
  --continue-training --resume <campaign-directory>
```

Each single-cell supplemental run defers RCA cohort evaluation until the
complete cohort exists; a one-cell training plan has no test fault matrix to
score. If a previously completed training run was blocked only by the known
`campaign contains no test fault types` post-processing error,
`--continue-training` accepts it only when its ledger, run ID and experiment
gates match the recorded failure. The command repeats readiness and history
checks, validates the completed run and continues without reinjecting that
case. Other blocked or failed runs remain blocked.

The test phase additionally requires the reviewed 35-pair training base and
uses `--continue-tests --resume <campaign-directory> --training-base <rag_base.json>`.

An empty cgroup exec response with the exact transport error
`error: error stream protocol error: unknown error` is retried once, with the
failure recorded and the application-container identity rechecked first.
The successful read has its own timing interval. Partial output, changed
identity, other command errors, a repeated transport error or invalid timing
still block collection. This is a read retry, never a repeated fault injection.

## Historical no-fault control revalidation

Controlled measurements now record `controlled-measurement-v2`: Prometheus
rates and histogram rates use a 60-second lookback (the query step remains
5 seconds), and cgroup CPU deltas use monotonic elapsed time. Clock jumps over
1 second during/between reads block collection explicitly. Stability floors,
the 20% rule, effect thresholds and recovery thresholds are unchanged. The
longer lookback includes observations before each window's first timestamp;
this is a versioned measurement change, not identical-protocol replication.

To requery completed controls of a blocked supplemental campaign without
starting load or injecting faults:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.revalidate_load_controls \
  --campaign experiment/eval/memory_hog/out/supplemental_campaign/<campaign-id>
```

The command queries historical MCP at `127.0.0.1:18082/sse`, preserves the
original windows and resource samples, and recomputes coverage and stability
for every preparation, baseline and 600-second control window. It writes
separate timestamped reports under `revalidations/`, with query responses and
SHA-256 provenance, and updates `campaign_state.json` to reference the revised
checks. Original reports, raw evidence and the campaign manifest are not
overwritten. Legacy CPU reads with clock anomalies become `invalid`, not
successful: absent monotonic timestamps cannot be reconstructed retroactively.
The campaign remains blocked pending review/fresh preflight even if revised
checks pass. No training/test set is changed by this command.

## Quantitative retrieval for new procedural evaluations

New test-base evaluations compare the original Skill with RAG using the Qwen 3.5
procedural corpus and select `opensearch-bm25-metric-gated-rrf-v6`. Both
diagnostic methods use the same model, temperature `0.1` and seed `20261107`.
The retriever uses the same
`incident-a-v2` records and schema-4 OpenSearch index as v4/v5: **no A/B rewrite
or reindex is required**. Previously prepared configurations remain pinned.

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --prepare --api-base http://127.0.0.1:8010
```

Preparation does not run inference or inject chaos. Use
`--retriever-version opensearch-bm25-field-idf-v5` or
`--retriever-version opensearch-hybrid-rrf-v4` for an explicit older comparison.
Before executing a new RCA evaluation, complete fresh historical MCP collection
with `--precollect <evaluation-directory>`; `--run <evaluation-directory>`
retains the existing online collection gates. Do not reuse old diagnostic
responses as a new evaluation.

To prepare the completed 35-run test cohort produced by the supplemental
campaign, pass its derived manifest and the matching indexed training corpus:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --prepare \
  --test-cohort experiment/eval/memory_hog/out/supplemental_campaign/<campaign-id>/derived_test_source_cohort.json \
  --corpus-id supplemental-training-35-<campaign-id> \
  --api-base http://127.0.0.1:8010
```

This validates the original 32 runs, the three completed supplemental cases,
the training-base file hash, and train/test disjointness before writing a
frozen evaluation plan. The derived cohort reuses 32 previously evaluated
runs, so the resulting 35-run evaluation remains exploratory rather than a
blind independent test. Preparation does not collect evidence or run
diagnostics; do not start precollection or inference until explicitly ready.

To compare the shared resource-signature prompt fairly, prepare a paired
2x2 Skill/RAG comparison. The command creates frozen `none` and
`cpu-memory-v1` arms, preserves the same cohort/corpus/retriever, and balances
the order of the two prompt conditions across runs:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --prepare-comparison --api-base http://127.0.0.1:8010
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --precollect-comparison <comparison-directory>
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --run-comparison <comparison-directory>
```

For a small, balanced CPU/memory pilot, select the same number of `cpu-hog`
and `memory-hog` runs. The sample and scheduling seeds are separate and saved
in the parent manifest. For example, three cases per type:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --prepare-comparison --resource-cases-per-type 3
```

This creates six paired runs (24 diagnoses across the four conditions). It is
an exploratory pilot, not a confirmatory estimate; the selected run IDs and
selection seed are recorded before precollection and inference.

Every Skill/RAG model call keeps the 120,000-character evidence-context cap.
After lossless compaction, evidence over 60,000 characters is summarized before
it is reused across model stages; the hard-cap fallback still partitions only
at evidence-ID boundaries. The shared pipeline uses neutral, per-ID summaries
that retain source IDs and hashes, allow only source-supported numeric values
(with one repair attempt), and redact unsupported numeric claims if repair
also fails. Redaction is explicit in the summary metadata. Summaries are cached
by source hash across Skill/RAG stages and newly added evidence, and recorded
as `evidence_context_projection` under `bounded-evidence-summary-v7`. The
manifest pins both character limits. Repeated nested metric-series labels use
lossless JSON-Pointer columns; all series, values, source IDs, and hashes are
preserved. Summary calls are capped at 3,072 output tokens. An item that still
cannot fit fails explicitly. This changes the inference pipeline, so
comparisons using it are exploratory under the new protocol and require fresh
MCP precollection.

Precollection performs new historical MCP queries for both arms; it must
complete before inference. The runner interleaves conditions per run and does
not score any condition until all four diagnoses per run have been sealed.
It then writes `factorial_report.json` with paired accuracy contrasts, exact
McNemar tests with Holm adjustment, bootstrap intervals, and CPU-/memory-hog
confusion counts. This previously inspected 32-run cohort is exploratory, not
a blind independent confirmation.

For a single-method-pair diagnostic run, `--prepare` and
`--prepare-and-run` also accept `--resource-signature-policy none` or
`--resource-signature-policy cpu-memory-v1`. The default `none` preserves the
legacy prompt and options fingerprints.

New test-base evaluations pin model generation to temperature `0.1` and seed
`20261107`. Both values are saved in the evaluation config/report and sent with
each model call. This improves repeatability for identical prompts, model and
runtime; it does not guarantee byte-identical output across different runtimes,
hardware, or changing online evidence.

The test-base comparison gives both methods the same per-case model budget:
30 calls, 300,000 tokens and 600 seconds. Skill's four additional checks match
RAG's four additional rounds by count; they are not identical work units.
Skill calls for planning, retries, diagnosis and evidence-projection summaries
all count toward its shared call and token limits. Token checks use the same
accounting as RAG: canonical input characters divided by four plus the
requested output cap before a call, then provider token usage when available or
input/output character estimates otherwise. Reports separate measured from
estimated usage. Earlier Skill results without budget metadata are not
equal-budget observations and must not be relabeled as such.

V6 compares positive metric deltas by service/metric/unit/statistic, with
corpus-only median scales, threshold 10, numeric weight 0.75 and RRF constant 10.
Below the threshold it preserves BM25 v5. Scores are normalized to [0, 1]:
RRF divided by its theoretical maximum when active, BM25 divided by the largest
BM25 score otherwise. New v6 evaluation configurations use `min_score=0`, matching
the exploratory benchmark; API callers may explicitly choose another floor.
The response records route activation, strength, parameter values and both
ranking components under retrieval diagnostics.

Both routes apply the procedural eligibility gate and source-run exclusion
before fusion. Pending guides remain limited to explicit external experimental
evaluation; they are not promoted. No test label, B diagnosis or extra checks
enter the numeric features. This is an **experimental evaluation default**,
not evidence of independent generalization or a change to local/legacy corpora.
Restart an already-running API after updating the backend; its
`/api/skill-rag/strategy` capability advertises v6 and the recommended evaluation
floor.

## Experimental quantitative retrieval benchmark

`metric_profile_benchmark` compares BM25 v5 with corpus-normalized metric
profiles and gated reciprocal-rank fusion. It reads frozen training/test A
records and queries an **existing** OpenSearch v5-compatible index; it does not
create an index, change eligibility, inject chaos or run a new RCA.

```bash
./.venv/bin/python -m experiment.eval.memory_hog.metric_profile_benchmark \
  --evaluation experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z \
  --output experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v4-20261106T222721823606Z/metric_profile_retrieval_benchmark.json
```

The runner validates the frozen corpus checksum, excludes the query source run
and rejects overlapping training/test metric windows. Scales use only corpus A.
It freezes rankings before consulting exact service/fault relevance labels,
filters both ranking routes before fusion for the eligible pool, and reports
coverage separately from Hit@k, per-service/per-fault scores, gains/losses,
metric-channel ablations and implementation/query hashes. It also runs the
integrated v6 retriever and fails explicitly if any complete ranking differs
from the selected experimental reference, before applying relevance labels.

The selected experimental route (`metric-profile-gated-rrf-v1`) activates when
the largest positive delta is at least 10 corpus median absolute deltas,
uses positive-delta cosine rankings, and fuses 75% numeric / 25% BM25 with
RRF constant 10. Otherwise it retains BM25. These are exploratory, post-hoc
parameters, not a validated production default. Missing facts are zero-filled
only for similarity; partial coverage is not treated as evidence of absence.
Results use full rankings and `min_score=0`; context packing and RCA accuracy
are outside this benchmark. A **new RCA evaluation** still requires fresh
historical MCP collection.

See [quantitative retrieval results and limitations](../../../research/diario/2026-11-07-relatorio-melhoria-recuperacao-rag.md#segunda-revisao-recuperacao-quantitativa-condicionada).

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
For v2 callers that provide structured A fields, lexical matching queries each
field family separately with at most 24 terms per field. This bounds Boolean
clauses while retaining lexical coverage across A; dense retrieval still uses
the full retrieval text.

Build the v2 index explicitly from the installed frozen base before preparing
an OpenSearch test campaign:

```bash
python -m experiment.eval.memory_hog.opensearch_experiments \
  --base-file experiment/platform/data/object_store/.rca_knowledge/<corpus-id>/exploratory_base.json
```

The command verifies the base checksum, creates a separate v2 index, and prints
the index metadata. Reuse the base's SHA-256 for test-only preparation; the v1
index remains untouched.

### Rebuild procedural training B with Qwen 3.5

The reconstruction preserves the frozen v2 training cohort as the source of A,
target labels and run windows. Protocol `training-b-rebuild-v4` gives Qwen 3.5
the target service and fault type from the beginning, then requires it to build
an evidence-supported investigation from A to that target. The procedure must
consult available MCP metrics for the labeled service, explain why dependent
services or similar fault types are not the origin, and distinguish observed
facts from unavailable data. This is supervised guide authoring, not a held-out
accuracy estimate.

#### Factual A and RAG-assisted correction

With `--audit-rag-base`, A is rebuilt from a **new online MCP collection**, not
from a previous diagnosis. Its metric narrative is a deterministic projection
of service, metric, unit, baseline/fault means, direction and source pointer.
Historical MCP `top` means and platform `top_delta` means are both supported.
Marked factual narratives reject edits that change those observations; old
unmarked corpora remain readable. Other free-text claims still require review.
CPU/memory checks additionally compare baseline/fault/post for both resources.

The optional auditor retrieves up to three eligible procedures using retriever
v6 from a frozen, training-only reference. It excludes the same run and repeated
incident descriptions and never learns from guides approved during that build.
Historical procedures are hypotheses, never current evidence. Private training
labels remain outside the auditor prompt; supervised authoring and the final
factual review still check the expected destination.

The auditor can approve, correct, request new executable MCP checks, or leave
the case pending. Up to two correction rounds are followed by fresh factual
reviews and a final audit. Approval cannot bypass deterministic gates: the
target metric, factual review, causal chain, independent evidence families and
any required supported restart reason must pass. Metrics derived from spans
alone do not independently corroborate traces. Missing data, contradictions,
exhausted budgets and tool/contract errors do not promote a guide.

For every `http-status-code` B rebuild, the Istio status/flags/edge metric is
collected before planning so the model can select the observed caller. A `FI`
response is attributed to the proxy only when the destination resolves to the
target; the procedure then investigates caller/proxy logs and caller CLIENT
traces rather than requiring a destination SERVER span. A 4xx/5xx without
`FI` is not proof of injection. Caller corroboration is accepted only when a
fault-phase log/trace reports an HTTP 5xx and a positive Istio `5xx`/`FI` rate
resolves that caller-to-target edge. The family gate collapses
Prometheus evidence to one metrics family, maps span-derived metrics to the
trace family, and does not count unknown metric provenance. The inventory
reports SDK HTTP metrics
as `not_queried`: historical `http_client_*`/`http_server_*` series have no
registered executable query contract here, so they are not silently treated
as missing data or mixed with Istio request rates.

When an audit prompt is oversized, optional historical guidance is removed
before current evidence or schema, and the omitted-document count is recorded.
Already executed audit checks are skipped and linked to existing evidence,
while available new checks still run. A reviewer cannot return `pass` with
non-empty factual/scope violation lists; a contradictory response is retried
and remains pending if it does not satisfy the contract.

Limits per audited case:

- Coverage: nine fresh queries (Prometheus, Loki and Tempo in each of
  baseline/fault/post); then two triage queries.
- Investigation: three model planning rounds, at most four MCP checks each.
  Forced target checks add at most six queries for CPU/memory or three for
  other faults, skipping checks already executed. The HTTP target Istio check
  runs before planning and counts toward that same limit.
- Audit: at most three decisions, two corrections and two follow-up reviews;
  at most four new MCP checks per correction round (eight total). This cycle
  has a hard ceiling of **21 model attempts**, including JSON retries, and
  **450,000 estimated reserved tokens** (input characters/4, rounded up, plus
  maximum output for every attempted call). Budget exhaustion is an error,
  not another retry or an approval.
- Maximum logical MCP queries: 37 for CPU/memory, 34 for other faults.
  Each query has at most two transport attempts. Initial planner, author and
  review have up to three JSON attempts each, outside the additional audit
  budget. Model evidence is projected under 72,000 characters; up to 24,000
  characters of historical guidance are supplied. Omission counts and full
  raw responses are persisted.

Use distinct output/corpus directories; the reference corpus cannot be
overwritten. A five-case pilot never freezes or indexes a corpus:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.training_b_rebuild \
  --pilot-five \
  --audit-rag-base experiment/platform/data/object_store/.rca_knowledge/rag-rebuild-qwen35-procedural-metrics-20261106/exploratory_base.json \
  --output-dir experiment/eval/memory_hog/out/knowledge_rebuild/<new-campaign> \
  --corpus-dir experiment/platform/data/object_store/.rca_knowledge/<new-corpus>
```

To investigate only the 13 pending guides of that reference, replace
`--pilot-five` with `--pending-only` (optionally `--limit 1`). This writes a
`pending_reaudit.json` report and per-case artifacts but **does not publish a
partial corpus** or count the remaining 19 guides as newly collected. A full
32-case build uses the same paths and audit reference after a
provenance-matched, reviewed pilot; renewing A recomputes its retrieval vectors.

To measure benefit, compare factual-A-only and factual-A-plus-auditor training
on the same cohort. For the control, add `--factual-a-only` to the same
reference/selection command and use separate output/corpus directories.
It retains fresh factual A, resource comparisons and causal gates, but disables
the RAG auditor; its provenance cannot be reused as an audited pilot.
Then evaluate the resulting frozen corpora on the held-out
test base with fresh online MCP evidence and equal Skill/RAG budgets. Report
eligibility by service/fault, retrieval Hit@3, joint diagnosis accuracy and
joint accuracy conditional on Hit@3. A training pilot is not evidence of
improved held-out accuracy; new independent runs are required where the
existing windows cannot discriminate the annotated fault.

The current frozen source contains 32 pairs and all have `split=train`; there
are no `split=test` pairs in that source. Start with one deterministically
selected case for each fault type. This five-case pilot is for human review and
does not create or publish an OpenSearch index:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.training_b_rebuild --pilot-five
```

The historical MCP must already be reachable at `HISTORICAL_MCP_SSE_URL`
(default `http://127.0.0.1:18082/sse`), or at the explicit
`--historical-mcp-url` loopback endpoint. The command never restores port-forwards,
restarts workloads or injects faults. An unreachable backend stops the build;
it is not converted into `no_data` or a diagnosis. Historical queries are
read-only, and persist their arguments, responses, timestamps and SHA-256
references. Invalid summaries, including non-finite Prometheus samples, remain
explicitly marked as unusable evidence rather than aborting the whole case or
being converted to `no_data`.

The guide procedure records ordered observed steps, executed tool/evidence IDs,
hypotheses, verification and interpretation. To keep retrieval history bounded,
the guide projection does not duplicate query arguments; complete arguments,
responses, timestamps and digests remain in the per-case investigation
artifacts. The host derives step links from the ordered list. A procedural
review checks that the route reaches the labeled service and fault, includes a
metric confirmation in the target service, and explains plausible
dependent-service or similar-fault alternatives. The final diagnosis fields
are constrained to the supervised labels, which remain separate from
telemetry; the guide must not disclose or cite those private labels as a reason.
Every evidence ID used in the diagnosis,
hypotheses or alternatives must also appear in an observed step, including the
initial golden-signal evidence used to describe A. The target metric is queried
in both baseline and fault windows so the guide can compare the change. For each
alternative service discussed, at least one relevant available fault-window
check must be observed; a range query covering multiple windows is classified
from its returned phase summaries in both evidence projection and validation,
so its fault data is not mislabeled as baseline data. Redundant checks need not
all be repeated. Unlinked
secondary citations are removed only when observed support remains, and the
removal is recorded in the model-attempt artifact. A confirmed diagnosis cannot
lose its last support reference. A missing or
nondiscriminating target metric is recorded as a limitation and cannot count as
successful confirmation. CPU and memory comparisons use actual numeric
baseline/fault samples even when a query's `found` flag is false, but never
promote `no_data` to evidence. For `container-kill`,
the target service's restart row is used when available; an interval that only
overlaps the fault window retains timing uncertainty, and an unknown termination
reason is not replaced with an invented mechanism.

Per-case outputs are partitioned by producer SHA-256 so changed code cannot
reuse older results or overwrite earlier attempts. By default, the full cohort
requires a successful pilot with one procedurally validated case per fault type
and matching source/model/endpoint provenance. After explicitly reviewing an
incomplete pilot, an operator may continue with
`--continue-after-incomplete-pilot`; the exact five-case cohort and provenance
are still checked, and pending or failed guides remain ineligible. Review pilot
examples before starting the full run; the full run creates a new frozen corpus
and, only when explicitly requested with `--index`, publishes eligible
procedures to a new v4 index. Procedural eligibility is independent of
`effect_validated`: that field is retained as provenance but is not a v4
publication filter. Only pairs with
`guide_status=procedurally_validated` and `b_procedural_status=passed` are
retrieved. If a full-run case fails to produce a guide, the pipeline records
`full_rebuild_incomplete.json` and does not freeze a corpus or publish an index.

The new corpus ID is `rag-rebuild-qwen35-procedural-20261106`. To prepare a test-only
campaign from this base, use its SHA-256 printed by the rebuild report:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.exploratory_rag_campaign \
  --prepare-test-only \
  --source-campaign experiment/eval/memory_hog/out/exploratory_campaigns/<source-campaign> \
  --reuse-corpus-id rag-rebuild-qwen35-procedural-20261106 \
  --opensearch-base-sha256 <base-sha256> \
  --full-test-only
```

The prepared configuration pins retriever v4 and Qwen 3.5; it does not start
test runs. V4 retrieves only procedures that passed the procedural review.
Keep the prior v1/v2/v3 corpora and indexes available for comparison.

For corpora adjudicated from independent blind reviews and a fresh live MCP
collection, pairs use `review_status=blind_live_review_adjudicated` and pin the
review-manifest SHA-256. Their ground-truth labels remain separate from the
historical model diagnosis; label agreement is not required for this status.
The history projection still omits both labels and diagnosis, and keeps every
guide inconclusive.

### Import supplemental training runs

The supplemental importer validates the completed run ledgers, controlled
reports, effective specs, historical read-back gates and the exact 35-cell
matrix before rebuilding A/B for the three new training runs. It reads
historical MCP data only and never injects chaos. The derived corpus preserves
all 32 parent pairs and hashes, records the parent/active Skill-hash lineage,
and uses its manifest pair count for test-base evaluation (legacy manifests
without that field continue to mean 32 pairs).

```bash
python -m experiment.eval.memory_hog.supplemental_training_import --plan
python -m experiment.eval.memory_hog.supplemental_training_import --build
python -m experiment.eval.memory_hog.supplemental_training_import --prepare-review
```

`--build` is resumable and retains `procedurally_validated` versus
`procedural_pending` results. By default, `--index` refuses to publish until
every pair has the independent blind-live adjudication status and
review-manifest hash. The model's supervised procedural review is not a
substitute for that adjudication.

An explicitly authorized exception is available for completing the index
before blind-live review:

```bash
python -m experiment.eval.memory_hog.supplemental_training_import \
  --index --bypass-blind-review
```

This writes a separate `rag_base_indexed.json` and a versioned 35-document
index without altering the candidate base or claiming that a review occurred.
Only the three supplemental pairs may use
`review_status=not_reviewed_user_authorized`; no review-manifest hashes are
created for them. The bypass is recorded in the indexed base and manifest.
Procedurally validated examples follow the normal retrieval policy, while
`procedural_pending` examples remain excluded unless the separately gated
external-test opt-in is used. This command does not run an evaluation or change
an existing alias.

`--prepare-review` emits a reviewer packet with randomized case IDs and no
source run/pair IDs or ground-truth labels. Its separate custodian map is
written with owner-only permissions and must not be shared with reviewers.
The packet includes incident A and its historical-MCP evidence, but excludes
the supervised B guide and review. Reviewers must independently query the
historical MCP over the supplied phase windows and attach those fresh results
before returning their adjudication.

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

#### RAG-only evaluation on the held-out test base

To evaluate the v6 retriever on the existing completed test runs without
injecting new faults, prepare and run the evaluator:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.test_base_rag_evaluation \
  --prepare-and-run \
  --api-base http://127.0.0.1:8010 \
  --mcp-sse-url http://127.0.0.1:18082/sse
```

It first queries Prometheus, Loki, and Tempo online through the historical MCP
for baseline, fault, and post windows of all 32 cases. Loki and Tempo queries
are namespace-wide and do not filter on the injected service or fault labels.
Inference is blocked until all 288 precollection queries finish without
backend errors; `no_data` remains visible as a coverage limitation. The run
executes only `exploratory_rag` with the v6 retriever, persisting its response
before reading test labels for scoring. It reports retrieval and diagnosis
separately, with breakdowns by service and fault, per-run results, MCP coverage,
mean request/diagnostic time, token use, and budgets. Historical results for the
original Skill, exploratory control, and v4 RAG are shown only as non-paired
comparators; they are not rerun in this evaluation.

The test cohort was used in earlier evaluations, so this remains an exploratory,
non-blind assessment. The full index contains 32 cards, but retrieval receives
only the 19 procedurally validated guides by default; the 13
`procedural_pending` guides are excluded. The report distinguishes all 32
queries from those with an exact eligible pair, which bounds possible Hit@3.
The Markdown report is written under `research/diario/` using the evaluation ID
in its filename; `--report-output <path>` can override the destination.
For a staged run, use `--prepare`, `--precollect <evaluation-dir>`, then
`--run <evaluation-dir>`. The prepared directory is under
`out/knowledge_evaluation/`. External use of the corpus is restricted to
completed runs pinned to the test split. Completed supplemental runs without a
split pin are accepted only when they are listed as new test cases in that
evaluation's hash-checked cohort and complete MCP precollection manifests.
Explicitly train-pinned runs and any run ID in the frozen corpus training set
remain rejected.

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

When reusing a legacy frozen index, pin the retriever version to its schema.
For the original v1 index, add
`--retriever-version opensearch-hybrid-rrf-v1`; preparation verifies the index
read-only and records the selected retriever in the campaign manifest. It does
not rebuild or modify the index.

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
The controlled pre-intervention sample-age query is scoped to the active
`history_campaign`, so a still-visible series from an older campaign cannot
duplicate or satisfy the current pod's freshness gate.
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

### Train-only 35-cell campaign

The `train-only` profile collects the seven-service by five-fault training
matrix without starting RAG inference or a test split. Advance only to an
explicit tranche boundary:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --level train-only --through-train-count 35 --preflight
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --level train-only --through-train-count 35 --run
```

Its freshness gate queries the historical Prometheus MCP for the current pod.
The infrastructure preflight does not prove that this historical series is
fresh. Ensure campaign dual-write is active; if the first cell is blocked before
submission because no recent sample is visible, enable dual-write, verify the
current pod's sample through the historical datasource, and retry only after
confirming cleanup and no active Locust load. Preserve the original ledger row:

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/<campaign-id> \
  --run --retry-blocked <cell-id> --through-train-count 35
```

The retry guard must pass without weakening it; submitted or intervened
attempts are not eligible for `--retry-blocked`.

The continuation amendment for campaign
`controlled-20261103T234452388707Z` permits continuing after a completed,
measured technical gate failure only when recovery, cleanup, stopped load, and
complete historical read-back all pass. Such cells remain failed and are
excluded from training. Blocked, aborted, failed, or stopped runs, missing
measurements, invalid historical telemetry, or any recovery/cleanup failure
remain fail-stop conditions, except for one pre-registered retry of a clean,
unsubmitted pre-intervention block when cleanup and stopped-load gates pass.
A user-approved amendment now permits skipping that cell after the retry limit
only when the block remains unsubmitted, cleanup and load-stop pass, the
application is healthy, and a fresh environment preflight passes. The cell
remains blocked and ineligible for training; all other hard stops remain. The
original campaign configuration and run reports are not rewritten. Future CPU cells use the reviewed candidate profile
with an inclusive 80% quota floor:

The approved late recovery check for `orders-container-kill-r0` measures CPU
and memory from the exact application container's cgroup-v2 counters, matching
the original baseline source. A versioned, user-authorized amendment permits
one final passive attempt with a 370 ms latency P95 ceiling for this check only;
the prior ceiling was approximately 366 ms. The 600 s timeout, 120 s continuous
stability requirement, load, baseline, and all other thresholds remain fixed.
The run stays aborted and excluded from training; only a passing recovery and
cleanup check can authorize skipping it and continuing. No fault is reinjected.

```bash
./.venv/bin/python -m experiment.eval.memory_hog.controlled_campaign \
  --resume experiment/eval/memory_hog/out/controlled_campaign/controlled-20261103T234452388707Z \
  --run --through-train-count 35 --continue-after-technical-failure \
  --continue-after-safe-blocked-cell \
  --calibration experiment/eval/memory_hog/cpu-calibration.yaml
```

The explicit continuation mode is restricted to a full train-only resume and
records its authorization and each excluded cell append-only in
`campaign_events.jsonl`. Progress distinguishes collection completion from
all-gates-passed status, reports each exclusion reason, and keeps safe blocked
cells separate from measured technical failures.

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
duas janelas passivas de recuperacao operacional, cada uma limitada a 600s,
estabilidade por 120s, limite de duas recuperacoes consecutivas ou cinco no
total e tres retries de consulta para erros transitorios (conexao, timeout e
HTTP 502/503/504). Os retries usam o mesmo run, com backoff de 5/10/20s; nao
repetem submissao nem injecao.
Uma falha `blocked` antes da submissao pode receber uma unica nova tentativa,
somente se a limpeza e a parada da carga estiverem confirmadas. A politica
`pre_intervention_retry` e gravada na configuracao antes da campanha; a nova
tentativa espera 60s, cria outro run/attempt e usa warmup e baseline novos, sem
alterar seed, intensidade ou thresholds. O bloqueio original permanece no
ledger e nos artefatos. Se a politica ou os gates de limpeza faltarem, nao ha
retry automatico; campanhas antigas nao ganham essa autorizacao ao serem
retomadas.

Ao expirar a recuperacao experimental, o run permanece `aborted` e
`recovery.passed=false`. A plataforma tenta recuperacao operacional passiva
**sob a mesma carga e usando a mesma baseline e os mesmos limites** por ate
600s. Se essa janela falhar, tenta uma segunda janela passiva, tambem limitada
a 600s, antes de interromper. A contagem de estabilidade reinicia quando um
check falha ou o intervalo entre checks excede 60s. Nao ha restart automatico
de workloads, troca de intensidade, remocao de gates nem espera ilimitada.
Os dados ficam em `operational_recovery` e `late_operational_recovery`; mesmo
se a segunda janela passar, o run original permanece `aborted`, inelegivel e
fora do treino. A campanha pode apenas pular essa celula apos limpeza e parada
da carga comprovadas, e iniciar a proxima com warmup e baseline novos. A janela
experimental termina em `experimental_recovery_expired`, sem incluir as esperas
operacionais. O timeout do orquestrador e o prazo da carga incluem as duas
janelas.

A emenda `passive-recovery-extension-amendment.yaml` ativa a segunda janela
somente para novas submissões da campanha `controlled-20261103T234452388707Z`;
o config original e os runs já registrados não são reescritos. A emenda não
reclassifica o aborto existente nem o libera sem evidência própria de
recuperação tardia.

A validação tardia de `run-503646080ca5` é autorizada separadamente por
`late-operational-recovery-check-amendment.yaml` e só pode ser executada para
essa célula abortada. Ela usa a carga normal, a baseline e os thresholds
originais, sem injeção ou restart; o resultado é salvo em
`late_recoveries/<run_id>/report.json` e anexado ao ledger sem alterar o status
`aborted`. A campanha só pode pular essa célula se a estabilidade passar e a
carga da validação for comprovadamente parada. Um artefato incompleto ou uma
falha de limpeza mantém o hard stop e não inicia outro experimento.

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
  fracao do limite real de CPU no container-alvo. Perfis legados podem usar
  `target_quota_fraction` com `target_tolerance_fraction`; nesse modo, tanto a
  media quanto a serie sustentada precisam ficar na faixa configurada.
  `minimum_quota_fraction` define um piso inclusivo para a media aritmetica da
  janela `fault`, sem limite superior nem requisito de duracao sustentada; a
  serie acima do piso fica como diagnostico. Os dois modos sao mutuamente
  exclusivos. A emenda pos-hoc `cpu-mean80-amendment.yaml` documenta a aplicacao
  do piso de 80% nesta campanha e preserva os gates restantes. Baseline ja
  >=70% bloqueia e pede recalibracao. Consumo/quota e throttling sao registrados
  separadamente; throttling/5xx nao sao obrigatorios.
  Runs CPU concluidos podem ser reavaliados, sem reinjecao ou edicao do ledger,
  com:

  ```bash
  ./.venv/bin/python -m experiment.eval.memory_hog.revalidate_cpu_mean80 \
    --campaign-dir experiment/eval/memory_hog/out/controlled_campaign/<campaign-id>
  ```

  O resultado append-only fica em `cpu_mean80_revalidation.jsonl`; execucoes
  em andamento nao sao alteradas: a ferramenta recusa executar enquanto o
  supervisor mantiver o lock da campanha.
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
  A confirmacao do stressor exige observar no container-alvo o processo
  `stress-ng` com `--vm-bytes`; o `CHAOS_KILL_COMMAND` do template deve encerrar
  esse mesmo worker. A ausencia dessa assinatura nao autoriza estimar a janela
  de falha: o run e abortado sem avaliar o efeito.
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

Os controles sem falha da complementacao suplementar usam a politica de
preparacao persistida no manifesto: warmup minimo de 180 s, duas janelas
sucessivas nao sobrepostas de 60 s, prazo de preparacao de 600 s e baseline de
180 s. Cada janela precisa passar cobertura e estabilidade para todos os
servicos; falha zera a sequencia de janelas estaveis. A saude e verificada a
cada 15 s durante warmup, janelas e baseline. Esses valores nao alteram os
limiares de variabilidade nem certificam recuperacao apos falha.

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
    cores: 2
    load: 100
    minimum_quota_fraction: 0.80
    calibration_status: candidate
    rationale: "Piso candidato; validar a media da janela fault por servico."
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

No modo train-only com `--continue-after-technical-failure`, um run `aborted`
por recuperacao experimental tardia pode liberar a proxima celula sem
intervencao somente se `operational_recovery.passed` satisfizer a politica
persistida, incluindo 120 s estaveis, e os gates de intervencao, limpeza,
parada da carga, telemetria e read-back historico forem aprovados. O run
permanece `aborted` e inelegivel para treino. Recuperacao operacional reprovada,
como estabilidade abaixo do limite, continua sendo hard stop; nao ha retry de
injecao.

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
