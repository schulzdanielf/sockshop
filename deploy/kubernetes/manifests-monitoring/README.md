# Montoring

First create the monitoring namespace using the `00-monitoring-ns.yaml` file:

`$ kubectl create -f 00-monitoring-ns.yaml`


### Prometheus

To deploy simply apply all the prometheus manifests (01-10) in any order:

`kubectl apply $(ls *-prometheus-*.yaml | awk ' { print " -f " $1 } ')`

The prometheus server will be exposed on Nodeport `31090`.

### Grafana

First apply the grafana manifests from 20 to 22:

`kubectl apply $(ls *-grafana-*.yaml | awk ' { print " -f " $1 }'  | grep -v grafana-import)`

Once the grafana pod is in the Running state apply the `23-grafana-import-dash-batch.yaml` manifest to import the Dashboards:

`kubectl apply -f 23-grafana-import-dash-batch.yaml`

Grafana will be exposed on the NodePort `31300` 

### OpenTelemetry

Deploy the OpenTelemetry Collector manifests from 27 to 29:

`kubectl apply -f 27-otel-collector-configmap.yaml -f 28-otel-collector-dep.yaml -f 29-otel-collector-svc.yaml`

Logs pipeline (single path):

- Use OpenTelemetry Collector as the only log shipper to Loki.
- Promtail manifests (41-43) should not be applied together with OTel logs pipeline to avoid duplicated ingestion.
- The filelog receiver is scoped to `sock-shop` container logs.

Install the OpenTelemetry Operator (required for Node.js auto-instrumentation):

`kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/latest/download/opentelemetry-operator.yaml`

Apply the front-end Instrumentation CR:

`kubectl apply -f 33-otel-instrumentation-frontend.yaml`

### Tempo

Deploy Tempo and Grafana datasources manifests:

`kubectl apply -f 34-tempo-configmap.yaml -f 35-tempo-dep.yaml -f 36-tempo-svc.yaml -f 37-grafana-datasources-configmap.yaml`

Restart Grafana and OTel Collector to load the new datasource and trace exporter:

`kubectl -n monitoring rollout restart deployment/grafana-core deployment/otel-collector`
