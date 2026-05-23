# Chaos Platform (MVP Phase 1)

This module implements a product-oriented chaos engineering platform with:

- Headless API backend (FastAPI)
- Plugin-first orchestration engine (ports/adapters)
- Basic GUI wizard and run console
- SQLite metadata store + filesystem artifact store
- Run timeline events and run comparison endpoint

## Architecture

- Domain core: experiment definitions, run records, timeline events
- Ports: chaos/load/metrics/traces/storage/notification
- Adapters:
  - Chaos: Litmus
  - Load: Locust
  - Metrics: MCP Prometheus
  - Traces: MCP Tempo
- Engine: timeline execution, idempotency key support, persistence, summary generation

## Folder Layout

- backend: API, engine, ports, adapters, plugins, storage
- frontend: web GUI (wizard + monitor + compare)
- contracts: versioned JSON schemas for experiment and plugins
- data: generated at runtime (SQLite DB + object_store)

## Run

1. Install dependencies:

```bash
pip install -r experiment/platform/backend/requirements.txt
```

2. Start API + GUI:

```bash
uvicorn experiment.platform.backend.main:app --host 0.0.0.0 --port 8010 --reload
```

3. Open:

- http://localhost:8010/

## Current MVP Capabilities

- Create/version experiment definitions from GUI wizard
- Start run and monitor timeline events in near real-time (polling)
- Stop run manually (kill-switch path)
- Store run summaries and artifacts (metrics/traces/load/chaos)
- Compare runs by run id
- Persist manual conclusion per run

## Product Evolution Hooks Implemented

- Contracts versioning (`schema_version`)
- Domain events (`run_started`, `warmup_started`, `fault_started`, `run_completed`, etc.)
- Idempotency support with `idempotency_key`
- Approval gate (`requires_approval` -> `pending_approval`)
- Separation between raw artifacts and summarized run data
- Headless API as primary integration surface

## Notes

- Litmus adapter uses `kubectl` and optional manifest apply/delete.
- Locust adapter executes configured command and tracks process by pid.
- MCP adapters expect SSE endpoint configured in experiment observability section.

---

## RAG Pipeline (Phases A & B)

The platform produces a layered representation of every run so that a 4k‑context
LLM (Qwen 14B served at `localhost:8001/generate`) can reason over historical
campaigns without loading raw Prometheus/Tempo payloads.

```
L0  raw artifacts            metrics_raw.json, traces_raw.json, load_summary, chaos_artifacts
                                  │
L1  structured features       run_features.json   ← Phase A
                                  │
L2  RAG‑friendly summary      summary_text + tags ← Phase B
                                  │
L3  retrieval‑augmented prompt   target + top‑K neighbours (≤ 4k tokens)
```

### Phase A — Structured features (L1)

For every completed run the engine builds an L1 feature record with:

| Field | Description |
| --- | --- |
| `verdict` | `resilient` / `degraded_recoverable` / `degraded_persistent` — derived from SLOs |
| `chaos_type` | Litmus engine name (or manifest path) used |
| `slo_thresholds` | The thresholds taken from `spec.analysis.slo` |
| `phase_metrics` | `{metric_id: {baseline,warmup,fault,post: stats}}` (`count,min,max,mean,p95`) |
| `slo_violations` | `[{phase, metric, threshold, observed, delta_pct}]` |
| `recovery_time_seconds` | Seconds after `fault_end` until metric returned to `baseline×(1+tolerance)`, two consecutive samples |
| `recovery_reference_metric` | Which metric drove the recovery measurement |
| `affected_services` | Deduped from trace failure signatures + error spans + hot spans |
| `top_failure_signatures` | Up to 5, ranked by `occurrence_count × max_duration_ms` |
| `rca_hypotheses` | Top 3 HIGH/MEDIUM confidence hypotheses from MCP traces |
| `trace_summary` | `{trace_count, error_trace_count, duration_ms}` |

SLO‑aware verdict logic (`OrchestratorEngine._build_run_features`):

- no SLO violation **and** no error traces → **resilient**
- violations exist **and** `recovery_time ≤ max_recovery_seconds` → **degraded_recoverable**
- violations remain at the end of the post window → **degraded_persistent**

