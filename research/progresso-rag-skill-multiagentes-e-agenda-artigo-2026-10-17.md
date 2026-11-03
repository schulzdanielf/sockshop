# De RAG a skill e multiagentes: progresso, bloqueios e agenda para artigo

**Data de corte:** 17/10/2026.  
**Objeto:** Sock Shop, plataforma de caos e investigação RCA.  
**Conclusão principal:** houve progresso substancial de engenharia e de
disciplina de investigação, mas ainda não há demonstração experimental de
superioridade causal da estratégia skill/multiagente sobre o RAG original.
O próximo salto deve ser de validade experimental e avaliação, não de
quantidade de agentes.

## Leitura rápida — experimentos realizados e avaliação da skill

Esta seção responde diretamente a duas perguntas: **o que já foi testado?**
e **como avaliar o resultado da skill no próximo ciclo?** Os detalhes de
arquitetura e ameaças à validade aparecem depois.

### A. Resumo dos experimentos realizados

| Experimento/etapa | O que foi executado ou avaliado | Resultado registrado | O que permite concluir |
|---|---|---|---|
| RAG + validadores, antes da transição | LLM sobre resumos e runs semelhantes, com correções determinísticas de serviço/categoria | O histórico relata ganhos em mini-run, mas a tabela final do baseline está sem preenchimento | Há baseline implementado e experiência acumulada; falta um resultado agregado congelado para comparar a transição. |
| GAT/Istio e controle MLP, setembro | Localização de serviço com features de nós/arestas, comparada a heurísticas nos splits S1/S2/S3 | No relatório de 22/09, top-3 GAT = 0,57/0,36/0,49; MLP = 0,79/0,54/0,61; heurística B1 = 0,58/0,70/0,68 | Aquele desenho não demonstrou benefício do GAT sobre baselines simples. Não é avaliação da skill. |
| Campanha de caos, 12/10 | 40 pares distintos, sete serviços e sete tipos de falha; uma réplica por par | 35 runs concluídos, cinco falhos; 17/40 aprovados no validador de injeção/efeito | Há material de investigação, mas os 40 registros não são 40 intervenções causalmente validadas. |
| RCA antiga, primeira avaliação dessa campanha | Investigação dos 35 runs concluídos | O resumo registra apenas descoberta de tools, sem coleta efetiva, e zero acertos | Falha de acesso/coleta; não usar esse resultado como medida isolada de capacidade do modelo. |
| RCA antiga, reavaliação com MCP | Nova investigação dos mesmos 35 runs concluídos | Serviço 22/35, categoria 8/35, ambos 6/35; logs consultados em 10/35 | Este é o comparador legado disponível; ainda não é o baseline RAG original. |
| Skill, tentativa inicial de 13/10 | Relatório de 20 tentativas | Oito diagnósticos retornados: um confirmado e sete inconclusivos; 12 erros HTTP | Mostra instabilidade inicial. Não é uma avaliação completa da coorte. |
| Skill, relatório parcial de 13/10 | Relatório de 22 tentativas | Serviço 19/22, categoria 3/22, ambos 2/22; uma confirmação e 21 inconclusivos | Resultado intermediário, sem encerramento registrado; não usar a maior porcentagem como resultado final. |
| Skill, tentativa de 14/10 às 07:46 UTC | 35 chamadas ao endpoint | 35 erros de conexão | Mede indisponibilidade operacional, não qualidade da skill. |
| Skill, avaliação completa de 14/10 às 07:47 UTC | 35 chamadas; Qwen3.5-9B registrado nas 34 respostas | 34 inconclusivos, um erro de JSON, zero confirmações; 22 serviços, oito categorias e cinco pares corretos | Localização provável é possível, mas não houve causa confirmada pelo contrato nessa avaliação. |
| Multiagentes, 16/10 | 23 tentativas exploratórias em sete runs, com revisões/retries | 12 inconclusivos e 11 erros; quatro tentativas da versão compacta foram inconclusivas | A execução existe, mas ainda não há comparação controlada que prove ganho multiagente. |
| Testes de software desta revisão | Fixtures/mocks de contratos, API, CLI, contexto e avaliador | 93 testes passaram | Valida comportamento programado; não substitui os experimentos de diagnóstico com telemetria real. |

**Essas linhas não são populações somáveis.** Várias reavaliam os mesmos
runs, em momentos e versões diferentes. Os horários acima estão em UTC;
os nomes de arquivo não devem ser interpretados como horário UTC.

#### Quais falhas e serviços entraram na campanha de 40?

- **Serviços-alvo:** `front-end`, `catalogue`, `carts`, `orders`, `payment`,
  `shipping` e `user`.
- **Falhas:** `container-kill` (6), `cpu-hog` (6), `memory-hog` (6),
  `network-latency` (6), `network-loss` (5), `pod-delete` (6) e
  `http-status-code` (5).
- **Efeito aprovado pelo validador:** respectivamente 3, 3, 5, 2, 0, 4 e 0.
- **Fora dessa matriz:** bancos, RabbitMQ, `queue-master`, DNS e I/O.
  Dependências fora da matriz ainda podem aparecer como candidatas na RCA.

**Resultado central da skill, com denominador fixo de 35:** serviço
22/35 (62,86%), categoria 8/35 (22,86%) e ambos 5/35 (14,29%).
Esses são acertos de rótulo, não confirmações de mecanismo causal.
O número 22/27 (81,48%) do relatório considera só os casos com localização
avaliável; deve ser acompanhado das sete respostas sem serviço e do erro.

Fontes de resultados:
[histórico RAG](./abordagens/001-llm-rag-validadores.md),
[GAT/MLP](./diario/2026-09-22-relatorio-pos-correcao-gat.md),
[campanha e relatórios](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/),
[validação de efeito](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/effect_validation.json)
e [artefatos de investigação](../experiment/platform/data/object_store/).

### B. Workflow a aplicar para avaliar o resultado da skill

**Objetivo:** descobrir se a Skill RCA produz diagnóstico correto,
sustentado por evidências e operacionalmente confiável, comparada à RCA
antiga nos mesmos runs. A comparação com RAG e multiagentes é um passo
posterior separado; não deve ser misturada ao primeiro resultado.

```text
Fixar runs e versões
        ↓
Validar intervenção, janela e cobertura de cada run
        ↓
Executar um piloto da skill sem revelar o gabarito
        ↓
Executar a coorte e preservar todas as tentativas
        ↓
Avaliar serviço + categoria + mecanismo + evidências + abstenção
        ↓
Comparar com o legado, run a run, e estratificar falhas
        ↓
Decidir: corrigir coleta, corrigir skill/executor ou testar nova versão
```

