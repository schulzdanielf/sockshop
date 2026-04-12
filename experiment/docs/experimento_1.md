# Experimento 1: Carga baseline

## Objetivo
O objetivo deste experimento é estabelecer uma linha de base de desempenho para a aplicação, utilizando uma carga moderada de usuários. Além disso, o experimento visa coletar métricas de desempenho e exercitar o monitoramento da aplicação utilizando o Prometheus.

## Identificação do Experimento
- **ID do Experimento**: baseline_20260412_001
- **Data de Execução**: 12 de abril de 2026
- **Descrição**: Este experimento tem como objetivo estabelecer uma linha de base de desempenho para a aplicação, utilizando uma carga moderada de usuários. Além disso, o experimento visa coletar métricas de desempenho e exercitar o monitoramento da aplicação utilizando o Prometheus.
## Ambiente de Teste
- **Kubernetes** 
    - Tipo: Kubernetes local (Docker Desktop)
    - Versão do Kubernetes: 1.32.2
    - Numero de Nós: 1
- Hardware do Nó:
    - CPU: 16 vCPUs
    - Memória: 23 GB
## Aplicação
- Nome: Sock Shop
- Versão: 
- Link: https://github.com/microservices-demo/microservices-demo

## Configurações de Carga
- Número de Usuários Simulados: 50
- Taxa de Spawn: 20 usuários por segundo
- Tipo de Carga: Carga constante
- Cenário de Teste: Simulação de navegação típica do usuário, incluindo acesso à página inicial, página de catálogo, página de carrinho e página de login. Cada usuário espera aleatóriamente entre 1 e 3 segundos entre as requisições.

## Configurações de Tempo
- Duração do Experimento: 10 minutos
- Período de Aquecimento: 1 minuto
- Intervalo de Coleta de Métricas: 30 segundos

## Métricas Coletadas
- Taxa de Requisições por Segundo (RPS): sum(rate(request_duration_seconds_count{route!="metrics"}[1m]))
- Latência P95: histogram_quantile(0.95, sum(rate(request_duration_seconds_bucket{name="front-end"}[1m])) by (le))
- Taxa de Erros: sum(rate(request_duration_seconds_count{route!="metrics",status_code=~"5.."}[1m]))
## Ferramentas Utilizadas
- Locust para simulação de carga
- Prometheus para monitoramento e coleta de métricas
- Grafana para visualização de métricas
## Resultados
  - Arquivo CSV com os resultados: [resultado_experimento_1.csv](experiment/data/resultado_experimento_1.csv)
  - Observações Iniciais:
    - Sistema estável, sem erros.
    - p95 baixo, de 19 ms. Começou em torno de 4 ms e subiu para 19 ms.
## Limitações
- O experimento foi realizado em um ambiente local, o que pode não refletir o desempenho em um ambiente de produção.
- A carga simulada pode não representar completamente o comportamento real dos usuários.
- Não foi aplicada engenharia do caos.
- Não foi realizda a autoinstrumentação com open telemetry.


    