The SLO is configured in the GUI wizard (step 5) and serialized as:

```json
"analysis": {
  "slo": {
    "error_rate_threshold": 0.05,
    "latency_p95_threshold_ms": 800,
    "recovery_tolerance_pct": 0.20,
    "max_recovery_seconds": 600
  }
}
```

By convention, the wizard ships Prometheus queries with ids `error_rate` and
`latency_p95`; those are the metrics consumed by the SLO/recovery logic.

### Phase B — L2 summary + retrieval

For each completed run the engine also generates a deterministic Markdown
summary (~400–700 tokens) plus a controlled‑vocabulary tag set:

```
# Run <id> (experiment <id>)
Verdict: degraded_recoverable | chaos_type: pod-delete | fault_duration: 120s | total: 480s
SLOs: error_rate<=0.050, p95<=800ms, tolerance=0.20, max_recovery=600s

## SLO violations
- [fault] error_rate: observed=0.18 > threshold=0.05 (Δ=260.0%)

## Recovery
Recovered in 47s (reference metric: `error_rate`).

## Per-phase metrics
- error_rate: baseline:mean=0.01 p95=0.02 | fault:mean=0.18 p95=0.21 | post:mean=0.02 p95=0.03
...

## Top failure signatures
- HTTP 500 on `catalogue` (x12, max_duration=842ms)

## RCA hypotheses
- [HIGH] Cascading failure from catalogue → front-end retries exhausted
```

Tag namespace used for retrieval:

| Prefix | Example |
| --- | --- |
| `verdict:` | `verdict:degraded_recoverable` |
| `chaos:` | `chaos:pod-delete` |
| `svc:` | `svc:catalogue` |
| `violation:` | `violation:error_rate@fault` |
| `recovery:` | `recovery:fast` (≤60s), `recovery:medium` (≤300s), `recovery:slow`, `recovery:none` |

Similarity is a deterministic Jaccard over the tag set (+0.1 bonus when
verdicts match). The interface in `analysis/retrieval.py` is intentionally
swap‑in compatible with future sentence‑transformer / Qwen embeddings.

### Storage

New table populated by the engine at run completion:

```sql
CREATE TABLE run_features (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    chaos_type TEXT,
    verdict TEXT,
    slo_violation_count INTEGER NOT NULL DEFAULT 0,
    recovery_time_seconds REAL,
    affected_services_json TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL,
    summary_text TEXT,                      -- L2
    tags_json TEXT NOT NULL DEFAULT '[]',   -- L2 retrieval keys
    created_at TEXT NOT NULL
);
-- Indexes: experiment_id, verdict, chaos_type
```

A copy of the L1 features is also saved as artifact `run_features.json`,
and the L2 record as `run_summary_l2.json`.

### API endpoints (added)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/runs/{run_id}/features` | Full L1 feature record |
| `GET` | `/api/runs/{run_id}/summary` | L2 summary + tags |
| `GET` | `/api/runs/{run_id}/similar?limit=5` | Top‑K similar past runs (tag Jaccard) |
| `GET` | `/api/runs/{run_id}/rag-context?limit=3` | Bundle target + neighbour summaries ready for prompt assembly |
| `GET` | `/api/features?experiment_id=&verdict=&limit=` | List feature index records |

Example RAG context response (truncated):

```json
{
  "target": {
    "run_id": "r-2026-05-22-001",
    "verdict": "degraded_recoverable",
    "tags": ["verdict:degraded_recoverable", "chaos:pod-delete", "svc:catalogue", "violation:error_rate@fault", "recovery:fast"],
    "summary_text": "# Run r-2026-05-22-001 ..."
  },
  "neighbours": [
    {"run_id": "r-...-998", "score": 0.71, "verdict": "degraded_recoverable", "summary_text": "..."},
    {"run_id": "r-...-944", "score": 0.55, "verdict": "degraded_persistent", "summary_text": "..."}
  ]
}
```

### Module layout

