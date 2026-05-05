#!/bin/bash
# Test script for MCP Observability Server

NAMESPACE="${1:-mcp-server}"
POD_NAME=$(kubectl get pods -n "$NAMESPACE" -l app=mcp-observability -o jsonpath='{.items[0].metadata.name}')

if [ -z "$POD_NAME" ]; then
    echo "Error: No pod found in namespace $NAMESPACE"
    exit 1
fi

echo "Testing MCP Observability Server in pod: $POD_NAME"
echo "Namespace: $NAMESPACE"
echo ""

# Test 1: Health check
echo "1. Health Check"
kubectl exec -it "$POD_NAME" -n "$NAMESPACE" -- python -c "
import json
from main import health_check
print(health_check())
" || echo "Health check command failed"
echo ""

# Test 2: Get golden metrics
echo "2. Golden Metrics Available"
kubectl exec -it "$POD_NAME" -n "$NAMESPACE" -- python -c "
import json
from main import get_golden_metrics
result = get_golden_metrics()
metrics = json.loads(result)
print(json.dumps(metrics, indent=2))
" || echo "Get golden metrics command failed"
echo ""

# Test 3: Get KPIs
echo "3. KPIs Available"
kubectl exec -it "$POD_NAME" -n "$NAMESPACE" -- python -c "
import json
from main import get_kpis
result = get_kpis()
kpis = json.loads(result)
print(json.dumps(kpis, indent=2))
" || echo "Get KPIs command failed"
echo ""

# Test 4: Prometheus query
echo "4. Prometheus Instant Query (up{job=~'prometheus|loki'})"
kubectl exec -it "$POD_NAME" -n "$NAMESPACE" -- python -c "
import json
from main import prometheus_instant_query
result = prometheus_instant_query('up{job=~\"prometheus|loki\"}')
print(result)
" || echo "Prometheus query command failed"
echo ""

echo "Tests completed!"
