# Plano de evolução da RAG e campanha única de treino com 35 experimentos

**Data:** 03/11/2026.  
**Estado:** plano para revisão; nenhuma implementação, injeção ou nova avaliação autorizada por este documento.  
**Modelo generativo obrigatório:** Qwen 3.5, com identidade, artefato e configuração fixados.  
**Decisão confirmada pelo usuário:** uma campanha de treino, um perfil de carga, iniciado e parado por experimento, com nova baseline. Não manter carga contínua atravessando os 35 casos.  
**Princípio de avanço:** executar uma etapa, registrar a validação, revisar o gate e somente então autorizar a próxima.

## 1. Objetivos e limites

1. Corrigir a cadeia **intervenção → evidência → A → recuperação de A/B → checks → diagnóstico**, antes de atribuir a baixa acurácia exclusivamente ao modelo.
2. Produzir até 35 experiências de treino novas, uma por combinação de sete serviços e cinco falhas, sem gerar automaticamente uma segunda réplica de teste.
3. Armazenar métricas, logs e spans nos datasources históricos por dual-write, desde antes do warmup até o encerramento documentado do run.
4. Permitir reconstruir A, revisar B e reindexar o corpus posteriormente, sempre preservando versões anteriores.
5. Avaliar futuras variantes consultando **Prometheus History, Loki History e Tempo History via MCP**, em baseline/fault/post de cada incidente.
6. Manter o Qwen 3.5 para A, B, proposição de checks, síntese, reparos e eventual reranking generativo. Não retornar ao Qwen 14B.

“Retreino”, neste plano, significa inicialmente **reconstrução do conhecimento RAG**: nova anotação A/B, curadoria, embeddings e índice. Não significa atualizar pesos do Qwen. Fine-tuning não está autorizado; exigiria protocolo separado, divisão independente e nova avaliação online.

Um encoder de embeddings não generativo pode ser reutilizado, com versão/hash fixados, para não confundir mudança de corpus com mudança de embedding. Nenhum segundo LLM generativo será usado.

## 2. Base documental e natureza desta análise

### Fontes examinadas

- Relatório compartilhado e [relatório da execução](../../../../.copilot/session-state/01993d5e-7524-47c6-bec1-e277155c98ca/files/online-rag-32-20261103T203730Z/inference-bounded512-retry-1/evaluation-report.md).
- As **32 respostas completas** da tentativa `inference-bounded512-retry-1`: A, diagnóstico inicial, evidências, pares recuperados, checks, diagnóstico final, limitações e metadados.
- Os **32 pares A/B** do [corpus legado](../../experiment/platform/data/object_store/.rca_knowledge/exploratory-rag-20261028T191703038725Z/exploratory_base.json), suas etiquetas e elegibilidade.
- As **96 projeções históricas recuperadas** nas respostas finais: três por incidente.
- Os **32 controlled_report.json**, principalmente `gates.mechanism_observed` e `gates.intervention_confirmed_on_target`, além dos resumos de coleta e cobertura.
- [Configuração da campanha](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/config.yaml), [executor de conhecimento](../../experiment/platform/backend/rca_knowledge.py), [validador](../../experiment/platform/backend/rca_skill.py), [retriever](../../experiment/eval/memory_hog/opensearch_experiments.py) e [supervisor](../../experiment/eval/memory_hog/controlled_campaign.py).
- [Plano/piloto de 02/11](./2026-11-02-plano-piloto-melhoria-rag-historico.md), como antecedente exploratório, sem reunir seus números aos desta execução.

Esta é uma **auditoria retrospectiva de artefatos**, não uma nova avaliação de RCA. Nenhuma consulta MCP nova nem inferência foi executada para atribuir causa aos incidentes. Os números abaixo descrevem saídas já existentes; não renovam sua validade como evidência atual. A próxima avaliação deve obter uma nova coleta MCP, conforme a [skill de avaliação](../../.github/skills/rca-evaluation/SKILL.md).

Identidades que não devem ser confundidas:

- campanha de injeções: `exploratory-rag-20261101T214710257439Z`;
- avaliação online: `online-rag-32-20261103T203730Z`;
- tentativa consolidada: `inference-bounded512-retry-1`;
- corpus de treino legado: `exploratory-rag-20261028T191703038725Z`.

Os arquivos de sessão são locais e não constituem armazenamento de longo prazo. Antes de executar o plano, copiar os artefatos necessários para armazenamento persistente versionado, preservando hashes e origem; não depender deste caminho local.

## 3. Diagnóstico acadêmico dos resultados atuais

### 3.1 Classificação não equivale a mecanismo comprovado

| Medida retrospectiva | Resultado | IC 95% de Wilson, descritivo |
|---|---:|---:|
| Serviço selecionado igual ao alvo configurado | 13/32 = 40,6% | 25,5–57,7% |
| Tipo selecionado igual à falha configurada | 9/32 = 28,1% | 15,6–45,4% |
| Correspondência conjunta | 5/32 = 15,6% | 6,9–31,8% |
| Diagnóstico final `confirmed` | 3/32 | Não é acurácia |
| Correspondência conjunta entre os `confirmed` | 0/3 | 0–56,1% |

Os intervalos são marginais e aproximados: não corrigem dependência entre runs, serviço compartilhado, efeitos de ordem ou seleção dos casos concluídos. Com n=3, zero acertos entre confirmações é um alerta, não estimativa precisa do risco populacional.

