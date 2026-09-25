#!/usr/bin/env bash
# Watchdog de port-forwards essenciais para a coleta de experimentos.
# Mantém SEMPRE vivos (reinicia se cair):
#   * MCP observability  18080 -> mcp-server/mcp-observability:8000  (coleta de métricas)
#   * Prometheus-v3      9091  -> monitoring/prometheus-v3:9090      (check_infra / validate_metrics)
#
# Uso:
#   bash experiment/eval/gat/ensure-port-forwards.sh        # foreground (logs)
#   nohup bash experiment/eval/gat/ensure-port-forwards.sh >/tmp/pf-watchdog.log 2>&1 &   # background
# Parar: pkill -f ensure-port-forwards.sh

set -u

declare -A PF_PID=()

start_pf() {
  local name="$1" local_port="$2" ns="$3" svc="$4" svc_port="$5"
  # já há um port-forward escutando na porta? então não duplica
  if curl -s -m 3 -o /dev/null "http://127.0.0.1:${local_port}/" 2>/dev/null || \
     (exec 3<>"/dev/tcp/127.0.0.1/${local_port}") 2>/dev/null; then
    (exec 3>&-) 2>/dev/null || true
    return 0
  fi
  kubectl port-forward -n "$ns" "svc/${svc}" "${local_port}:${svc_port}" \
    >/tmp/pf-${name}.log 2>&1 &
  PF_PID[$name]=$!
  echo "[$(date +%H:%M:%S)] start ${name} :${local_port} (pid ${PF_PID[$name]})"
}

# loop de vigilância
trap 'echo "stopping"; kill ${PF_PID[@]} 2>/dev/null' EXIT INT TERM
while true; do
  start_pf "mcp"        18080 mcp-server mcp-observability 8000
  start_pf "prometheus" 9091  monitoring prometheus-v3     9090
  sleep 10
done
