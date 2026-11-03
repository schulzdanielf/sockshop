# Load .env file if present (never committed — see .env.example)
-include .env
export

.PHONY: gen-complete-demo
TOOLS_BIN ?= $(CURDIR)/.tools/bin
HELM ?= $(TOOLS_BIN)/helm
LITMUS_OPERATOR_URL ?= https://litmuschaos.github.io/litmus/litmus-operator-latest.yaml
LITMUS_ADMIN_RBAC_URL ?= https://litmuschaos.github.io/litmus/litmus-admin-rbac.yaml
LITMUS_POD_DELETE_URL ?= https://hub.litmuschaos.io/api/chaos/master?file=faults/kubernetes/pod-delete/fault.yaml
LITMUS_HELM_REPO_URL ?= https://litmuschaos.github.io/litmus-helm
LITMUS_K8S_CHAOS_CHART ?= litmuschaos/kubernetes-chaos
LITMUS_K8S_CHAOS_RELEASE ?= k8s-chaos
LITMUS_K8S_CHAOS_NAMESPACE ?= sock-shop
LITMUS_CHAOS_CENTER_RELEASE ?= chaos-center
LITMUS_CHAOS_CENTER_NAMESPACE ?= litmus
LITMUS_CHAOS_CENTER_FRONTEND_SERVICE ?= $(LITMUS_CHAOS_CENTER_RELEASE)-litmus-frontend-service
LITMUS_CHAOS_CENTER_FRONTEND_PORT ?= 9092
LITMUS_CHAOS_CENTER_SERVER_SERVICE ?= $(LITMUS_CHAOS_CENTER_RELEASE)-litmus-server-service
LITMUS_CHAOS_CENTER_SERVER_PORT ?= 9002
LITMUS_CHAOS_CENTER_SERVER_WS_PORT ?= 8000
PORT_FORWARD_CHECK_HOST ?= $(shell tailscale ip -4 2>/dev/null | head -n1 || ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $$7; exit}')
PORT_FORWARD_CHECK_PORTS ?= 8080 3000 9090 16686 8089 9091

FRONT_END_IMAGE ?= weaveworksdemos/front-end:node18-otel

# ── LLM model server ──────────────────────────────────────────────────────────
# Node IP for the otel-collector NodePort (port 30318).
# Override on the command line: make model-serve NODE_IP=<your-node-ip>
NODE_IP         ?= $(shell kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)
OTEL_ENDPOINT   ?= http://localhost:4318
DEPLOYMENT_ENV  ?= local
MODEL_HOST      ?= 127.0.0.1
MODEL_PORT      ?= 8001
MODEL_PROXY_HOST ?= 127.0.0.1
MODEL_PROXY_PORT ?= 8011
QWEN35_LAUNCHER ?= $(HOME)/.local/bin/qwen35-grafana
QWEN35_MCP_SERVERS_CONFIG ?= $(CURDIR)/model/qwen35_grafana_mcp.json
QWEN35_MODEL ?= qwen3.5-9b-grafana
QWEN35_UPSTREAM_URL ?= http://127.0.0.1:$(MODEL_PORT)
QWEN35_LLM_URL ?= http://$(MODEL_PROXY_HOST):$(MODEL_PROXY_PORT)/v1/chat/completions
QWEN35_PROXY_HEALTH_URL ?= http://$(MODEL_PROXY_HOST):$(MODEL_PROXY_PORT)/health
QWEN35_OTEL_ENDPOINT ?= http://127.0.0.1:4318
QWEN35_AUDIT_PATH ?= $(HOME)/.local/share/qwen35-grafana/qwen35-audit.jsonl
PLATFORM_AUDIT_PATH ?= $(HOME)/.local/share/qwen35-grafana/platform-audit.jsonl
GRAFANA_MCP_IMAGE ?= grafana/mcp-grafana@sha256:2ed63c4d9d67557c8ea6cc6988743ddd9008e248bd9842e760fec378a0b61062
GRAFANA_MCP_CONTAINER ?= qwen-grafana-mcp
GRAFANA_MCP_PORT ?= 18081
GRAFANA_MCP_SSE_URL ?= http://127.0.0.1:$(GRAFANA_MCP_PORT)/sse
GRAFANA_MCP_TIMEOUT_SECONDS ?= 30
GRAFANA_MCP_ENABLED_TOOLS ?= search,dashboard,datasource,prometheus,loki,tempo,navigation

