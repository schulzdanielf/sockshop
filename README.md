# Microservices Demo para AIOps

Este repositório evolui o Sock Shop como base experimental para estudos de observabilidade, testes de carga, engenharia de caos e enriquecimento de dados para AIOps. O foco deixou de ser apenas demonstrar uma aplicação de microserviços e passou a ser a construção de um ambiente reproduzível para análise operacional.
 
 O projeto é usado para coletar sinais de execução sob condições normais e sob perturbação controlada, com o objetivo de apoiar tarefas como análise de causa raiz, identificação automática de gargalos e melhoria de arquitetura.
 
 ## Visão Geral
 
 O sistema é uma adaptação do Sock Shop original, mantida como projeto próprio e orientada a pesquisa aplicada. A aplicação continua representando uma loja virtual baseada em microserviços, mas o repositório foi estendido com:
 
 - ajustes de deployment para Kubernetes
 - stack de observabilidade com Prometheus, Grafana, OpenTelemetry, Loki e Tempo
 - carga sintética com Locust
 - experimentos de caos com Litmus
 - artefatos de experimento e documentação de apoio
 
 Referências úteis:
 
 - [internal-docs/design.md](./internal-docs/design.md)
 - [internal-docs/testing.md](./internal-docs/testing.md)
 - [deploy/kubernetes/manifests-monitoring/README.md](./deploy/kubernetes/manifests-monitoring/README.md)
 - [experiment/docs/experimento_1.md](./experiment/docs/experimento_1.md)
 
 ## Objetivo de AIOps
 
 O objetivo central do projeto é enriquecer a coleta de dados operacionais para experimentos e rotinas de AIOps. Em termos práticos, isso significa produzir uma base observável e reproduzível para:
 
 - análise de causa raiz
 - identificação automática de gargalos
 - avaliação de impacto arquitetural sob carga e falhas induzidas
 - correlação entre métricas, logs e eventos de falha
 
 A estratégia do repositório é combinar três frentes:
 
 - observabilidade contínua da aplicação
 - geração controlada de carga
 - injeção controlada de falhas
 
 ## Arquitetura do Projeto
 
 A aplicação preserva a estrutura de microserviços do Sock Shop, com serviços independentes para catálogo, carrinho, pedidos, usuários, pagamento e front-end. O ambiente alvo atual é Kubernetes, com manifests organizados em [deploy/kubernetes](./deploy/kubernetes).
 
 Além da aplicação em si, o repositório organiza os recursos de apoio em áreas bem definidas:
 
 - [deploy/](./deploy): manifests e composições de deployment
 - [experiment/](./experiment): scripts, resultados e documentação de experimentos
 - [graphs/](./graphs): dashboards e utilitários para visualização
 - [metrics/](./metrics): notas e consultas relacionadas a métricas
 - [internal-docs/](./internal-docs): documentação técnica complementar
 
 ## Quickstart
 
 Para subir rapidamente o ambiente principal no cluster atual:
 
 ```bash
 make cluster-up
 make port-forward
 ```
 
 Principais endpoints expostos pelo port-forward:
 
 - aplicação: `http://localhost:8080`
 - Grafana: `http://localhost:3000`
 - Prometheus: `http://localhost:9090`
 - Jaeger UI: `http://localhost:16686`
 - Locust: `http://localhost:8089`
 
 Comandos úteis adicionais:
 
 ```bash
 make app-up
 make observability-up
 make loadtest-up
 make port-forward-check
 ```
 
 ## Observabilidade Atual
 
 O projeto mantém uma trilha principal de observabilidade voltada a métricas, logs e, em menor escala, traces. A arquitetura atual foi ajustada para privilegiar consistência de coleta e experimentação em ambiente Kubernetes.
 
 ### Fluxo de Métricas
 
 O fluxo de métricas está centrado em Prometheus e Grafana, com OpenTelemetry Collector atuando também como ponto de integração para sinais instrumentados.
 
 Fluxo resumido:
 
 1. os serviços expõem métricas operacionais e de aplicação
 2. o Prometheus coleta esses dados
 3. o OpenTelemetry Collector agrega e encaminha métricas conforme a configuração atual
 4. o Grafana consome Prometheus como principal fonte de visualização
 
 Exemplos de métricas já usadas nos experimentos:
 
 - taxa de requisições por segundo
 - latência p95
 - taxa de erros HTTP 5xx
 
 Essas métricas são usadas tanto para observação operacional quanto para comparação entre cenários baseline e cenários degradados.
 
 ### Fluxo de Logs
 
 O fluxo atual de logs segue uma trilha única via OpenTelemetry, evitando duplicidade de ingestão.
 
 Fluxo resumido:
 
 1. o OpenTelemetry Collector lê os logs dos containers do namespace `sock-shop`
 2. o receiver `filelog` faz o parsing do JSON do runtime do container
 3. os campos relevantes são promovidos para labels, com destaque para `namespace`, `pod`, `container` e `log_source`
 4. o Loki recebe os logs já normalizados
 5. o Grafana consulta o Loki para investigação operacional
 
 Pontos importantes do estado atual:
 
 - o pipeline de logs foi consolidado em OTel -> Loki
 - Promtail não deve ser executado junto com essa trilha para evitar duplicação
 - o collector foi ajustado para enviar linhas de log em formato mais limpo, reduzindo o ruído de campos redundantes
 
 Consultas úteis de partida no Loki:
 
 ```logql
 {log_source="otel_filelog", namespace="sock-shop"}
 ```
 
 ```logql
 {log_source="otel_filelog", namespace="sock-shop", pod=~"front-end.*"}
 ```
 
 ### Tracing
 
 Tracing está presente de forma complementar no ambiente atual. O stack inclui Tempo e componentes ligados a Jaeger para inspeção distribuída, com OpenTelemetry Collector exportando traces para o backend configurado.
 
 Neste momento, tracing não é a trilha principal do projeto, mas funciona como suporte para correlação pontual entre sintomas observados em métricas, logs e requisições distribuídas.
 
 ## Teste de Carga Atual
 
 O teste de carga atual usa Locust para gerar tráfego sintético sobre a aplicação. O objetivo não é apenas medir desempenho bruto, mas produzir dados controlados para análise de comportamento da arquitetura sob carga.
 
 Características do cenário atual:
 
 - ferramenta principal: Locust
 - padrão de navegação: jornada típica de usuário
 - ações simuladas: registro, login, navegação no catálogo, manipulação de carrinho, endereço e parte do fluxo de checkout
 - intervalo entre ações: entre 1 e 3 segundos
 - parte dos usuários tenta concluir compra
 
 O experimento baseline documentado até aqui registra:
 
 - 50 usuários simulados
 - taxa de spawn de 20 usuários por segundo
 - duração de 10 minutos
 - aquecimento de 1 minuto
 - coleta de métricas a cada 30 segundos
 
 Resumo do experimento inicial:
 
 - o sistema permaneceu estável
 - não houve erros relevantes no cenário baseline
 - a latência p95 observada ficou baixa no ambiente local, chegando a aproximadamente 19 ms
 
 Artefatos relacionados:
 
 - [experiment/docs/experimento_1.md](./experiment/docs/experimento_1.md)
 - [experiment/data/resultado_experimento_1.csv](./experiment/data/resultado_experimento_1.csv)
 - [deploy/kubernetes/manifests-loadtest/loadtest-configmap.yaml](./deploy/kubernetes/manifests-loadtest/loadtest-configmap.yaml)
 
 Para habilitar a carga atual:
 
 ```bash
 make loadtest-up
 make port-forward
 ```
 
 Depois disso, a interface do Locust fica acessível em `http://localhost:8089`.
 
 ## Engenharia de Caos
 
 A engenharia de caos é usada para induzir falhas controladas e observar como a aplicação e a camada de observabilidade respondem. Essa trilha é importante para gerar dados não apenas de degradação, mas também de recuperação e propagação de impacto.
 
 O projeto já contém experimentos Litmus configurados para cenários como:
 
 - deleção de pods do serviço `carts`
 - deleção de pods com sonda baseada em Prometheus
 - estresse de CPU no serviço `catalogue`
 
 Hipóteses operacionais que esses cenários ajudam a explorar:
 
 - como a aplicação reage à indisponibilidade súbita de um serviço
 - quais métricas se alteram primeiro durante a degradação
 - quais logs ajudam a localizar a origem do problema
 - se os sinais coletados são suficientes para apoiar análise de causa raiz
 
 Comandos úteis:
 
 ```bash
 make chaos-install
 make chaos-run-pod-delete
 make chaos-run-pod-delete-prom-probe
 make chaos-run-catalogue-cpu-hog
 make chaos-status
 ```
 
 Arquivos de referência:
 
 - [deploy/kubernetes/manifests-chaos/pod-delete-engine.yaml](./deploy/kubernetes/manifests-chaos/pod-delete-engine.yaml)
 - [deploy/kubernetes/manifests-chaos/catalogue-cpu-hog.yaml](./deploy/kubernetes/manifests-chaos/catalogue-cpu-hog.yaml)
 
 ## Relação Entre Observabilidade, Carga e Caos
 
 O valor do projeto está menos em cada mecanismo isolado e mais na combinação entre eles.
 
 - a carga sintética produz comportamento repetível
 - a engenharia de caos introduz perturbações controladas
 - a observabilidade registra a resposta do sistema em múltiplos sinais
 
 Essa composição cria uma base adequada para comparar estado normal e estado degradado, correlacionar sintomas e investigar padrões úteis para AIOps.
 
 ## Estado Atual e Limitações
 
 O projeto está funcional como ambiente experimental, mas ainda é uma base em evolução. As principais limitações atuais são:
 
 - a maior parte das validações foi feita em ambiente Kubernetes local
 - os resultados de desempenho não devem ser tratados como equivalentes a produção
 - tracing existe, mas ainda não é a trilha mais madura do projeto
 - parte da documentação operacional ainda está distribuída em arquivos internos e manifests
 - os experimentos de AIOps ainda dependem de ampliação de cenários, datasets e critérios de avaliação
 
 Na trilha de logs, o fluxo único via OpenTelemetry está estabelecido, mas ainda há espaço para refinar a apresentação de metadados e a experiência de análise no Grafana.
 
 ## Próximos Passos Naturais
 
 - ampliar o catálogo de experimentos de caos
 - consolidar consultas e dashboards para comparação entre baseline e falha
 - enriquecer a correlação entre métricas, logs e traces
 - estruturar datasets e rotinas para análise de causa raiz e detecção de gargalos
 
 ## Origem do Projeto
 
 Este trabalho usa o Sock Shop como base arquitetural, mas o repositório foi reposicionado como um projeto próprio, voltado a observabilidade experimental e AIOps.
 
 Projeto original:
 
 - [microservices-demo/microservices-demo](https://github.com/microservices-demo/microservices-demo)
