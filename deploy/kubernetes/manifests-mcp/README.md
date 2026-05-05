# MCP Observability Server - Kubernetes Manifests

## Descrição

Manifestos Kubernetes para o MCP Server que fornece consultas integradas no Prometheus e Loki.

O deployment atual executa o FastMCP com transporte `sse`, expondo conectividade HTTP em `GET /sse`.
As verificacoes de disponibilidade do pod usam `tcpSocket` na porta `8000`, nao `HTTP GET /health`.

## Estrutura de Arquivos

- `00-mcp-ns.yaml` - Namespace `mcp-server`
- `01-mcp-configmap.yaml` - ConfigMap com variáveis de ambiente
- `02-mcp-dep.yaml` - Deployment do MCP server
- `03-mcp-svc.yaml` - Service para expor o MCP server
- `04-mcp-ingress.yaml` - Ingress (opcional) para acesso HTTP
- `deploy.sh` - Script helper para build e deploy
- `test.sh` - Script para testar o MCP server

## Instalação

### Pré-requisitos

1. Cluster Kubernetes em execução
2. Prometheus e Loki já instalados no namespace `monitoring`
3. Imagem Docker do MCP server disponível (construída a partir de `mcp-observability-server/Dockerfile`)

### Build da Imagem Docker

```bash
cd mcp-observability-server
docker build -t microservices-demo/mcp-observability-server:latest .
```

Se usar Minikube com Docker local:
```bash
eval $(minikube docker-env)
docker build -t microservices-demo/mcp-observability-server:latest .
```

### Deploy dos Manifestos

```bash
# Opção 1: Usar o script helper (recomendado)
./deploy.sh deploy

# Opção 2: Aplicar todos os manifestos
kubectl apply -f deploy/kubernetes/manifests-mcp/

# Opção 3: Aplicar individualmente na ordem correta
kubectl apply -f deploy/kubernetes/manifests-mcp/00-mcp-ns.yaml
kubectl apply -f deploy/kubernetes/manifests-mcp/01-mcp-configmap.yaml
kubectl apply -f deploy/kubernetes/manifests-mcp/02-mcp-dep.yaml
kubectl apply -f deploy/kubernetes/manifests-mcp/03-mcp-svc.yaml
```

## Uso Rápido (Script Helper)

```bash
cd deploy/kubernetes/manifests-mcp

# Build e deploy completo
./deploy.sh deploy

# Apenas build
./deploy.sh build

# Ver status
./deploy.sh status

# Ver logs
./deploy.sh logs

# Port-forward para testes locais
./deploy.sh port-forward

# Remover deployment
./deploy.sh undeploy

# Ajuda
./deploy.sh help
```

## Testes

```bash
# Rodar testes no pod
./test.sh

# Com namespace customizado
./test.sh monitoring
```

## Verificação

### Status do Deployment

```bash
# Verificar status (recomendado usar o script)
./deploy.sh status

# Ou manualmente
kubectl get pods -n mcp-server
kubectl describe pod <pod-name> -n mcp-server

# Ver logs
kubectl logs -f deployment/mcp-observability-deployment -n mcp-server

# Ver ConfigMap
kubectl get configmap -n mcp-server
kubectl describe configmap mcp-observability-config -n mcp-server
```

### Testar Conectividade

```bash
# Port-forward para testar localmente (use o script)
./deploy.sh port-forward

# Em outro terminal, testar conectividade HTTP
curl -i http://localhost:8000/sse

# Teste funcional dentro do pod
kubectl exec -it deploy/mcp-observability-deployment -n mcp-server -- \
  python -c "from main import health_check; print(health_check())"

# Verificar logs do servidor
kubectl logs -f deployment/mcp-observability-deployment -n mcp-server

# Ou use o script de testes
./test.sh
```

## Configuração

As variáveis de ambiente estão definidas no ConfigMap `01-mcp-configmap.yaml`:

- `PROMETHEUS_URL`: URL do Prometheus (padrão: `http://prometheus.monitoring.svc.cluster.local:9090`)
- `PROMETHEUS_TIMEOUT`: Timeout para queries do Prometheus (padrão: 30s)
- `LOKI_URL`: URL do Loki (padrão: `http://loki.monitoring.svc.cluster.local:3100`)
- `LOKI_TIMEOUT`: Timeout para queries do Loki (padrão: 30s)

## Acessar o MCP Server

### De dentro do cluster

```bash
# DNS internal do cluster
http://mcp-observability.mcp-server.svc.cluster.local:8000
```

### Via Port-Forward

```bash
kubectl port-forward -n mcp-server svc/mcp-observability 8000:8000

# Conectividade HTTP
curl -i http://localhost:8000/sse
```

Observacoes:
- `GET /sse` retorna `200 OK` com `content-type: text/event-stream`
- `GET /health` retorna `404 Not Found` no estado atual da aplicacao

### Via NodePort (opcional)

Para expor via NodePort, modifique o arquivo `03-mcp-svc.yaml`:

```yaml
spec:
  type: NodePort  # Mude de ClusterIP para NodePort
  ports:
  - name: http
    port: 8000
    targetPort: 8000
    nodePort: 30800  # Adicione nodePort
```

Depois obtenha a URL com:
```bash
kubectl get svc -n mcp-server
```

## Integração com Claude Desktop

Para usar o MCP server com Claude Desktop:

1. Obtenha a URL do service (via port-forward ou NodePort)
2. Aponte o cliente para o endpoint SSE exposto pelo service
3. Configure em `~/.config/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "observability": {
      "command": "python",
      "args": ["-m", "fastmcp.cli"],
      "env": {
        "FASTMCP_URL": "http://localhost:8000"
      }
    }
  }
}
```

## Removendo os Manifestos

```bash
# Remover todos os recursos
kubectl delete namespace mcp-server

# Ou remover individualmente
kubectl delete -f deploy/kubernetes/manifests-mcp/
```

## Troubleshooting

### Pod em CrashLoopBackOff

Verifique os logs:
```bash
kubectl logs deployment/mcp-observability-deployment -n mcp-server --tail=50
```

Causas comuns ja encontradas neste repositorio:
- `deploy.sh` resolvendo a raiz do projeto incorretamente
- versoes incompativeis entre `fastmcp`, `httpx`, `pydantic` e `python-dotenv`
- uso de `BaseSettings` via `pydantic` em vez de `pydantic-settings`
- servidor iniciado no transporte padrao `stdio`, que encerra imediatamente em Kubernetes

### Conexão recusada com Prometheus/Loki

Verifique se os serviços existem no namespace `monitoring`:
```bash
kubectl get svc -n monitoring | grep -E 'prometheus|loki'
```

Teste a conectividade de dentro do pod:
```bash
kubectl exec -it <pod-name> -n mcp-server -- bash
curl http://prometheus.monitoring.svc.cluster.local:9090/api/v1/status/buildinfo
```

### ImagePullBackOff

A imagem precisa estar disponível no ambiente Docker/Kubernetes:
```bash
# Build localmente
docker build -t microservices-demo/mcp-observability-server:latest \
  -f mcp-observability-server/Dockerfile mcp-observability-server/

# Se usar Minikube
eval $(minikube docker-env)
docker build -t microservices-demo/mcp-observability-server:latest \
  -f mcp-observability-server/Dockerfile mcp-observability-server/
```

## Próximas Etapas

1. Integrar com Ingress para acesso HTTP público
2. Adicionar PersistentVolume para cache de queries
3. Implementar autoscaling com HPA
4. Adicionar monitoring do próprio MCP server
