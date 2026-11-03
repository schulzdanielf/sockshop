# Próximos passos

**Data:** 17/10/2026.  
**Objetivo:** corrigir as dificuldades iniciais dos experimentos com o menor
esforço que produza ganho verificável em validade e qualidade da Skill RCA.
**Escopo:** avaliação e priorização das sete sugestões; a seção 9 registra
as correções de código aplicadas nesta etapa. Não houve injeção nem alteração
de cluster.

## 1. Recomendação executiva

As sugestões são pertinentes, mas não precisam virar sete projetos de
instrumentação. **A melhor primeira entrega é um pacote comum de ciclo de
vida e identidade, acompanhado de critérios de efeito menos ambíguos.**
Ele beneficia memória, kill, delete e a interpretação de restarts em rede.

Ordem recomendada:

1. **Corrigir o que significa “efeito comprovado”.** Não aceitar restart
   como prova de exaustão de memória, nem 5xx como prova de perda de pacotes.
2. **Ligar identidade e ciclo de vida à skill.** Reutilizar métricas já
   existentes e preservar eventos/status/logs antes que desapareçam.
3. **Fechar CPU e memória com os sinais já disponíveis**, validando unidades,
   escopo e controles antes de adicionar novos exporters.
4. **Validar HTTP no ponto correto do mesh**, primeiro com uma rota e um
   chamador conhecidos. Pode recuperar uma classe hoje sem efeito aprovado.
5. **Tratar latência como cadeia temporal**, sem transformar restart
   secundário em causa.
6. **Investigar network-loss em piloto separado.** Tem valor alto, mas
   exige validação no namespace/interface corretos; não é a correção mais
   barata para começar.

**Não priorizar agora:** mais agentes, modelo maior, profiling completo,
instrumentação de todas as bibliotecas ou uma nova campanha de 40 runs.
Não é preciso resolver todo o tracing para começar a corrigir os gates e
o ciclo de vida; causas sem corroboração continuam inconclusivas.

## 2. Base da avaliação e significado de esforço

Foi inspecionado o código de
[investigadores MCP](../../mcp-observability-server/investigators.py),
[validação de efeito](../../experiment/eval/memory_hog/run_targeted_training.py),
[runner](../../experiment/eval/memory_hog/runner.py),
[injeção](../../experiment/platform/backend/plugins/litmus_plugin.py),
[skill](../../experiment/platform/backend/rca_skill.py),
[multiagente](../../experiment/platform/backend/multiagent_rca.py) e
[resumo de lifecycle](../../experiment/platform/backend/analysis/telemetry_coverage.py).
Isso confirma capacidades do código, não disponibilidade das séries ou
funcionamento do deployment atual.

Estimativas **relativas**, não prazos:

- **Pequeno:** ajuste localizado em regra, consulta ou relatório, com fixture.
- **Médio:** integrar coleta, persistência e consumo, com validação real.
- **Grande:** instrumentar namespace/rede ou reconstruir caminhos de
  telemetria, com dependências de runtime e permissões.

O retorno esperado é redução de ambiguidade e de diagnóstico sem suporte,
não aumento garantido de acurácia. Em alguns casos, corrigir o gate vai
**reduzir** a taxa de aprovação, revelando problemas antes ocultos.

Na campanha anterior, memória passou em 5/6, CPU em 3/6, kill em 3/6,
delete em 4/6, latência em 2/6, perda em 0/5 e HTTP em 0/5. Esses números
são resultados do validador atual, não confirmação independente dos
mecanismos. Ver [avaliação consolidada](../progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md).

## 3. Priorização por esforço e resultado

| Ação | Esforço incremental | Retorno esperado | Ordem e dependência |
|---|---|---|---|
| Separar intervenção, mecanismo e impacto nos gates | Pequeno–médio | Muito alto: impede aprovações por sintomas genéricos em várias classes | 1; começar com fixtures e preservar relatórios antigos |
| Pacote comum de identidade/lifecycle e consulta pela skill | Médio | Muito alto: atende memória, kill, delete e restarts secundários | 2; sem depender de novos agentes |
| Auditar queries/unidades e controle de CPU/memória | Pequeno–médio | Alto: reutiliza coleta existente e evita novas métricas redundantes | 3; parte pode ocorrer junto da etapa 2 |
| Piloto HTTP por aresta/chamador e ponto de injeção | Pequeno–médio se os sinais existirem; médio se exigir access logs | Alto: verifica uma categoria com 0/5 aprovações | 4; depende de rota/labels reais, não de tracing completo |
| Timeline de latência, timeout e probes | Médio | Alto: separa causa de efeitos e reaproveita lifecycle | 5; aproveitar etapas 2 e 3 |
| Verificação tc/netem e sonda TCP/HTTP para perda | Médio–grande | Alto para rede, menor abrangência imediata | 6; piloto dedicado antes de ampliar a matriz |

