# MCP Observability Server - QUICK REFERENCE

## 📦 O que foi criado

```
✅ mcp-observability-server/          - Código Python do servidor
   ├── main.py                         - 19 tools MCP prontas
   ├── Dockerfile                      - Container prod-ready
   ├── requirements.txt                - Dependências mínimas
   └── config.py, *_client.py, ...     - Módulos suporte

✅ deploy/kubernetes/manifests-mcp/   - Manifestos K8s
   ├── deploy.sh                       - Script helper (build/deploy/logs/status)
   ├── test.sh                         - Testes automatizados
   ├── 00-04.yaml                      - Manifestos K8s
   └── README.md, ARCHITECTURE.md, EXAMPLES.md
```

## 🚀 Deploy em 3 passos

```bash
# 1. Build & Deploy
cd deploy/kubernetes/manifests-mcp
./deploy.sh deploy

# 2. Verificar
./deploy.sh status

# 3. Testar
./deploy.sh port-forward
# Em outro terminal: curl -i http://localhost:8000/sse
```

Notas importantes:
- o servidor usa transporte `sse`
- `GET /sse` e o endpoint HTTP de conectividade
- `GET /health` nao existe como rota HTTP neste projeto

## 🛠️ 19 Tools Disponíveis

| Categoria | Tools |
|-----------|-------|
| **Prometheus (4)** | instant_query, range_query, get_metrics, get_series |
| **Loki (4)** | query, range_query, get_labels, get_label_values |
| **Golden Metrics (2)** | get_golden_metrics, query_golden_metric |
| **KPIs (3)** | get_kpis, query_kpi, query_all_kpis |
| **Utils (1)** | health_check |

## 📝 Exemplos Rápidos

```python
# Prometheus
from main import prometheus_instant_query
prometheus_instant_query('up{job="prometheus"}')

# Loki
from main import loki_query
loki_query('{job="frontend"} | level="error"')

# Golden Metrics
from main import query_golden_metric
query_golden_metric('Request Rate')

# KPIs
from main import query_kpi
query_kpi('Service Availability')
```

## 🐳 Local Testing

```bash
cd mcp-observability-server
docker-compose up -d
curl -i http://localhost:8000/sse
```

## 📊 Golden Metrics Incluídas

- Request Rate
- Error Rate
- Latency P95/P99
- CPU Usage
- Memory Usage

## 📈 KPIs Incluídos

- Service Availability
- Mean Time To Recovery (MTTR)
- Error Budget Consumption
- Cache Hit Rate
- Queue Depth
- Database Connection Pool Utilization

## 🔧 Comandos Úteis

```bash
# Status geral
./deploy.sh status

# Logs em tempo real
./deploy.sh logs

# Port-forward
./deploy.sh port-forward

# Testes
./test.sh

# Remover tudo
./deploy.sh undeploy

# Build apenas
./deploy.sh build

# Build com registry custom
./deploy.sh build --registry=myregistry --tag=v1.0.0
```

## 🔗 URLs Importantes

```
Local Testing SSE:       http://localhost:8000/sse
K8s Internal DNS:        http://mcp-observability.mcp-server.svc.cluster.local:8000
Prometheus (K8s):        http://prometheus.monitoring.svc.cluster.local:9090
Loki (K8s):             http://loki.monitoring.svc.cluster.local:3100
```

## 📚 Documentação

- [README.md](README.md) - Overview
- [ARCHITECTURE.md](ARCHITECTURE.md) - Diagrama & componentes
- [EXAMPLES.md](EXAMPLES.md) - 10 exemplos práticos
- [../../mcp-observability-server/README.md](../../mcp-observability-server/README.md) - Código

## ⚙️ Configuração

```env
PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090
LOKI_URL=http://loki.monitoring.svc.cluster.local:3100
PROMETHEUS_TIMEOUT=30
LOKI_TIMEOUT=30
HOST=0.0.0.0
PORT=8000
```

## ✅ Features

- ✓ Containerizado para replicabilidade
- ✓ Prod-ready (healthchecks, security, resources)
- ✓ Auto-discovery via DNS K8s
- ✓ 19 tools prontas para uso
- ✓ Golden Metrics (RED method)
- ✓ KPIs aplicação
- ✓ Scripts helper de deploy/teste
- ✓ Documentação completa

## 🎯 Próximos Passos Opcionais

- [ ] Ingress para acesso HTTP (manifesto incluído)
- [ ] Monitoring do próprio MCP server
- [ ] Cache de queries
- [ ] Autoscaling com HPA
- [ ] Integração com ChatGPT/Claude

## 📞 Troubleshooting

```bash
# Pod não inicia?
kubectl logs deployment/mcp-observability-deployment -n mcp-server

# Conexão com Prometheus?
kubectl exec <pod> -n mcp-server -- curl http://prometheus.monitoring.svc.cluster.local:9090/api/v1/status/buildinfo

# Testar manualmente
kubectl exec -it <pod> -n mcp-server -- python -c "from main import health_check; print(health_check())"
```

Checklist rapido de validacao:
- `./deploy.sh status` mostra `READY 1/1`
- `curl -i http://localhost:8000/sse` retorna `200 OK`
- `python -c "from main import health_check; print(health_check())"` retorna Prometheus/Loki `ok`

## 📌 Estratégia de Replicabilidade

✅ **Tudo containerizado** - Mesmo comportamento em qualquer K8s
✅ **Configuração via ConfigMap** - Sem hardcoding
✅ **Scripts helper** - Deploy automatizado
✅ **Documentação completa** - ARCHITECTURE + EXAMPLES + README
✅ **Testes inclusos** - Validar funcionamento

---

**Pronto para começar?** 🚀

```bash
cd deploy/kubernetes/manifests-mcp
./deploy.sh deploy
```