```
backend/
├── analysis/
│   ├── __init__.py
│   ├── summary.py        # build_run_summary_l2, build_summary_tags
│   ├── retrieval.py      # score_similarity, rank_similar_runs, rank_by_embedding
│   ├── embeddings.py     # HashingEmbeddingProvider, SentenceTransformerProvider
│   ├── prompt.py         # assemble_prompt (4k‑token RAG)
│   └── llm_client.py     # call_llm, parse_verdict_response, llm_health
├── plugins/
│   ├── mcp_metrics_plugin.py   # + summarize_per_phase, find_recovery_time
│   └── mcp_traces_plugin.py    # + aggregate_failures
├── engine.py             # _build_run_features, SLO-aware verdict, L2 + embedding hooks
└── storage/sqlite_storage.py   # run_features + summary/tag/embedding/llm_analysis methods
```

### Phase C — Semantic retrieval with embeddings

The Jaccard ranker from Phase B is now backed by a vector store and
cosine similarity, while keeping the `analysis.retrieval` interface
untouched (the API decides which mode to use).

**Pluggable embedding providers** (`analysis/embeddings.py`):

| Provider | When used | Notes |
| --- | --- | --- |
| `HashingEmbeddingProvider` (default) | `RAG_EMBEDDING_PROVIDER` unset or `hashing` | Zero‑dependency feature‑hashing vectorizer (signed hashes, 1‑2 grams). 256‑dim by default. Deterministic. |
| `SentenceTransformerProvider` | `RAG_EMBEDDING_PROVIDER=sentence-transformers` | Lazy‑loads `sentence-transformers`; defaults to `all-MiniLM-L6-v2` (384‑dim). Override with `RAG_EMBEDDING_MODEL`. |

Both expose:

```python
class EmbeddingProvider(Protocol):
    name: str            # used to invalidate stale vectors across providers
    dim: int
    def embed(self, text: str) -> np.ndarray: ...
    def embed_batch(self, texts: Sequence[str]) -> np.ndarray: ...
```

Vectors are L2‑normalized at write time so cosine reduces to a dot
product. They are stored as raw float32 bytes in the `run_features`
table via three additional columns:

```sql
ALTER TABLE run_features ADD COLUMN embedding_blob BLOB;
ALTER TABLE run_features ADD COLUMN embedding_provider TEXT;
ALTER TABLE run_features ADD COLUMN embedding_dim INTEGER;
```

(The migration runs automatically on startup for older databases.)

**Engine integration** — after persisting the L2 summary the engine
calls `get_embedding_provider().embed(summary_text)` and stores the
vector. Failures degrade gracefully: retrieval falls back to the tag
Jaccard mode if the target run has no embedding or the cached vector
was produced by a different provider.

**Retrieval API (updated)**:

| Method | Path | Modes |
| --- | --- | --- |
| `GET` | `/api/runs/{id}/similar?mode=embedding\|tags&limit=5` | Default `embedding`, auto‑falls back to `tags` |
| `GET` | `/api/runs/{id}/rag-context?mode=embedding\|tags&limit=3` | Returns `mode` actually used |
| `GET` | `/api/embeddings/info` | Provider name, dim, count of runs pending reindex |
| `POST` | `/api/features/reindex?limit=500` | Recomputes embeddings for runs missing or stale under the current provider; uses `embed_batch` for throughput |

Example response:

```json
{
  "run_id": "r-2026-05-22-001",
  "mode": "embedding",
  "neighbours": [
    {"run_id": "r-...-998", "score": 0.91, "verdict": "degraded_recoverable", "chaos_type": "pod-delete", "tags": ["..."]},
    {"run_id": "r-...-944", "score": 0.74, "verdict": "degraded_persistent", "chaos_type": "pod-delete", "tags": ["..."]}
  ]
}
```

**Switching providers** without losing history:

```bash
# Restart with sentence-transformers
export RAG_EMBEDDING_PROVIDER=sentence-transformers
export RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
uvicorn experiment.platform.backend.main:app --port 8010 --reload

# Re-embed existing summaries under the new provider
curl -X POST 'http://localhost:8010/api/features/reindex?limit=1000'
```

Until reindex finishes, mixed‑provider runs are skipped from semantic
search (the candidate query filters by `embedding_provider`) and the
endpoint falls back to tag mode for the target. After reindex, all
runs become semantically searchable again. The `embedding_provider`
column means embeddings produced by different models never get mixed
in cosine ranking.