**O relatório omitiu uma estratificação decisiva:** todos os sete runs `cpu-hog` têm `mechanism_observed.passed=false` e `intervention_confirmed_on_target=false`, apesar de `status=completed`. Os outros 25 têm ambos os gates verdadeiros.

| Estrato segundo os gates salvos | n | Serviço | Tipo | Conjunto |
|---|---:|---:|---:|---:|
| Intervenção/mecanismo confirmados | 25 | 11/25 | 7/25 | 4/25 |
| Intervenção/mecanismo não confirmados, todos CPU | 7 | 2/7 | 2/7 | 1/7 |

Consequências:

- 5/32 mede principalmente concordância com a configuração do injetor, não reconhecimento de 32 mecanismos comprovados.
- 4/25 = 16,0% é a correspondência conjunta no estrato com gates positivos; ainda não comprova a qualidade causal do texto.
- Não usar os sete casos CPU para concluir que o modelo ignorou sete injeções CPU bem-sucedidas.
- Não remover os sete casos silenciosamente: reportar os dois denominadores e as razões dos gates negativos.
- Antes de treinar novamente, corrigir ou validar a calibração CPU. Os sete exemplos CPU do corpus legado também têm `effect_validated=false` e ficam fora da recuperação padrão.

### 3.2 A saída inicial e a saída final não constituem um A/B experimental

Aqui, **A/B são representações de conhecimento**, não nomes de dois braços de controle. O diagnóstico inicial é uma etapa da própria investigação RAG.

| Estágio da mesma execução | Serviço | Tipo | Conjunto |
|---|---:|---:|---:|
| Diagnóstico inicial salvo | 15/32 | 13/32 | 8/32 |
| Diagnóstico final com recuperação/checks | 13/32 | 9/32 | 5/32 |

Houve quatro transições de erro conjunto para acerto e sete de acerto para erro. Isso sugere regressões na investigação/síntese, mas **não identifica o efeito causal da RAG**: houve mudança de contexto, estágio, checks e geração. A comparação correta exige uma condição sem histórico, com o mesmo executor, contrato, evidência e orçamento.

Os 32 runs já examinados passam a ser **dados de desenvolvimento**. Escolher melhorias após observar seus erros e depois apresentá-los como teste cego seria contaminação adaptativa.

### 3.3 A: fatos existentes não garantem representação fiel

Achados nas 32 representações A:

- Cada A tem 20 fatos: quatro por perfil de request rate, error rate, P95, CPU e memória. Nenhum `fact_limitations` foi preenchido.
- O alvo real aparece em algum fato nos 32 casos, mas em `observed_services` em apenas 26. O fato CPU do alvo aparece em 27.
- 14 A não possuem narrativa de logs; quatro não possuem narrativa de traces, embora o resultado agregado tenha evidências utilizáveis dessas famílias.
- Os fatos não carregam, por si só, todo o ciclo de vida, arestas, códigos HTTP ou sequência temporal. `fact_limitations=[]` não demonstra completude.
- No caso 1, a narrativa atribui ao `shipping` CPU de **7,79→8,27%**, valores que o próprio E001 associa a **payment**. O fato estruturado de `shipping` registra **4,85→58,51%**, delta **53,66 pontos percentuais**. É um exemplo concreto de troca de identidade na narrativa, não ausência de sinal métrico.

**O ajuste bounded-512 resolveu transporte, mas introduziu viés de representação:**

- O texto v2 coloca fatos métricos primeiro.
- A contagem reconstruída dos textos finais encontrou 502–515 tokens ASCII alfanuméricos somente nos fatos.
- Dos 16.384 tokens lexicais enviados nos 32 casos, 16.244 eram fatos métricos; os demais 140 eram `metric_changes`/`symptoms`.
- **Zero tokens de `service_relations`, `log_patterns`, `trace_patterns` ou `temporal_sequence` chegaram à busca lexical**, em todos os 32 casos.
- O embedding continuou recebendo o texto completo. Portanto, não se trata de perda total dessas famílias na recuperação, mas de redução da perna BM25 quase exclusivamente a métricas.

O limite de 512 não deve ser promovido como solução científica definitiva. Deve ser substituído por uma política determinística, balanceada por campo e testada contra o analisador real do índice.

### 3.4 B: concordância supervisionada não valida causalidade

O corpus local tem 32 B, todos `pending`/`inconclusive`. As classificações B concordam com as etiquetas nos 32 pares, mas isso é esperado em uma anotação que recebeu essas etiquetas.

- 24 pares têm efeito validado; oito não têm: sete CPU e um network-latency.
- A criação original usou Qwen 14B e Skill diferente; a avaliação usou Qwen 3.5 com compatibilidade legada autorizada.
- Há 32 B com menções a rótulo/gabarito/ground truth no texto serializado. Algumas são avisos adequados de limitação; outras tornam a interpretação dependente do rótulo, como “a natureza http-error do rótulo alinha-se…”.
- Essa linguagem aparece em 31 das 96 projeções recuperadas, presentes em 27 incidentes. Isso **não prova vazamento do gabarito do incidente de teste**. Mostra necessidade de distinguir metadados de supervisão, aviso de procedência e orientação baseada em observações.
- Os guias incluem applicability heterogênea e checks livres, por vezes amplos ou não executáveis; equivalência com a etiqueta não garante que discriminem falhas concorrentes.

