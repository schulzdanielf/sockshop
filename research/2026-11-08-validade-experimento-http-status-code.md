# Validade do experimento `http-status-code` na Sock Shop

**Data:** 08/11/2026.  
**Finalidade:** subsidiar a decisão metodológica e a descrição do experimento no artigo.  
**Natureza:** revisão da implementação, de artefatos de execuções anteriores e de fontes públicas, complementada por nova coleta histórica online na seção 13. Não constitui nova campanha ou nova pontuação de RCA.

## 1. Resumo e parecer

O experimento atual **não injeta uma exceção dentro do código da Sock Shop**. Ele configura o Istio para abortar chamadas HTTP destinadas a um serviço, devolvendo HTTP 500 no proxy Envoy, antes de encaminhar essas chamadas à aplicação de destino. As rotas das probes são excluídas.

Essa intervenção é artificial e controlada, como outras intervenções de chaos engineering. Entretanto, o tráfego, as respostas recebidas pelos chamadores e a telemetria produzida pelo sistema são observações reais da execução; não são amostras de erro fabricadas posteriormente.

A flag `FI`, de `FaultInjected`, é produzida pelo próprio Envoy. Preservá-la na observabilidade é tecnicamente coerente. **Entregá-la ao modelo, explicando seu significado, torna a tarefa mais informativa e específica para o injetor.** Isso precisa ser declarado, controlado por ablação e separado da capacidade de diagnosticar erros naturais da aplicação.

**Parecer:** o cenário é defensável como experimento de **abort HTTP no service mesh e propagação de falhas de dependência**, mas não deve ser apresentado como equivalente a uma falha interna de aplicação, banco de dados ou armazenamento. Os resultados atuais, por si sós, não validam generalização para incidentes naturais de produção.

Antes de sustentar essa generalização, há quatro pontos prioritários:

1. Separar a confirmação privada da intervenção dos dados oferecidos ao diagnóstico; comparar condições com e sem indicadores explícitos do injetor.
2. Definir o alvo de RCA como destino/aresta afetada e distinguir esse alvo do componente que efetivamente produziu a resposta.
3. Corrigir e revisar a avaliação de mecanismos: um diagnóstico pode acertar o serviço rotulado e ainda inventar uma causa de IO.
4. Acrescentar cenários de erro gerado na aplicação e repetições independentes se essa for a população de incidentes reivindicada no artigo.

O uso do mesmo benchmark ou de uma ferramenta citada por outros artigos não elimina essas exigências.

## 2. O que significa `http-status-code` neste projeto

### 2.1 Três conceitos que não devem ser confundidos

| Conceito | Definição | O que demonstra |
|---|---|---|
| Status HTTP 500 | Código da resposta recebida pelo cliente | Uma requisição terminou com erro; não identifica sozinho quem o produziu |
| Tipo experimental `http-status-code` | Nome da intervenção controlada | Qual cenário o pesquisador configurou, não uma evidência independente para o modelo |
| Categoria causal `http-error` | Categoria utilizada na saída do diagnóstico | Uma classificação; não explica automaticamente o mecanismo |

HTTP e HTTPS não são classes diferentes de status 500: HTTPS acrescenta TLS ao transporte. Os exemplos desta campanha usam URLs `http://` e serviço na porta 80. Não se trata de um experimento de certificado, handshake ou criptografia.

### 2.2 Implementação atual

A função [_render_http_status_code](../experiment/eval/memory_hog/generate_chaos_manifests.py), linhas 408–458 na leitura desta revisão, gera uma `VirtualService`, e não um workflow Litmus de modificação do processo. O trecho determinante é:

```yaml
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: <identificador-da-intervencao>
  namespace: sock-shop
spec:
  hosts:
    - payment
  http:
    - match:
        - uri:
            exact: /health
      route:
        - destination:
            host: payment
            port:
              number: 80
    - fault:
        abort:
          percentage:
            value: 100
          httpStatus: 500
      route:
        - destination:
            host: payment
            port:
              number: 80
```

É uma ilustração do manifesto realmente gerado, com o nome substituído por um marcador. A regra normal de `/health` precede a regra de abort.

Para `front-end`, a exclusão configurada é `/`; para os demais serviços presentes no mapa de probes, é `/health`. A regra catch-all abrange as outras rotas do host, não apenas uma operação de negócio escolhida.

A intensidade nominal é **100% das chamadas que correspondem à regra de abort**. Não significa 100% de todo o tráfego da aplicação: probes, chamadas a outros serviços e requisições fora do escopo da regra continuam podendo funcionar.

A [campanha controlada](../experiment/eval/memory_hog/controlled_campaign.py), linhas 709–716, salva esse manifesto na tentativa, atribui um nome próprio e entrega seu caminho ao executor. O [executor](../experiment/platform/backend/controlled_experiment.py):

- verifica tráfego de baseline na aresta e disponibilidade de métricas;
- recusa sobrescrever ou competir com uma `VirtualService` existente para o host;
- aplica a intervenção;
- lê o objeto de volta na API Kubernetes e compara a especificação;
- aguarda o período ativo;
- remove a intervenção e verifica a limpeza;
- acompanha recuperação e parada da carga.

**Ler a configuração de volta comprova o objeto na API, não o instante exato em que cada Envoy recebeu/aplicou a regra.** O próprio executor registra essa limitação.

### 2.3 Por que não é o mesmo que o mecanismo Litmus antigo

O repositório também contém um [manifesto Litmus `pod-http-status-code`](../deploy/kubernetes/manifests-chaos/catalogue-http-status-code.yaml), com proxy na porta 20000 e alteração de tráfego via rede do pod.

O comentário do gerador registra que esse mecanismo anterior era contornado pelo sidecar no ambiente Istio. Essa é a justificativa histórica registrada no código para usar `fault.abort`; esta revisão não repetiu a comparação de rede para comprovar novamente o bypass.

Não atribuir indistintamente a Litmus todas as falhas do artigo: os três runs HTTP examinados têm uma `VirtualService` aplicada. A ferramenta e o mecanismo devem ser descritos por tipo de intervenção e por versão da campanha.

## 3. Onde a resposta 500 é produzida

### 3.1 Erro produzido pela aplicação

```text
orders -> proxy -> payment recebe e executa a chamada
                      |
                      +-> erro interno -> resposta 500 -> orders
```

A instrumentação do servidor pode observar a execução, status, exceção e span SERVER. O chamador pode observar um span CLIENT e o erro recebido.

### 3.2 Abort injetado no mesh

```text
orders -> Envoy com filtro de fault injection
             |
             +-> resposta local HTTP 500 -> orders
             |
             +-> a chamada abortada não é encaminhada a payment
```

A regra referencia o destino `payment`, mas isso não torna o processo `payment` o produtor do erro. Em tráfego interno com reporte `source`, o sinal pode aparecer no proxy do chamador; a localização exata deve ser associada ao reporter e à configuração efetiva do dataplane.