#### B1. Preparar a coorte e o gabarito externo

Usar inicialmente os **35 runs concluídos da campanha de 40**, conforme o
[relatório legado](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/rca_evaluation_retry.json).
Manter dois recortes explícitos:

1. **Operacional:** todos os 35, incluindo abstenções e erros da skill.
2. **Intervenção aprovada:** os 17 aprovados no validador atual, identificados
   pela [validação de efeito](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/effect_validation.json).
   Essa aprovação precisa de revisão quando o mecanismo for ambíguo.

Os cinco runs falhos continuam no balanço de execução dos 40, sem fingir
que houve diagnóstico sobre eles. Não selecionar apenas casos em que a
skill acertou ou encontrou duas famílias.

O gabarito deve ficar em registro separado com `run_id`, serviço-alvo,
categoria, intervenção aplicada, efeito observado e grau de certeza.
Alvo e tipo da injeção não entram no contexto do Qwen.

#### B2. Fazer o gate de cobertura antes de medir qualidade

Para cada run, conferir baseline/fault/post, origem dos horários e dados
disponíveis nas famílias `metrics`, `logs` e `traces`, por serviço.
Registrar `no_data`, erro e truncamento como estados diferentes.

**Regra de avanço:** a execução pode continuar para medir abstenção mesmo
com lacunas, mas não pode ser apresentada como teste com evidência completa.
Se o histórico expirou, não usar o estado atual do cluster para preenchê-lo.

**Comparação causal controlada:** requer pacote congelado e replay comum
para skill e legado. Esse modo ainda não está integrado aos executores.
Sem ele, o procedimento abaixo é uma **reavaliação online exploratória**:
reconsulta telemetria e não isola o efeito da skill de mudanças dos dados.
Uma nova coleta para substituir dados perdidos exige campanha autorizada;
não é parte automática da avaliação.

#### B3. Fixar a versão e executar um piloto

Registrar commit/worktree, hash da skill e referências, cartão do sistema,
modelo/quantização, configuração de inferência, endpoint MCP, orçamento e
data da execução. O mesmo nome de modelo não basta para identificar os
pesos e a configuração.

Executar um run conhecido para verificar retorno e preservação do artefato,
não para ajustar o score. Exemplo, a partir da raiz do repositório:

```bash
./.venv/bin/python -m experiment.platform.rca_cli \
  --run-id run-e3735ba3a48a \
  --timeout 900 \
  --output /tmp/skill-rca-piloto-20261017.json
```

O run do exemplo é `carts/container-kill` na campanha, mas essa informação
fica com o avaliador. O comando chama somente a investigação do run
existente; não injeta caos. Usar arquivo de saída ainda inexistente.

**Gate do piloto:** resposta/erro explícito, tentativa preservada, janelas
corretas, ausência de gabarito no contexto e referências verificáveis.
`inconclusive` não reprova o gate técnico; precisa explicar a lacuna.
Se houver indisponibilidade, registrá-la e resolver antes do lote, sem
restaurar infraestrutura automaticamente durante a investigação.

#### B4. Executar a avaliação da coorte com o runner existente

Com plataforma, MCP e modelo previamente disponíveis, o comando já
implementado é:

```bash
CAMPAIGN=experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z

./.venv/bin/python -m experiment.eval.memory_hog.evaluate_skill_rca \
  --campaign-dir "$CAMPAIGN" \
  --baseline "$CAMPAIGN/rca_evaluation_retry.json" \
  --api-base http://127.0.0.1:8010 \
  --timeout 900 \
  --output "$CAMPAIGN/rca_skill_evaluation_protocolo_20261017.json"
```

Escolher outro nome se o relatório já existir. O runner não sobrescreve
relatórios existentes, chama `/skill-rca` para a coorte e persiste o
progresso. O timeout é por requisição, não a duração total do lote.
**Nenhum desses comandos foi executado nesta revisão documental.**

O avaliador atual entrega scores de rótulo, status, referências de
artefatos, duração e comparação com o legado. **Ainda não entrega sozinho**
revisão causal, recorte dos 17, ICs, replay, nem correção do viés de
denominadores. Essas etapas precisam de análise adicional ou extensão do
avaliador; não são funcionalidades implícitas do comando.

#### B5. Revisar cada resultado da skill, não apenas seu resumo

| Verificação | Pergunta que o avaliador deve responder | Critério |
|---|---|---|
| Serviço | Localizou a origem, ou apenas um chamador afetado? | Comparar com intervenção validada; registrar confundidor quando outra origem for plausível. |
| Categoria | Categoria corresponde ao mecanismo observado? | Comparação estruturada, separada do acerto de serviço. |
| Mecanismo | Explicou como a falha começou? | Cadeia causa → sintoma → impacto; “reiniciou” ou “houve 5xx” não basta. |
| Evidências | IDs existem e sustentam a alegação? | Conferir família, serviço, operação e horário; dado normal não vira suporte só por estar em fault. |
| Corroboração | Há duas famílias independentes da mesma ocorrência? | Duas métricas não bastam; logs e traces que apenas repetem a mesma mensagem exigem cautela. |
| Restart | Explicou o que aconteceu antes do reinício? | Motivo sustentado ou explicitamente desconhecido, com próximo check. |
| Alternativas | Confrontou explicações concorrentes? | Pelo menos duas alternativas e tratamento de contradições, não apenas lista genérica. |
| Abstenção | `inconclusive` foi adequado? | Revisor distingue evidência realmente insuficiente de falha do raciocínio em usar dados suficientes. |
| Confirmação | `confirmed` é causalmente defensável? | Aprovação do host não substitui revisão semântica independente. |
| Operação | A skill chegou a responder? | Separar JSON inválido, tool error, timeout e modelo indisponível de resultado inconclusivo. |

O produto desta etapa é **uma linha por run/tentativa**, incluindo versão,
status, acertos de serviço/categoria/conjunto, julgamento causal,
adequação da abstenção, cobertura, erro, custo e referência ao artefato.
Preservar também a justificativa humana. Esse registro complementar é
proposto; não é o schema atual do runner.

#### B6. Calcular o placar e comparar com o legado

- Publicar serviço, categoria e acerto conjunto sobre **35**, além dos
  scores condicionados à resposta, com denominadores visíveis.
- Informar os números de confirmados, inconclusivos, erros e respostas sem
  serviço; zero confirmações não significa 100% de precisão causal.
- Repetir a análise no recorte de intervenção aprovada e por falha/serviço,
  sem esconder os resultados operacionais completos.
- Comparar run a run: legado acertou/skill errou, skill acertou/legado
  errou, ambos acertaram e ambos falharam/abstiveram-se.