Problemas da projeção fornecida ao Qwen:

- Cada histórico expõe quatro fatos e omite 16.
- Nas 96 projeções, as observações são dominadas por relações, sintomas e métricas; há apenas 20 observações de logs, três de traces e uma temporal.
- Nenhuma expõe `executed_checks`. Não se pode aprender adequadamente “check → resultado → hipótese apoiada/refutada” apenas de recomendações textuais.
- IDs históricos, quando mantidos nos fatos, precisam de namespace distinto dos IDs atuais, mesmo com avisos textuais.

B deve transmitir **como investigar e refutar**, não ensinar uma associação rígida entre “latência/5xx” e a etiqueta supervisionada.

### 3.5 Recuperação, execução e síntese

- Três pares foram recuperados **e fornecidos** em cada caso. A queda não se explica por todos os históricos terem sido descartados pelo orçamento.
- Apenas 18 dos 24 pares elegíveis apareceram entre os 96 resultados.
- O exemplo `orders/network-latency` apareceu em 21 casos; `orders/http-status-code`, em 18. Juntos ocupam 39/96 posições.
- Sobreposição descritiva com etiquetas: algum par do mesmo serviço em 14/32; mesma falha em 17/32; combinação exata em 5/32. Esses números **não são qrels causais nem recall de conhecimento útil**.
- O modelo escolheu `orders` em 12 casos, mas esse era alvo em quatro; nunca escolheu `payment`, alvo em cinco.
- Dos 53 follow-ups registrados: 19 rejeitados, 17 `no_data` e 17 `ok`. Logs respondem por 32 propostas; há quatro lifecycle e quatro trace_detail.
- Há `unresolved_check_rejection` em 12 incidentes; 14 terminaram com `reserved_final_budget`. Revisar o orçamento de reparos/reserva sem remover a proteção da síntese.
- Todos os 193 `model_attempts` têm `usage={}`. `total_tokens` do executor usa estimativa por tamanho quando não recebe usage; não apresentar os aproximadamente 120–140 mil por caso como tokens faturados medidos.
- Os 55,1 minutos do manifesto final não incluem toda a coleta, tentativas interrompidas e resultados reaproveitados. Tempo de ponta a ponta e custo de retries devem ser relatados separadamente.

### 3.6 Exemplos que orientam a revisão causal

| Caso | Observação auditável da saída | Questão a verificar antes de confirmar |
|---|---|---|
| 1 | A troca os valores CPU de payment/shipping; initial shipping/CPU vira final front-end/CPU | O contrato garante serviço, unidade e valor ligados ao mesmo fato? |
| 4 | `confirmed front-end/network-latency`; texto infere timeout Istio e 504 a partir de latência | Há erro/código/timeout explícito e origem de atraso, ou apenas duração elevada? |
| 7 | `confirmed orders/network-latency`; texto atribui espera a user/carts e possível banco | Há separação client/server e evidência da dependência, ou hipótese de contenção? |
| 26 | `confirmed front-end/http-status-code`; mecanismo descreve parsing de JSON após falha downstream | Parsing é sintoma/propagação ou origem demonstrada? O alvo catalogue foi rastreado? |
| 14, 19, 22 | Inicial memory-hog muda para CPU; fatos de memória existem | A síntese ignora memória, confunde GC/CPU, ou falta evidência discriminativa? |
| 32 | Inicial payment/CPU muda para orders/http-status-code | A memória histórica desloca o alvo para quem apresenta o impacto? CPU tem gate negativo: não presumir injeção válida |

Esta leitura identifica hipóteses de falha da estratégia. Não confirma retrospectivamente timeout, OOM, parsing causal ou mecanismo injetado com base no texto do modelo.

## 4. Hipóteses pré-registráveis e critérios de refutação

| ID | Hipótese | Intervenção isolada | Evidência esperada / refutação |
|---|---|---|---|
| H1 | Perda de identidade/famílias em A prejudica diagnóstico e BM25 | Validação factual + resumo balanceado | Menos trocas serviço/valor e melhor relevância; refutada se preservação não mudar recuperação/síntese |
| H2 | B genérico/pendente e sua projeção induzem ancoragem | B contrastivo com checks executados e limites | Melhora causal sem aumentar confirmações falsas; refutada se só mudar linguagem/confiança |
| H3 | Ranking concentra sintomas em orders | Campos/arestas + reranking de relevância | Diversidade útil e melhor localização; refutada se ranking melhora, mas diagnóstico não |
| H4 | Propostas inválidas/no-data consomem investigação | Inventário executável + reparo limitado | Menos rejeições e mais checks discriminativos; refutada se só aumentar número de consultas |
| H5 | Validador estrutural permite confirmação sem mecanismo | Gate semântico factual e revisão cega | Menos suporte inválido/falsa confirmação; não aceitar mera redução indiscriminada de `confirmed` |
| H6 | Corpus/calibração legados limitam CPU e consistência | Treino novo validado, Qwen 3.5, Skill fixada | Exemplos CPU com efeito comprovado e B sustentado; refutada se calibração passa e lacuna persiste |

As hipóteses são propostas. Não estão validadas pelo relatório atual.

## 5. Fases de engenharia e validação, sem executar agora

### F0 — Preservar, identificar e pré-registrar