Não usar um score numérico arbitrário de ROI. Confirmar primeiro quais
labels, eventos e logs já estão acessíveis pode mudar o custo de HTTP e
rede e, portanto, sua posição.

## 4. Avaliação das sete sugestões

### 4.1 `memory-hog`: concordo, sem tornar OOM obrigatório

**O que já existe:** working set/limite, consulta de OOM, restarts e
intensidade calculada a partir do limite do container
([cálculo](../../experiment/eval/memory_hog/resource_intensity.py)).
O gate atual aceita aumento de memória **ou** OOM/restart. Um restart
isolado pode, portanto, aprovar o experimento sem provar o mecanismo.

**Menor correção útil:**

- Preservar working set e limite, identidade do container e eventos de
  terminação na janela, reutilizando o que já existe.
- Separar **pressão de memória**, **OOM do container** e **impacto no
  serviço**. Consumo elevado pode ser efeito válido de stress, mas não
  autoriza chamar o resultado de exaustão/OOM.
- Remover restart isolado como comprovação de mecanismo de memória.
- Correlacionar `OOMKilled`, timestamp de término, exit code e log/evento
  relevante. Exit code 137 sozinho não prova OOM; pode indicar outro SIGKILL.

**Cuidado:** working set não é medição perfeita de toda a memória cobrada
pelo cgroup. Um pico pode acontecer entre scrapes. Ausência de OOM não
refuta pressão, e `OutOfMemoryError` da JVM não é igual a `OOMKilled`.
Não aumentar a intensidade só para produzir uma confirmação.

**Aceite:** um fixture com restart sem evidência de memória não pode
comprovar exaustão; um OOM antigo não pode ser atribuído ao run atual; um
run com pressão sem término deve preservar esse resultado sem inventar OOM.

### 4.2 `cpu-hog`: concordo, corrigindo o controle proposto

**O que já existe:** CPU versus quota, throttling e RED no harness/MCP.
Não é necessário começar construindo outro coletor. CPU próxima da quota
mostra contenção possível, não que a aplicação executou trabalho útil.

**Menor correção útil:**

- Conferir uso e quota com a mesma identidade, unidade e fonte canônica;
  distinguir aplicação, sidecar/helper e agregação por pod.
- Relacionar throttling com requisições iniciadas/concluídas, erros e
  latência. Throughput pode cair mesmo sem aumento da latência das poucas
  requisições que completaram.
- Fazer o **controle principal com a mesma carga, sem caos**. Comparar
  stress sob carga apenas a um cenário sem carga mistura dois tratamentos.
- Usar teste sem carga apenas como diagnóstico complementar para verificar
  quanto do consumo vem do stress, não como substituto do controle principal.

**Cuidado:** sem quota válida, a razão não pode virar zero ou saturação
fictícia. CPU/throttling/RED continuam na família `metrics`; logs ou traces
precisam sustentar o mecanismo para confirmação pela skill. Não alegar
“CPU útil” ou função culpada sem profiling, que pode ficar para depois.

**Aceite:** separar “stress consumiu CPU”, “houve contenção” e “serviço
degradou”; comparar sob a mesma carga e não confirmar gargalo só por CPU alta.

### 4.3 `container-kill`: concordo; prioridade no pacote comum

**O que já existe:** horários do plugin, restarts e busca de logs pela
skill. O timestamp `injected_at` do caminho Argo é registrado após a
submissão do workflow, não prova o instante exato em que o processo morreu.

**Menor correção útil:**

- Registrar UTC e origem de cada horário: submissão, início observado da
  ação, término do container, primeiro restart observado e recuperação.
- Preservar estado anterior/última terminação, container ID, Pod UID,
  motivo e exit code quando disponíveis.
- Preservar logs já enviados ao Loki e consultar o período anterior ao
  evento. Em nova coleta, capturar logs anteriores enquanto disponíveis;
  uma consulta posterior a `previous` não garante histórico completo.