- Separar **acerto do rótulo** de **confirmação causal correta**. Não contar
  hipótese inconclusiva correta como causa confirmada.
- Reportar duração e chamadas; tokens só quando disponíveis, distinguindo
  medição de estimativa. Fazer repetição de inferência com evidência fixa
  para medir estabilidade, sem tratá-la como nova intervenção.

#### B7. Aplicar a decisão ao resultado

| Resultado encontrado | Próxima ação |
|---|---|
| Erros de conexão/modelo impedem respostas | Corrigir disponibilidade e repetir em nova tentativa; manter o registro da falha operacional. |
| Skill se abstém porque logs/traces ou histórico de restart faltam | Corrigir coleta/replay ou limitar a alegação científica; não “melhorar” o prompt para forçar confirmação. |
| Há dados suficientes, mas a skill escolhe causa errada | Revisar procedimento, seleção de candidatos e digests; adicionar fixture que reproduza a discordância. |
| Host aceita duas famílias sem suporte ao mecanismo | Fortalecer avaliação semântica e casos adversariais; presença de dados não equivale a evidência causal. |
| Acurácia sobe apenas ao excluir abstenções/erros | Não declarar melhoria geral; analisar cobertura e acerto end-to-end. |
| Melhora pareada, evidência válida e custo aceitável | Congelar versão e confirmar em runs novos, controles e réplicas, antes de alegar generalização. |

**Entregas mínimas desse workflow:** manifesto da coorte; relatório bruto
da execução; tabela de revisão causal por tentativa; comparação pareada;
lista de falhas atribuídas a experimento/coleta/modelo/host; decisão de
manter ou revisar a skill. O workflow geral da seção 5 amplia este protocolo
para a comparação completa com RAG e multiagentes.

## 1. Escopo e como interpretar esta avaliação

Esta revisão cruza código, documentação histórica e artefatos locais. Não
executou novas injeções, chamadas de RCA ao modelo, rollouts ou alterações
no cluster. Mudanças de infraestrutura já presentes no workspace foram
lidas, não modificadas.

Distinguem-se quatro níveis:

- **Implementado:** caminho encontrado no código atual.
- **Observado:** resultado registrado em artefato local, com sua versão e
  população; não garante que o código atual produza o mesmo resultado.
- **Histórico documentado:** problema relatado no diário, sem reprodução
  nesta revisão.
- **Proposto:** trabalho ou critério de aceitação futuro.

O HEAD inspecionado é `5122e04` (16/10, “Add multiagent”). Havia alterações
não commitadas em observabilidade e documentação. A campanha de 40 falhas
registra outro estado: `236abea`, com worktree suja. Portanto, não se deve
atribuir retrospectivamente os recursos atuais àquela campanha.

O histórico foi reconstruído de [research](../research/README.md), dos
diários e do Git; não havia histórico de conversas disponível na consulta
de sessões. Os resultados gerados em runtime podem não existir em um clone
limpo. Sua preservação em pacote publicável continua necessária.

**Validação de software nesta revisão:** 93 testes passaram no ambiente
virtual existente, cobrindo skill, API, CLI, contratos multiagentes,
otimização de contexto e avaliador. São testes com fixtures/mocks; não
demonstram acurácia do Qwen nem saúde do pipeline real.

## 2. O que mudou, de fato

### 2.1 Linha do tempo e motivação

| Etapa | O que tentava resolver | Resultado e aprendizado |
|---|---|---|
| Abril–setembro: LLM + RAG + validadores | Recuperar runs semelhantes e produzir serviço/categoria a partir de resumo compacto | Criou coleta, memória de exemplos, resumo L2, auditoria e baseline. Regras posteriores ficaram frágeis e a tabela final de avaliação não foi congelada. |
| Setembro: GAT para WHERE, agentes para WHY | Substituir heurísticas por localização aprendida e separar localização de explicação | O ciclo GAT/Istio não sustentou ganho sobre heurísticas; MLP foi melhor que GAT nos splits documentados. |
| A partir de 25/09: investigação LLM/MCP | Trocar decisão sobre contexto estático por coleta ativa e especialização | A direção de pesquisa ficou explícita; inicialmente ainda havia agente único e ferramentas, não uma equipe executável. |
| Outubro: Skill RCA | Transformar o procedimento causal em instrução versionada e executável | Loader real, contrato JSON, evidências identificáveis, alternativas, análise de restart e abstenção. |
| 16/10: multiagentes e contexto compacto | Separar interpretação por domínio e investigar lacunas com orçamento | Coordenador, especialistas, registro compartilhado e até duas rodadas adaptativas implementados. Falta avaliação comparativa estabilizada. |

Fontes: [abordagem RAG](../research/abordagens/001-llm-rag-validadores.md),
[abordagem híbrida](../research/abordagens/002-hibrido-gat-agentes.md),
[abordagem multiagente](../research/abordagens/003-llm-multiagente-rca.md)
e [relatório pós-correção GAT](../research/diario/2026-09-22-relatorio-pos-correcao-gat.md).
Os resultados GAT são contexto histórico, não comparação direta com a RCA nova.

### 2.2 Quatro caminhos coexistem; não são o mesmo baseline

| Caminho | Entrada e raciocínio | Situação |
|---|---|---|
| `/llm-analysis` | Resumo do run, vizinhos recuperados por RAG, chamada LLM e validadores determinísticos | Baseline RAG original, ainda presente. |
| `/rca` | Investigação online pelo MCP, com host e agente da RCA antiga | Baseline usado na avaliação de 40 falhas; **não é sinônimo do RAG original**. |
| `/skill-rca` | Procedimento carregado da skill, planejamento Qwen, coleta progressiva e diagnóstico validado | Implementado e avaliado em tentativas locais. |
| `/multiagent-rca` | Triagem, coordenador, especialistas de métricas/logs/traces, aprofundamento e síntese | Implementado; artefatos exploratórios, sem campanha comparativa completa encontrada. |

Os caminhos podem ser conferidos na [API](../experiment/platform/backend/api.py),
no [executor da skill](../experiment/platform/backend/rca_skill.py),
no [executor multiagente](../experiment/platform/backend/multiagent_rca.py)
e no [agente legado](../experiment/platform/backend/agent_console.py).
Uma comparação skill versus `/rca` não responde, sozinha, se abandonar RAG
foi vantajoso.

### 2.3 Avanços que já são defensáveis

1. **Procedimento explícito:** a [skill](../.github/skills/rca/SKILL.md)
   estabelece métricas → ciclo de vida → logs → traces → hipóteses →
   conclusão/abstenção, em vez de depender somente de exemplos recuperados.
