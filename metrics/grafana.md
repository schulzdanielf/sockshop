# Métricas no grafana

## Requisições diferentes de 200:
request_duration_seconds_count{status_code!~"2.."}


## Tempo médio de resposta:
sum(rate(request_duration_seconds_sum{route="/"}[1m]))/sum(rate(request_duration_seconds_count{route="/"}[1m]))

## Consumo de CPU:
100 *
sum(
  rate(container_cpu_usage_seconds_total{
    namespace="sock-shop",
    pod=~"catalogue-.*",
  }[1m])
)
/
sum(
  kube_pod_container_resource_limits{
    namespace="sock-shop",
    pod=~"catalogue-.*",
    resource="cpu",
    unit="core"
  }
)

## Consumo de memória:
100 *
sum(
  container_memory_working_set_bytes{
    namespace="sock-shop",
    pod=~"catalogue-.*",
  }
)
/
sum(
  kube_pod_container_resource_limits{
    namespace="sock-shop",
    pod=~"catalogue-.*",
    container="catalogue",
    resource="memory",
    unit="byte"
  }
)