**Cuidado:** o registro do injetor prova a intervenção para o avaliador,
não é evidência a entregar ao Qwen. Um evento dizendo que o injector matou
o alvo entrega o gabarito. Ausência de mensagem de encerramento é esperada
em morte abrupta e não é, sozinha, confirmação.

**Aceite:** a linha do tempo distingue morte e reinício; sem motivo
verificável, a skill pode localizar o incidente, mas mantém a causa aberta.

### 4.4 `pod-delete`: concordo; UID é mais valioso que mais contadores

**O que já existe:** o MCP já trata resets e agrega incrementos de
restart por pod/container. Isso não resolve toda a substituição: um Pod
novo pode começar saudável com `restartCount=0`.

**Menor correção útil:**

- Preservar `namespace`, nome/UID do Pod, container ID, owner references,
  criação e término; mapear Pod → ReplicaSet → Deployment.
- Correlacionar o UID anterior e o novo no mesmo workload e janela.
- Tratar **substituição de Pod** separadamente de **restart de container**.
  Não depender de aumento do contador para reconhecer a troca.

**Cuidado:** uma substituição também pode ser rollout, eviction ou outra
ação de controle. Apenas observar UID novo não prova a causa da remoção.
Snapshots antes/depois são um início barato, mas podem perder substituições
rápidas; eventos/watch limitado à janela complementam a coleta.

**Aceite:** fixture com novo UID e contador zero registra substituição, não
“nenhum evento”; rollout continua hipótese alternativa quando não excluído.

### 4.5 `network-latency`: concordo, priorizando tempo e aresta

**O que já existe:** latência Istio e RED, readiness, restarts e logs.
O harness mantém percentis em milissegundos; a consulta golden do MCP
converte P95 para segundos. São superfícies distintas: explicitar unidades
antes de reutilizar thresholds, sem presumir que já exista um bug entre elas.

**Menor correção útil:**

- Medir a aresta chamador → destino afetada, não só uma média do sistema.
- Correlacionar latência/timeouts do cliente e probes, com estado anterior
  e referência sem caos sob a mesma carga.
- Registrar a sequência: atraso → timeout → falha de probe → reinício,
  quando as evidências sustentarem essa ordem.

**Cuidado:** readiness falhando retira endpoints, mas não reinicia o
container por si só; liveness/startup podem levar ao restart. Configuração
da probe não comprova sua execução histórica. Histogramas de requisições
concluídas podem omitir justamente as chamadas que expiraram.

**Aceite:** não confirmar `pod-failure` só porque houve restart após
timeouts; registrar incerteza quando scrapes/eventos não ordenarem os fatos.

### 4.6 `network-loss`: concordo, mas não começaria por ela

**Problema:** drops de interface e perda criada pelo qdisc não são
equivalentes. O gate atual também aceita erros de serviço como efeito de
perda, embora 5xx não identifique esse mecanismo.

**Menor correção útil, em piloto separado:**

- Verificar, com acesso autorizado, qdisc/filtros, interface, direção e
  netns do alvo durante a janela; preservar saída sanitizada e timestamps.
- Observar deltas dos contadores específicos da regra, não apenas sua
  existência. Confirmar que o tráfego testado atravessa a regra.
- Executar uma sonda TCP/HTTP a partir do chamador relevante, com tentativas,
  sucessos, falhas, timeouts e duração. Ping isolado não valida o caminho HTTP.
- Usar retransmissões como complemento, com atribuição explícita. Contador
  do nó sem identidade do fluxo é contexto, não prova do alvo.

**Cuidado:** retransmissões não são assinatura exclusiva de netem e, com
perda total, pode não haver amostra útil de latência/sucesso. Contadores da
regra pertencem à validação externa da intervenção; não revelá-los ao modelo
como se ele tivesse inferido a causa pela telemetria da aplicação.

**Aceite:** regra presente sem tráfego/delta não equivale a perda validada;
5xx sem evidência discriminativa não comprova `network-loss`.
Se o runtime não permitir atribuição, limitar a alegação ou mudar o cenário,
não baixar o threshold para fazer o gate passar.

### 4.7 `http-status-code`: concordo, com uma correção conceitual

**Um abort local no proxy não precisa chegar à aplicação.** Isso é
comportamento esperado do mecanismo, não necessariamente falha da injeção.
O erro está em exigir evidência no servidor que o proxy deliberadamente
não chamou, ou em rotular o episódio como defeito interno da aplicação.