# ── Full experiment orchestration (make experiment-run) ────────────────────────
# Interpreter used to launch the model server, platform and harness.
PYTHON               ?= .venv/bin/python
# Health endpoints used to decide whether a service is already up.
LLM_HEALTH_URL       ?= http://127.0.0.1:$(MODEL_PORT)/health
PLATFORM_PORT        ?= 8010
PLATFORM_BASE        ?= http://127.0.0.1:$(PLATFORM_PORT)
PLATFORM_HEALTH_URL  ?= $(PLATFORM_BASE)/api/health
# The platform runs in WSL but is opened from the notebook/browser host.
# Override with 127.0.0.1 to restrict it to the WSL loopback interface.
PLATFORM_BIND_HOST   ?= 0.0.0.0
HISTORY_MIGRATION_ENABLED ?= true
HISTORY_ARCHIVE_ENABLED ?= false
HISTORICAL_MCP_SSE_URL ?= http://127.0.0.1:18082/sse
OPENSEARCH_URL ?= http://127.0.0.1:9200
OPENSEARCH_HEALTH_URL ?= $(OPENSEARCH_URL)/_cluster/health
OPENSEARCH_COMPOSE_FILE ?= experiment/eval/memory_hog/opensearch-compose.yml
# Local port the MCP observability server is port-forwarded to (see `port-forward`).
MCP_LOCAL_PORT       ?= 18080
# Experiment harness wiring.
EXPERIMENT_CONFIG    ?= experiment/eval/memory_hog/config.yaml
EXPERIMENT_RUNNER    ?= experiment/eval/memory_hog/runner.py
# Extra args forwarded to the harness, e.g. RUN_ARGS="--phase chaos --retry-failed".
RUN_ARGS             ?=
# Readiness polling. The model load (ExLlamaV2) can take minutes, hence the
# larger retry budget; the platform comes up in seconds.
HEALTH_DELAY         ?= 5
HEALTH_RETRIES       ?= 60
MODEL_BOOT_RETRIES   ?= 120
SMOKE_AGENT_ARGS     ?=
ENV_ASSESSOR_ARGS    ?=
AGENT_CONSOLE_ARGS   ?=


.PHONY: front-end-build
front-end-build:
	docker build -t $(FRONT_END_IMAGE) deploy/kubernetes/front-end/

.PHONY: front-end-load
front-end-load: front-end-build
	# Carrega a imagem no cluster (Docker Desktop / kind)
	docker save $(FRONT_END_IMAGE) | \
	  kubectl debug -n sock-shop -it --image=alpine --target=front-end -- sh -c "cat > /dev/null" 2>/dev/null || true
	@echo "Se estiver usando kind: kind load docker-image $(FRONT_END_IMAGE)"
	@echo "Se estiver usando Docker Desktop: a imagem já está disponível no cluster."

gen-complete-demo:
	make -C deploy/kubernetes docker-gen-complete-demo

.PHONY: check-generated-files
check-generated-files:
	make -C deploy/kubernetes docker-check-complete-demo

.PHONY: app-up
app-up:
	kubectl apply -f deploy/kubernetes/manifests

.PHONY: app-down
app-down:
	kubectl delete -f deploy/kubernetes/manifests --ignore-not-found

.PHONY: observability-up
observability-up:
	kubectl apply -f deploy/kubernetes/manifests-monitoring/00-monitoring-ns.yaml
	kubectl apply -f deploy/kubernetes/manifests-monitoring
	kubectl apply -f deploy/kubernetes/manifests-jaeger/jaeger.yaml
	kubectl apply -f deploy/kubernetes/manifests-jaeger/catalogue-dep.yaml
	kubectl apply -f deploy/kubernetes/manifests-jaeger/user-dep.yaml
	kubectl apply -f deploy/kubernetes/manifests-jaeger/payment-dep.yaml
	kubectl apply -f deploy/kubernetes/manifests-logging

.PHONY: observability-down
observability-down:
	kubectl delete -f deploy/kubernetes/manifests-logging --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-jaeger/payment-dep.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-jaeger/user-dep.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-jaeger/catalogue-dep.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-jaeger/jaeger.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-monitoring --ignore-not-found

.PHONY: history-mcp-up
history-mcp-up:
	kubectl apply -f deploy/kubernetes/manifests-monitoring/68-history-observability-mcp-configmap.yaml \
	  -f deploy/kubernetes/manifests-monitoring/69-history-observability-mcp-dep.yaml \
	  -f deploy/kubernetes/manifests-monitoring/70-history-observability-mcp-svc.yaml
	kubectl rollout status -n monitoring-history deployment/history-observability-mcp

