.PHONY: gen-complete-demo
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

.PHONY: observability-port-forward
observability-port-forward:
	nohup kubectl port-forward -n sock-shop svc/front-end 8080:80 >/tmp/pf-front-end.log 2>&1 &
	nohup kubectl port-forward -n monitoring svc/grafana 3000:80 >/tmp/pf-grafana.log 2>&1 &
	nohup kubectl port-forward -n monitoring svc/prometheus 9090:9090 >/tmp/pf-prometheus.log 2>&1 &
	nohup kubectl port-forward -n jaeger svc/jaeger-query 16686:80 >/tmp/pf-jaeger.log 2>&1 &
	nohup kubectl port-forward -n kube-system svc/kibana 5602:5601 >/tmp/pf-kibana.log 2>&1 &
	nohup kubectl port-forward -n loadtest svc/locust-web 8089:8089 >/tmp/pf-locust.log 2>&1 &

.PHONY: observability-stop-port-forward
observability-stop-port-forward:
	pkill -f "kubectl port-forward -n sock-shop svc/front-end" || true
	pkill -f "kubectl port-forward -n monitoring svc/grafana" || true
	pkill -f "kubectl port-forward -n monitoring svc/prometheus" || true
	pkill -f "kubectl port-forward -n jaeger svc/jaeger-query" || true
	pkill -f "kubectl port-forward -n kube-system svc/kibana" || true
	pkill -f "kubectl port-forward -n loadtest svc/locust-web" || true
.PHONY: observability
observability: observability-up observability-port-forward

.PHONY: port-forward
port-forward:
	nohup kubectl port-forward -n sock-shop svc/front-end 8080:80 >/tmp/pf-front-end.log 2>&1 &
	nohup kubectl port-forward -n monitoring svc/grafana 3000:80 >/tmp/pf-grafana.log 2>&1 &
	nohup kubectl port-forward -n monitoring svc/prometheus 9090:9090 >/tmp/pf-prometheus.log 2>&1 &
	nohup kubectl port-forward -n jaeger svc/jaeger-query 16686:80 >/tmp/pf-jaeger.log 2>&1 &
	nohup kubectl port-forward -n kube-system svc/kibana 5602:5601 >/tmp/pf-kibana.log 2>&1 &
	nohup kubectl port-forward -n loadtest svc/locust-web 8089:8089 >/tmp/pf-locust.log 2>&1 &

.PHONY: cluster-down
cluster-down: observability-stop-port-forward loadtest-down observability-down app-down

.PHONY: cluster-up
cluster-up: app-up observability-up loadtest-up

.PHONY: cluster-restart
cluster-restart: cluster-down cluster-up port-forward