**Passos**

1. Preservar relatório, A/B brutos, dados enviados ao modelo, saída bruta/normalizada, rejeições, transportes MCP e todos os retries. Não sobrescrever a tentativa anterior.
2. Registrar hashes de corpus local, projeção, índice, Skill/prompt/system card, código efetivamente carregado, manifesto e modelo.
3. Fixar identidade Qwen 3.5 real: nome servido não basta; registrar GGUF/checkpoint, quantização, template, contexto, parâmetros e seed quando suportada.
4. Separar papel do injetor, anotador de treino e avaliador. Proibir o diagnosticador de acessar especificação com `target`, tags da falha, ground truth e resultados do controlador.
5. Pré-registrar hipóteses, orçamento, ordem das condições, splits e critérios de promoção. Marcar os 32 runs antigos como desenvolvimento.

**Gate F0:** identidades verificáveis, backup legível, divisão de papéis e protocolo aprovado. Sem isso, nenhuma inferência pontuável.

### F1 — Tornar a execução persistente e reproduzível

**Mudanças futuras, cirúrgicas**

1. Vincular `validate_historical_diagnostic` à versão/hash da campanha, não à constante global. Validar explicitamente versões suportadas; testar sucesso v1/v2 e rejeição de divergência.
2. Remover a necessidade de monkeypatch/global mutation do runner. Registrar política lexical como versão própria; a configuração “v1” sozinha não identifica bounded-512.
3. Inicializar OpenTelemetry antes de iniciar workers e tornar a inicialização segura contra concorrência; testar que nenhum worker recebe tracer `None`.
4. Persistir runner, requests e checkpoints retomáveis por `(evaluation_id, condition, repetition, run_id)`. Não confundir ordem de conclusão com case_id.
5. Separar tentativa de inferência de tentativa de injeção. Retry automático restrito a transporte/contrato, com limite prévio, sem escolher a resposta que acerta mais.
6. Capturar usage real quando disponível; caso contrário, reportar estimativa e sua fórmula, sem custo monetário fictício.
7. Exigir comprovante por família **e fase**, endpoint, argumentos, status, contagens e horários; `observability_source=history` e existência agregada das três famílias não bastam.
8. Distinguir timeout cliente MCP de timeout servidor Prometheus/Loki/Tempo. O [ConfigMap histórico](../../deploy/kubernetes/manifests-monitoring/68-history-observability-mcp-configmap.yaml) declara 30 s por backend; cliente 180 s não aumenta esse limite automaticamente.

**Gate F1:** testes de contrato, concorrência e retomada passam; smoke read-only com MCP histórico confirma requests auditáveis. Nenhum reparo de infraestrutura automático.

### F2 — Tornar o supervisor explicitamente train-only

O [supervisor atual](../../experiment/eval/memory_hog/controlled_campaign.py) cria 70 células no perfil `rag` e exige preservar essa matriz; `medium` cria 35, mas as trata como teste. **Não executar `--level rag --run` para este plano e não renomear CSV para simular treino.**

**Passos**

1. Adicionar um modo train-only versionado que reuse as 35 células `train` e os mesmos gates de segurança, sem atravessar automaticamente para `test`.
2. Garantir em teste que o plano contém exatamente 35 combinações distintas, `split=train`, uma réplica, nenhuma célula de teste e nenhum congelamento implícito seguido de injeção.
3. Separar coleta dos runs de geração A/B. A análise Qwen deve ocorrer com a carga parada, evitando contenção e dependência da latência de anotação durante a campanha.
4. Criar pausas aprováveis em 1, 5, 10, 20 e 35 células; retomada reconcilia run existente em vez de reinjetá-lo.
5. Expandir o read-back histórico obrigatório também para treino. O fluxo atual de `process_cell` faz a espera explícita de histórico no ramo de teste; não presumir que isso já cobre treino.
6. Salvar todos os estados: planned, submitted, completed, blocked, aborted, failed, not_run; B inelegível é estado de conhecimento, não motivo para apagar um run.

**Gate F2:** dry-run sem cluster e testes provam 35 train/0 test, pausa/retomada, um workload ativo no máximo e ausência de reinjeção/congelamento automático.

### F3 — Preflight histórico e calibração sem injeção

1. Verificar read-only saúde, capacidade e retenção dos históricos, identidade do MCP e read-back de um run arquivado. Todas as ferramentas devem resolver os destinos `monitoring-history`.
2. Confirmar dual-write de métricas, logs e traces; **não chamar esse fluxo de migração** nem desligar os exporters atuais.
3. Conferir filtros: métricas de app, quotas, throttling, lifecycle, última terminação, tráfego/arestas e observabilidade do próprio executor precisam chegar ao destino apropriado. Eventos/probes/runtime que não existem hoje permanecem lacuna explícita até instrumentação validada.
4. Medir scrape/export delay, falhas de exportação, filas, P50/P95 de consulta e consumo de disco. Dimensionar retenção para todo o treino e horizonte de reanotação aprovado; usar volume por run medido + margem, não estimativa sem coleta.
5. Escolher um perfil único: ponto de partida da campanha anterior, **30 usuários, spawn_rate=5**, mesmo mix Locust e versão. Esses valores são candidatos a congelar após baseline saudável, não obrigação de manter carga inadequada.
6. Conferir intensidade CPU contra quota real do **container de aplicação**, identidade/UID, worker, unidade e duração. Validar configuração/predição sem injetar; a confirmação empírica virá na célula CPU da primeira tranche.
7. Fixar limites de CPU/memória, topologia, imagens, tráfego e regras de rede. Não relaxar o gate para “preencher” corpus CPU.