.PHONY: history-mcp-forward
history-mcp-forward:
	kubectl -n monitoring-history port-forward svc/history-observability-mcp 18082:8000

.PHONY: loadtest-up
loadtest-up:
	kubectl apply -f deploy/kubernetes/manifests-loadtest/loadtest-configmap.yaml

.PHONY: loadtest-down
loadtest-down:
	kubectl delete -f deploy/kubernetes/manifests-loadtest/loadtest-configmap.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-loadtest/loadtest-dep.yaml --ignore-not-found

.PHONY: chaos-install
chaos-install:
	kubectl apply -f $(LITMUS_OPERATOR_URL)
	kubectl apply -f $(LITMUS_ADMIN_RBAC_URL)
	kubectl apply -n sock-shop -f $(LITMUS_POD_DELETE_URL)
	kubectl apply -f deploy/kubernetes/manifests-chaos/pod-delete-rbac.yaml

# Network chaos (pod-network-latency / pod-network-loss) needs the `sch_netem`
# qdisc module in the (shared) node kernel. On docker-desktop / WSL2 the module
# ships with the kernel but is not auto-loaded; run this once per host boot
# BEFORE any network-* chaos. NOTE: non-persistent — re-run after a WSL restart
# (or add `sch_netem` to /etc/modules-load.d/ to make it permanent).
.PHONY: chaos-enable-netem
chaos-enable-netem:
	sudo modprobe sch_netem
	@lsmod | grep -q '^sch_netem' && echo "sch_netem loaded OK" || (echo "sch_netem NOT loaded" && exit 1)

.PHONY: chaos-run-pod-delete
chaos-run-pod-delete:
	kubectl apply -f deploy/kubernetes/manifests-chaos/pod-delete-engine.yaml

.PHONY: chaos-run-pod-delete-prom-probe
chaos-run-pod-delete-prom-probe:
	kubectl apply -f deploy/kubernetes/manifests-chaos/pod-delete-engine-prom-probe.yaml

.PHONY: chaos-run-catalogue-cpu-hog
chaos-run-catalogue-cpu-hog:
	kubectl apply -f deploy/kubernetes/manifests-chaos/catalogue-cpu-hog.yaml

.PHONY: chaos-clean-catalogue-cpu-hog
chaos-clean-catalogue-cpu-hog:
	kubectl delete workflow -n litmus catalogue-cpu-hog --ignore-not-found
	kubectl delete chaosengine,chaosresult -n litmus -l workflow_name=catalogue-cpu-hog --ignore-not-found

.PHONY: chaos-status
chaos-status:
	kubectl get pods -n litmus
	kubectl get chaosexperiments,chaosengines,chaosresults -n sock-shop

.PHONY: chaos-down
chaos-down:
	kubectl delete -f deploy/kubernetes/manifests-chaos/pod-delete-engine.yaml --ignore-not-found
	kubectl delete -f deploy/kubernetes/manifests-chaos/pod-delete-rbac.yaml --ignore-not-found
	kubectl delete -n sock-shop -f $(LITMUS_POD_DELETE_URL) --ignore-not-found
	kubectl delete -f $(LITMUS_ADMIN_RBAC_URL) --ignore-not-found
	kubectl delete -f $(LITMUS_OPERATOR_URL) --ignore-not-found

.PHONY: chaos-up
chaos-up: chaos-install chaos-run-pod-delete

.PHONY: chaos-experiments-install
chaos-experiments-install: chaos-center-bootstrap-helm
	kubectl delete -n $(LITMUS_K8S_CHAOS_NAMESPACE) -f $(LITMUS_POD_DELETE_URL) --ignore-not-found
	$(HELM) repo add litmuschaos $(LITMUS_HELM_REPO_URL)
	$(HELM) repo update
	$(HELM) upgrade --install $(LITMUS_K8S_CHAOS_RELEASE) $(LITMUS_K8S_CHAOS_CHART) --namespace $(LITMUS_K8S_CHAOS_NAMESPACE) --create-namespace

.PHONY: chaos-experiments-status
chaos-experiments-status:
	kubectl get chaosexperiment -n $(LITMUS_K8S_CHAOS_NAMESPACE)

.PHONY: chaos-experiments-down
chaos-experiments-down:
	$(HELM) uninstall $(LITMUS_K8S_CHAOS_RELEASE) -n $(LITMUS_K8S_CHAOS_NAMESPACE) || true

