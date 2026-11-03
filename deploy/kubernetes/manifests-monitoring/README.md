# Montoring

First create the monitoring namespace using the `00-monitoring-ns.yaml` file:

`$ kubectl create -f 00-monitoring-ns.yaml`


### Prometheus

To deploy simply apply all the prometheus manifests (01-10) in any order:

`kubectl apply $(ls *-prometheus-*.yaml | awk ' { print " -f " $1 } ')`

The legacy Prometheus server is exposed on NodePort `31090`. Prometheus v3 is
the Grafana datasource and is exposed on NodePort `31091`; apply
`30-prometheus-v3-configmap.yaml`, `31-prometheus-v3-dep.yaml`, and
`32-prometheus-v3-svc.yaml`. Prometheus v3 loads the shared rules from
`05-prometheus-alertrules.yaml`.

### Grafana

First apply the grafana manifests from 20 to 22:

`kubectl apply $(ls *-grafana-*.yaml | awk ' { print " -f " $1 }'  | grep -v grafana-import)`

Once the grafana pod is in the Running state apply the `23-grafana-import-dash-batch.yaml` manifest to import the Dashboards:

`kubectl apply -f 23-grafana-import-dash-batch.yaml`

The OpenTelemetry pipeline health dashboard is provisioned from
`48-otel-pipeline-dashboard.yaml` when Grafana is restarted.

Grafana will be exposed on the NodePort `31300` 

### OpenTelemetry

Deploy the OpenTelemetry Collector manifests from 27 to 29:

`kubectl apply -f 27-otel-collector-configmap.yaml -f 28-otel-collector-dep.yaml -f 29-otel-collector-svc.yaml`

Logs pipeline (single path):

- Use the OpenTelemetry Collector as the only log shipper for `sock-shop`.
- Promtail may remain enabled for other namespaces; its pipeline must drop
  `sock-shop` to avoid duplicate ingestion after the OTel path is verified.
- The filelog receiver is scoped to `sock-shop` container logs.
- Kubernetes events from `sock-shop` are watched by a separate single-replica
  Collector with namespace-scoped read-only event permissions.

Apply `41-promtail-configmap.yaml` and `49-otel-k8s-events.yaml` after the
central Collector pipeline is available.

Install the OpenTelemetry Operator (required for Node.js auto-instrumentation):

`kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/latest/download/opentelemetry-operator.yaml`

Apply the front-end Instrumentation CR:

`kubectl apply -f 33-otel-instrumentation-frontend.yaml`

The traces pipeline uses tail sampling: it keeps errors, traces slower than
800 ms, all `rca-platform`/`llm-qwen35` traces, and 5% of other application
traces. `/health` and `/metrics` traces are excluded. Keep the
SDK sampler at 100% so the Collector can make decisions using complete traces.
The tail sampler requires all spans for a trace to reach the same Collector;
the current single-node cluster satisfies this, but a multi-node deployment
needs trace-ID-aware routing to a Collector gateway.

The logs pipeline keeps all logs except `rabbitmq` `info` records, which are
sampled at 10%; `warn` and `error` records and logs from other services are
kept. Sampling uses the observed log timestamp and a fixed hash seed. Filelog
positions and the Loki export queue are persisted under
`/var/lib/otelcol/storage`, so they survive Collector restarts on the same
node. The Loki exporter retries indefinitely while Loki is unavailable; monitor
the queue and Loki's discarded-record counters.

The Tempo exporter has a persistent, retrying queue under
`/var/lib/otelcol/storage`. Its HostPath survives Collector pod restarts on the
same node, but not loss of the node or its disk. Alert on queue growth and
export failures so a prolonged Tempo outage is visible. Spans still inside the
30-second tail-sampling decision window remain in Collector memory and can be
lost on an abrupt process or node failure; the persistent queue protects
batches only after sampling decisions are made.

### Tempo

Deploy Tempo and Grafana datasources manifests:

`kubectl apply -f 34-tempo-configmap.yaml -f 35-tempo-dep.yaml -f 36-tempo-svc.yaml -f 37-grafana-datasources-configmap.yaml`

Tempo retains searchable blocks for seven days. The PVC uses the local
`hostpath` StorageClass and is not expandable in place; verify node disk
capacity before extending retention further. Use an object-storage backend
and per-run trace archives for protection against node or disk loss.
The Tempo Deployment requests 2 GiB of memory and is limited to 4 GiB; monitor
memory and OOM events after changes. Retention also affects PVC disk use, which
is a separate capacity concern.

Restart the monitoring components to load configuration changes:

`kubectl -n monitoring rollout restart deployment/loki deployment/prometheus-v3 deployment/tempo daemonset/otel-collector`

### Historical experiment telemetry

The isolated `monitoring-history` namespace keeps the live backends unchanged.
Prometheus history stores Sock Shop samples on its own PVC for 365 days; Loki
and Tempo store their retained blocks in SeaweedFS through its S3 gateway, with
365-day retention. SeaweedFS's `hostpath` PVC is local to Docker Desktop and is
not disaster recovery.
Prometheus TSDB allows out-of-order/backfill samples within the same 365-day
window so old run windows can be replayed after newer samples have arrived.
The initial PVCs are a pilot sizing baseline, not a capacity guarantee; monitor
volume usage before extending the pilot or relying on it as the only archive.
Historical ingestion is disabled by default. For a test campaign, enable
temporary dual-write after recording its campaign ID:

```sh
./.venv/bin/python -m experiment.platform.tools.history_dual_write \
  enable --campaign-id <campaign-id>
```