Também não se deve confundir o container `istio-proxy` com o `front-end-envoy` adicional existente no deployment. O [manifesto de front-end](../deploy/kubernetes/manifests/09-front-end-dep.yaml) informa a remoção de `HTTP_PROXY`/`HTTPS_PROXY` da aplicação; a mera presença desse segundo Envoy não comprova que ele transporta essas chamadas.

**O alvo experimental é o serviço cujas chamadas são sabotadas; o mecanismo observado é o abort no proxy.** Uma RCA adequada pode localizar a aresta `orders -> payment` e explicar a falha sem afirmar que payment sofreu uma exceção interna.

## 4. Origem de `500/FI` e do enriquecimento da telemetria

`500/FI` é uma abreviação usada na análise para dois campos:

```text
response_code = "500"
response_flags = "FI"
```

`FI` significa `FaultInjected`; `UF` significa `UpstreamConnectionFailure`. O primeiro não é uma forma abreviada de erro de conexão. A flag identifica um abort com código de erro injetado, e não revela, sozinha, qual recurso/configuração administrativa o autorizou.

Há três etapas distintas:

1. **Produção:** o Envoy marca o resultado do filtro de fault injection.
2. **Exposição:** o Istio expõe dimensões de requisição em `istio_requests_total`; o formato de access log pode incluir `%RESPONSE_FLAGS%`.
3. **Consumo/enriquecimento:** as consultas preservam a dimensão, e o Collector pode copiá-la e derivar uma classificação.

No código examinado:

- [istio_edges.py](../experiment/platform/backend/analysis/istio_edges.py), linhas 28–32 e 80–85: `sum by (...)` preserva `response_flags`, `response_code`, origem, destino e reporter. Não insere o literal `FI` nas séries.
- [configure_http_access_logs.py](../experiment/platform/tools/configure_http_access_logs.py), linhas 15–34: `%RESPONSE_FLAGS%` solicita o valor produzido pelo Envoy. Não configura um valor constante `FI`.
- [Collector OTel](../deploy/kubernetes/manifests-monitoring/27-otel-collector-configmap.yaml), linhas 84–92: copia a flag do log estruturado e **adiciona um atributo derivado**, `error_fingerprint=proxy_fault_injection`, quando encontra `FI`.
- [migrate_observability_history.py](../experiment/platform/tools/migrate_observability_history.py), função `migrate_metrics`: copia amostras existentes, verifica conflitos e igualdade de valores/timestamps e pode preservar/adicionar o identificador de campanha histórica. Não cria erros 500 ausentes na origem nem injeta `FI` em séries sem essa flag.

Portanto, não houve fabricação de `FI` para favorecer o diagnóstico. Porém, houve uma escolha de **disponibilizar e interpretar um indicador explícito do mecanismo**. O fingerprint é uma derivação, não uma segunda observação independente.

Instrumentação nova não recupera logs ou spans que nunca foram emitidos. Migração só pode preservar o que ainda existe na origem; comprovar integridade da cópia não comprova completude do incidente original.

## 5. Como a Sock Shop reage

### 5.1 Topologia pertinente

A [system card](../experiment/platform/data/system_cards/sock-shop.yaml) descreve:

```text
cliente -> front-end
              +-> user -> user-db
              +-> catalogue -> catalogue-db
              +-> carts -> carts-db
              +-> orders
                    +-> user
                    +-> carts
                    +-> payment
                    +-> shipping -> RabbitMQ -> queue-master
                    +-> orders-db
```

É contexto estático, não evidência de uma execução. As setas não estabelecem a ordem exata de todas as operações, retries ou persistências.

`payment` é uma autorização simulada em memória, sem gateway externo na card. `orders` orquestra chamadas a várias dependências. `shipping` aceita chamadas HTTP e participa de um pipeline assíncrono com RabbitMQ.

### 5.2 Efeitos esperados por destino

| Destino sabotado | Reação plausível/esperada | O que não concluir sem evidência |
|---|---|---|
| payment | Orders recebe erro na autorização; checkout pode falhar e reduzir chamadas posteriores | Falha em gateway externo, disco ou banco de payment |
| shipping | Orders recebe erro ao solicitar envio; requisições abortadas não chegam ao handler de shipping | Crescimento obrigatório da fila RabbitMQ; falha do consumidor queue-master |
| user | Login/cadastro e consultas de usuário falham nos chamadores; a carga de checkout pode cair por mudança do fluxo de clientes | Falha comprovada de user-db ou de uma dependência externa não identificada |
| carts/catalogue | Falhas em operações de carrinho/catálogo propagam-se ao front-end e podem impedir checkout | Causa interna no banco apenas porque apareceu um 500 |
| orders/front-end | O abort pode impedir operações inteiras no ponto de entrada/orquestração | Que todas as dependências ficaram defeituosas ou que cada queda de tráfego é uma causa |

Esses efeitos são hipóteses operacionais a verificar, não resultados medidos para todos os serviços.

Uma falha rápida pode **reduzir**, não aumentar, a latência do alvo: a execução normal foi evitada. CPU/memória do destino podem cair pela diminuição da carga, enquanto o chamador pode consumir recursos tratando exceções. Nenhum desses sinais isolados comprova exaustão de recursos.

A exclusão das probes permite prontidão saudável durante falhas de negócio. Reinício de pod não é necessário para que a intervenção produza erro; restart preexistente não comprova um novo evento.

### 5.3 Evidência efetivamente registrada em três runs

Foram examinados os artefatos originais de:

- [shipping — run-abc3a42dc026](../experiment/platform/data/object_store/run-abc3a42dc026/);
- [payment — run-73da08fee5d3](../experiment/platform/data/object_store/run-73da08fee5d3/);
- [user — run-29cf2c69c070](../experiment/platform/data/object_store/run-29cf2c69c070/).

Os `http_applied.json` confirmam `fault.abort`, HTTP 500, percentual 100 e exclusão de `/health`. Os `effective_spec.json` registram Locust com 30 usuários, spawn rate 5, URL HTTP de front-end e tempos nominais de warmup 60 s, baseline 90 s, intervenção 120 s e post 120 s, com amostragem configurada de 5 s.

Tempos nominais não são as fronteiras exatas: os `observation_window.json` registram intervalos que incluem espera/coleta. Para métricas e comparação, utilizar as janelas registradas e explicar a convenção, não reconstruí-las apenas somando durações.

Nos `controlled_report.json`, as médias das taxas observadas pelo reporter de destino foram:

| Run/alvo | Tráfego no destino: baseline -> intervenção (req/s) | Outro efeito observado |
|---|---:|---|
| shipping | 4,5058 -> 0,7032 | Orders continuou recebendo cerca de 5 req/s; shipping recebeu menos chamadas |
| payment | 4,9611 -> 0,6768 | Shipping caiu de 4,5228 para 0,7109 req/s |
| user | 51,6951 -> 6,5066 | Orders caiu de 4,7957 para 0,7947 req/s; payment/shipping também receberam menos tráfego |