**Gate F3:** perfil/sizing congelados, quotas coerentes, leituras históricas funcionais, proteção contra vazamento, nenhuma lacuna de infraestrutura bloqueante. Falta de instrumento causal vira item de instrumentação, não conclusão do modelo.

## 6. Campanha única de treino: 35 experimentos

### 6.1 Matriz e ordem

| Dimensão | Valores |
|---|---|
| Serviços | front-end, catalogue, carts, orders, payment, shipping, user |
| Falhas | cpu-hog, memory-hog, network-latency, container-kill, http-status-code |
| Células | 7 × 5 = 35, uma execução planejada por combinação |
| Carga | Um perfil congelado; início/parada e baseline novos por run |
| Concorrência de injeção | 1 |
| Dados de teste novos nesta campanha | 0 |

Randomizar com seed e restrições pré-registradas, evitando alvos consecutivos quando possível. As primeiras cinco células devem incluir as cinco falhas em alvos distintos, incluindo CPU cedo, com ordem aprovada antes da execução. Esses canários **fazem parte dos 35**, não são cinco injeções extras.

Se uma célula CPU falhar na confirmação, pausar e revisar. Não refazer silenciosamente a combinação: guardar o caso inelegível e os restantes não executados. Qualquer substituição precisa de emenda e autorização; o número de tentativas reais deve aparecer além do número de células.

### 6.2 Ciclo obrigatório de cada run

1. **Identificar:** run_id opaco, cell_id restrito ao controlador, config/hash, container/UID e parâmetros.
2. **Verificar estado:** limpeza anterior, app saudável, carga anterior parada, ausência de netem/helpers/VirtualService residual.
3. **Iniciar carga:** mesmo perfil, novo warmup e estabilização. Não reutilizar baseline do run anterior.
4. **Delimitar baseline:** guardar horários UTC, scrapes, volume e disponibilidade; se instável, pausar sem injetar.
5. **Injetar uma falha:** registrar início/fim observados e intervalo de incerteza. Registrar ground truth separadamente; nomes/tags de caos não entram no contexto diagnóstico.
6. **Verificar intervenção:** avaliar `intervention_confirmed_on_target` e `mechanism_observed` com critérios existentes de quota/intensidade/duração/identidade; separar efeito do impacto HTTP.
7. **Observar post:** mesma carga até terminar a janela experimental e verificar recuperação.
8. **Recuperar e limpar:** preservar gates de recuperação experimental e operacional. Política atual: prazo operacional 600 s, estabilidade 120 s; duas recuperações consecutivas ou cinco totais interrompem a campanha. Não converter recuperação tardia em sucesso experimental.
9. **Parar carga:** comprovar identidade e parada; não iniciar outra célula sem essa prova.
10. **Validar histórico online:** consultar baseline/fault/post nas três famílias via MCP após visibilidade. Timeout/retry de leitura não reinjeta a falha.
11. **Selar o run:** guardar gates, limitações, referências de telemetria, comprovantes MCP e hashes; o controlador decide continuar ou pausar.

Manter os thresholds/timings de recuperação do supervisor; qualquer modificação é emenda antes da execução, não ajuste para obter um resultado favorável. Amostras de recuperação operacional depois da janela experimental ficam separadas.

### 6.3 Tranches e gates

| Ponto de parada | Validação mínima | Ação se não passar |
|---|---|---|
| 1/35 | Carga isolada, janelas/UID, cleanup/parada e read-back das três famílias | Parar sem segunda injeção |
| 5/35 | Cinco mecanismos inspecionados; CPU confirmado; lifecycle e traces recuperáveis; revisão A/B piloto | Revisar infraestrutura/calibração; não compensar dados inválidos |
| 10/35 | Continuidade, tamanho/retention, cobertura e ausência de resíduos | Pausar e preservar ledger |
| 20/35 | Distribuição parcial, estabilidade operacional e evidência de todas as classes executadas | Não ampliar automaticamente |
| 35/35 | Todas as células com estado explícito; relatório por gate/serviço/falha | Congelar somente dados elegíveis após revisão |

Planejar 35 não promete 35 B validados. A campanha pode terminar com menos runs completos ou poucos guias sustentados; isso é resultado científico a relatar.

## 7. Armazenamento e recuperação posterior

### 7.1 Dois planos de persistência, sem confundi-los

**Datasources históricos** armazenam as séries, linhas de log e spans originais, com identidades e tempos preservados.

**Object store/registro persistente** armazena protocolo, ledger, requests MCP, especificações restritas, manifests, saída Qwen, A/B, hashes e referências aos históricos. Esses registros permitem auditoria e preparação posterior, mas **não substituem a consulta MCP numa avaliação**.

Por run, persistir:

- janelas baseline/fault/post e incerteza temporal;
- mapping serviço/pod/container/UID e versão de app;
- consultas Prometheus e séries/unidades/sampling; LogQL/container/stream; TraceQL/trace_id/span_id/arestas;
- status, no_data, paginação, limites, contagens, export/read-back e erros;
- mecanismo do injetor, recuperação/limpeza e ground truth em área restrita;
- A pré-rótulo, B pós-anotação, evidência de cada claim, contraditórios e revisões;
- hashes e proveniência de cópias S3; IDs/timestamps inalterados;
- parâmetros Qwen/embeddings/índice, prompts e entradas/saídas exatas, inclusive reparos.

