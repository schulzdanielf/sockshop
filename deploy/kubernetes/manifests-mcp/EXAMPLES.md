# MCP Observability Server - Exemplos de Uso

## 1. Queries Prometheus

### Exemplo 1: Taxa de Requisições
```python
from main import prometheus_instant_query
import json

result = prometheus_instant_query('rate(http_requests_total[5m])')
print(json.loads(result))
```

**Resposta esperada:**
```json
{
  "status": "success",
  "data": {
    "resultType": "vector",
    "result": [
      {
        "metric": {"job": "frontend"},
        "value": [1234567890, "100.5"]
      }
    ]
  }
}
```

### Exemplo 2: Latência P95
```python
from main import query_golden_metric

result = query_golden_metric('Latency P95')
print(result)
```

### Exemplo 3: Série Temporal
```python
from main import prometheus_range_query

result = prometheus_range_query(
    query='up{job="prometheus"}',
    start='1h',  # 1 hora atrás
    end='now',
    step='1m'
)
print(result)
```

## 2. Queries Loki

### Exemplo 1: Logs Recentes
```python
from main import loki_query

result = loki_query('{job="frontend"} | level="error"')
print(result)
```

**Resposta esperada:**
```json
{
  "status": "success",
  "data": {
    "resultType": "streams",
    "result": [
      {
        "stream": {"job": "frontend", "pod": "frontend-1"},
        "values": [
          ["1234567890000000000", "error: connection timeout"]
        ]
      }
    ]
  }
}
```

### Exemplo 2: Logs em Intervalo
```python
from main import loki_range_query

result = loki_range_query(
    query='{namespace="sock-shop"} | json | status >= 500',
    limit=500
)
print(result)
```

### Exemplo 3: Labels Disponíveis
```python
from main import loki_get_labels

labels = loki_get_labels()
print(labels)
# Output: {"status": "success", "data": ["job", "pod", "namespace", ...]}
```

## 3. Golden Metrics

### Exemplo 1: Listar Todas
```python
from main import get_golden_metrics
import json

metrics = get_golden_metrics()
data = json.loads(metrics)
for metric in data['metrics']:
    print(f"{metric['name']}: {metric['description']} ({metric['unit']})")
```

**Output:**
```
Request Rate: Number of requests per second (req/s)
Error Rate: Proportion of requests that result in error (%)
Latency P95: 95th percentile of request duration (s)
Latency P99: 99th percentile of request duration (s)
CPU Usage: CPU usage per container (cores)
Memory Usage: Memory usage per container (bytes)
```

### Exemplo 2: Consultar Específica
```python
from main import query_golden_metric

result = query_golden_metric('Request Rate')
print(result)
```

## 4. KPIs

### Exemplo 1: Listar KPIs
```python
from main import get_kpis
import json

kpis = get_kpis()
data = json.loads(kpis)
for kpi in data['kpis']:
    print(f"{kpi['name']}")
    print(f"  Threshold: {kpi['threshold']}")
    print(f"  Alert if: {kpi['alert_condition']}")
```

### Exemplo 2: Consultar KPI Específico
```python
from main import query_kpi

result = query_kpi('Service Availability')
print(result)
```

**Output esperado:**
```json
{
  "status": "success",
  "data": {
    "result": [
      {
        "metric": {},
        "value": [1234567890, "99.95"]
      }
    ]
  },
  "kpi_info": {
    "name": "Service Availability",
    "description": "Percentage of successful requests",
    "threshold": ">= 99.9%",
    "alert_condition": "< 99"
  }
}
```

### Exemplo 3: Todos os KPIs
```python
from main import query_all_kpis

all_kpis = query_all_kpis()
print(all_kpis)
```

## 5. Health Check

```python
from main import health_check

status = health_check()
print(status)
```

**Output esperado:**
```json
{
  "status": "healthy",
  "services": {
    "prometheus": "ok",
    "loki": "ok"
  }
}
```

## 6. Usando com Claude

### Setup
```json
// ~/.config/claude_desktop_config.json
{
  "mcpServers": {
    "observability": {
      "command": "python",
      "args": ["/path/to/mcp-observability-server/main.py"]
    }
  }
}
```

### Conversa de Exemplo

**Usuário:** "Qual é a taxa de erro nos últimos 5 minutos?"

