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
LITMUS_CHAOS_CENTER_FRONTEND_PORT ?= 9091
LITMUS_CHAOS_CENTER_SERVER_SERVICE ?= $(LITMUS_CHAOS_CENTER_RELEASE)-litmus-server-service
LITMUS_CHAOS_CENTER_SERVER_PORT ?= 9002
LITMUS_CHAOS_CENTER_SERVER_WS_PORT ?= 8000
PORT_FORWARD_CHECK_HOST ?= $(shell tailscale ip -4 2>/dev/null | head -n1 || ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $$7; exit}')
PORT_FORWARD_CHECK_PORTS ?= 8080 3000 9090 16686 8089 9091

FRONT_END_IMAGE ?= weaveworksdemos/front-end:node18-otel

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
	nohup kubectl port-forward --address 0.0.0.0 -n jaeger svc/jaeger-query 16686:80 >/tmp/pf-jaeger.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n kube-system svc/kibana 5602:5601 >/tmp/pf-kibana.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n loadtest svc/locust-web 8089:8089 >/tmp/pf-locust.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_FRONTEND_SERVICE) $(LITMUS_CHAOS_CENTER_FRONTEND_PORT):9091 >/tmp/pf-chaos-center.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_PORT):9002 >/tmp/pf-chaos-center-server.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n $(LITMUS_CHAOS_CENTER_NAMESPACE) svc/$(LITMUS_CHAOS_CENTER_SERVER_SERVICE) $(LITMUS_CHAOS_CENTER_SERVER_WS_PORT):8000 >/tmp/pf-chaos-center-server-ws.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n mcp-server svc/mcp-observability 18080:8000 >/tmp/pf-mcp-observability.log 2>&1 &
	nohup kubectl port-forward --address 0.0.0.0 -n monitoring svc/loki 3100:3100 >/tmp/pf-loki.log 2>&1 &

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
	pkill -f "^kubectl port-forward .* -n loki svc/loki" || true

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
cluster-up: app-up observability-up loadtest-up

.PHONY: cluster-restart
cluster-restart: cluster-down cluster-up port-forward

.PHONY: git
git:
	git add .
	git status