2. **Host com garantias:** fixa identidades e janelas, valida schemas,
   serviços, categorias e referências. Duas consultas Prometheus não contam
   como duas famílias independentes.
3. **Incerteza representada:** `inconclusive` é diferente de erro técnico e
   de causa confirmada. Restart é evento a explicar, não resposta causal.
4. **Rastreabilidade:** tentativas separadas, IDs de evidência/consulta,
   hashes, consultas, limitações e motivo de parada ficam preservados.
5. **Especialização real de contexto:** há relatórios separados por domínio,
   síntese e reanálise seletiva após uma consulta discriminativa.
6. **Controle de complexidade:** até 32 consultas MCP, 10 chamadas de modelo,
   120 mil tokens cumulativos, 180 s e duas rodadas adaptativas no padrão
   multiagente. Esses limites não são garantia de qualidade nem de SLA.
7. **Contexto mais disciplinado:** cartão arquitetural projetado, versionado
   e com hash; logs agrupados por padrão; distinção entre contagem da amostra
   e da janela; probes de cobertura separados de evidência causal.

Há duas distinções importantes para descrever a contribuição:

- A skill é explicitamente carregada como instrução no executor da skill.
  O multiagente lê seu hash, reutiliza auxiliares e aplica regras causais
  compactas no código; não injeta integralmente os textos da skill nos
  especialistas. Não atribuir todo comportamento multiagente ao arquivo da
  skill sem testar essa ligação.
- Os especialistas atuais são chamados em sequência pelo executor. Há
  multiagência por papéis/contextos, **não paralelismo demonstrado**. Usam o
  mesmo modelo configurado, não modelos independentes. Concordância entre
  eles não é nova fonte de evidência.

## 3. Quais eram suas dificuldades — e quais continuam

| Dificuldade anterior | Evidência/efeito | Evolução e pendência |
|---|---|---|
| Acertar WHERE, mas errar WHY | RAG documenta categoria fraca; campanha legada acertou serviço em 22/35 e categoria em 8/35 | Saída nova separa evento, mecanismo e causa; os resultados ainda mostram dificuldade em categoria. |
| Regras corrigirem o LLM de forma errada | Histórico de `user/cpu-hog`: localizador sobrescreveu RCA correta após categoria não validada | Novo host prioriza validação e abstenção. Ainda é preciso auditar rebaixamentos, correções e hipóteses remanescentes. |
| Thresholds frágeis e cobertura parcial | OOM/CPU confundidos; regras dedicadas a memória/CPU/pod, não a toda a taxonomia | Procedimento mais geral implementado; evidência discriminativa para rede/HTTP permanece insuficiente. |
| Similaridade não significar causalidade | Ranker de grafo heurístico, contexto compacto e exemplos históricos | MCP permite investigar a ocorrência. Ainda falta isolar o ganho da investigação em relação ao ganho do prompt/modelo/contexto. |
| Modelo/host retornarem formato inválido ou aparência de sucesso | Fallbacks legados documentados; novas tentativas também registram JSON inválido | Schemas, erros explícitos e artefatos melhoraram robustez. Conformidade estrutural não garante semântica correta. |
| Dados ausentes serem interpretados como sistema saudável | Traces vazios, OOM `no_data`, HTTP/AMQP com cobertura desigual | Contratos e cobertura agora explicitam lacunas. Ausência de evidência continua limitando a conclusão. |
| Telemetria contaminar o próprio sistema | Diário relata OOM do collector e cascata de erros/restarts, exigindo reset do dataset | Há ajustes de memória, ingestão, alertas e dashboard no workspace. Não foram validados no cluster nesta revisão. |
| Infra local interromper campanhas | Port-forward MCP, processo com código antigo em memória, labels ausentes | Preflight, fingerprint e auditoria avançaram; ainda falta vincular cada execução a um build/configuração imutável. |
| Label representar intenção, não falha observada | HTTP Litmus contornado pelo mesh; efeitos não aprovados em rede/HTTP | Há validação de injeção/efeito, mas aprovação ainda pode depender de sintomas pouco específicos. |
| Dados expirarem antes da reavaliação | Prometheus/Tempo/Loki são reconsultados; snapshots parciais não equivalem a replay | Replay completo das três famílias não está integrado à skill/multiagente. |
| Maior complexidade não gerar ganho | GAT perdeu de baselines; novo multiagente já teve estouro de contexto e erros de contrato | Contexto compacto e orçamentos implementados; ganho líquido de qualidade/custo ainda precisa ser medido. |

Fontes adicionais: [registro de erros](../experiment/docs/erros-experimentos.md),
[revisão metodológica](../docs/revisao-experimentos-caos.md),
[avaliação de 13/10](../docs/avaliacao-campanha-40-falhas-2026-10-13.md)
e [diagnóstico do fluxo RCA](../docs/fluxo-rca-atual-e-gap-para-skill.md).
Status históricos desses documentos não devem ser copiados como estado atual.

### 3.1 Problemas antigos que já tiveram avanço concreto

Não seria correto repetir que o projeto ainda não tem suporte a controles,
fingerprint, randomização ou origem temporal:

- O [runner](../experiment/eval/memory_hog/runner.py) já suporta sham,
  shuffle/semente, jitter e fingerprint; o [engine](../experiment/platform/backend/engine.py)
  suporta `provider: none` e limiares relativos ao baseline.
- Na campanha de 40, porém, não há sham, a ordem é determinística,
  `shuffle: false` e jitter zero. Ter suporte não significa tê-lo exercitado.
- Nos artefatos de janela dessa campanha: 29 usam
  `litmus_result_created_at_plus_configured_duration`, um usa
  `argo_step_started_at_plus_configured_duration`, cinco usam
  `manifest_observed`; cinco não registram origem fault.
- Isso melhora o antigo “horário desconhecido”, mas início inferido de
  criação/etapa e fim calculado pela duração não equivalem a observar todo
  o intervalo real de perturbação.

O novo [manifesto de eventos Kubernetes](../deploy/kubernetes/manifests-monitoring/49-otel-k8s-events.yaml)
também já existe no workspace, ainda não versionado no momento da revisão.
Isso não contradiz a lacuna da RCA: os seletores de logs da aplicação não
consultam automaticamente os eventos com `service: kubernetes`. O executor
multiagente consulta métricas históricas de última terminação, não um
histórico completo de eventos/probes. Coletar, consultar e correlacionar são
três entregas diferentes.

## 4. Resumo dos testes de caos e seus problemas

### 4.1 O que a campanha de 40 realmente exercitou

Campanha `forty-faults-20261012T190530194018Z`: 40 pares distintos,
sete serviços e sete tipos de falha, uma réplica por par. **Não são 40
tipos de falha**, nem uma matriz fatorial completa.