.PHONY: chaos-center-bootstrap-helm
chaos-center-bootstrap-helm:
	mkdir -p $(TOOLS_BIN)
	test -x $(HELM) || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 -o $(TOOLS_BIN)/get-helm-3
	test -x $(HELM) || chmod 700 $(TOOLS_BIN)/get-helm-3
	test -x $(HELM) || PATH=$(TOOLS_BIN):$$PATH USE_SUDO=false HELM_INSTALL_DIR=$(TOOLS_BIN) $(TOOLS_BIN)/get-helm-3

.PHONY: chaos-center-install
chaos-center-install: chaos-center-bootstrap-helm
	$(HELM) repo add litmuschaos $(LITMUS_HELM_REPO_URL)
	$(HELM) repo update
	$(HELM) upgrade --install $(LITMUS_CHAOS_CENTER_RELEASE) litmuschaos/litmus --namespace $(LITMUS_CHAOS_CENTER_NAMESPACE) --create-namespace

.PHONY: chaos-center-status
chaos-center-status:
	kubectl get pods,svc -n $(LITMUS_CHAOS_CENTER_NAMESPACE)

.PHONY: chaos-center-port-forward
chaos-center-port-forward:
	nohup kubectl port-forward -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_FRONTEND_SERVICE) $(LITMUS_CHAOS_CENTER_FRONTEND_PORT):9091 >/tmp/pf-chaos-center.log 2>&1 &
	nohup kubectl port-forward -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_PORT):9002 >/tmp/pf-chaos-center-server.log 2>&1 &
	nohup kubectl port-forward -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_WS_PORT):8000 >/tmp/pf-chaos-center-server-ws.log 2>&1 &

.PHONY: chaos-center-stop-port-forward
chaos-center-stop-port-forward:
	pkill -f "kubectl port-forward -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_FRONTEND_SERVICE)" || true
	pkill -f "kubectl port-forward -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE)" || true

.PHONY: chaos-center-down
chaos-center-down:
	$(HELM) uninstall $(LITMUS_CHAOS_CENTER_RELEASE) -n $(LITMUS_CHAOS_CENTER_NAMESPACE) || true

.PHONY: chaos-center-up
chaos-center-up: chaos-center-install chaos-center-port-forward

.PHONY: port-forward
port-forward:
	nohup kubectl port-forward --address 0.0.0.0 -n sock-shop svc/front-end 8082:80 >/tmp/pf-front-end.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/grafana 3000:80 >/tmp/pf-grafana.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/prometheus 9090:9090 >/tmp/pf-prometheus.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/prometheus-v3 9091:9090 >/tmp/pf-prometheus-v3.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n jaeger svc/jaeger-query 16686:80 >/tmp/pf-jaeger.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n kube-system svc/kibana 5602:5601 >/tmp/pf-kibana.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n loadtest svc/locust-web 8089:8089 >/tmp/pf-locust.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_FRONTEND_SERVICE) $(LITMUS_CHAOS_CENTER_FRONTEND_PORT):9091 >/tmp/pf-chaos-center.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_PORT):9002 >/tmp/pf-chaos-center-server.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_WS_PORT):8000 >/tmp/pf-chaos-center-server-ws.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n mcp-server svc/mcp-observability 18080:8000 >/tmp/pf-mcp-observability.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/loki 3100:3100 >/tmp/pf-loki.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/tempo 3200:3200 >/tmp/pf-tempo.log 2>&1 &
	nohup kubectl port-forward --address 127.0.0.1 -n monitoring svc/otel-collector 4318:4318 >/tmp/pf-otel-collector.log 2>&1 &
	@if curl -sf --max-time 3 http://127.0.0.1:$(MODEL_PORT)/health >/dev/null 2>&1; then \
		echo ">> Qwen3.5 llama-server: http://127.0.0.1:$(MODEL_PORT)"; \
	else \
		echo ">> Qwen3.5 llama-server is not running; start it with 'make model'."; \
	fi
.PHONY: port-forward-stop
port-forward-stop:
	pkill -f "^kubectl port-forward .* -n sock-shop svc/front-end" || true
	pkill -f "^kubectl port-forward .* -n monitoring svc/grafana" || true
	pkill -f "^kubectl port-forward .* -n monitoring svc/prometheus" || true
	pkill -f "^kubectl port-forward .* -n jaeger svc/jaeger-query" || true
	pkill -f "^kubectl port-forward .* -n kube-system svc/kibana" || true
	pkill -f "^kubectl port-forward .* -n loadtest svc/locust-web" || true
	pkill -f "^kubectl port-forward .* -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_FRONTEND_SERVICE)" || true
	pkill -f "^kubectl port-forward .* -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE)" || true
	pkill -f "^kubectl port-forward .* -n mcp-server svc/mcp-observability" || true
	pkill -f "^kubectl port-forward .* -n monitoring svc/loki" || true
	pkill -f "^kubectl port-forward .* -n monitoring svc/otel-collector" || true