**O que já existe:** geração de `VirtualService` quando o manifesto
correspondente está disponível e métricas Istio. O gate atual exige aumento
do erro rotulado no destino, o que pode não representar esse abort.

**Menor correção útil:**

- Escolher uma rota HTTP usada pelo teste e identificar chamador, proxy
  que aplica a regra e destino lógico.
- Confirmar match efetivo de host/porta/path, status visto pelo cliente e
  origem local/upstream da resposta; aplicação do YAML não basta.
- Consultar métricas por aresta e reporter (`source`/`destination`) quando
  disponíveis, sem somar os dois lados como requisições independentes.
- Usar response flags/detalhes e access log Envoy já disponíveis. Verificar
  os campos reais antes de exigir uma flag específica ou habilitar novos logs.

**Cuidado:** response flags são contexto de interpretação, não prova
universal do mecanismo. Ausência de log no servidor não refuta abort local.
Separar “proxy interrompeu chamada a X” de “X gerou HTTP 500”.

**Aceite:** demonstrar o status no cliente e o ponto da resposta; rota não
atingida não aprova intervenção. O diagnóstico deve localizar a camada
correta, sem culpar automaticamente a aplicação que não recebeu a chamada.

## 5. Entrega transversal de maior retorno

### 5.1 Pacote de lifecycle por run

Reutilizar storage, MCP e Loki existentes; evitar um novo serviço.
O pacote proposto deve conter:

- Identidades e owners do workload, Pods e containers.
- Estados/status/última terminação observados, com horário de captura e
  horário do evento separados.
- Restarts e substituições, distinguindo intervalo entre scrapes de
  timestamp exato.
- Eventos Kubernetes pertinentes, inclusive probes, com UID e janela.
- Referências a logs anteriores/posteriores, cobertura e truncamento.
- Fonte, query, estado, retenção e limitações explícitas.

O [manifesto de eventos](../../deploy/kubernetes/manifests-monitoring/49-otel-k8s-events.yaml)
já está no workspace. Primeiro verificar se ele está aplicado, exportando
para Loki e preservando os campos necessários. Depois integrar uma consulta
de eventos à skill: o seletor de logs da aplicação não cobre automaticamente
os eventos com identidade `service: kubernetes`.

O multiagente já consulta métricas de última terminação em
[seu executor](../../experiment/platform/backend/multiagent_rca.py), mas
a Skill RCA não faz essa mesma consulta dedicada. Reutilizar/extrair
somente a parte comum de coleta/normalização, sem importar o orquestrador
multiagente inteiro. Não duplicar a implementação.

**Limites:** status atual não reconstrói o passado; eventos podem expirar,
ser agregados ou não registrar toda ocorrência; `lastState` pode descrever
um término anterior ao run. Capturar durante a nova coleta e persistir,
em vez de tentar recuperar tudo dias depois.

### 5.2 Duas saídas separadas, sem vazamento do gabarito

| Registro | Conteúdo | Quem pode usar |
|---|---|---|
| Validação da intervenção | Alvo, manifesto, ação do injector, regra netem/abort e confirmação da aplicação | Harness e avaliador externo |
| Evidências diagnósticas | Métricas, logs, traces e eventos do sistema, com identidade e janela, sem metadados que revelem a injeção | Skill e avaliador |

Compartilhar formato, storage e relógio não exige compartilhar acesso ao
gabarito. Um log/evento cujo conteúdo identifica a ação de caos deve ficar
na validação externa ou ser excluído do contexto diagnóstico.

Não contar `OOMKilled`, exit code e restarts derivados de kube-state-metrics
como três fontes independentes: continuam `metrics`. Copiar a mesma
ocorrência para Loki também não cria corroboração independente. A política
de eventos novos precisa ser explícita no contrato antes de usá-los nos gates.

### 5.3 Ajuste barato e importante do validador

Preservar os relatórios antigos e emitir uma versão nova com resultados
separados para:

1. **Intervenção aplicada:** sucesso técnico e confirmação do mecanismo de
   injeção pelo harness.
2. **Mecanismo observado:** sinal discriminativo, ou insuficiente/ambíguo.
3. **Impacto observado:** degradação e recuperação, ou ausência de impacto
   no tráfego exercitado.

Não reduzir esses estados a um único `passed` sem explicação. Para memória,
restart sozinho não comprova exaustão; para rede, 5xx sozinho não comprova
perda; para delete, queda de readiness sozinha não comprova remoção.
Preservar os sinais como sintomas úteis, sem transformá-los em mecanismos.