Configuração efetiva: baseline 60 s, warmup 30 s, fase fault 270 s,
injeção configurada de 180 s, post 120 s e cooldown 60 s; Locust com
30 usuários e spawn de 5/s. A duração da fase não é a duração observada
da indisponibilidade. Ver [configuração](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/config.yaml).

| Falha | Categoria esperada | Runs | Injeção + efeito aprovados | Dificuldade principal |
|---|---|---:|---:|---|
| `memory-hog` | `memory-exhaustion` | 6 | 5/6 | Distinguir pressão, GC, OOM e reinício; consumo configurado não prova mecanismo observado. |
| `cpu-hog` | `cpu-exhaustion` | 6 | 3/6 | Stress pode elevar CPU do pod/nó sem evidência suficiente do gargalo da aplicação; verificar quota e throttling. |
| `container-kill` | `pod-failure` | 6 | 3/6 | Restart é compatível com kill, mas também com OOM, crash e probes; precisar da evidência anterior. |
| `pod-delete` | `pod-failure` | 6 | 4/6 | Pod substituído, UID novo e reset de contador exigem tratamento temporal. |
| `network-latency` | `network-latency` | 6 | 2/6 | Latência pode causar timeout de probe e restart secundário; não rotular o efeito como origem. |
| `network-loss` | `network-loss` | 5 | 0/5 | Drops de interface do cAdvisor não medem necessariamente perda netem no alvo. Falhar no gate não prova ausência de perda. |
| `http-status-code` | `http-error` | 5 | 0/5 | Abort do mesh pode ocorrer no lado cliente e não aparecer como erro da aplicação-alvo; validar rota, chamador e flags Envoy. |
| **Total** | | **40** | **17/40** | Aprovação pelo validador atual não é revisão causal independente. |

Fontes: [validação de efeito](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/effect_validation.json)
e [planejador/avaliador da campanha](../experiment/eval/memory_hog/run_forty_faults.py).
`dns-error` e `io-stress` constam do catálogo, mas não desta matriz:
há impedimentos documentados de netns/interceptor e overlayfs/O_DIRECT no
ambiente histórico. Não anunciar cobertura dessas categorias.

### 4.2 Serviços e observabilidade necessária

| Superfície | Papel no experimento | O que precisa ser acompanhado |
|---|---|---|
| `front-end` | Entrada e agregador de sintomas | Logs de app separados de proxies; chamada upstream que originou 5xx; carga e event loop. |
| `catalogue`, `user`, `payment` | Alvos Go, com responsabilidades diferentes | Erros locais versus dependência, CPU/memória, processo/probe e spans cliente/servidor disponíveis. |
| `carts`, `orders`, `shipping` | Alvos Java; orders tem fan-out e shipping publica em fila | GC/heap, timeouts, dependências, encadeamento de checkout e caminho assíncrono. |
| `queue-master`, `rabbitmq` | Contexto/candidatos possíveis, mas não alvos da campanha de 40 | Filas, consumidores, backlog e conexões AMQP; HTTP isolado não cobre o mecanismo. |
| Bancos e `session-db` | Dependências conhecidas, não alvos da matriz atual | Saúde, conexões, latência de operação e logs próprios; não atribuir logs `app-db` à aplicação. |
| Collector, MCP, Prometheus, Loki, Tempo e ponte local | Instrumentos do experimento, também sujeitos a falha | Ingestão, drops, export failures, cobertura, disponibilidade e interferência no sistema. |

O [cartão atual](../experiment/platform/data/system_cards/sock-shop.yaml)
inclui 14 componentes. O espaço possível de resposta é maior que os sete
alvos avaliados. Não forçar o modelo a escolher um alvo conhecido pela
campanha: respostas em dependências podem denunciar confundidores reais,
mas precisam de adjudicação externa, não de aceitação automática.

### 4.3 Os números atuais, sem misturar denominadores

**RCA antiga, reavaliação:** 35/40 runs concluídos, localização 22/35
(62,86%), categoria 8/35 (22,86%), acerto conjunto 6/35 (17,14%).
Só 17/40 passaram no validador de injeção/efeito. Nos 17 aprovados, os
acertos foram 14, seis e cinco, respectivamente; é outro estrato, não
resultado global.

**Skill, avaliação completa de 14/10:** 35 tentativas, 34 diagnósticos
inconclusivos e um erro HTTP por JSON de diagnóstico inválido. Nenhuma
confirmação. Há 22 localizações corretas, oito categorias corretas e cinco
acertos conjuntos. O relatório calcula:

| Medida | Denominador usado pelo avaliador | Leitura conservadora sobre os 35 runs |
|---|---:|---:|
| Serviço | 22/27 = 81,48% | 22/35 = 62,86% |
| Categoria | 8/34 = 23,53% | 8/35 = 22,86% |
| Ambos | 5/27 = 18,52% | 5/35 = 14,29% |

A coluna conservadora conta abstenções/erros como ausência de acerto; não
os transforma em diagnósticos errados do ponto de vista de segurança.
O avaliador exclui valores não booleanos do cálculo. Sete respostas sem
serviço e um erro reduzem o denominador de localização a 27. Portanto,
**81,48% versus 62,86% não demonstra ganho de localização**.

Nos mesmos 27 casos com localização avaliável, o legado acertou 19 e a
skill 22. Nos 34 diagnósticos retornados, ambos acertaram 22 serviços e
oito categorias; o legado teve seis acertos conjuntos, a skill cinco.
É indispensável reportar ganho pareado, cobertura e abstenção juntos.

Fontes: [reavaliação legada](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/rca_evaluation_retry.json),
[avaliação completa da skill](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/rca_skill_evaluation_20261014T044707.json)
e [implementação do avaliador](../experiment/eval/memory_hog/evaluate_skill_rca.py).

Há ainda um [relatório parcial de 22 tentativas](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/rca_skill_evaluation.json),
com 19/22 serviços corretos e uma confirmação, sem `completed_at`.
Ele não deve substituir a avaliação completa por apresentar porcentagem
maior. Outra [tentativa de 14/10](../experiment/eval/memory_hog/out/forty_faults/forty-faults-20261012T190530194018Z/rca_skill_evaluation_20261014T044637.json)
teve 35/35 erros de conexão: isso mede indisponibilidade, não competência
de diagnóstico.

**Limite adicional:** a pontuação legada identifica categoria por texto e
aliases; a skill compara o campo estruturado. Acertar o nome da categoria
não demonstra que a explicação causal está correta. Os relatórios também
não estabelecem equivalência de modelo, prompt e evidência entre variantes.