### Phase D — LLM verdict with citations

Phase D closes the RAG loop by feeding `/rag-context` into the local
Qwen 14B server (`model/server.py`, default `http://localhost:8001/generate`).
The assembler stays under the 4k‑token window and asks the model to
return a structured JSON answer that always cites the neighbour runs it
used.

**Prompt assembly** (`analysis/prompt.py`):

```python
prompt, meta = assemble_prompt(
    rag_context,          # payload of GET /api/runs/{id}/rag-context
    budget_tokens=3200,   # leaves ~900 tokens of headroom for the answer
    max_neighbours=3,     # K retrieved past runs
    target_share=0.45,    # fraction of the budget for the target summary
)
# meta = {
#   "prompt_tokens_estimate": 362,
#   "target_tokens_estimate": 90,
#   "neighbour_count": 2,
#   "citations": [{"ref": "n1", "run_id": "r-998", ...}, ...],
#   "budget_tokens": 3200,
# }
```

Token accounting uses a cheap chars/4 heuristic — Qwen's tokenizer runs
in a separate process and we just need to stay safely below the 4k
context window. The body is truncated proportionally: target summary
gets `target_share * budget`, the remainder is split evenly across the
embedded neighbours, each prefixed with a `[n1]`/`[n2]` citation tag.

**LLM client** (`analysis/llm_client.py`):

| Function | Purpose |
| --- | --- |
| `call_llm(prompt, max_new_tokens=512, url=LLM_URL)` | POST to `/generate`; raises `LLMClientError` on transport/JSON failure. |
| `parse_verdict_response(text)` | Extracts the first JSON block, validates `verdict ∈ {resilient, degraded_recoverable, degraded_persistent}`, clamps `confidence` to `[0,1]`, returns `parse_error` instead of raising. |
| `llm_health()` | `GET /health` probe; returns `{ok, ...}` with the model name. |

Environment knobs: `LLM_URL` (default `http://localhost:8001/generate`),
`LLM_TIMEOUT` (default `120` seconds).

**Persistence** — `run_features` gains two columns:

```sql
ALTER TABLE run_features ADD COLUMN llm_analysis_json TEXT;
ALTER TABLE run_features ADD COLUMN llm_analysis_at   TEXT;
```

(Idempotent migration on startup, like the embedding columns.)

**Endpoints**:

| Method | Path | Notes |
| --- | --- | --- |
| `GET`  | `/api/llm/health` | Probes the Qwen server. |
| `POST` | `/api/runs/{id}/llm-analysis?limit=3&mode=embedding&max_new_tokens=512&budget_tokens=3200&force=false` | Reads cache unless `force=true`. Builds RAG context, assembles prompt, calls Qwen, parses, persists. Returns `502` if the LLM is unreachable. |
| `GET`  | `/api/runs/{id}/llm-analysis` | Returns the cached analysis or `404`. |

Response shape:

```json
{
  "run_id": "r-2026-05-22-001",
  "heuristic_verdict": "degraded_recoverable",
  "cached": false,
  "analysis": {
    "verdict": "degraded_recoverable",
    "confidence": 0.82,
    "reasoning": "Recovery in 42s matches the pattern of past run [n1] under the same chaos...",
    "citations": ["n1"],
    "follow_ups": ["Add PodDisruptionBudget to catalogue"],
    "parse_error": null,
    "raw_response": "...",
    "rag_mode": "embedding",
    "prompt_meta": {
      "prompt_tokens_estimate": 362,
      "neighbour_count": 2,
      "citations": [{"ref": "n1", "run_id": "r-998", "score": 0.91, "verdict": "degraded_recoverable"}]
    },
    "max_new_tokens": 512
  }
}
```

If the model returns prose without a JSON object, `verdict` stays
`null` and `parse_error` describes what went wrong — the call still
succeeds (the raw response is preserved for offline inspection).

### Phase E — Operator feedback loop

Phase E closes the human‑in‑the‑loop side: the operator can confirm,
correct or flag every run, and the platform persists those labels next
to the heuristic and LLM verdicts so they can later be used as a
labelled evaluation set.

