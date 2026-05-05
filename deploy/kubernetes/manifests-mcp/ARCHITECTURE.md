# MCP Observability Server - Arquitetura

## Diagrama de Componentes

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        KUBERNETES CLUSTER                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    Namespace: monitoring                             │  │
│  │                                                                      │  │
│  │  ┌────────────────────┐        ┌─────────────────────────┐         │  │
│  │  │   Prometheus       │        │    Loki                 │         │  │
│  │  │   :9090            │        │    :3100                │         │  │
│  │  └────────────────────┘        └─────────────────────────┘         │  │
│  │                                                                      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                           ▲                      ▲                         │
│                           │                      │                         │
│                    (Queries via DNS)             │                        │
│                           │                      │                        │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                 Namespace: mcp-server                               │  │
│  │                                                                      │  │
│  │  ┌────────────────────────────────────────────────────────────────┐ │  │
│  │  │         MCP Observability Server (FastMCP)                     │ │  │
│  │  │                     Port: 8000                                 │ │  │
│  │  │                                                                │ │  │
│  │  │  ┌──────────────────────────────────────────────────────────┐ │ │  │
│  │  │  │  Tools/Functions (19 total)                             │ │ │  │
│  │  │  │                                                          │ │ │  │
│  │  │  │  PROMETHEUS:                   LOKI:                    │ │ │  │
│  │  │  │  • instant_query               • query                  │ │ │  │
│  │  │  │  • range_query                 • range_query            │ │ │  │
│  │  │  │  • get_metrics                 • get_labels             │ │ │  │
│  │  │  │  • get_series                  • get_label_values       │ │ │  │
│  │  │  │                                                          │ │ │  │
│  │  │  │  GOLDEN METRICS:               KPIs:                    │ │ │  │
│  │  │  │  • get_golden_metrics          • get_kpis               │ │ │  │
│  │  │  │  • query_golden_metric         • query_kpi              │ │ │  │
│  │  │  │                                • query_all_kpis         │ │ │  │
│  │  │  │  UTILS:                                                  │ │ │  │
│  │  │  │  • health_check                                          │ │ │  │
│  │  │  └──────────────────────────────────────────────────────────┘ │ │  │
│  │  │                                                                │ │  │
│  │  │  Components:                                                  │ │  │
│  │  │  • PrometheusClient (httpx)   → PROMETHEUS_URL              │ │  │
│  │  │  • LokiClient (httpx)         → LOKI_URL                    │ │  │
│  │  │  • Metrics & KPIs (dataclass) → get_*_dict()               │ │  │
│  │  │                                                                │ │  │
│  │  └────────────────────────────────────────────────────────────────┘ │  │
│  │                                                                      │  │
│  │  ┌────────────────────────────────────────────────────────────────┐ │  │
│  │  │  Config (ConfigMap)                                            │ │  │
│  │  │  • PROMETHEUS_URL                                              │ │  │
│  │  │  • LOKI_URL                                                    │ │  │
│  │  │  • Timeouts, Host, Port                                        │ │  │
│  │  └────────────────────────────────────────────────────────────────┘ │  │
│  │                                                                      │  │
│  │  Service (ClusterIP:8000)                                          │  │
│  │  ↓ (Internal DNS)                                                  │  │
│  │  mcp-observability.mcp-server.svc.cluster.local:8000             │  │
│  │                                                                      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │ Port-Forward (local testing)
         │ :8000
         ▼
    ┌─────────────────────────────────────────────────────┐
    │  Claude Desktop / Copilot / Local Client           │
    │  (MCP Client)                                        │
    │                                                      │
    │  Uses: prometheus_instant_query, loki_query, etc.  │
    └─────────────────────────────────────────────────────┘
```

## Fluxo de Dados

```
User Request (Claude/Copilot)
        ↓
    [MCP Client]
        ↓
    [MCP Server - FastMCP]
        ↓
    [Tool Handler]
        ├─→ [Prometheus Client] ──→ [Prometheus API]
        ├─→ [Loki Client]      ──→ [Loki API]
        ├─→ [Metrics Module]   ──→ (Query Definitions)
        └─→ [Health Check]     ──→ (Service Validation)
        ↓
    [JSON Response]
        ↓
    [MCP Client]
        ↓
    User receives result
```

## Lifecycle (Kubernetes)

```
1. kubectl apply -f manifests-mcp/
        ↓
2. Namespace mcp-server criado
        ↓
3. ConfigMap com env vars
        ↓
4. Deployment inicia
        ↓
5. Container iniciado
        ↓
6. Readiness/liveness probe (tcpSocket :8000)
        ↓
7. Service disponível (ClusterIP)
        ↓
8. MCP Server pronto para receber conexoes SSE em /sse
```

## Segurança & Best Practices

```
✅ Security Context
   - readOnlyRootFilesystem: true
   - runAsNonRoot: true
   - runAsUser: 1000
   - allowPrivilegeEscalation: false

✅ Resource Management
   - Requests: 100m CPU / 256Mi Memory
   - Limits: 500m CPU / 512Mi Memory

✅ Health Checks
   - livenessProbe (30s interval)
   - readinessProbe (10s interval)

✅ Pod Anti-Affinity
   - Preferir diferentes nodes

✅ Rolling Update
   - maxSurge: 1
   - maxUnavailable: 0
```

## Deploy Scenarios

### Cenário 1: Local Development
```bash
docker-compose up -d
# Acesso: http://localhost:8000
```

### Cenário 2: Kubernetes (Single Cluster)
```bash
./deploy.sh deploy
# Acesso: via port-forward ou NodePort
```

### Cenário 3: Multi-Cluster (Future)
```bash
# Deploy replicado em múltiplos clusters
# Mesma imagem, mesmos manifestos
# Apenas ajustar PROMETHEUS_URL e LOKI_URL por cluster
```

## Extensibilidade

```
Adicionar nova ferramenta:
    1. Editar main.py
    2. Usar @mcp.tool() decorator
    3. Rebuildar imagem Docker
    4. Redeploy Kubernetes

Adicionar nova métrica de ouro:
    1. Editar metrics.py
    2. Adicionar GoldenMetric à lista
    3. Restart container (redeploy)

Adicionar novo KPI:
    1. Editar metrics.py
    2. Adicionar KPI à lista
    3. Restart container (redeploy)
```

## Monitoramento do Próprio MCP Server

```
Possíveis métricas a adicionar:
- Latência de queries (P50, P95, P99)
- Taxa de erro das tools
- Uso de CPU/Memória
- Tempo de conexão com Prometheus/Loki

Tools para expor:
- /metrics (Prometheus format)
- /health/detailed

Estado atual:
- conectividade HTTP disponivel em /sse
- rota /health nao implementada
```