The toggle adds a filtered Prometheus remote-write target for `sock-shop`
metrics and enables queued Collector fan-out for its file logs and sampled
traces. Existing live exporters remain unchanged. The campaign validates
read-back from historical Prometheus, Loki, and Tempo through the historical
MCP before evaluating each run. After the final run is validated, disable the
fan-out and pass the same campaign ID:

```sh
./.venv/bin/python -m experiment.platform.tools.history_dual_write \
  disable --campaign-id <campaign-id>
```

Disabling waits for final exporter batches to flush and rolls both monitoring
workloads. Monitor historical PVC and exporter-queue capacity throughout a
long campaign. Do not run the per-run migration Jobs for the same dual-written
runs; they are reserved for backfilling older runs.

Create the namespace and generated S3 credentials, then build the migrator
image used by the bucket bootstrap and per-run migration Jobs:

```sh
kubectl apply -f 50-history-namespace.yaml
python3 bootstrap-history-secret.py
docker build -f experiment/platform/tools/Dockerfile.history-migrator \
  -t sockshop/telemetry-history-migrator:local .
kubectl apply -f 51-seaweedfs-history-pvc.yaml -f 52-seaweedfs-history-dep.yaml \
  -f 53-seaweedfs-history-svc.yaml -f 54-seaweedfs-history-networkpolicy.yaml
kubectl rollout status -n monitoring-history deployment/seaweedfs-history
kubectl apply -f 55-seaweedfs-history-bootstrap-job.yaml
kubectl wait -n monitoring-history --for=condition=complete job/seaweedfs-history-bootstrap --timeout=180s
```

After bucket creation, apply the history backends and Grafana datasources:

```sh
kubectl apply -f 56-prometheus-history-configmap.yaml -f 57-prometheus-history-pvc.yaml \
  -f 58-prometheus-history-dep.yaml -f 59-prometheus-history-svc.yaml \
  -f 60-loki-history-configmap.yaml -f 61-loki-history-pvc.yaml \
  -f 62-loki-history-dep.yaml -f 63-loki-history-svc.yaml \
  -f 64-tempo-history-configmap.yaml -f 65-tempo-history-pvc.yaml \
  -f 66-tempo-history-dep.yaml -f 67-tempo-history-svc.yaml
kubectl apply -f 37-grafana-datasources-configmap.yaml -f 30-prometheus-v3-configmap.yaml \
  -f 27-otel-collector-configmap.yaml
kubectl -n monitoring rollout restart deployment/prometheus-v3 daemonset/otel-collector deployment/grafana-core
```

Per-run Jobs remain available for backfilling older Sock Shop runs. Each Job
writes a parity manifest to the S3-compatible archive; missing or truncated
source data must remain explicitly partial rather than being represented as a
complete snapshot. A migration Job requests 1 GiB and is limited to 4 GiB to
accommodate the high-cardinality metric windows; avoid submitting many run
migrations concurrently on memory-constrained clusters.
Tempo searches use a 5,000-result cap and recursively split saturated time
windows; a one-second interval that still reaches the cap remains partial.
Trace IDs returned as base64 are normalized to OTLP hex IDs, and missing traces
are ingested in batches of 50. Loki parity is rechecked with bounded retries
after ingestion to account for indexing delay; manifests compare counts and
content digests, and record bounded-snapshot parity separately from full-log
coverage.
Tempo manifests separate direct trace-ID/span-count verification from
time-window search-index coverage. OTLP backfills in this stack can be stored
in blocks indexed by ingestion time, so successful direct retrieval does not
guarantee that Grafana search by the original run window will find the trace.
Use the per-run source bundle's trace IDs for direct retrieval, and retain a
partial status until the search-index limitation is resolved or explicitly
accepted.

Submit the 35-run test cohort. The pilot input preserves the two aborted runs and includes
their bounded log snapshots when present:

```sh
./.venv/bin/python -m experiment.platform.tools.submit_history_pilot \
  --ledger experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261028T191703038725Z/runs.csv \
  --submit
```

Each Job archives its source query responses and per-telemetry parity manifest
under `history-runs/<campaign>/<run>/` in the S3-compatible archive. The
Makefile targets that start the experiment platform enable automatic per-run
Job submission by default; direct Uvicorn starts must set
`HISTORY_MIGRATION_ENABLED=true`. Restart the platform after changing this
setting. The platform submits a Job through its configured `kubectl` context;
the Job itself uses only in-cluster services and credentials from the
Kubernetes Secret. The bootstrap-only S3 administrator credential is
separate from the service credential used by Loki, Tempo, and migration Jobs.
A queued Job is not a successful migration: inspect its manifest in the archive
and require `overall_status: complete` before treating a
run as fully preserved.

### Historical MCP for online RCA evaluation

The test-only Skill/RAG workload queries online historical Prometheus, Loki,
and Tempo while the live observability path remains unchanged. Deploy a
separate MCP server wired only to the history backends, then forward it on
loopback:

```sh
make history-mcp-up
make history-mcp-forward
```

The historical endpoint is `http://127.0.0.1:18082/sse`; the existing live MCP
continues to use `http://127.0.0.1:18080/sse`. In dual-write mode, start the
platform with `HISTORY_MIGRATION_ENABLED=false` and
`HISTORY_ARCHIVE_ENABLED=false`; the test-only campaign waits for historical
read-back of metrics, logs, and traces before it evaluates Skill or RAG. The
Skill, multi-agent, knowledge, and campaign evaluators query the historical
MCP only; set `HISTORICAL_MCP_SSE_URL` to override the platform default. The
campaign config pins the same URL into each run spec, and never falls back to
the live MCP. Dual write is validated by read-back and does not dispatch a
migration Job. The
RAG condition still retrieves only from the pinned, existing OpenSearch
corpus; no new run is added to that index.