**Storage** — `run_features` gains four columns (idempotent migration):

```sql
ALTER TABLE run_features ADD COLUMN operator_label     TEXT;
ALTER TABLE run_features ADD COLUMN operator_label_at  TEXT;
ALTER TABLE run_features ADD COLUMN operator_label_by  TEXT;
ALTER TABLE run_features ADD COLUMN operator_note      TEXT;
```

Allowed labels: `resilient`, `degraded_recoverable`, `degraded_persistent`, `unsure`.

**Endpoints**:

| Method | Path | Notes |
| --- | --- | --- |
| `GET`  | `/api/runs/{id}/verdicts` | Consolidated view: heuristic, LLM verdict + confidence, operator label, plus `agreement`/`disagreement` booleans. |
| `GET`  | `/api/verdicts?experiment_id=&disagreement_only=&limit=` | Same shape, list form. `disagreement_only=true` keeps only runs where at least two of the three verdicts differ. (Path lives under `/api/verdicts` to avoid colliding with `/api/runs/{id}/...`.) |
| `POST` | `/api/runs/{id}/operator-label` | Body `{label, by?, note?}`. 404 if the run has no `run_features` yet. |

Response example:

```json
{
  "run_id": "r-2026-05-22-001",
  "heuristic_verdict": "degraded_recoverable",
  "llm_verdict": "degraded_persistent",
  "llm_confidence": 0.7,
  "operator_label": "degraded_recoverable",
  "operator_label_by": "alice",
  "agreement": false,
  "disagreement": true
}
```

`agreement` is `true` when every available verdict is identical (a run
with only the heuristic populated counts as agreement); `disagreement`
is its negation, or `null` when nothing has been recorded yet.

**GUI** — the *Histórico de Runs* table now shows three verdict columns
(`Heur.` / `🤖 LLM` / `👤 Op.`), a ⚠ marker on runs where they diverge,
and:

* a `<select>` per row that PATCHes the operator label inline;
* a `🤖` button that triggers `POST /api/runs/{id}/llm-analysis?force=true`
  on demand (the engine does not call the LLM automatically);
* a "só divergências" checkbox that toggles `disagreement_only=true`
  on `/api/verdicts` and filters the table.

This setup gives a cheap path to bootstrap a labelled dataset for the
future Phase F/G evaluation pipelines without ever forcing the operator
to leave the platform UI.

### Roadmap

- **Phase F** — optional vector index (sqlite‑vss / faiss) once the run corpus grows past a few thousand records and the linear cosine scan becomes a bottleneck.
- **Phase G** — automatic post‑run LLM analysis hook in the engine (currently on‑demand via `POST /api/runs/{id}/llm-analysis` or the GUI 🤖 button).
- **Phase H** — offline evaluation script that uses `operator_label` as ground truth and reports heuristic vs LLM accuracy/confusion matrices.

---

## Phase F1 — Trace‑aware retrieval & temporal feature engineering

Phases A–E gave us a deterministic L1 → L2 → vector pipeline with a
semantic RAG retriever. Two systematic blind spots remained:

1. **Two runs with identical aggregate stats but different propagation
   shapes** (e.g. `front-end → orders → carts` vs `front-end → carts`)
   were collapsed together by the cosine ranker.
2. **The dynamics of a fault** — how fast it ramps, when it peaks, how
   the recovery curve looks — were thrown away by per‑phase
   min/max/mean aggregates.

Phase F1 closes both gaps without changing the existing LLM contract.

### Temporal features

A new pure module — [experiment/platform/backend/analysis/temporal.py](experiment/platform/backend/analysis/temporal.py)
— derives per‑metric descriptors from the raw Prometheus range‑query
points and persists them under `features.temporal_features`:

| Field                              | Meaning                                                       |
| ---------------------------------- | ------------------------------------------------------------- |
| `time_to_first_violation_s`        | Seconds between fault start and first point above the SLO/baseline threshold. |
| `time_to_peak_s` / `peak_value`    | When and how high the metric peaked inside the fault window.  |
| `max_abs_derivative`               | Largest `|dv/dt|` between consecutive points (proxy for "violence"). |
| `coefficient_of_variation_fault`   | `stddev/mean` during the fault — instability indicator.       |
| `recovery_shape`                   | One of `step`, `exponential`, `oscillating`, `persistent`, `unknown`. |
| `overall_recovery_shape`           | Worst shape across all metrics (`persistent > oscillating > exponential > step`). |