Não acrescentar o tipo da falha configurada aos logs/spans visíveis ao diagnosticador. A identidade natural do serviço é indispensável à observabilidade; o alvo **configurado pelo injetor** não é evidência.

### 7.2 Gate de read-back e completude

1. Toda avaliação nova inicia coleta MCP nova de todas as fases/famílias para todos os casos incluídos, antes da inferência.
2. `live_shared` pode compartilhar essa coleta entre condições/repetições somente dentro daquela avaliação, com pacote selado e hashes. Nova evaluation_id exige nova coleta.
3. Erros de backend bloqueiam pontuação. `no_data`, amostragem, truncamento e cobertura parcial permanecem limitações; não retornar sucesso-shaped defaults.
4. Não exigir logs de erro para uma falha que não necessariamente gera erro; exigir fonte consultada e cobertura explicável. Se a cobertura inviabilizar diagnóstico, manter inconclusivo.
5. Para Loki: coleta estratificada por serviço/container/fase e queries focadas quando houver truncamento; orçamento transparente. Não aceitar 28 linhas como cobertura de toda uma janela.
6. Para Tempo: registrar filtro, limite, paginação quando suportada, total/amostra, buscas de erro e latência por operação, detalhes dos candidatos e baseline comparável. Cinco resultados não são prova de completude.
7. Arquivo S3 é preservação auxiliar: validar SHA-256, paridade de traces/spans, cobertura e fetch errors. Não servir como fallback silencioso nem como fonte independente adicional.
8. Ao atingir retenção ou perder dados, registrar indisponibilidade e suspender avaliações afetadas; não pontuar replay congelado.

## 8. Reconstruir A/B com Qwen 3.5

### F4 — A fiel, observacional e balanceado

1. Consultar o treino online pelo MCP histórico e gerar A em contexto novo, sem etiqueta, alvo de injeção, diagnóstico B ou explicação do controlador.
2. Vincular cada fato a ponteiro de origem, serviço/container, unidade, janela, estatística e amostras; validar automaticamente identidade e números. Narrativa não pode contradizer o fato estruturado sem registrar a inconsistência.
3. Preservar perfis separados; não somar deltas de CPU%, req/s, bytes e ms.
4. Incluir evidência observacional de lifecycle, arestas client/server, códigos/flags, operações e sequência temporal. Cobertura ausente deve aparecer no A.
5. Não depender de “top-4 por delta” como única representação: sinais de mecanismo/ciclo de vida podem não liderar RED. Selecionar por perfil e registrar omissões.
6. Construir campos lexicais com orçamento por família, normalização/deduplicação e identidade preservada. Derivar o teto do `_analyze`/reescrita real da query, incluindo expansão por campos, não apenas `split()` ou regex.
7. Manter texto completo auditável e embedding separado da projeção; registrar quais fatos e grupos foram omitidos.

**Gate F4:** caso 1 não permite trocar payment/shipping; fixtures cobrem no_data, picos, quotas, restart e unidades; campos disponíveis de logs/traces/relações não somem por corte de prefixo. Se não houver conteúdo, registrar ausência, não inventar.

### F5 — B contrastivo, executável e revisável

1. Em contexto separado, o anotador Qwen 3.5 recebe etiqueta **de treino** e A já selado. Rótulo é supervisão, não suporte causal.
2. Fazer pelo menos duas hipóteses e checks históricos online que possam discriminar mecanismo, sintoma e impacto.
3. Estruturar guia como: sinal, aplicabilidade, contraindicações, alternativas, check executável, suporte esperado, refutação esperada, resultado realmente observado e limitação.
4. CPU: quota/worker/throttling/tempo de processamento; memória: pressão/limite/OOM/GC; rede: mesma aresta e client vs server; container-kill: identidade/timestamp/motivo antes do restart; HTTP: local do código/flag e origem vs propagação. Não inferir falha a partir de limiar genérico de 50%, 5xx ou restart isolados.
5. Separar etiqueta restrita de texto fornecido à RAG. Avisos de supervisão são metadados; eliminar raciocínio cuja única justificativa seja “o rótulo diz”. Referências históricas usam namespace próprio.
6. Expor na projeção os checks executados e resultados, sem reapresentá-los como evidência atual.
7. Revisar todos os B em rubrica causal. Preferir dois revisores independentes, inicialmente cegos às etiquetas na revisão de claims; resolver divergências e registrar concordância. Revisão humana não pode criar dados ausentes.
8. Promover apenas efeito validado + evidência independente de fault + cadeia causal/identidade sustentada. B inconclusivo é preservado como hipótese/experiência negativa; não promovido para completar 35.

**Gate F5:** inventário dos 35 estados, qrels/rubrica de relevância e causalidade, origens auditáveis, nenhum gabarito de teste; compatibilidade legada desativada no corpus novo, criado integralmente com Qwen 3.5/Skill fixada.

## 9. Evolução incremental do retrieval e da síntese

### F6 — Medir recuperação antes de aumentar contexto