### 4.4 O que os artefatos multiagentes permitem afirmar

Foram encontrados 23 artefatos de tentativa para sete runs: 12
`inconclusive`, 11 `error`, zero `confirmed`. Misturam revisões e retries;
**não são 23 experimentos independentes nem uma taxa final do código atual**.
Há erros históricos de JSON, limites de campo, pedido de filtro inválido e
orçamento de contexto.

Quatro tentativas, em três runs, registram `prompt_version:
compact-context-v2`: todas inconclusivas, com sete–oito chamadas de modelo,
36.963–43.890 tokens contabilizados e 53,54–79,192 s. São indícios de
viabilidade operacional, não comparação controlada de custo/qualidade.
Exemplos em [run-4b597cdcc57e](../experiment/platform/data/object_store/run-4b597cdcc57e/),
[run-462871f1702e](../experiment/platform/data/object_store/run-462871f1702e/)
e [run-7d305297534b](../experiment/platform/data/object_store/run-7d305297534b/).

Nas quatro, traces fault continuam sem observações e os probes sem filtro
também retornam vazio nas fases. Isso não distingue retenção, ingestão e
ausência de tráfego. Persistem logs truncados, candidatos malformados
removidos e motivos de restart desconhecidos.

Os 35 arquivos originais de traces da campanha têm `count: 0`. Na
reavaliação legada houve traces em oito runs; na avaliação completa da skill,
nenhum dos 34 diagnósticos registra família traces em fault e só 12
registram logs com observações em fault. A entrada do modelo mudou entre
análises: mais contexto ou agentes não recuperam automaticamente dados
expirados.

## 5. Seção de workflow: avaliar o processo inteiro, não apenas a resposta

O workflow abaixo é **proposto**. Reutiliza capacidades existentes, mas
exige integração de replay e avaliação comum. Não é uma campanha executada
nem autorização para injeção.

### 5.1 Fluxo com gates e entregas

| Etapa | Ação | Artefato/decisão de saída | Se falhar |
|---|---|---|---|
| 1. Congelar protocolo | Definir hipótese, variantes, métricas, orçamento, população, exclusões e sementes antes dos testes finais | Protocolo e manifesto versionados; separar piloto de teste | Não iniciar campanha comparativa sem pergunta e denominador definidos. |
| 2. Validar instrumentos | Requisição conhecida sem caos; procurar logs, métricas e trace por ID e busca; medir atraso; verificar MCP/modelo e coleta | Relatório de cobertura por serviço/operação e saúde da plataforma | Classificar falha do instrumento; não produzir score de RCA. |
| 3. Medir normalidade | Executar controles sem falha sob a mesma carga; estabilizar baseline e calibrar limiares/SLO | Distribuição natural, falsos positivos e critério de estabilidade | Ajustar ambiente/carga e repetir piloto, preservando rejeições. |
| 4. Planejar tratamento | Matriz suportada, ordem aleatorizada em blocos, sham intercalado, intensidade e recuperação | Ledger de todos os pares, réplicas e tentativas, com sementes | Não substituir silenciosamente células difíceis. |
| 5. Executar e validar injeção | Registrar manifesto/hash, alvo, mecanismo, aplicação, início/fim, veredito e cleanup | Ground truth da intervenção separado da evidência oferecida ao modelo | Marcar não aplicada, efeito não observado ou efeito ambíguo; preservar run. |
| 6. Observar serviços e recuperação | RED/recursos, lifecycle, logs, dependências, spans e integridade da instrumentação em todas as fases | Linha do tempo com precisão e cobertura; recuperação por métrica | Separar falha do serviço, do ambiente e da coleta; não tratar vazio como zero. |
| 7. Congelar evidências | Exportar respostas, queries, limites, IDs e dados necessários para novas consultas previstas | Pacote por run, checksum e manifesto; replay sem backend vivo | Não comparar variantes em dias diferentes sobre evidência mutável. |
| 8. Investigar às cegas | Executar variantes com mesmo modelo/configuração e pacote; host restringe consultas e orçamento | Tentativas imutáveis, relatórios, custos, erros e parada | Erro técnico continua no denominador operacional; inconclusivo continua abstenção. |
| 9. Avaliar externamente | Confrontar diagnóstico com intervenção validada e revisão causal independente | Scores por run/variante e estratos; justificativa de divergências | Não usar o próprio LLM como único juiz nem texto contendo o rótulo como prova. |
| 10. Analisar e decidir | Comparação pareada, ICs, falhas por estágio e trade-off qualidade/custo | Tabelas reproduzíveis, ameaças à validade, decisão de manter/descartar | Resultado nulo/negativo é resultado; não ajustar protocolo no teste final. |

O pacote precisa suportar consultas adaptativas: salvar só as respostas
que uma variante pediu favorece essa variante. Definir previamente um
conjunto suficiente de séries/logs/spans ou uma interface de replay comum,
com política explícita para consulta não coberta. Não responder “vazio”
quando o pacote simplesmente não contém os dados solicitados.

### 5.2 Três estados separados por run

Não sobrecarregar `completed` ou `confirmed`:

1. **Experimento:** execução técnica, intervenção aplicada, efeito e
   recuperação observados, saúde do ambiente.
2. **Observabilidade:** famílias disponíveis por fase/serviço, truncamento,
   atraso, erro de consulta e precisão temporal.
3. **Diagnóstico:** serviço/categoria propostos, mecanismo sustentado,
   abstenção, erro técnico e qualidade da explicação.

Guardar também quem decidiu a validade e por quê. Um experimento válido
pode ter telemetria insuficiente; uma resposta que acerta o alvo de um
experimento inválido não valida a técnica.

### 5.3 Variantes mínimas para descobrir a origem do ganho

| Variante proposta | O que isola |
|---|---|
| A0: ranking determinístico simples | Piso sem LLM; mede quanto o serviço já é localizável por anomalia. |
| A1: LLM único sem RAG | Capacidade do modelo com evidência/contexto comum. |
| A2: RAG original, com corpus de treino congelado | Valor da recuperação; incluir com/sem validadores e preservar saída antes/depois das regras. |
| B: Skill RCA | Valor do procedimento e da investigação progressiva versus RAG/resumo. |
| C: multiagente sem aprofundamento | Valor da separação de contextos, usando `max_followup_rounds: 0`. |
| D: multiagente adaptativo | Valor de perguntas adicionais e reanálise seletiva versus C. |

Em B/C/D, controlar o conjunto inicial de dados e registrar os dados
adicionais obtidos. Uma variante com mais consultas pode ganhar por acesso,
não por raciocínio. Comparar desempenho em condições equivalentes e curvas
qualidade versus orçamento; não exigir número idêntico de chamadas quando
isso elimina o comportamento que se quer estudar.

