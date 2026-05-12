# MCP Observability Server

Um servidor MCP (Model Context Protocol) com FastMCP para consultas em Prometheus, Loki e Tempo.

## Funcionalidades

### Prometheus Tools
- `prometheus_instant_query` - Consultas PromQL instantâneas
- `prometheus_range_query` - Consultas PromQL em intervalos de tempo
- `prometheus_get_metrics` - Listar métricas disponíveis
- `prometheus_get_series` - Buscar séries por padrão

### Loki Tools
- `loki_query` - Consultas LogQL instantâneas
- `loki_range_query` - Consultas LogQL em intervalos de tempo
- `loki_get_labels` - Listar labels disponíveis
- `loki_get_label_values` - Valores de labels específicos

### Tempo Tools
- `tempo_search_traces` - Buscar traces no Tempo
- `tempo_get_trace` - Obter trace completo por trace ID

### Golden Metrics (RED Method)
- `get_golden_metrics` - Listar métricas de ouro
- `query_golden_metric` - Consultar uma métrica de ouro

**Métricas incluídas:** Request Rate, Error Rate, Latency P95/P99, CPU Usage, Memory Usage, Disk Write Throughput, MySQL Query Rate, MongoDB Ops Rate, MongoDB Avg Op Latency, Redis Commands Rate

### KPIs
- `get_kpis` - Listar KPIs da aplicação
- `query_kpi` - Consultar um KPI específico
- `query_all_kpis` - Todos os KPIs

**KPIs incluídos:** Service Availability, MTTR, Error Budget, Cache Hit Rate, Queue Depth, DB Connection Pool, Concurrency

### Utilitários
- `health_check` - Status de Prometheus, Loki e Tempo

## Quick Start

### Local (Docker Compose)

```bash
cd mcp-observability-server
docker-compose up -d
curl http://localhost:8000/health
```

### Kubernetes

```bash
# Build da imagem (local ou no cluster)
docker build -t microservices-demo/mcp-observability-server:latest \
  -f mcp-observability-server/Dockerfile mcp-observability-server/

# Deploy
kubectl apply -f deploy/kubernetes/manifests-mcp/

# Verificar
kubectl get pods -n mcp-server
kubectl port-forward -n mcp-server svc/mcp-observability 8000:8000
curl http://localhost:8000/health
```

## Configuração

Variáveis de ambiente:

```env
PROMETHEUS_URL=http://localhost:9090
PROMETHEUS_TIMEOUT=30
LOKI_URL=http://localhost:3100
LOKI_TIMEOUT=30
TEMPO_URL=http://localhost:3200
TEMPO_TIMEOUT=30
HOST=0.0.0.0
PORT=8000
```

Para Kubernetes, use DNS interno do cluster.

## Arquivo de Estrutura

```
mcp-observability-server/
├── config.py              # Configuração
├── prometheus_client.py    # Cliente Prometheus
├── loki_client.py          # Cliente Loki
├── metrics.py              # Métricas de ouro e KPIs
├── main.py                 # Servidor MCP
├── Dockerfile              # Container
├── docker-compose.yml      # Compose local
├── requirements.txt        # Dependências
└── README.md               # Documentação
```

Para mais detalhes, veja [deploy/kubernetes/manifests-mcp/README.md](../deploy/kubernetes/manifests-mcp/README.md)