**Claude:** Vou usar a ferramenta de consulta do Prometheus para você.
```python
prometheus_instant_query('sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))')
```

**Usuário:** "Procure por erros críticos nos últimos 30 minutos"

**Claude:** Vou buscar os logs de erro no Loki.
```python
loki_range_query('{level="error"} or {level="critical"}', limit=1000)
```

**Usuário:** "Qual é a disponibilidade do serviço?"

**Claude:** Vou consultar o KPI de disponibilidade.
```python
query_kpi('Service Availability')
```

## 7. Queries Úteis de Prometheus

```promql
# Taxa de requisições por serviço
sum(rate(http_requests_total[5m])) by (service)

# Taxa de erro por serviço
sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)

# Latência P99 por endpoint
histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m])) by (endpoint)

# CPU por pod
sum(rate(container_cpu_usage_seconds_total[5m])) by (pod_name)

# Memória em uso por pod
sum(container_memory_usage_bytes) by (pod_name)

# Disponibilidade de serviço
up{job="my-service"}

# Top 5 endpoints mais lentos
topk(5, histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])))
```

## 8. Queries Úteis de Loki

```logql
# Erro de conexão
{job="my-service"} |= "connection"

# Logs com latência alta
{job="my-service"} | json | duration > 1000

# Logs de erro
{job="my-service"} | json | level="error"

# Contar logs por nível
{job="my-service"} | json | stats count() by level

# Buscar padrão de exceção
{job="my-service"} |= "Exception"

# Logs de pod específico
{pod="my-pod-123"}

# Logs de namespace
{namespace="sock-shop"} | json

# Filtrar por status HTTP
{job="frontend"} | json | status >= 500
```

## 9. Automatizar com Scripts

### Script: Monitorar Disponibilidade

```bash
#!/bin/bash
INTERVAL=60

while true; do
    kubectl exec -it deployment/mcp-observability-deployment -n mcp-server -- \
    python -c "
from main import query_kpi
import json
result = json.loads(query_kpi('Service Availability'))
availability = float(result['data']['result'][0]['value'][1])
echo 'Availability: {availability}%'
if availability < 99:
    echo 'ALERT: Availability below threshold!'
fi
"
    sleep $INTERVAL
done
```

### Script: Coleta de Dados

```bash
#!/bin/bash
# Coletar todas as métricas a cada 5 minutos

for metric in "Request Rate" "Error Rate" "Latency P95"; do
    kubectl exec deployment/mcp-observability-deployment -n mcp-server -- \
    python -c "
from main import query_golden_metric
import json
from datetime import datetime
result = query_golden_metric('$metric')
data = json.loads(result)
timestamp = datetime.now().isoformat()
print(f'{timestamp} - $metric: {data}')
" >> /tmp/metrics-$(date +%Y%m%d).log
done
```

## 10. Integração com ELK/Observabilidade

```python
# Enviar métricas para elasticsearch/datadog
from main import query_all_kpis
import json
import requests

result = json.loads(query_all_kpis())

for kpi in result['kpis_status']:
    payload = {
        "timestamp": datetime.now().isoformat(),
        "kpi_name": kpi['name'],
        "value": float(kpi['current_value'][0]['value'][1]) if kpi['current_value'] else 0,
        "threshold": kpi['threshold'],
        "alert_condition": kpi['alert_condition']
    }
    requests.post('http://elasticsearch:9200/kpis/_doc', json=payload)
```

## Troubleshooting

### Erro: Connection refused (Prometheus)
```
Solução:
1. Verificar se Prometheus está running: kubectl get pods -n monitoring
2. Testar DNS: kubectl exec <pod> -n mcp-server -- nslookup prometheus.monitoring.svc.cluster.local
3. Testar conectividade: kubectl exec <pod> -n mcp-server -- curl http://prometheus.monitoring.svc.cluster.local:9090/api/v1/status/buildinfo
```

### Erro: Query timeout
```
Solução:
1. Aumentar PROMETHEUS_TIMEOUT em ConfigMap
2. Simplificar a query
3. Aumentar resources.limits.cpu/memory
```

### Erro: Empty results
```
Solução:
1. Verificar se as métricas existem: prometheus_get_metrics()
2. Ajustar labels/selectors
3. Verificar intervalo de tempo
```