Ablar regras causais/skill separadamente se o artigo atribuir ganho a elas.
RAG e skill não são conceitualmente incompatíveis: a pesquisa está mudando
o modo de investigação, não provando que memória de casos seja inútil.
Não é necessário reconstruir GAT para a primeira comparação nova.

### 5.4 O que medir depois

| Eixo | Medidas recomendadas |
|---|---|
| Execução experimental | Planejados, iniciados, aplicados, efeito aprovado/ambíguo, cleanup, retries e perdas por causa. |
| Serviço | Top-1 e macro por serviço/falha; top-k só se todas as variantes retornarem ranking comparável. |
| Categoria/mecanismo | Categoria exata, acerto conjunto e mecanismo causal por rubrica; matriz de confusão. |
| Abstenção | Cobertura de respostas, taxa de confirmação, acurácia condicionada à resposta, falsas confirmações e risco seletivo. |
| Controles | Falso alarme e falsa confirmação sem intervenção; definir previamente o que conta como resposta negativa correta. |
| Evidências | IDs válidos, suporte discriminativo em fault, correspondência de serviço/operação e coerência temporal; não só número de famílias. |
| Restart | Motivo sustentado versus desconhecido; causa anterior versus efeito do reinício; acerto da sequência. |
| Robustez | JSON inválido, serviço inventado, tool error, fonte ausente, truncamento, timeout e limite de orçamento. |
| Custo | Tokens medidos/estimados separados, chamadas, latência mediana/P95, hardware e, quando medido, memória/energia. |
| Estabilidade | Variação entre inferências do mesmo run e entre novas réplicas experimentais; não confundir as duas. |

Definições mínimas a manter juntas:

- **Acerto end-to-end:** acertos / todos os runs elegíveis predefinidos,
  com erros/abstenções explicitados.
- **Acurácia seletiva:** acertos entre respostas emitidas, junto da fração
  de runs respondidos. Nunca publicar essa acurácia isoladamente.
- **Precisão das confirmações:** confirmações causalmente corretas /
  confirmações emitidas; sem confirmações, é indefinida, não 100%.

Usar ICs e testes pareados sobre os mesmos runs. Com poucas discordâncias,
preferir procedimento exato/reamostragem adequado, reportando o tamanho do
efeito. Repetições de inferência do mesmo run não são amostras experimentais
independentes. Dimensionar réplicas a partir de piloto e precisão desejada,
não escolher um número mágico por célula.

Para a rubrica causal, revisores sem acesso à identidade da variante devem
avaliar suporte, contradições, mecanismo e sequência; medir concordância e
adjudicar divergências. O ground truth da intervenção permanece disponível
ao avaliador externo, nunca ao investigador.

## 6. O que impede transformar o trabalho em artigo agora

Adoto **SBRC — Simpósio Brasileiro de Redes de Computadores e Sistemas
Distribuídos** como referência de enquadramento. Confirmar edição, trilha,
formato e chamada antes da submissão. A lista abaixo é uma avaliação
científica do projeto, não transcrição de regras de uma chamada consultada.

### 6.1 Bloqueios prioritários

| Prioridade | Bloqueio | Por que impede a conclusão | Entrega para destravar |
|---|---|---|---|
| P0 | Pergunta/contribuição ainda ampla | “Usamos skill e multiagentes” descreve arquitetura, não achado científico | Uma hipótese falsificável sobre ganho de evidência, diagnóstico ou custo, com comparadores. |
| P0 | Comparação não mede a transição RAG → novo método | Números existentes comparam principalmente `/rca` e skill; o baseline RAG tem tabela final em aberto | Avaliador comum para A1/A2/B/C/D sobre o mesmo corpus e critérios. |
| P0 | Evidência muda entre reavaliações | Retenção, indexação e fontes online confundem efeito do método | Replay verificável de métricas/logs/traces e versão exata do pacote por tentativa. |
| P0 | Tratamento/efeito não validados em boa parte da campanha | Só 17/40 passaram; rede/HTTP têm zero aprovações em duas classes | Validar mecanismos por piloto, revisar gates e preservar todos os casos com estratificação. |
| P0 | Cobertura não sustenta mecanismos em muitos casos | Traces vazios, logs ausentes/truncados e restart sem motivo | Cobertura por serviço/operação e integração de eventos/lifecycle ou escopo explicitamente limitado. |
| P0 | Score mistura populações e não mede causalidade | 22/27 pode parecer ganho sobre 22/35; aliases e categoria exata não são equivalentes | Denominadores fixos, rubrica causal, métricas de abstenção e relatório de erros. |
| P1 | Controles e réplicas insuficientes | Não estima falso positivo nem variabilidade por célula | Sham intercalado, blocos aleatorizados, réplicas dimensionadas e cooldown validado. |
| P1 | Sem evidência isolada do valor multiagente | Mais agentes também significam mais tokens/dados; mesmo modelo pode repetir o mesmo viés | C versus D e B versus C, custos e orçamentos comparáveis, repetições de inferência. |
| P1 | Garantia causal apenas estrutural | Duas famílias com observações podem ser normais, de outro componente ou não sustentar o mecanismo | Casos adversariais de evidência irrelevante e avaliação humana da relação alegação→evidência. |
| P1 | Reprodutibilidade e documentação divergentes | Build sujo, docs antigas e estado de deployment não permitem reconstrução exata | Release/commit, hashes de prompt/modelo/quantização/config, arquivo de dados e guia de reprodução. |
| P1 | Novidade frente à literatura não estabelecida | Arquiteturas com agentes e ferramentas não são, por si, novas | Revisão relacionada de RCA multimodal, agentes e abstenção, com comparação de objetivos e garantias. |
| P2 | Validade externa limitada | Sock Shop e histórico de cluster único não representam todos os sistemas distribuídos | Segundo cenário/topologia ou delimitação honesta como estudo de caso. |

### 6.2 Enquadramento de artigo plausível

**Proposta de foco:** “Investigação RCA orientada por evidências com
abstenção: avaliação de procedimentos e especialistas LLM sob falhas
controladas em microsserviços”.

Perguntas possíveis:

- **RQ1:** o procedimento progressivo reduz confirmações sem suporte e
  confusão entre sintoma e mecanismo, comparado a LLM/RAG?
- **RQ2:** especialistas separados e aprofundamento adaptativo aumentam
  acerto causal/cobertura em relação à skill única, a custo aceitável?
- **RQ3:** como cobertura ausente, truncamento e motivos de restart
  desconhecidos afetam acerto, abstenção e custo?