Uma injeção válida pode não degradar o serviço, e isso pode ser resultado de
resiliência. Nesse caso, não descartá-la automaticamente do balanço
experimental nem declarar que o modelo deveria descobrir uma causa que não
deixou evidência diagnóstica.

## 6. Sequência de trabalho e critérios de saída

| Etapa | Implementar/verificar | Só avançar quando |
|---|---|---|
| A — Fixtures e gates | Casos negativos: restart sem OOM, OOM antigo, Pod novo com contador zero, CPU alta sem impacto, 5xx sem perda e abort local | Nenhum desses casos for promovido automaticamente a mecanismo comprovado; dados ausentes gerarem estado explícito |
| B — Lifecycle compartilhado | Coleta/persistência, consulta pela skill, identidade e limites de tempo | Um incidente puder ser reconstruído do pacote sem consultar o estado atual do Pod |
| C — CPU/memória e controle | Auditar séries/unidades, reutilizar quotas/limites e RED; definir controle com mesma carga | Pressão, contenção, término e impacto estiverem separados, com cobertura conhecida |
| D — HTTP e latência | Uma rota/aresta por vez, cliente/proxy/servidor e probes | O ponto de erro/atraso e as consequências estiverem identificados, ou explicitamente indeterminados |
| E — Perda | Piloto tc/netem + sonda, com registro restrito ao avaliador | Houver evidência de tráfego afetado e limites de atribuição declarados |
| F — Avaliar a skill | Coorte e versões fixas, revisão causal e denominadores comuns | Ganho de evidência/qualidade puder ser distinguido de mudança dos dados e de exclusão de erros |

Testes relacionados para estender, não resultados novos desta revisão:
[investigadores](../../mcp-observability-server/tests/test_investigators.py),
[skill](../../experiment/platform/tests/test_rca_skill.py),
[contexto/lifecycle](../../experiment/platform/tests/test_rca_context_optimization.py)
e [gates da campanha](../../experiment/eval/memory_hog/tests/test_targeted_training.py).

Pilotos com injeção só após autorização. Começar com um serviço representativo
por mecanismo e controle equivalente para verificar o instrumento; isso não
dimensiona o teste estatístico final nem demonstra generalização.

## 7. Como demonstrar que as correções ajudaram a skill

Não usar apenas “mais runs aprovados” ou “mais causas confirmadas”.
Comparar antes/depois:

- Fração dos incidentes com identidade/horário e motivo de término
  verificáveis; motivos sustentados versus desconhecidos.
- Falsas confirmações nos casos negativos e adequação das abstenções.
- Acerto de serviço, categoria e mecanismo, com evidências revisadas.
- Cobertura por família/serviço/fase, truncamento e erros de coleta.
- Acerto sobre todos os runs elegíveis e sobre o recorte validado, sempre
  com denominadores e rejeições visíveis.
- Chamadas, duração e custo adicional da coleta/investigação.

Para isolar o ganho do raciocínio, testar skill antiga e nova com o
**mesmo pacote congelado**. Para medir o ganho de observabilidade, comparar
o pacote enriquecido com sua projeção anterior, declarando a ablação de
dados. Não confundir essas duas comparações. O replay integrado ainda é
pendência; reconsultas online permanecem exploratórias.