.PHONY: port-forward-check
port-forward-check:
	@test -n "$(PORT_FORWARD_CHECK_HOST)" || (echo "PORT_FORWARD_CHECK_HOST is empty" >&2; exit 1)
	@for p in $(PORT_FORWARD_CHECK_PORTS); do \
		echo "== $(PORT_FORWARD_CHECK_HOST):$$p =="; \
		curl -sS -I --max-time 3 http://$(PORT_FORWARD_CHECK_HOST):$$p | head -n 1 || echo "(sem resposta)"; \
		echo; \
	done

.PHONY: apply-loadtest
apply-loadtest:
	kubectl apply -f deploy/kubernetes/manifests-loadtest/loadtest-configmap.yaml
	kubectl rollout restart deployment/locust-web -n loadtest
	kubectl rollout status deployment/locust-web -n loadtest

.PHONY: cluster-down
cluster-down: observability-stop-port-forward loadtest-down observability-down app-down

.PHONY: cluster-up

.PHONY: metrics-agent-smoke
metrics-agent-smoke:
	$(PYTHON) -m experiment.platform.backend.metrics_smoke_agent $(SMOKE_AGENT_ARGS)

.PHONY: environment-assessor
environment-assessor:
	$(PYTHON) -m experiment.platform.backend.environment_assessor $(ENV_ASSESSOR_ARGS)

.PHONY: agent-console
agent-console:
	$(PYTHON) -m experiment.platform.backend.agent_console $(AGENT_CONSOLE_ARGS)
cluster-up: app-up observability-up loadtest-up

.PHONY: cluster-restart
cluster-restart: cluster-down cluster-up port-forward

.PHONY: model-serve
## Start the isolated Qwen3.5 llama.cpp server in the foreground.
model-serve:
	QWEN35_MCP_SERVERS_CONFIG=$(QWEN35_MCP_SERVERS_CONFIG) \
	QWEN35_HOST=$(MODEL_HOST) QWEN35_PORT=$(MODEL_PORT) $(QWEN35_LAUNCHER)

.PHONY: model-otel-proxy-serve
## Start the Qwen3.5 OpenTelemetry proxy in the foreground.
model-otel-proxy-serve:
	OTEL_SERVICE_NAME=llm-qwen35 \
	OTEL_EXPORTER_OTLP_ENDPOINT=$(QWEN35_OTEL_ENDPOINT) \
	OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0 \
	DEPLOYMENT_ENV=$(DEPLOYMENT_ENV) \
	LLM_MODEL=$(QWEN35_MODEL) \
	QWEN35_UPSTREAM_URL=$(QWEN35_UPSTREAM_URL) \
	LLM_IO_AUDIT_PATH="$(QWEN35_AUDIT_PATH)" \
	$(PYTHON) -m uvicorn model.qwen35_otel_proxy:app \
		--host $(MODEL_PROXY_HOST) --port $(MODEL_PROXY_PORT)

.PHONY: model
## Ensure the Qwen3.5 model server and OTel proxy are running in the background.
model: ensure-model

.PHONY: experiment-platform-up
## Start the experiment platform (FastAPI backend + GUI) on http://localhost:$(PLATFORM_PORT).
experiment-platform-up: ensure-model ensure-grafana-mcp ensure-opensearch
	HISTORY_MIGRATION_ENABLED=$(HISTORY_MIGRATION_ENABLED) \
	HISTORY_ARCHIVE_ENABLED=$(HISTORY_ARCHIVE_ENABLED) \
	HISTORICAL_MCP_SSE_URL=$(HISTORICAL_MCP_SSE_URL) \
	LLM_URL=$(QWEN35_LLM_URL) \
	LLM_MODEL=$(QWEN35_MODEL) \
	GRAFANA_MCP_SSE_URL=$(GRAFANA_MCP_SSE_URL) \
	GRAFANA_MCP_TIMEOUT_SECONDS=$(GRAFANA_MCP_TIMEOUT_SECONDS) \
	LLM_IO_AUDIT_PATH="$(PLATFORM_AUDIT_PATH)" \
	OTEL_SERVICE_NAME=rca-platform \
	OTEL_EXPORTER_OTLP_ENDPOINT=$(QWEN35_OTEL_ENDPOINT) \
	OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0 \
	DEPLOYMENT_ENV=$(DEPLOYMENT_ENV) \
	.venv/bin/python -m uvicorn experiment.platform.backend.main:app \
		--host $(PLATFORM_BIND_HOST) --port $(PLATFORM_PORT) --reload