A contribuição mais promissora não é “o multiagente descobriu a falha”,
mas um método auditável e uma avaliação que quantifiquem quando a RCA pode
concluir e quando deve se abster. Isso precisa de dados: **abster-se sempre
não é método útil**, mesmo que evite falsas confirmações.

Um resultado negativo bem controlado também pode ser relevante: mostrar
que o gargalo está em observabilidade/validade do experimento, e que
agentes adicionais não o resolvem. A aceitabilidade depende da novidade,
da qualidade da análise e da trilha; não é possível garantir publicação.

## 7. Próximos passos, em ordem de dependência

### Passo 1 — Fechar a pergunta e o relatório comum

- Escolher a métrica primária: por exemplo, precisão causal sob cobertura
  mínima, acompanhada obrigatoriamente de cobertura/end-to-end.
- Congelar piloto, teste, exclusões, critérios de parada e rubrica.
- Unificar saídas e avaliação dos endpoints sem mudar os resultados
  históricos; criar novos relatórios, preservando erros e abstenções.
- Recuperar/configurar baseline RAG e corpus de treino sem vazamento.
  Auditar tags de caos, IDs e metadados no retrieval e no prompt, não só no
  texto final; um filtro no prompt não garante retrieval cego.

**Saída:** protocolo e pequena tabela comparável, ainda sem alegar ganho.

### Passo 2 — Corrigir o instrumento antes de ampliar a campanha

- Provar o trajeto de uma requisição conhecida até Tempo; conferir
  atributos reais, filtros, propagação, sampling, ingestão e retenção.
- Provar consulta de logs por container e fase; separar app, banco,
  sidecars e eventos; medir truncamento.
- Validar as alterações existentes de collector/Prometheus/RabbitMQ e
  eventos, sem confundir manifesto escrito com deployment funcionando.
- Integrar eventos relevantes à investigação de restart com identidade,
  horário e referência próprios; não duplicar a mesma ocorrência para
  satisfazer o requisito de duas fontes.
- Instrumentar o caminho assíncrono se ele entrar no escopo; caso contrário,
  delimitá-lo na avaliação e manter confundidores visíveis.

**Saída:** matriz de cobertura e piloto saudável com mecanismos observáveis.

### Passo 3 — Implementar o replay que a estratégia exige

- Reaproveitar o desenho de [dados replayáveis](../experiment/docs/dados-replayaveis-observabilidade.md).
- Congelar dados suficientes para as consultas fixas e adaptativas.
- Garantir descoberta/execução offline, estados explícitos de dados não
  arquivados e mesmas regras de sanitização.
- Testar que reexecutar as consultas não depende de retenção nem chama o
  backend vivo. Distinguir “replay idêntico de evidência” de “mesma resposta
  probabilística do modelo”.

**Saída:** pacote por run com checksums e testes de equivalência do replay.

### Passo 4 — Executar piloto comparativo e só depois o teste final

- Incluir controles e poucos casos de cada mecanismo validado, com
  dependências e sintomas indiretos.
- Comparar primeiro LLM/RAG/skill e multiagente fixo/adaptativo.
- Revisar discordâncias causalmente e medir orçamento/contexto.
- Usar o piloto para dimensionar réplicas e recursos; então congelar a
  configuração e executar o teste final com autorização de caos.
- Registrar todos os descartes/retries; reportar população operacional e
  subconjunto com intervenção/telemetria válidas, sem esconder a primeira.

**Saída:** resultados pareados, ICs, análise de falhas e custo.

### Passo 5 — Preparar o pacote do artigo

- Relacionados e diferença da contribuição para RCA multimodal/agentes.
- Método, protocolos, taxonomia, versão do sistema e ameaça de confundidores.
- Tabelas de acurácia/abstenção, cobertura, mecanismo, custo e falhas.
- Estudos de caso: acerto sustentado, erro plausível e abstenção correta.
- Limitações: aplicação única, cobertura, temporalidade, validade da
  intervenção e repetição dos vieses do mesmo modelo.
- Artefato executável, dados sanitizados/licenças e comandos de reprodução.

**Critério de prontidão:** outra pessoa consegue reproduzir a avaliação
sem cluster vivo, identificar a população de cada número e entender tanto
os acertos quanto as falhas. Não é necessário vencer em todas as métricas;
é necessário sustentar exatamente a contribuição afirmada.

## 8. Síntese final

Você saiu de um pipeline de **resumo + recuperação + correção por regras**
para uma infraestrutura de **investigação explicitamente governada,
auditável e capaz de reconhecer limites**. Esse é um avanço real.

O que falta agora não é outra troca de arquitetura. É demonstrar,
com intervenções válidas e evidências congeladas, se o procedimento e os
especialistas produzem diagnósticos mais úteis e confiáveis por unidade de
custo. Os dados atuais localizam razoavelmente alguns alvos, mas ainda não
demonstram explicação causal superior. O artigo depende de fechar essa
distância, ou de transformá-la em um resultado negativo rigoroso.

## 9. Referências de implementação e verificação

- [Contrato da skill](../.github/skills/rca/references/evidence-contract.md)
  e [checklist causal](../.github/skills/rca/references/causal-checklist.md).
- [RAG/retrieval](../experiment/platform/backend/analysis/retrieval.py),
  [montagem do prompt](../experiment/platform/backend/analysis/prompt.py)
  e [contexto arquitetural](../experiment/platform/backend/analysis/system_card.py).
- [Executor skill](../experiment/platform/backend/rca_skill.py),
  [executor multiagente](../experiment/platform/backend/multiagent_rca.py)
  e [cobertura/lifecycle](../experiment/platform/backend/analysis/telemetry_coverage.py).
- [Documentação operacional](../experiment/platform/README.md)
  e [runner de campanhas](../experiment/eval/memory_hog/runner.py).
- Testes executados:
  [skill](../experiment/platform/tests/test_rca_skill.py),
  [API](../experiment/platform/tests/test_rca_skill_api.py),
  [CLI](../experiment/platform/tests/test_rca_cli.py),
  [multiagentes](../experiment/platform/tests/test_multiagent_rca.py),
  [contexto](../experiment/platform/tests/test_rca_context_optimization.py)
  e [avaliação](../experiment/platform/tests/test_skill_rca_evaluation.py).

```bash
./.venv/bin/python -m pytest -q \
  experiment/platform/tests/test_rca_skill.py \
  experiment/platform/tests/test_rca_skill_api.py \
  experiment/platform/tests/test_rca_cli.py \
  experiment/platform/tests/test_multiagent_rca.py \
  experiment/platform/tests/test_rca_context_optimization.py \
  experiment/platform/tests/test_skill_rca_evaluation.py
```