O [workflow específico da skill](../progresso-rag-skill-multiagentes-e-agenda-artigo-2026-10-17.md#b-workflow-a-aplicar-para-avaliar-o-resultado-da-skill)
detalha a coorte, execução e revisão. Este plano prioriza as correções que
devem alimentá-lo.

## 8. Decisão final

**Se fosse possível fazer apenas uma entrega:** implementar o pacote
compartilhado de identidade/lifecycle, integrado à skill, e corrigir os gates
que hoje aceitam sintomas como mecanismos. Se houver espaço apenas para uma
mudança pequena, começar pelos gates e fixtures, preservando os sintomas
sem aprovar causalidade.

**Se fosse possível fazer uma segunda frente:** fechar CPU/memória com
controle sob a mesma carga e verificar uma rota de abort HTTP pelo cliente.

**O que deixaria por último:** ampliar network-loss e instrumentação profunda
antes de comprovar o caminho afetado em piloto.

Essa ordem favorece reutilização e corrige diretamente as dificuldades
iniciais, em vez de adicionar complexidade para o modelo compensar a falta
de evidência.

## 9. Implementação aplicada e avaliação

### O que mudou

- **Gates do validador:** cada resultado de métrica agora separa
  `signal_observed`, `mechanism_signal_observed`, `impact_observed`,
  `mechanism_status` e o `result_status` do run. `passed` continua sendo o
  gate de intervenção + sinal-alvo, não uma afirmação de causa comprovada.
- **Memória:** working set/percentual de limite e OOM continuam válidos;
  restart isolado não passa mais o gate. A resposta distingue pressão de
  memória de `OOMKilled`.
- **CPU:** uso relativo à quota e throttling continuam sinais de contenção,
  enquanto latência, erro e queda de tráfego são registrados em separado.
  O relatório alerta que consumo de CPU não prova trabalho útil. Ainda falta
  o controle experimental sob carga equivalente sem caos.
- **Lifecycle:** restart/readiness/impacto ficam como sintomas para
  `pod-delete` e `container-kill`, sem confirmar mecanismo sem identidade do
  Pod ou motivo de término. A normalização de lifecycle passou a ser
  compartilhada entre skill e multiagente. A skill consulta timestamps,
  reason, exit code e UID de última terminação por fase; só atribui o evento
  à fase se o timestamp do término cair na janela.
- **Latência/perda:** aumento de p95 é exposto como impacto agregado, sem
  afirmar qual aresta foi afetada. Em `network-loss`, 5xx genérico não
  aprova perda; contador cAdvisor fica explicitamente ambíguo até validar
  interface, direção, regra `tc/netem` e sonda.
- **HTTP:** a campanha coleta 5xx do reporter `source` por
  `source_workload` → `destination_workload` e `response_code`; o resumo
  preserva a aresta. Um 5xx agregado no destino, sem origem/rota, não passa.
  `response_flags` de access log Envoy ainda não é coletado e não é inferido.

### Evidência de melhoria e limites

Os testes focados cobrem restart sem OOM, timestamps antigos, evento atual
com reason/exit code, 5xx sem perda, HTTP sem aresta de origem, edge HTTP com
status, scrape posterior ao limite da fase e sintomas de lifecycle sem
confirmação causal. A execução focada de seis arquivos de teste terminou em
**97 passed**; `git diff --check` também passou. Isso demonstra correção dos
gates e estrutura de dados usando fixtures; **não** demonstra que as séries
existem/estão corretas no cluster, nem aumento da acurácia da Skill RCA.
Nenhum run foi repetido e nenhum evento foi injetado.

As verificações de formatação em escopo dos trechos alterados passaram.
Black no arquivo inteiro ainda aponta trechos não relacionados em
[telemetry_coverage.py](../../experiment/platform/backend/analysis/telemetry_coverage.py),
[rca_skill.py](../../experiment/platform/backend/rca_skill.py),
[runner.py](../../experiment/eval/memory_hog/runner.py) e
[test_rca_context_optimization.py](../../experiment/platform/tests/test_rca_context_optimization.py).
Flake8 aponta um local não usado em `runner.py`, espaçamento em
`multiagent_rca.py` e alinhamento em
`test_rca_context_optimization.py`, fora das alterações funcionais. Não
apliquei reformatação ampla nem corrigi esses avisos preexistentes.

Efeitos observáveis da mudança:

1. Elimina aprovações falsas específicas antes permitidas: restart por si só
   não comprova exaustão de memória e 5xx por si só não comprova perda.
2. Expõe como inconclusivos, em vez de esconder, `pod-delete`,
   `container-kill`, latência sem aresta e perda sem validação do netem.
3. Faz o workflow de avaliação mostrar sinal observado, sinal específico do
   mecanismo, impacto e estado da injeção separadamente, tornando possível
   adjudicar resultados sem tratar todo efeito como causa.

Continuam pendentes pilotos autorizados para validar: labels e direção reais
de `http_5xx_by_edge`, access logs/response flags, identidade/UID no harness
de `pod-delete`, `tc/netem` e sonda de conectividade, unidades/escopo de CPU
e memória, e controle CPU com carga idêntica sem caos. Artefatos antigos sem
`http_5xx_by_edge` não devem ser reinterpretados como se contivessem a nova
evidência; preserve os relatórios históricos e execute avaliação pareada em
pacotes congelados antes de alegar melhoria de RCA ou resultado para artigo.