.PHONY: opensearch-up
## Start the persistent local OpenSearch service used by RAG evaluations.
opensearch-up:
	docker compose -f $(OPENSEARCH_COMPOSE_FILE) up -d opensearch

.PHONY: ensure-opensearch
## Ensure the local OpenSearch service is healthy, starting it if necessary.
ensure-opensearch:
	@if curl -sf --max-time 3 $(OPENSEARCH_HEALTH_URL) >/dev/null 2>&1; then \
		echo ">> OpenSearch already healthy at $(OPENSEARCH_URL)."; \
	else \
		echo ">> OpenSearch down — starting the evaluation service..."; \
		$(MAKE) opensearch-up; \
		i=0; \
		until curl -sf --max-time 3 $(OPENSEARCH_HEALTH_URL) >/dev/null 2>&1; do \
			i=$$((i+1)); \
			if [ $$i -ge $(HEALTH_RETRIES) ]; then \
				echo "ERROR: OpenSearch not healthy after $$((HEALTH_RETRIES*HEALTH_DELAY))s. Check 'docker compose -f $(OPENSEARCH_COMPOSE_FILE) logs opensearch'." >&2; \
				exit 1; \
			fi; \
			sleep $(HEALTH_DELAY); \
		done; \
		echo ">> OpenSearch is healthy."; \
	fi

# ── Full experiment orchestration ──────────────────────────────────────────────
# `make experiment-run` brings up every dependency a complete experiment needs
# (port-forwards → LLM model server → platform), validates them, then launches
# the chaos/RCA harness. Each dependency is started only if it isn't already up,
# so the target is safe to re-run. Background services log to /tmp/*.log.
#
# Usage examples:
#   make experiment-run                              # full train+eval matrix
#   make experiment-run RUN_ARGS="--phase chaos"     # only inject chaos
#   make experiment-run RUN_ARGS="--dry-run"         # print first spec and exit

.PHONY: model-serve-bg
## Start the Qwen3.5 llama.cpp server in the background.
model-serve-bg:
	@echo ">> Starting Qwen3.5 llama.cpp server on :$(MODEL_PORT) (log: /tmp/model-serve.log)"
	@QWEN35_MCP_SERVERS_CONFIG=$(QWEN35_MCP_SERVERS_CONFIG) \
		QWEN35_HOST=$(MODEL_HOST) QWEN35_PORT=$(MODEL_PORT) \
		nohup $(QWEN35_LAUNCHER) >/tmp/model-serve.log 2>&1 &

.PHONY: model-otel-proxy-bg
## Start the Qwen3.5 OpenTelemetry proxy in the background.
model-otel-proxy-bg:
	@echo ">> Starting Qwen3.5 OTel proxy on :$(MODEL_PROXY_PORT) (log: /tmp/model-otel-proxy.log)"
	@OTEL_SERVICE_NAME=llm-qwen35 \
		OTEL_EXPORTER_OTLP_ENDPOINT=$(QWEN35_OTEL_ENDPOINT) \
		OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0 \
		DEPLOYMENT_ENV=$(DEPLOYMENT_ENV) \
		LLM_MODEL=$(QWEN35_MODEL) \
		QWEN35_UPSTREAM_URL=$(QWEN35_UPSTREAM_URL) \
		LLM_IO_AUDIT_PATH="$(QWEN35_AUDIT_PATH)" \
		nohup $(PYTHON) -m uvicorn model.qwen35_otel_proxy:app \
			--host $(MODEL_PROXY_HOST) --port $(MODEL_PROXY_PORT) \
			>/tmp/model-otel-proxy.log 2>&1 &