Essas médias são de janelas amostradas, não contagens de operações de negócio concluídas. As três execuções registram `completed`, limpeza e recuperação aprovadas. Shipping tinha contador de restart acumulado 1 antes e durante a intervenção; nos outros dois, o contador registrado era 0. A estabilidade desses contadores não sustenta um restart novo nessas observações.

**Limitação importante:** os relatórios originais registram `errors_percent=0` e `target_edge_error_percent=0` durante a intervenção, com `target_edge_error_increased=false`. Também classificam `mechanism_observed` como `configured`, apoiado no readback da API. Não se deve transformar esse `completed` em comprovação de impacto HTTP por esses campos.

A discrepância é compatível com a lacuna investigada na seleção de identidade/reporter: um abort pode ser reportado na origem com `destination_workload=unknown` e FQDN do serviço, enquanto métricas no destino não observam a chamada abortada. Os artefatos disponíveis sustentam a discrepância; esta revisão não executou uma reprodução adicional de todas as versões de consulta do controlador.

### 5.4 Consultas posteriores e o piloto de B

O [piloto de reauditoria de 08/11](../experiment/eval/memory_hog/out/knowledge_rebuild/rag-training-http-eligibility-pilot-rerun-20261108/pending_reaudit.json) consultou novamente os datasources históricos via MCP. Os artefatos N012 do próprio piloto registram:

| Run | Aresta com 500/FI em fault | Média da taxa 500/FI (req/s) | Conclusão possível |
|---|---|---:|---|
| run-abc3a42dc026 | orders -> shipping | 3,8176 | Abort observado em chamadas destinadas a shipping |
| run-73da08fee5d3 | orders -> payment | 4,5031 | Abort observado em chamadas destinadas a payment |
| run-29cf2c69c070 | front-end -> user | 5,5395 | Abort observado em chamadas destinadas a user |

Os recebimentos N012 foram registrados em 08/11 às 13:02:01, 13:08:31 e 13:14:25, respectivamente, no fuso UTC−03. São consultas históricas posteriores sobre os runs de 04/11 e 05/11, não novas injeções.

Em user, também existe uma série rotulada `orders -> user`, 500/FI, mas sua média e máximo em fault são zero. **A presença da série/flag não comprova ocorrência positiva nessa aresta.**

Nessas coletas aparecem logs de chamadores com `HttpServerErrorException: 500` em orders e erros de login/respostas 500 no front-end. Isso mostra que a telemetria da aplicação **já capturava sintomas HTTP**. O déficit era de identificação do mecanismo/aresta e de aproveitamento/vinculação da evidência, não uma impossibilidade de detectar 500.

Em user, a consulta direcionada de service graph retornou `no_data`; logs do próprio user não trouxeram o erro específico procurado. Estados `ok` de consultas gerais não significam cobertura exaustiva, e `no_data` de uma consulta não significa inexistência de todos os traces.

As médias N012 são projeções de `rate(...[1m])` com amostragem de 15 s. Não são quantidade total de erros nem equivalem exatamente à duração física da injeção: o lookback atravessa fronteiras de fase.

**Esta seção descreve resultados já persistidos. Não utiliza os artefatos como nova evidência para pontuar uma nova avaliação.**

## 6. Por que a telemetria tradicional pode parecer não mostrar o erro

| Ponto de observação | Abort no proxy | Erro interno no servidor |
|---|---|---|
| Handler do serviço-alvo | Não executa a chamada abortada | Executa e pode registrar erro |
| Métrica HTTP do servidor-alvo | Pode não contabilizar a chamada abortada | Pode contabilizar status 500 |
| Span SERVER do alvo | Pode não existir para a chamada | Pode existir, conforme instrumentação/amostragem |
| Span CLIENT/log do chamador | Pode mostrar 500 recebido | Também pode mostrar 500 recebido |
| Métrica/access log do Envoy que abortou | Pode mostrar status 500, flag FI e destino pretendido | Pode mostrar o erro encaminhado, sem FI |

A observabilidade do cliente deve preservar destino, operação, timestamp, status e IDs de correlação quando disponíveis. Um log genérico de checkout não identifica sozinho qual dependência falhou.

No executor de B, o inventário ainda informa as famílias SDK `http_client_*`/`http_server_*` como `not_queried`, por falta de contrato executável registrado naquele fluxo, conforme [README do treinamento](../experiment/eval/memory_hog/README.md). Ter uma família no datasource não significa que o modelo recebeu uma ferramenta utilizável para consultá-la.

Métricas de aplicação, métricas do mesh, logs e traces são complementares. Duas consultas Prometheus não são duas famílias independentes; access log e métrica do mesmo proxy podem ser representações correlacionadas do mesmo evento. A diversidade de formatos não substitui avaliação da proveniência e do conteúdo causal.

## 7. O que a auditoria adicional valida — e o que ela não valida

O fluxo descrito em [training_b_rebuild.py](../experiment/eval/memory_hog/training_b_rebuild.py) e no [README](../experiment/eval/memory_hog/README.md) tem três partes:

1. **Autoria supervisionada de B:** o modelo recebe serviço e tipo de falha do treino, consulta evidência e escreve um guia. Isso não é diagnóstico cego de teste.
2. **Revisão factual/procedimental e RAG Auditor:** chamadas adicionais ao modelo revisam caminho, alegações e alternativas; podem pedir consultas/correções ou deixar pendente. O auditor usa guias históricos elegíveis como orientação, não como evidência do run atual.
3. **Gates determinísticos:** verificam requisitos como IDs válidos, consulta discriminante, cadeia causal, famílias utilizáveis e restart sustentado quando aplicável.

O gate HTTP atual exige `FI` para confirmar o tipo de intervenção e, na verificação compartilhada de famílias, taxa 5xx/FI positiva associada ao destino e erro HTTP do alvo ou de seu chamador correlacionado. Isso é uma política do experimento, **não uma necessidade universal da RCA de HTTP 500**.

O resultado do piloto foi 1/3 `procedurally_validated` (payment), 2/3 pendentes (shipping e user), com `publication_allowed=false`.

**Há uma ressalva factual crítica no caso aprovado:** o B de payment identifica payment, mas atribui sua causa a IO em orders e especula sobre volume persistente/orders-db. O B de shipping também inverte a propagação ao sugerir que orders envia o erro para shipping. Essas narrativas não são comprovadas por uma exceção genérica recebida em orders e não explicam corretamente o abort `orders -> destino`.

O B de user especula sobre dependência externa não identificada e cita somente N012; a system card inclusive registra user-db, não sustenta a dedução de que uma dependência desconhecida existe por ausência de downstream.

Consequentemente:

- `confirmed` escrito pelo modelo não significa causa confirmada por revisão independente;
- `procedurally_validated` não é certificação científica;
- acertar `primary_service` e `fault_type` não garante mecanismo ou direção causal corretos;
- a aprovação pelo auditor do mesmo tipo de modelo pode repetir erros do autor.

Esses achados são uma revisão documental qualitativa, não uma alteração da pontuação/report do piloto. **Não usar o 1/3 aprovado como prova de que agora há uma RCA causalmente correta em payment sem revisar a narrativa.**

