# MCP Observability Server

Um servidor MCP (Model Context Protocol) com FastMCP para consultas em Prometheus, Loki e Tempo.

## Funcionalidades

### Prometheus Tools
- `prometheus_instant_query` - Consultas PromQL instantâneas
- `prometheus_range_query` - Consultas PromQL em intervalos de tempo
- `prometheus_get_metrics` - Listar métricas disponíveis
- `prometheus_get_series` - Buscar séries por padrão

### Investigator Tools
- `check_oom_kills` - Detectar OOMKills por serviço em uma janela fechada
- `check_pod_restarts` - Contar incrementos por pod/container na janela e informar o intervalo entre scrapes observado
- `check_service_readiness` - Comparar prontidão por serviço/fase e registrar transições observadas entre scrapes
- `get_cpu_saturation_top` - Top serviços por saturação de CPU
- `get_memory_saturation_top` - Top serviços por saturação de memória
- `check_http_error_rate_top` - Top serviços por taxa de erro HTTP 5xx
- `check_request_latency_top` - Top serviços por latência P95
- `check_network_packet_drops` - Comparar drops de receive/transmit por interface entre baseline e fault

As tools de investigação escondem a PromQL crua e devolvem contratos JSON
estáveis. As ferramentas de evento incluem `found`, `count`, `top`, `query` e
`window`; `check_network_packet_drops` retorna métricas e cobertura por fase.
`check_network_packet_drops` recebe janelas fechadas de baseline e fault e
informa cobertura por fase; esses contadores são do nó/interface, não de um
serviço específico, e não identificam sozinhos a causa raiz.
`check_service_readiness` usa `kube_pod_status_ready` e reporta cobertura e
percentual médio pronto por serviço; transições incluem faixa UTC observada,
nunca um instante exato inferido. `check_pod_restarts` preserva contagens
existentes e acrescenta observações por container com limites entre scrapes;
uma primeira amostra alta não comprova restart dentro da janela. `no_data` não
significa indisponibilidade.

### Loki Tools
- `loki_query` - Consultas LogQL instantâneas
- `loki_range_query` - Consultas LogQL em intervalos de tempo
- `loki_get_labels` - Listar labels disponíveis
- `loki_get_label_values` - Valores de labels específicos
- `check_service_logs` - Amostras saneadas e limitadas por serviço nas fases baseline/fault/post, com cobertura e assinaturas observadas
- `capture_run_log_snapshot` - Captura agregada dos serviços/fases sob um limite global de linhas; destinada ao host da plataforma

`check_service_logs` exige janelas fechadas e serviço/namespace validados; o
cliente RCA não fornece LogQL livre. Mensagens e tokens são redigidos antes de
serem incluídos em exemplos, inclusive credenciais em query parameters.
Contagens são das linhas recuperadas e podem estar
truncadas; uma resposta vazia não comprova que o serviço não gerou logs.
`capture_run_log_snapshot` distribui o orçamento global igualmente por serviço
e fase, limita a 1200 linhas por captura e rejeita budgets que não reservem ao
menos uma linha para cada combinação pedida.

### Tempo Tools
- `tempo_search_traces` - Buscar traces no Tempo
- `tempo_get_trace` - Obter trace completo por trace ID

O resumo `tempo_service_graph_phases` preserva também as assinaturas de falha
já extraídas dos traces, com trace ID, destino, operação e fase, dentro de um
limite por fase. Ausência de assinatura ou de aresta não prova ausência de
falha ou dependência.

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