.PHONY: experiment-platform-up-bg
## Start the experiment platform in the background.
experiment-platform-up-bg: ensure-model ensure-grafana-mcp ensure-opensearch
	@echo ">> Starting experiment platform on $(PLATFORM_BIND_HOST):$(PLATFORM_PORT) (log: /tmp/experiment-platform.log)"
	@HISTORY_MIGRATION_ENABLED=$(HISTORY_MIGRATION_ENABLED) \
		HISTORY_ARCHIVE_ENABLED=$(HISTORY_ARCHIVE_ENABLED) \
		HISTORICAL_MCP_SSE_URL=$(HISTORICAL_MCP_SSE_URL) \
		LLM_URL=$(QWEN35_LLM_URL) LLM_MODEL=$(QWEN35_MODEL) \
		GRAFANA_MCP_SSE_URL=$(GRAFANA_MCP_SSE_URL) \
		GRAFANA_MCP_TIMEOUT_SECONDS=$(GRAFANA_MCP_TIMEOUT_SECONDS) \
		LLM_IO_AUDIT_PATH="$(PLATFORM_AUDIT_PATH)" \
		OTEL_SERVICE_NAME=rca-platform \
		OTEL_EXPORTER_OTLP_ENDPOINT=$(QWEN35_OTEL_ENDPOINT) \
		OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=0 \
		DEPLOYMENT_ENV=$(DEPLOYMENT_ENV) \
		nohup $(PYTHON) -m uvicorn experiment.platform.backend.main:app \
		--host $(PLATFORM_BIND_HOST) --port $(PLATFORM_PORT) >/tmp/experiment-platform.log 2>&1 &

.PHONY: ensure-cluster
## Fail fast if the Kubernetes cluster is unreachable.
ensure-cluster:
	@kubectl get nodes >/dev/null 2>&1 || \
		{ echo "ERROR: cannot reach the Kubernetes cluster (kubectl get nodes failed)." >&2; exit 1; }
	@echo ">> Kubernetes cluster reachable."

.PHONY: ensure-port-forward
## Bring up port-forwards (MCP/Prometheus/Tempo/...) if the MCP port is closed.
ensure-port-forward:
	@if $(PYTHON) -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',$(MCP_LOCAL_PORT)))==0 else 1)" 2>/dev/null; then \
		echo ">> Port-forwards already up (MCP :$(MCP_LOCAL_PORT))."; \
	else \
		echo ">> Port-forwards down — running 'make port-forward'..."; \
		$(MAKE) port-forward; \
		echo "   waiting for port-forwards to settle..."; \
		sleep $(HEALTH_DELAY); \
	fi

.PHONY: ensure-model
## Ensure the Qwen3.5 runtime and its OTel proxy are healthy.
ensure-model:
	@if curl -sf --max-time 3 $(LLM_HEALTH_URL) >/dev/null 2>&1; then \
		echo ">> Qwen3.5 model server already healthy at $(LLM_HEALTH_URL)."; \
	else \
		echo ">> Qwen3.5 model server down — starting it (model load may take minutes)..."; \
		$(MAKE) model-serve-bg; \
		i=0; \
		until curl -sf --max-time 3 $(LLM_HEALTH_URL) >/dev/null 2>&1; do \
			i=$$((i+1)); \
			if [ $$i -ge $(MODEL_BOOT_RETRIES) ]; then \
				echo "ERROR: Qwen3.5 model not healthy after $$((MODEL_BOOT_RETRIES*HEALTH_DELAY))s. See /tmp/model-serve.log" >&2; \
				exit 1; \
			fi; \
			sleep $(HEALTH_DELAY); \
		done; \
		echo ">> Qwen3.5 model server is healthy."; \
	fi
	@$(MAKE) ensure-model-otel-proxy

.PHONY: ensure-model-otel-proxy
## Ensure the Qwen3.5 OTel proxy is healthy, starting it if necessary.
ensure-model-otel-proxy:
	@if curl -sf --max-time 3 $(QWEN35_PROXY_HEALTH_URL) >/dev/null 2>&1; then \
		echo ">> Qwen3.5 OTel proxy already healthy at $(QWEN35_PROXY_HEALTH_URL)."; \
	else \
		echo ">> Qwen3.5 OTel proxy down — starting it..."; \
		$(MAKE) model-otel-proxy-bg; \
		i=0; \
		until curl -sf --max-time 3 $(QWEN35_PROXY_HEALTH_URL) >/dev/null 2>&1; do \
			i=$$((i+1)); \
			if [ $$i -ge $(HEALTH_RETRIES) ]; then \
				echo "ERROR: Qwen3.5 OTel proxy not healthy after $$((HEALTH_RETRIES*HEALTH_DELAY))s. See /tmp/model-otel-proxy.log" >&2; \
				exit 1; \
			fi; \
			sleep $(HEALTH_DELAY); \
		done; \
		echo ">> Qwen3.5 OTel proxy is healthy."; \
	fi