`recovery_shape` is classified on the post‑fault window using the
fraction of time above target, the number of sign changes of the first
derivative and how quickly the series collapses back to baseline. This
turns *"recovered in 90s"* into *"recovered in 90s with an oscillating
curve"* — a much richer signal for the operator and the LLM.

### Propagation graph (trace‑aware retrieval)

`MCPTempoTracesPlugin.build_propagation_graph(raw)` aggregates the
per‑trace `dependency_map` returned by `tempo_analyze_trace` into a
single service‑level graph for the run, persisted under
`features.propagation_graph`:

```json
{
  "nodes": ["front-end", "orders", "carts"],
  "edges": [
    {"from": "orders", "to": "carts", "trace_count": 12, "error_count": 8},
    {"from": "front-end", "to": "orders", "trace_count": 12, "error_count": 5}
  ],
  "cascade_order": [
    {"service": "orders", "t_offset_s": 0.0},
    {"service": "carts",  "t_offset_s": 1.234}
  ],
  "edge_count": 2,
  "error_edge_count": 2,
  "trace_count": 12
}
```

To support the cascade ordering, `collect_window` now also preserves
`startTimeUnixNano`, `rootServiceName` and `durationMs` for every
analysed trace.

### Hybrid retrieval mode

`analysis/retrieval.py` gains two new helpers:

* `score_graph_similarity(qg, cg)` — returns a multi‑component
  similarity dict containing `nodes_jaccard`, `edges_jaccard`,
  `cascade_overlap` (order‑aware via Kendall‑tau on the common
  services) and the final blended `score`.
* `rank_hybrid(query_vec, query_graph, candidates, weights)` —
  combines L2‑summary cosine similarity with `score_graph_similarity`.
  Default weights are `0.6 semantic / 0.4 graph`. Weights are
  renormalised per candidate so a missing component never penalises the
  rest of the score.

Both `GET /api/runs/{id}/similar?mode=hybrid` and
`GET /api/runs/{id}/rag-context?mode=hybrid` now expose this ranker.
The hybrid `/similar` response carries the extra fields
`semantic_score`, `graph_score`, `cascade_overlap_services` and
`graph_detail` to keep the ranking explainable in the GUI/notebook.

To make this efficient the storage helper
`iter_run_embeddings(..., include_features=True)` returns
`features_json` in the same query that fetches the embedding blob, so
the hybrid path still costs a single SQL round‑trip.

### Updated L2 summary & tag namespace

`build_run_summary_l2` now emits two extra sections:

```text
## Temporal dynamics
Overall recovery shape: **oscillating**
- error_rate: ttfv=12s | ttp=45s | |dv/dt|max=0.0134 | cv=0.62 | shape=oscillating

## Propagation graph
Cascade: orders(+0.00s) -> carts(+1.23s)
Edges: 2 total, 2 with errors (top 2 below)
- orders → carts (traces=12, errors=8) ❗
- front-end → orders (traces=12, errors=5) ❗
```

`build_summary_tags` adds three new controlled‑vocabulary tags so the
existing Jaccard retriever also benefits from the new signals:

| Tag           | Example                              |
| ------------- | ------------------------------------ |
| `cascade:`    | `cascade:orders->carts->shipping`    |
| `edges:`      | `edges:7`                            |
| `shape:`      | `shape:oscillating`                  |

These tags are consumed transparently by `mode=tags`, so even
deployments without embeddings benefit from Phase F1.

### Engine integration

`engine._build_run_features` calls `compute_temporal_features` and
`MCPTempoTracesPlugin.build_propagation_graph` in best‑effort mode
right after the existing per‑phase aggregation. Failures are logged as
events but never abort the run; the new fields default to empty dicts
so older replays remain compatible.

### Module layout (additions)