1. Baseline de engenharia: política bounded-512 explicitamente versionada, para reprodução exploratória.
   Nas comparações principais, usar A/B reconstruídos com Qwen 3.5 em todas as condições RAG. O corpus Qwen 14B fica apenas como referência legada separada; não confundir seu efeito com o da arquitetura de recuperação.
2. Variante 1: projeção lexical balanceada por campos, com limite verificado no analisador e ranking BM25+k-NN/RRF. Não alterar embedding/top-k ao mesmo tempo.
3. Variante 2, somente se a anterior passar: campos de serviço/aresta/operação e projeção B com discriminadores/checks. Não filtrar pelo serviço injetado; candidatos só vêm de sinais atuais.
4. Variante 3 opcional: reranking pelo próprio Qwen 3.5 ou método determinístico, condicionado a ganho demonstrável e custo. Não introduzir outro LLM.
5. Manter top-k=3 inicialmente; testar 5 somente como ablação posterior. O piloto anterior não sustenta que mais exemplos bastam.
6. Separar `pending heuristics` de `validated guides`. A análise principal usa guias sustentados; qualquer inclusão de hipóteses pendentes é braço exploratório explicitamente marcado.
7. Medir nDCG@k/Recall@k/MRR com relevância atribuída por rubricadores a partir de mecanismo/discriminadores. Reportar também cobertura por classe, concentração e diversidade de serviços.
8. Correspondência de rótulo é métrica auxiliar, nunca substituto de relevância. Se nenhum guia relevante existe no corpus, reportar lacuna de corpus; não tratar como erro puro de ranking.

**Gate F6:** sem 400 de cláusulas, sem campos disponíveis perdidos, score/ranks/projeção registrados. Aprovar relevância sem concluir automaticamente ganho de RCA.

### F7 — Checks viáveis e síntese sem falsa autoridade

1. Gerar check a partir do inventário realmente executável e do escopo vigente. Guardar hipótese, discriminador, instrumento e query antes de executar.
2. Rejeição recebe no máximo o reparo pré-registrado; repetição/no_data não abre loop ilimitado nem libera acesso ao alvo do controlador.
3. Reservar orçamento final proporcional ao contrato efetivo; medir quantos checks se perderam por reserva. Não simplesmente aumentar todos os limites.
4. Tornar disponíveis pares de amostras baseline/fault da mesma operação/aresta, dados de última terminação e flags/códigos, quando existirem.
5. Na síntese, exigir distinção entre causa local, espera pela dependência e impacto no chamador. “orders apresenta 5xx” não implica que orders seja a origem.
6. Validar cada claim discriminativo contra evidência atual: identidade, fase, valor/código, relação causal e contraditórios. Duas famílias por ID não provam que apoiam o mesmo mecanismo.
7. Se timeout, OOM, GC ou erro de parsing não é demonstrado, manter como hipótese. Não transformar ausência de logs em falha silenciosa comprovada.
8. Gate semântico deve abster-se de confirmação sem cadeia sustentada; não usar acerto do gabarito como validador do diagnóstico.

**Gate F7:** regressões dos casos 1/4/7/26 e propostas fora do escopo cobertas por testes; rubrica cega mostra suporte factual. Uma confirmação mais rara, sozinha, não constitui melhoria.

## 10. Avaliação online e desenho científico

### F8 — Desenvolvimento, validação e teste não são a mesma coisa

- Os 32 casos antigos são desenvolvimento. Reavaliá-los exige MCP novo e não permite reivindicar teste intocado.
- Os 35 novos são uma campanha **de treino**. Avaliar um run com seu próprio A/B no corpus é proibido.
- Para diagnóstico interno, usar folds agrupados por serviço: sete folds de cinco runs, removendo do corpus os cinco runs de validação e todos seus derivados/retries. Os seis serviços restantes fornecem até 30 exemplos; documentar a dificuldade de transferência para serviço excluído.
- Hiperparâmetros/arquitetura devem ser congelados antes dos folds externos. Se houver tuning, usar apenas folds internos; não escolher variante pelos rótulos do fold externo.
- Os folds não criam novas injeções. Todas as consultas de avaliação continuam online via MCP; não usar evidências de anotação como coleta de avaliação.
- Após decidir a configuração, reanotar/congelar o corpus final com todos os runs de treino elegíveis. Não apresentar seu desempenho ressubstituído como teste.
- Um teste confirmatório independente, idealmente 35 novos incidentes com uma réplica por combinação, fica **fora deste plano de execução** e requer autorização própria. Com uma única campanha de treino, não há prova confirmatória independente da melhoria.

### Condições e ablações

| Condição | Finalidade |
|---|---|
| C0: Qwen 3.5 + mesma Skill/card, sem histórico | Controle principal |
| C1: mesma estratégia + RAG baseline versionada | Efeito incremental do histórico |
| C2: C1 + A/projeção balanceados | Isolar representação |
| C3: C2 + B contrastivo/curado | Isolar conhecimento |
| C4: C3 + seleção de checks/gate semântico | Isolar uso e confiabilidade |

Não executar todas as variantes simultaneamente de início. Primeiro smoke em três runs de desenvolvimento selecionados antes da consulta, depois a mesma coorte de 32, e só avançar para validação agrupada quando os gates técnicos passarem.

Separar dois experimentos:

1. **Ablação de síntese:** mesma coleta online selada, mesmo A e mesmos checks dentro da avaliação; muda apenas o histórico/instrução fornecido. Mede uso do contexto.
2. **End-to-end:** mesmo orçamento/instrumentos e coleta inicial; condições podem escolher diferentes follow-ups MCP. Mede a estratégia completa.

Para cada comparação:

- fixar Qwen 3.5, geração, Skill, system card e orçamento total; alternar/randomizar ordem por run;
- usar três repetições pareadas de inferência quando o custo aprovado permitir, sem multiplicar injeções; se houver apenas uma, declarar exploração;
- garantir coleta de todas as fases/famílias antes de inferência e fresh MCP a cada evaluation_id;
- comparar por run_id, incluindo falhas/abstenções; não escolher melhor retry;
- manter rótulos acessíveis somente ao avaliador após a inferência.

### Métricas obrigatórias

1. Correspondência de serviço, tipo e conjunta; macro por serviço/falha e matriz de confusão.
2. Resultados estratificados por confirmação do efeito, cobertura e elegibilidade B; denominator completo separado do mecanismo válido.
3. Retrieval com qrels: relevância, ranking, concentração, par fornecido/usado e cobertura de classes.
4. Fidelidade A: troca de identidade/unidade/valor, omissões por família e preservação de origem.
5. Qualidade B: aplicabilidade, suporte/refutação, executabilidade, independência e revisão.
6. Checks: propostos/executados/rejeitados/no_data, discriminação, reparos e reserva.
7. Causalidade: claims verificáveis, origem→sintoma→impacto, restarts explicados, contradições, suporte atual vs histórico.
8. Abstenção: cobertura de confirmações, precisão seletiva e falsa confirmação; separar acerto de rótulo de causa sustentada.
9. Custo: tokens medidos vs estimados, chamadas, latência/backend/LLM/fila, ponta a ponta, coleta e todos os retries.
10. Segurança experimental: planejados/submetidos/concluídos/bloqueados/abortados/não executados e recuperações tardias.

Análise estatística: contrastes pareados por run, IC para diferenças por bootstrap agrupado e teste exato de McNemar quando aplicável; repetições do mesmo run não são amostras independentes. Reportar tamanhos de efeito e intervalos, com comparações primárias prévias. Múltiplas variantes/classes são exploratórias ou recebem controle de multiplicidade. Uma matriz com uma observação por combinação não estima variância de injeção dentro do cenário.

### Gate científico de promoção

Pré-registrar e aprovar um ganho mínimo relevante antes de consultar resultados comparativos; não escolher esse limiar a posteriori. Promover a variante somente se:

- contratos/segurança/leitura online passam;
- não houver vazamento nem regressão de fidelidade/causalidade;
- houver ganho no contraste principal, com incerteza e custo explicitados;
- a taxa de falsas confirmações não piorar;
- limites do n pequeno e da reutilização de desenvolvimento forem declarados.

Se o intervalo não discriminar benefício, concluir **“evidência insuficiente”**, não “melhorou” nem “falhou definitivamente”. Retreinar pesos, trocar modelo ou relaxar confirmação não é a próxima ação automática.

## 11. Retreino posterior sem reinjeção

1. Escolher versão do corpus e uma nova annotation_id; preservar original e razões da revisão.
2. Verificar retenção e consultar novamente os históricos pelo MCP para os mesmos run_ids/janelas.
3. Reconstruir A sem etiquetas; comparar versões em identidade, cobertura e omissões.
4. Regerar B com Qwen 3.5, supervisão de treino separada e checks online; rubricar sem promover pelo gabarito.
5. Congelar só os pares elegíveis; salvar mapa `run_id → A_version → B_version → pair_id → index_version`.
6. Criar índice novo e alias explícito somente após gate; nunca modificar o índice congelado anterior.
7. Fazer nova avaliação MCP online com splits preservados. Não reusar coleta da avaliação passada.
8. Se a telemetria original não estiver mais recuperável, marcar o run indisponível para essa avaliação; backup não autoriza pontuação offline.

## 12. Entregáveis e ordem de autorização

| Entregável futuro | Gate | Não fazer antes |
|---|---|---|
| Manifesto reproduzível e arquivo histórico dos artefatos atuais | F0 | Novas inferências pontuadas |
| Validador/runner persistentes e inicialização concorrente segura | F1 | Paralelismo irrestrito |
| Modo train-only com 35 células e pausas | F2 | `--run` no perfil rag atual |
| Read-back, sizing, retenção e calibração configurada | F3 | Primeira injeção |
| Relatórios de tranches 1/5/10/20/35 | Campanha | Avançar sem gate |
| Dataset A/B Qwen 3.5, rubrica e estados de elegibilidade | F4–F5 | Congelar 35 guias por obrigação |
| Índices isolados e métricas de relevância | F6 | Alterar top-k/embedding/B simultaneamente |
| Checks e síntese causal auditáveis | F7 | Confirmar por contagem de famílias |
| Avaliações online pareadas e relatório de incerteza | F8 | Alegar ganho por treino ou desenvolvimento |
| Corpus final versionado e rotina de reanotação online | Retreino | Sobrescrever histórico ou usar fallback offline |

**Primeira autorização recomendada:** F0–F2, sem injetar falhas. Revisar seus artefatos/testes; depois autorizar F3. A autorização para a primeira célula e para cada ampliação de tranche deve ser explícita.