.PHONY: ensure-grafana-mcp
## Ensure the official read-only Grafana MCP SSE container is healthy.
ensure-grafana-mcp:
	@test -f .env || { echo "ERROR: .env is required for the Grafana MCP service." >&2; exit 1; }
	@grep -Eq '^GRAFANA_URL=.+$$' .env || { echo "ERROR: .env must define GRAFANA_URL." >&2; exit 1; }
	@grep -Eq '^GRAFANA_SERVICE_ACCOUNT_TOKEN=.+$$' .env || { echo "ERROR: .env must define GRAFANA_SERVICE_ACCOUNT_TOKEN." >&2; exit 1; }
	@if docker inspect $(GRAFANA_MCP_CONTAINER) >/dev/null 2>&1; then \
		if [ "$$(docker inspect --format '{{.State.Running}}' $(GRAFANA_MCP_CONTAINER))" != "true" ]; then \
			docker start $(GRAFANA_MCP_CONTAINER) >/dev/null; \
		fi; \
	else \
		docker run --detach --name $(GRAFANA_MCP_CONTAINER) \
			--restart unless-stopped \
			--publish 127.0.0.1:$(GRAFANA_MCP_PORT):8000 \
			--env GRAFANA_URL \
			--env GRAFANA_SERVICE_ACCOUNT_TOKEN \
			$(GRAFANA_MCP_IMAGE) \
			--usage-stats=disabled \
			--disable-write \
			--disable-api \
			--enabled-tools $(GRAFANA_MCP_ENABLED_TOOLS) \
			--allowed-hosts=localhost:$(GRAFANA_MCP_PORT),127.0.0.1:$(GRAFANA_MCP_PORT) \
			>/dev/null; \
	fi
	@i=0; until curl -sf --max-time 3 http://127.0.0.1:$(GRAFANA_MCP_PORT)/healthz >/dev/null; do \
		i=$$((i+1)); \
		if [ $$i -ge 30 ]; then \
			echo "ERROR: Grafana MCP not healthy at $(GRAFANA_MCP_SSE_URL). Check 'docker logs $(GRAFANA_MCP_CONTAINER)'." >&2; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo ">> Official read-only Grafana MCP is healthy at $(GRAFANA_MCP_SSE_URL)."

.PHONY: ensure-platform
## Ensure the experiment platform is healthy, starting it if necessary.
ensure-platform: ensure-grafana-mcp
	@if curl -sf --max-time 3 $(PLATFORM_HEALTH_URL) >/dev/null 2>&1; then \
		echo ">> Experiment platform already healthy at $(PLATFORM_HEALTH_URL)."; \
	else \
		echo ">> Experiment platform down — starting it..."; \
		$(MAKE) experiment-platform-up-bg; \
		i=0; \
		until curl -sf --max-time 3 $(PLATFORM_HEALTH_URL) >/dev/null 2>&1; do \
			i=$$((i+1)); \
			if [ $$i -ge $(HEALTH_RETRIES) ]; then \
				echo "ERROR: platform not healthy after $$((HEALTH_RETRIES*HEALTH_DELAY))s. See /tmp/experiment-platform.log" >&2; \
				exit 1; \
			fi; \
			sleep $(HEALTH_DELAY); \
		done; \
		echo ">> Experiment platform is healthy."; \
	fi

.PHONY: experiment-run
## One-shot: ensure every dependency is up, then run the full experiment harness.
experiment-run: ensure-cluster ensure-port-forward ensure-model ensure-platform
	@echo ">> Verifying the platform can reach the LLM (/api/llm/health)..."
	@curl -sf --max-time 5 $(PLATFORM_BASE)/api/llm/health \
		|| echo "   WARNING: /api/llm/health not OK — the analysis phase may fail."
	@echo
	@echo ">> Launching experiment harness: $(EXPERIMENT_RUNNER) (config: $(EXPERIMENT_CONFIG))"
	$(PYTHON) $(EXPERIMENT_RUNNER) --config $(EXPERIMENT_CONFIG) $(RUN_ARGS)

.PHONY: experiment-multiagent-rca-evaluation
## Controlled campaigns followed by multi-agent RCA evaluation; dry-run by default.
EXPERIMENT_LEVEL ?= small
experiment-multiagent-rca-evaluation:
	$(PYTHON) -m experiment.eval.memory_hog.controlled_campaign --level $(EXPERIMENT_LEVEL) $(RUN_ARGS)

.PHONY: git
git:
	git add .
	git status