O [relatório de teste anterior](./diario/test-base-v6-20261107T215646147881Z-relatorio-skill-rag.md) já separa acerto de classificação e confirmação: registra 7 casos HTTP, 7 acertos do tipo, 2 do conjunto serviço+tipo, nenhum par HTTP elegível no Top-3 e diagnósticos inconclusivos. Esse resultado retrospectivo não permite atribuir, sozinho, o acerto do tipo à flag FI.

## 8. Dados artificiais, evidência real e vazamento

### 8.1 O que é artificial

O pesquisador causa deliberadamente respostas que não ocorreriam naquele momento sem a intervenção. O abort é uma simulação de indisponibilidade funcional/erro de dependência, não uma descoberta de bug espontâneo.

Isso é diferente de inserir eventos fictícios em arquivos de evidência. O cliente executa chamadas reais, recebe respostas reais do proxy e reage com o código real da aplicação.

### 8.2 O risco de atalho diagnóstico

Uma flag produzida pelo sistema pode ser evidência tecnicamente legítima e, ao mesmo tempo, um atalho para o benchmark. `FI` explicita a intervenção; `proxy_fault_injection` explicita o mesmo fato em outro formato.

Mesmo que o modelo não receba a coluna privada `fault_type`, pode reconhecer a classe experimental por essa assinatura. Isso não é necessariamente vazamento literal do arquivo de gabarito, mas ameaça à validade de construto e à transferência para ambientes sem esse marcador.

Além disso, no fluxo Skill há orientação explícita sobre o significado de FI em [rca_skill.py](../experiment/platform/backend/rca_skill.py), próximo da linha 1198. No treinamento B, os rótulos são fornecidos intencionalmente para autoria supervisionada. As duas condições devem ser descritas separadamente.

**A interpretação depende da pergunta científica:**

- Se a pergunta é diagnosticar incidentes com toda a observabilidade operacional disponível, o uso de indicadores do proxy pode ser permitido, declarado e medido.
- Se a pergunta é inferir falhas naturais apenas por sintomas ou comparar raciocínio causal sem pistas do injetor, esses indicadores devem ficar fora da entrada e do caminho de decisão.
- Se a pergunta é detectar que um teste de chaos engineering foi acionado, FI é especialmente informativa; isso é outra tarefa.

### 8.3 Separação recomendada

| Camada | Pode consultar configuração/FI? | Papel |
|---|---|---|
| Validação privada da intervenção | Sim | Confirmar aplicação, efeito, janela e limpeza |
| Diagnóstico com observabilidade completa | Sim, explicitamente | Medir desempenho no cenário instrumentado |
| Diagnóstico sem indicadores do injetor | Não | Medir localização/explicação a partir de sintomas |
| Avaliação final independente | Sim, após persistir saídas | Comparar com intervenção e evidências sem instruir o diagnóstico |

Para a condição sem indicador:

- remover FI e campos derivados, inclusive de logs brutos, resumos, detalhes do abort e respostas de ferramentas;
- evitar nomes de recursos/campanhas/workflows ou rótulos que revelem o cenário;
- preservar status 500, destinos, timestamps e medidas necessárias à observação;
- neutralizar somente a pista de injeção, documentando se outras flags são preservadas;
- não exigir FI no gate dessa condição, pois isso bloquearia artificialmente todos os casos sanitizados;
- verificar também os guias recuperados, prompts e evidências do corpus; sanitizar apenas a consulta final não elimina um atalho já aprendido no B.

O sanitizador e os gates separados são **recomendações**, não mudanças implementadas por este documento.

## 9. Comparação com artigos e documentação técnica

A comparação bibliográfica abaixo distingue evidência de adoção do mesmo mecanismo, cenários parcialmente equivalentes e trabalhos apenas relacionados. Nenhuma fonte externa certifica automaticamente nosso desenho.

### 9.1 Artigo mais próximo: benchmark com manipulação HTTP

**Aoyang Fang et al. — “Rethinking the Evaluation of Microservice RCA with a Fault Propagation-Aware Benchmark”**, preprint de 2025, versão arXiv v2:

- [Registro e metadados](https://arxiv.org/abs/2510.04711).
- [Texto completo da versão consultada](https://arxiv.org/html/2510.04711v2).
- DOI do preprint: [10.48550/arXiv.2510.04711](https://doi.org/10.48550/arXiv.2510.04711).

**Onde verificar:** seção 4.4, *Fault Space Modeling*, quadro de 31 tipos de falhas. A categoria HTTP inclui `ReqAbort`, `RespAbort`, atrasos de request/response, modificação de body/path/method e `RespReplaceCode`. O trabalho implementa o espaço de falhas com ChaosMesh.

Esse é um precedente próximo para tratar manipulação HTTP como uma intervenção de benchmark de RCA. Entretanto, a ferramenta é ChaosMesh e o sistema utilizado é Train-Ticket, não a nossa combinação Istio/Envoy/Sock Shop.

**Diferença essencial:** alterar o status de uma resposta (`RespReplaceCode`) não deve ser descrito como operacionalmente idêntico a abortar antes de encaminhar a requisição. Na primeira possibilidade, o serviço pode ter executado a operação; no nosso abort, a chamada abortada não é executada no destino. A categoria HTTP agrupa intervenções com sintomas relacionados e mecanismos diferentes.

A [documentação oficial do HTTPChaos](https://chaos-mesh.org/docs/simulate-http-chaos-on-kubernetes/), nas descrições de tipos e campos de resposta, diferencia interrupção de conexão (`abort`) de substituição de status (`target: Response` com `replace.code`). Interromper a conexão não implica devolver HTTP 500. Não se verificou o comportamento do handler para todas as variantes request/response do ChaosMesh; não generalizar a propriedade do abort Envoy a essas variantes.

A seção 4.1 discute que uma injeção pode não produzir anomalia observável e descreve seleção baseada em impacto nos SLIs do usuário. Isso reforça a necessidade de verificar efeito, não apenas existência do manifesto.

A seção 4.2 justifica Train-Ticket por complexidade de dependências, contrastando com demonstradores menores, incluindo Sock Shop. É uma limitação de representatividade apontada pelos autores, **não uma prova de que Sock Shop seja inválida para qualquer pesquisa**.

Na seção 4.7, os rótulos de ground truth são derivados de parâmetros de injeção e fornecidos no dataset. Isso permite rastrear a intervenção, mas **não demonstra que esses rótulos foram apresentados como entrada aos métodos avaliados**. Não foi estabelecida, nesta pesquisa documental, uma política de remoção de `FI` para esse trabalho; ele não serve como evidência de que oferecer FI ao nosso modelo é prática validada.

### 9.2 Artigo que usa Sock Shop, mas com outras intervenções

**Luan Pham, Hongyu Zhang, Huong Ha, Flora Salim e Xiuzhen Zhang — “RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data”**, ACM Web Conference Companion, 2025:

- [Publicação — DOI 10.1145/3701716.3715290](https://doi.org/10.1145/3701716.3715290).
- [Texto completo da versão consultada](https://arxiv.org/html/2412.17015v5).
- [Repositório público do benchmark](https://github.com/phamquiluan/RCAEval).

**Onde verificar:** seção 3.1, sistemas; seção 3.2, injeção; seção 3.3, coleta.

O benchmark utiliza Sock Shop, Online Boutique e Train Ticket. Descreve:

- falhas de recursos com `stress-ng`, incluindo CPU, memória, disco e socket;
- falhas de rede com `tc`, incluindo atraso e perda;
- falhas de código, incluindo valores/parâmetros incorretos, chamadas ausentes, retorno incorreto e ausência de tratamento de exceções.

Prometheus, cAdvisor e Istio aparecem no contexto de observabilidade. **Usar Istio para medir não significa usar Istio `fault.abort` para injetar.**

Esse artigo fundamenta o uso de uma demo instrumentada e intervenções controladas em RCA, mas não é um precedente direto de HTTP 500 gerado por Envoy. A família de mutações de código é um contraste especialmente útil para nossa pergunta: ela interfere no comportamento executado pela aplicação, em vez de sintetizar a resposta no mesh. Não assumir que toda mutação de código produz 500.

O dataset contém anotações de serviço/indicador de causa. A existência dessas anotações não comprova exposição ao algoritmo no momento do diagnóstico; o estudo precisa separar rótulos de avaliação e features. Nesta revisão, não se verificou uma política específica de ocultação de flags de fault injection nesse artigo.

### 9.3 Artigo relacionado à propagação, não equivalente a HTTP 500

**Anatoly A. Krasnovsky — “Model Discovery and Graph Simulation: A Lightweight Gateway to Chaos Engineering”**, preprint de 2025, versão arXiv v2:

- [Registro e metadados](https://arxiv.org/abs/2506.11176).
- [Texto completo da versão consultada](https://arxiv.org/html/2506.11176v2).

O trabalho utiliza DeathStarBench Social Network e modelagem de dependências a partir de telemetria/traces. Adota hipótese de falhas fail-stop na granularidade de service-container.

**Onde verificar:** no texto da instanciação do modelo em DeathStarBench, imediatamente após a definição de disponibilidade estimada, antes dos resultados de replicação/não-replicação. O bloco descreve a hipótese fail-stop e frações de serviços falhos de 0,1 a 0,9. O número oficial dessa seção não foi confirmado na extração HTML; não se atribui um número por aproximação.

**Não é demonstração do mesmo experimento.** Um modelo fail-stop, ou um serviço parado, não é equivalente a um serviço vivo cujas chamadas são abortadas com HTTP 500. A modelagem de disponibilidade/propagação fornece contexto, mas não valida nosso injetor nem os nossos marcadores de telemetria. Não foi verificada uma política de ocultação de indicadores do injetor para esse trabalho.

### 9.4 Documentação das ferramentas — fontes técnicas, não artigos

**Istio — [Fault Injection](https://istio.io/latest/docs/tasks/traffic-management/fault-injection/).**

Na seção *Injecting an HTTP abort fault*, a documentação demonstra `VirtualService` com `fault.abort`, `httpStatus` e `percentage` em Bookinfo. É o mecanismo-base que utilizamos, mas o exemplo pode restringir chamadas por correspondência de cabeçalho. A escolha de abrangência catch-all no nosso projeto é parte do nosso desenho, não uma exigência da ferramenta.

**Envoy — [HTTP fault injection filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/fault_filter).**

A documentação explica atraso e abort com códigos definidos pelo usuário para testar resiliência. Limita o escopo a falhas observáveis por uma aplicação que se comunica pela rede: o filtro não emula falhas locais de CPU/disco.

O mapeamento de `FI` para `FaultInjected` está no [código público de definição das flags](https://github.com/envoyproxy/envoy/blob/main/source/common/stream_info/utility.h); a [interface de StreamInfo](https://github.com/envoyproxy/envoy/blob/main/envoy/stream_info/stream_info.h) descreve a flag como abort com código de erro injetado. A definição foi conferida em 08/11/2026. Esses links acompanham `main`, não são snapshots imutáveis; o artigo deve fixar a versão/commit do Envoy efetivamente utilizado.

**LitmusChaos — [Pod HTTP Status Code](https://litmuschaos.github.io/litmus/experiments/categories/pods/pod-http-status-code/).**

Descreve um proxy dentro do ambiente do pod, com redirecionamento de tráfego e modificação do status da resposta do servidor. É uma intervenção de camada HTTP alternativa, não a regra nativa de malha que foi aplicada aos três runs examinados. Modificação de resposta pode preservar a execução no servidor, diferentemente de um abort antecipado.

### 9.5 Quadro comparativo e limite da evidência bibliográfica

| Fonte | Sistema | Intervenção documentada | Relação com nosso cenário | Valida acesso do modelo a FI? |
|---|---|---|---|---|
| Fang et al., arXiv:2510.04711v2 | Train-Ticket | ChaosMesh; aborts e manipulações HTTP, além de outras classes | Próxima na família de falhas de comunicação; não é o mesmo injetor | Não estabelecido |
| Pham et al., RCAEval | Sock Shop, Online Boutique, Train Ticket | Recursos, rede e mutações de código | Mesmo benchmark em parte; mecanismos distintos | Não estabelecido |
| Krasnovsky, arXiv:2506.11176v2 | DeathStarBench Social Network | Modelo fail-stop e propagação | Contexto de dependências; não é HTTP abort | Não estabelecido |
| Documentação Istio/Envoy | Bookinfo/exemplos técnicos | Abort HTTP no filtro de proxy | Mesmo mecanismo-base | Não avalia desempenho de RCA |
| Documentação Litmus | Pods Kubernetes | Redirecionamento e alteração de status | Alternativa HTTP com ponto de intervenção distinto | Não avalia desempenho de RCA |

**Conclusão bibliográfica:** há precedente científico para intervenções HTTP e para uso da Sock Shop em benchmarks de RCA, e documentação oficial para o nosso mecanismo. Nesta busca delimitada, **não foi localizada uma publicação que reproduza exatamente Sock Shop + Istio abort 100% + diagnóstico com FI**. Isso é ausência de confirmação na busca, não prova de inexistência.

Foram consultados textos primários via arXiv e documentação oficial. Consultas a mecanismos adicionais de descoberta tiveram restrições de acesso/rate limit. Esta seção não é uma revisão sistemática, não mede prevalência na literatura e não sustenta que “todos os artigos fazem assim”.

Sobretudo, **publicar o ground truth no dataset e entregá-lo ao diagnóstico são coisas diferentes**. Nenhuma das fontes examinadas autoriza concluir, apenas por disponibilizar rótulos, que os métodos recebem a resposta correta durante a inferência.

## 10. Protocolo proposto para decidir a validade no artigo

### 10.1 Definir a afirmação e a unidade experimental

Denominação recomendada: **“injeção de abort HTTP 500 no service mesh para chamadas destinadas a um serviço”**.

Definir dois resultados separados:

1. **Localização:** o método identificou a dependência/aresta sabotada?
2. **Explicação:** distinguiu resposta local do proxy, erro recebido no chamador e eventual falha interna de aplicação?

Não exigir que a saída indique “bug interno em payment” quando o injetor não produziu isso. Um resultado pode acertar o destino e errar o mecanismo.

### 10.2 Validação operacional anterior à pontuação

Para cada run, conservar:

- manifesto, versão do gerador, alvo, escopo de rotas, percentual e duração;
- configuração aplicada/readback, timestamps do controlador e incerteza de propagação;
- tráfego positivo em baseline e efeito observável na aresta correta em fault;
- reporter, origem/destino originais e FQDN utilizado para resolver `unknown`;
- cobertura, truncamento, ausência de dados e retenção de cada datasource;
- limpeza da intervenção, recuperação e parada da carga.

Confirmar a intervenção privada combinando configuração e dataplane. HTTP 500 isolado não identifica necessariamente a intervenção; configuração isolada não prova impacto. Não criar uma regra que todo campo legado zero seja ausência real de falha.

Na comparação de rates, manter unidades, reporters e lookbacks consistentes. Não somar o mesmo tráfego contado no lado source e destination como se fossem requisições diferentes. Não converter média de percentuais em percentual agregado de eventos sem justificar denominadores.

### 10.3 Desenho comparativo

Comparar, em runs comuns e com orçamento controlado:

| Condição | Objetivo |
|---|---|
| Mesh abort + observabilidade completa | Desempenho com indicador operacional disponível |
| Mesmo mesh abort, sem pistas explícitas do injetor | Dependência do desempenho em relação a FI/derivações |
| Erro gerado no handler/aplicação, sem marcador explícito | Transferência para processamento realmente executado no alvo |
| Baseline sem intervenção, incluindo erros comerciais esperados | Falsos positivos e distinção de falha técnica |

A terceira condição exige um mecanismo novo e seguro, versionado, que faça a aplicação retornar erro controlado. Não se deve afirmar que isso já foi implementado ou que um erro controlado em handler reproduz todas as causas naturais de produção.

Variar intensidade parcial/intermitente e operações, além do 100% catch-all, melhora a análise de dificuldade. O percentual 100 não é inválido, mas representa uma falha forte e não representa sozinho o espectro de incidentes.

A intensidade, as repetições, os limiares, a política de recuperação e o critério de exclusão devem ser fixados antes da campanha. Não selecionar apenas runs favoráveis ou promover pendências para aumentar o corpus.

### 10.4 Controles de treino, teste e revisão

- Separar autoria supervisionada de B e diagnóstico de teste sem gabarito.
- Versionar código, prompts, corpus, sanitização, modelo e configuração de geração.
- Fixar o papel do auditor adicional e contabilizar suas consultas, tokens, latência e correções.
- Comparar estratégias com evidência/orçamento equivalentes.
- Usar novas consultas MCP históricas em uma nova avaliação; evidência compartilhada só pode ser selada dentro da mesma avaliação.
- Reportar casos planejados, executados, falhos, pendentes e elegíveis.
- Obter revisão humana/independente do mecanismo e da direção causal, sem confundir concordância autor–auditor com verdade.
- Usar runs e coortes independentes para alegações confirmatórias; os três casos já examinados são exploratórios.

### 10.5 Métricas que devem aparecer separadas

1. Intervenção aplicada e efeito validado.
2. Disponibilidade de evidência de cliente/servidor/mesh por fase.
3. Acerto de serviço, tipo e conjunto.
4. Acerto de mecanismo e direção de propagação.
5. Causa sustentada versus classificação provável/inconclusiva.
6. Elegibilidade do B e motivo das rejeições.
7. Custo do diagnóstico e da auditoria.
8. Diferença pareada com/sem indicadores do injetor, acompanhada de incerteza estatística.

## 11. Ameaças à validade e decisão

| Ameaça | Situação neste projeto | Mitigação necessária |
|---|---|---|
| Validade de construto | `http-status-code` pode ser interpretado como bug da aplicação, mas é abort no mesh | Descrever o mecanismo e separar localização de explicação |
| Atalho do injetor | FI e fingerprint revelam fault injection; prompt ensina esse significado | Ablação end-to-end e política explícita de entrada |
| Confirmação circular | O gate exige FI para classificar um cenário cuja entrada já o mostra | Validador privado e gate independente da pista na condição sanitizada |
| Erro de direção causal | Bs inferem IO em orders e invertem chamador/destino | Revisão causal independente e critérios específicos |
| Cobertura/identidade | Métricas no servidor não incluem o abort; destino pode aparecer unknown | Consultas por aresta/reporter/FQDN com proveniência preservada |
| Validade interna | Ativação, coleta e baseline podem ter fronteiras diferentes | Timestamps registrados, incerteza, mesma carga e controles |
| Validade externa | Demo, um mesh, abort forte, coorte reutilizada | Outros mecanismos, intensidades, sistemas e coortes independentes |
| Validade de conclusão | 1/3 elegível não é evidência de melhora generalizável | Repetições, pares, denominadores e revisão dos mecanismos |

### Decisão prática

**Pode ser mantido no artigo** se for caracterizado como falha de resposta/dependência injetada no mesh, com escopo e limitações explícitos.

**Não está validado para afirmar**, com os dados examinados:

- que o mecanismo reproduz bugs internos, problemas de DB ou indisponibilidade de disco;
- que o método generaliza para HTTP 500 natural;
- que ausência de erro no servidor significa ausência de falha;
- que os guias aprovados são necessariamente causalmente corretos;
- que a taxa de acerto observada é independente de FI;
- que a aprovação de outros trabalhos substitui controles experimentais próprios.

### Texto-base sugerido para a metodologia

> Injetamos falhas de resposta HTTP por meio de regras de abort no service mesh Istio, aplicadas às chamadas destinadas ao serviço selecionado e excluindo as rotas de probes. O proxy Envoy produz uma resposta local HTTP 500, de modo que a requisição abortada pode não alcançar a aplicação de destino. O cenário avalia localização da dependência afetada e explicação da propagação do erro; não modela, por si só, uma exceção interna ou falha de persistência. Metadados explícitos do mecanismo de injeção são tratados separadamente na validação operacional e na definição da entrada do diagnóstico.

Se o estudo continuar oferecendo FI ao modelo, acrescentar:

> Os diagnósticos têm acesso aos indicadores de resposta do proxy, incluindo fault injection. Portanto, os resultados representam uma condição com observabilidade do mecanismo e não constituem uma avaliação cega baseada exclusivamente em sintomas.

Se a ablação ainda não foi feita, dizer isso expressamente; não escrever que os indicadores foram ocultados ou que houve validação independente.

## 12. Rastreabilidade local e limites desta revisão

- Implementação revisada no worktree sobre o commit `985f820`; existem modificações não commitadas. O commit sozinho não identifica toda a versão examinada.
- [Gerador HTTP](../experiment/eval/memory_hog/generate_chaos_manifests.py): probes e `fault.abort`.
- [Campanha controlada](../experiment/eval/memory_hog/controlled_campaign.py): manifesto por tentativa.
- [Executor operacional](../experiment/platform/backend/controlled_experiment.py): preflight, readback, impacto, limpeza e recuperação.
- [Consultas Istio](../experiment/platform/backend/analysis/istio_edges.py): labels, fallback de identidade e reporter.
- [Projeção de métricas](../experiment/platform/backend/analysis/telemetry_coverage.py): fases, unidade e resolução do destino.
- [Gate e orientação Skill](../experiment/platform/backend/rca_skill.py): reconhecimento de FI e família HTTP.
- [Reconstrução B](../experiment/eval/memory_hog/training_b_rebuild.py) e [contrato documentado](../experiment/eval/memory_hog/README.md): autoria, revisão e auditoria.
- [Plano anterior de elegibilidade](./planos/2026-11-08-elegibilidade-http-status-code.md): contexto histórico, não comprovação automática de execução das etapas propostas.
- [Relatório anterior Skill/RAG](./diario/test-base-v6-20261107T215646147881Z-relatorio-skill-rag.md): resultados retrospectivos.
- [Relatório do piloto B](../experiment/eval/memory_hog/out/knowledge_rebuild/rag-training-http-eligibility-pilot-rerun-20261108/pending_reaudit.json): estado persistido e seleção dos três casos.

O piloto identifica o produtor pelo SHA-256 `8fc2dc9076ddad833c466df037bafd60ca38f1f337ab32c2f81f1cc7891df925`. Endurecimentos posteriores do gate não devem ser confundidos com uma nova execução desse piloto.

Não foram alterados experimentos, código de produção, resultados ou anexos para elaborar este documento. A revisão não reexecutou a Sock Shop nem realizou uma nova avaliação pontuada. As sugestões de protocolo são trabalho futuro, e os artefatos de saída locais podem não estar disponíveis em um clone limpo do repositório.

## 13. Revisão complementar: melhorar o diagnóstico sem revelar FI

**Data:** 08/11/2026. Esta revisão consultou novamente Prometheus, Loki e Tempo históricos, via MCP em `http://127.0.0.1:18082/sse`, nas janelas baseline/fault/post dos três runs. Não gerou novos Bs, não publicou pares e não recalculou elegibilidade ou scores.

Foram concluídas **84 consultas de dados**, incluindo 27 checks iniciais de cobertura. Os comprovantes, argumentos, horários e respostas desta coleta estão no diretório de sessão `/home/daniel/.copilot/session-state/69d2e23b-2859-4577-8d7f-1e46e442d489/files/http-review-online-20261108T1440/`, separados por run. Esse material é local e não acompanha um clone do repositório.

Os checks iniciais retornaram dados nas três famílias e fases, mas as buscas gerais de logs e traces atingiram os limites solicitados de 150 linhas e 20 traces. As buscas dirigidas de spans CLIENT com erro retornaram o limite de 10 candidatos por run; somente dois traces por run foram aprofundados. **Disponibilidade não significa cobertura exaustiva.**

### 13.1 Há evidência natural no chamador que a investigação deve aproveitar

Foram buscados spans CLIENT com erro pelo serviço chamador. Além do resumo analítico, foram consultados os traces OTLP originais com `tempo_get_trace`, verificando serviço, tipo do span, endpoint, código e timestamp dentro de fault.

| Run | Operação observada no chamador | Código no span CLIENT | Trace representativo |
|---|---|---|---|
| `run-abc3a42dc026` | `orders` → `POST http://shipping/shipping` | 500 | `1326da7394ca28f06a9b67d6c0a9aa7` |
| `run-73da08fee5d3` | `orders` → `POST http://payment/paymentAuth` | 500 | `aab88828e645a6151b9c7e1c4e804` |
| `run-29cf2c69c070` | `front-end` → `GET http://user/login` | 500 | `166ea78606f2ec89e4761390b4147ea` |

O segundo trace de user também contém `front-end` → `POST http://user/register` com 500. No primeiro trace, a resposta de entrada `GET /login` no front-end é 401, embora a chamada a user tenha recebido 500. Não exigir o mesmo código em todos os saltos: a aplicação pode traduzir o erro.

As métricas Istio consultadas nesta revisão também contêm taxas positivas de 500/FI na aresta do alvo: aproximadamente 3,818 req/s em shipping, 4,503 req/s em payment e 5,539 req/s em user durante fault. Essas taxas são observações agregadas, não contagens dos traces aprofundados.

**Lição:** buscar apenas traces do destino abortado perde o melhor observador. Nesta coleta, os grafos filtrados por payment e user retornaram `no_data`, enquanto a busca dirigida pelo chamador recuperou erros CLIENT com destino explícito. Isso não é evidência de falta de traces em toda a ocorrência.

Sem FI, os spans e códigos ainda sustentam a afirmação “a chamada à interface X recebeu 500 e afetou o chamador”. **Eles não distinguem, sozinhos, resposta local do proxy de erro interno no handler.** Duração curta e ausência de span SERVER não resolvem essa ambiguidade.

### 13.2 A lacuna SDK tem dois componentes concretos

O [inventário de checks B](../experiment/platform/backend/rca_knowledge.py) declara `sdk_http.available=false` e `status=not_queried`. O check `http` executável consulta somente Istio. Entretanto, os prompts pedem `http_client_*` e `http_server_*`. A instrução não está acompanhada da capacidade correspondente.

Na nova coleta:

1. A busca das famílias SDK por `namespace`/`kubernetes_namespace` retornou vazia.
2. A descoberta por nome e `job` encontrou `http_client_duration_milliseconds_{count,sum}` e `http_server_duration_milliseconds_{count,sum}` com `job="sock-shop/front-end"`. Os seletores de namespace anteriores não correspondem às labels dessas séries.
3. A consulta de `rate(...[1m])` pelos jobs corretos também retornou vazia.
4. Consultas do contador bruto e de `timestamp(contador)` mostraram atualizações com intervalo mediano de **64,266 s em shipping, 64,154 s em payment e 64,128 s em user**. A série SDK não fornece dois samples em uma janela de 60 s nessas observações, condição necessária para calcular `rate`.
5. Os contadores CLIENT nativos contêm séries 500: front-end → orders nos casos shipping/payment; front-end → user no caso user. Também existem séries 500 antigas cujo contador permanece constante. **Presença da série/código não prova erro novo em fault.**

Houve lacunas maiores em algumas séries de user e queda de contador em uma série de outro endpoint. Isso exige tratar resets e cobertura por série, sem atribuir causa ao reset a partir desse dado isolado.

**Melhorias legítimas:**

- Registrar checks SDK CLIENT/SERVER com seleção por labels realmente disponíveis, unidade e endpoint; descobrir o schema antes de afirmar ausência.
- Em campanhas futuras, alinhar exportação/scrape à resolução experimental, por exemplo 10–15 s, verificando a cadência real dos samples.
- Para runs antigos, preservar timestamps originais. Não interpolar novos samples para viabilizar `rate`.
- Uma janela maior pode permitir taxas, mas mistura fases curtas. Se utilizada, deve ser declarada previamente, com cobertura e incerteza de fronteira; não escolher o lookback após observar o score.
- Não interpretar os pontos repetidos de uma range query de 15 s como novas amostras de origem quando o último timestamp do contador não mudou.

### 13.3 Por que FI não corrigiu os Bs

Os estados abaixo são **os estados persistidos do piloto anterior**, não uma pontuação desta coleta:

| Caso | Estado anterior | Defeito observado no artefato B |
|---|---|---|
| shipping | `procedural_pending` | A cadeia atribui persistência/lógica a orders e descreve orders retornando 500 para shipping; confunde a direção da chamada. Na última rodada, o auditor pede `correct`, mas o orçamento termina sem executar essa correção. |
| payment | `procedurally_validated` | A cadeia começa com IO em orders e atribui a propagação a payment. A exceção do chamador não comprova falha de IO/disco como mecanismo primário; aprovação procedural não certificou a cadeia causal. |
| user | `procedural_pending` | Introduz dependência externa não demonstrada e cita somente N012 como suporte, apesar de um passo observado de front-end com N014. O reparo falha no contrato de corroboração. |

Assim, o indicador explícito estava disponível, mas o modelo continuou construindo explicações incompatíveis ou mais fortes que os dados. **Acrescentar mais pistas do injetor não resolve validação semântica, vínculo de evidências ou orçamento de correção.**

### 13.4 Fragilidades do gate confirmadas por inspeção e probes unitários

O [helper HTTP](../experiment/platform/backend/rca_skill.py) exige uma aresta 5xx/FI positiva, mas procura o texto 5xx/`HttpServerErrorException` no resultado inteiro do corroborador.

Dois probes unitários isolados, sem alterar dados experimentais, reproduziram aceitação da família `logs` quando:

- o erro HTTP estava somente em baseline e a observação de fault era normal;
- o erro de fault no chamador mencionava outro destino, não o alvo da aresta.

Portanto, ter contagem positiva em fault e um erro em algum lugar do JSON não garante corroboração da mesma ocorrência. O resultado inteiro também pode incluir metadados de consulta; a verificação precisa examinar observações, não qualquer string do payload.

O [gate causal B](../experiment/eval/memory_hog/training_b_rebuild.py) verifica a presença de uma cadeia não vazia, mas não valida deterministicamente que os saltos ou o mecanismo narrado correspondem às evidências. A revisão LLM não impediu a narrativa problemática de payment.

Há ainda chamadas de `_causal_issues` com e sem `fault_type` em superfícies diferentes de validação. Isso seleciona políticas diferentes de corroboração. Não foi demonstrado que essa divergência causou um dos três resultados; deve ser eliminada com testes de consistência, sem relaxar o gate final.

### 13.5 Prioridades recomendadas e controles científicos

**P0 — separar observação, localização e mecanismo; corrigir falsos positivos.**

Exigir que cada salto causal informe chamador, destino/operação, produtor da resposta conhecido ou desconhecido, fase e IDs de suporte. Para afirmar banco/IO, exigir evidência específica desse mecanismo. Não forçar a identidade do serviço-alvo a ser a identidade do processo que produziu o 500.

A autoria B supervisionada pode receber o alvo como orientação, mas isso deve ser declarado e separado de teste cego. Manter `inconclusive` quando as evidências só localizam uma interface problemática; não converter detecção de 500 em mecanismo confirmado.

**P0 — remover indicadores do injetor da condição principal, de ponta a ponta.**

Preservar os dados brutos e comprovantes privados de intervenção. A projeção recebida pelo modelo deve remover o token FI e os equivalentes explícitos, incluindo `proxy_fault_injection`, detalhes como `fault_filter_abort`, texto bruto de access log e nomes/descrições de recurso que revelem o injetor. Aplicar a política a prompts, argumentos/resultados de ferramentas, autoria, revisão, auditoria e corpus RAG.

Não remover indiscriminadamente todas as flags: outras flags de transporte podem ser sinais legítimos. Ao omitir FI de labels, reagrupar corretamente séries que passam a ter a mesma identidade, mantendo reporter e denominadores; não duplicar tráfego.

Separar o validador privado do efeito experimental do validador das afirmações do modelo. O primeiro pode usar FI/readback; o segundo deve verificar as evidências efetivamente disponibilizadas na condição cega. **O gate atual exige FI: apenas esconder a flag, sem essa separação, produz inelegibilidade por construção.**

**P1 — tornar a investigação por chamador/endpoint um caminho executável.**

A partir de uma aresta HTTP anômala, buscar spans CLIENT com erro no chamador e aprofundar candidatos com destino/operação correspondentes. Preservar span/trace IDs, timestamps, código e relação pai-filho no digest. Comparar operação em baseline/fault/post e paginar/dividir janelas quando limites forem atingidos. Logs e métricas do mesmo proxy não devem contar como corroboração causal independente apenas por estarem em datasources distintos.

**P1 — conectar SDK ao inventário e validar cadência real.**

Implementar a capacidade descrita na seção 13.2, sem inventar cobertura retroativa. Em novos runs, testar que existem samples suficientes para o lookback e para as fases antes de executar a injeção.

**P1 — reparar por defeito específico, não “tentar até aprovar”.**

Fazer uma checagem semântica das afirmações e fornecer a correção concreta: direção invertida, mecanismo não sustentado, evidência não vinculada ou fase errada. Preservar IDs já coletados. Se faltar o passo observado, exigir esse vínculo em vez de apagar a referência de suporte/contradição.

Definir orçamento e comportamento terminal antes da avaliação. Uma decisão final `correct` sem rodada de execução deve permanecer pendente, com motivo explícito. Não aumentar retries apenas para HTTP ou para casos já vistos até alcançar aprovação.

**P2 — avaliação cega pareada e controle de generalização.**

Comparar com/sem indicadores do injetor sobre os mesmos runs e a mesma coleta online selada dentro da avaliação, com política fixa e conjuntos de treino/teste separados. Avaliar localização, mecanismo sustentado, falsas confirmações, cobertura e abstenção, além de elegibilidade. Os três casos já analisados são desenvolvimento, não evidência independente de generalização.

Acrescentar depois intervenções no handler da aplicação em runs independentes, sem marcadores explícitos para o modelo, e casos de contraste: erros comerciais, falhas de dependência e erros de proxy não injetados. Não reclassificar abort de mesh como exceção interna apenas para ampliar a alegação científica.

**Parecer complementar:** há espaço real de melhoria sem FI, sobretudo recuperando os spans CLIENT já existentes e usando corretamente labels/cadência SDK. O ganho esperado deve vir de melhor evidência e de cadeias mais fiéis, não de eliminar a possibilidade de abstenção ou tornar o gate permissivo. Esta revisão não demonstra que qualquer B já se tornou elegível.