```
experiment/platform/backend/analysis/
├── temporal.py        # NEW — Phase F1 temporal features
├── retrieval.py       # + score_graph_similarity, rank_hybrid
└── summary.py         # + Temporal/Propagation sections & tags
experiment/platform/backend/plugins/
└── mcp_traces_plugin.py  # + build_propagation_graph,
                          #   preserves trace start_time_unix_nano
experiment/platform/backend/storage/
└── sqlite_storage.py  # iter_run_embeddings(include_features=True)
experiment/platform/backend/api.py  # mode=hybrid on /similar and /rag-context
```

No new dependencies and no schema migration: the new fields ride
inside `run_features.features_json`.


---

## Phase F2 — Persistent system context (system cards)

Every LLM verdict was being assembled with only the *dynamic* slice of
the incident (target summary + retrieved neighbours). The model had to
re‑infer the *static* moldura — service roles, stacks, dependencies,
allowed verdict vocabulary — on every call, and frequently invented
service names that did not exist in the system under test.

Phase F2 introduces a **system card**: a static, versioned YAML
document describing the target application that is injected once into
the LLM system prompt. The card lives outside the RAG context so it
never competes with retrieved runs for token budget, and the static
prefix is cacheable by the LLM runtime (Qwen / vLLM KV cache).

### What goes in a card

- Application description and platform (orchestrator, namespace, mesh).
- Per‑service block with: short `role`, `stack`, `type`
  (stateless / stateful / worker), `upstream`, `downstream`,
  `storage`, plus optional operational `notes`.
- **Controlled vocabulary** for the verdict: `allowed_rca_targets`
  (exact list of service names the LLM may use in `rca`) and
  `allowed_fault_categories`.

The Sock Shop card lives at
[experiment/platform/data/system_cards/sock-shop.yaml](experiment/platform/data/system_cards/sock-shop.yaml)
(13 services, ~3.9 KB → ~980 tokens rendered).

### Wiring

```
experiment/platform/backend/analysis/system_card.py  # loader + renderer
experiment/platform/backend/analysis/prompt.py       # injects <system_card>
                                                     # block right after
                                                     # the system role
experiment/platform/backend/api.py                   # /api/system-cards
                                                     # and system_id param
                                                     # on llm-analysis
experiment/platform/data/system_cards/sock-shop.yaml # the card itself
```

`assemble_prompt(..., system_id="sock-shop")` now produces a prompt of
the form:

```
<system>You are a senior resilience engineer ...</system>

<system_card>
## System under test: sock-shop (card v2026-05-23)
...
### Services (role, stack, dependencies)
- orders [java/spring-boot, stateful]
    role: Order orchestration. On checkout, fans out to ...
    upstream: front-end
    downstream: carts, payment, shipping, user, orders-db
    note: Highest fan-out service in the system ...
...
### Allowed values for the verdict `rca` field
Reply MUST use exactly one of: front-end, catalogue, ..., unknown.
</system_card>

<context> ... target run + retrieved neighbours ... </context>

<task> ... JSON schema ... </task>
```

Missing card files are tolerated silently — the prompt is built without
the `<system_card>` block, matching pre‑F2 behaviour.

### API

- `GET /api/system-cards` — list known card ids.
- `GET /api/system-cards/{system_id}` — return parsed metadata
  (`version`, `service_count`, `allowed_rca_targets`) plus the rendered
  text block that will be inlined in the prompt.
- `POST /api/runs/{run_id}/llm-analysis?system_id=<id>` — new optional
  query parameter; defaults to `sock-shop`.

`prompt_meta` on the persisted verdict now reports `system_card_id` and
`system_card_tokens_estimate` so ablations can be reproduced.

### How to extend

1. Drop a new file `data/system_cards/<system_id>.yaml` following the
   Sock Shop schema.
2. Call `/api/runs/{id}/llm-analysis?system_id=<id>`.
3. Validate via `/api/system-cards/<id>` that the rendered text fits
   your token budget (target ≤ 1200 tokens; the loader is cached per
   process so the cost is paid once).

Cards are deliberately hand‑written (or semi‑generated from K8s
manifests + baseline `propagation_graph`) rather than learned: the
goal is auditability and version control, not adaptation.

