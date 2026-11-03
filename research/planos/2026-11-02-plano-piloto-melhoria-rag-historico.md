# Plano e piloto de melhoria da RAG com historico

## Resumo executivo

O relatorio de 01/11 nao mostra ganho da RAG sobre a Skill pura: a classificacao conjunta servico+falha foi 7/32 para RAG, 10/32 para Skill e 11/32 para controle. A RAG acertou mais a dimensao de falha (14/32 vs. 13/32), mas menos a de servico (13/32 vs. 17/32). O principal problema nao parece ser apenas recuperar poucos exemplos: em nove casos o par exato estava no top-3 e nenhum produziu classificacao conjunta correta.

Foi feito um piloto pareado em tres runs ja concluidos. As consultas foram somente leitura, no MCP historico, e todas as inferencias usaram `qwen3.5-9b-grafana`. Nenhuma falha nova foi injetada. As tres alteracoes testadas nao melhoraram a classificacao:

1. Aumentar `top_k` de 3 para 5 incluiu um exemplo exato de `catalogue/container-kill`, mas a resposta continuou incorreta.
2. Incluir B nao validado, com seu estado explicitamente apresentado ao modelo, nao mudou a resposta `user/cpu-hog`, que ja estava correta no baseline novo.
3. Um prompt mais explicito para priorizar sinais atuais e separar origem de propagacao nao mudou a resposta do caso `shipping/http-status-code`.

Todos os resultados novos permaneceram `inconclusive`. Eles medem classificacao de rotulos, nao confirmacao de causa raiz.

## Escopo e reproducibilidade

- Relatorio de referencia: [FINAL_REPORT.md](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/FINAL_REPORT.md).
- Configuracao da campanha usada como referencia: [config.yaml](../../experiment/eval/memory_hog/out/exploratory_campaigns/exploratory-rag-20261101T214710257439Z/config.yaml).
- Os tres runs piloto ja estavam concluidos: `run-f8097606680a` (`catalogue-container-kill-r1`), `run-897c049ee6cf` (`user-cpu-hog-r1`) e `run-08459ef8f204` (`shipping-http-status-code-r1`).
- Cada especificacao exigia `historical_observability` e apontava para `http://127.0.0.1:18082/sse`. O ConfigMap do MCP historico aponta para Prometheus, Loki e Tempo no namespace `monitoring-history`: [68-history-observability-mcp-configmap.yaml](../../deploy/kubernetes/manifests-monitoring/68-history-observability-mcp-configmap.yaml). A conexao SSE e a lista de ferramentas Prometheus/Loki/Tempo foram verificadas.
- O endpoint local de modelos anunciou `qwen3.5-9b-grafana`; esse modelo foi fixado em todas as inferencias novas. O endpoint da API foi usado para ler o estado, a janela e a especificacao dos runs, nao para iniciar uma campanha.
- A base tem 32 pares e usa OpenSearch, indice congelado `sockshop-rca-experiments-v1-8255d164bb4d41b8`, `incident-a-v2`, RRF, `top_k=3` e orcamento de historico de 24.000 caracteres. O modelo de origem da base e `qwen-14b`; a compatibilidade com o Qwen 3.5 atual estava explicitamente autorizada pela campanha. Portanto, a diferenca de modelo continua sendo uma limitacao para comparar a base com o modelo atual.
- A Skill carregada no piloto tinha hash `ef69c45e...`, igual ao hash atual configurado na campanha; a base foi criada com hash `f11834ec...`. A compatibilidade legada da Skill tambem estava autorizada. Havia mudancas preexistentes no worktree em `SKILL.md`, referencias da Skill e `retrieval.py`; nenhuma foi alterada neste trabalho.
- Para isolar cada hipotese, o baseline novo fez a investigacao completa sobre o historico. A variante reutilizou a mesma analise A, evidencias atuais e checks daquele baseline e refez somente a sintese final, alterando top-k, elegibilidade/proveniencia de B ou instrucao. Cada variante fez uma chamada de modelo. Isso isola a etapa final, mas nao testa novamente a selecao de checks.
- O piloto e pequeno, uma observacao por variante. Os resultados foram mantidos inconclusivos; nao sao evidencia para promover B nem para relaxar gates causais.

Nenhum caminho da campanha interrompida de 02/11 foi reutilizado. Nenhum run, experimento de caos ou falha foi iniciado.

## Diagnostico do relatorio original

### Servicos com maior dificuldade

Acertos de servico agrupados pelo servico verdadeiro do run:

| Servico | Casos | Skill pura | RAG |
|---|---:|---:|---:|
| catalogue | 5 | 3 | 1 |
| front-end | 4 | 1 | 1 |
| shipping | 5 | 1 | 1 |
| carts | 5 | 3 | 2 |
| user | 4 | 3 | 2 |
| payment | 5 | 2 | 3 |
| orders | 4 | 4 | 3 |

O maior desvio e a superselecao de `orders`: a RAG escolheu esse servico em 16/32 respostas, embora o rotulo verdadeiro fosse `orders` em 4/32; apenas 3 das 16 escolhas estavam corretas. `catalogue`, `shipping` e `front-end` sao os grupos com menor acerto de servico.

### Falhas que continuam dificeis

| Falha | Casos | Skill pura | RAG | Leitura |
|---|---:|---:|---:|---|
| container-kill | 6 | 2 | 0 | Principal lacuna de classificacao da RAG |
| memory-hog | 7 | 5 | 3 | Quatro casos foram classificados como `cpu-hog` |
| network-latency | 5 | 2 | 2 | Sem ganho sobre a Skill |
| cpu-hog | 7 | 4 | 4 | Sem ganho; a base nao tem B validado para CPU |
| http-status-code | 7 | 0 | 5 | Melhor resultado relativo da RAG |

O relatorio registra 31/32 resultados RAG como inconclusivos e apenas 1 confirmado. Essa cautela e adequada: acerto do rótulo experimental nao prova causa. Em particular, restart sem motivo sustentado nao confirma por si so uma causa de container-kill.

Nos seis casos `container-kill`, a RAG os classificou como `http-status-code` em tres, `network-latency` em dois e `memory-hog` em um. Portanto, o problema e mais amplo que localizar o servico: a categoria da falha tambem nao esta sendo reconhecida.

## Hipoteses e testes do piloto

| Hipotese / mudanca isolada | Run e resultado baseline novo | Variante e resultado | Conclusao do piloto |
|---|---|---|---|
| **Mais candidatos ajudam quando o par correto esta abaixo do top-3.** Teste `top_k=5` em `catalogue-container-kill-r1`. | `front-end / network-loss`, inconclusivo. Tres candidatos validados; 20 fatos metricos; evidencias de metricas, logs e traces. | O par validado `catalogue / container-kill` apareceu no rank 4 e foi fornecido ao modelo. Foram recuperados cinco pares, mas quatro couberam no orcamento. A resposta virou `user / network-loss`, tambem incorreta e inconclusiva. | **Nao melhorou.** Recuperar o par exato nao bastou; mais contexto mudou o servico escolhido sem corrigir a falha. Manter `top_k=3` ate uma avaliacao maior. |
| **B pendente pode cobrir lacunas sem virar autoridade.** Teste `include_unvalidated_experimental=true` em `user-cpu-hog-r1`, expondo `effect_validation` por par e um aviso explicito de que B nao validado e apenas hipotese. | `user / cpu-hog`, inconclusivo e com rótulo conjunto correto. Tres pares validados; evidencias de metricas e traces. | O B exato `user / cpu-hog` apareceu em rank 1, mas tinha `effect_validated=false`; o resultado continuou `user / cpu-hog`, inconclusivo. | **Sem ganho mensuravel neste caso.** A resposta ja estava correta no baseline novo. Nao promover o B: a intervencao que o originou nao passou o gate experimental. |
| **Uma instrucao mais firme reduz o vies de servico e separa origem de propagacao.** Teste em `shipping-http-status-code-r1`. | `orders / io-stress`, incorreto e inconclusivo. O par validado exato `shipping / http-status-code` estava em rank 1; evidencias de metricas, logs e traces. | A instrucao pediu comparacao temporal por servico, separacao entre origem e efeito propagado e uso de historico apenas para sugerir checks. A resposta continuou `orders / io-stress`, inconclusiva. | **Nao melhorou.** Este caso reforca que cobertura e uma instrucao geral nao garantem que o modelo use corretamente o contexto ou as evidencias atuais. |

Cada baseline fez seis chamadas de modelo; as sinteses variantes fizeram uma chamada cada. Os baselines geraram 20 `metric_facts` por run. Esse volume demonstra que ha fatos estruturados em A, nao que todos sejam discriminativos.

### Instabilidade observada

Uma nova inferencia no mesmo run `user-cpu-hog-r1` mudou o resultado arquivado de `user / network-latency` para `user / cpu-hog`. Em `shipping-http-status-code-r1`, o resultado arquivado era `orders / http-status-code`; a inferencia nova produziu `orders / io-stress`. Isso mostra que uma amostra por condicao nao e suficiente para declarar uma melhoria estavel. Nao foi isolada a causa da variacao; ela pode depender de decodificacao ou de diferencas do runtime/prompt. O proximo piloto deve registrar os parametros de geracao e repetir cada condicao.

## Melhorias propostas para os conjuntos A e B

### A — observacoes do incidente atual

1. Preservar o isolamento de A: nao inserir labels, diagnosticos historicos, run IDs ou metadados do avaliador no texto de recuperacao.
2. Gerar uma matriz por servico e fase (`baseline`, `fault`, `post`) para taxa de erro, latencia, requests, CPU, memoria, reinicios e OOM. Manter unidade, janela, amostras e fonte para cada fato.
3. Usar IDs de evidencia separados por servico/familia, em vez de concentrar muitos fatos num unico ID. Distinguir sinal local da falha de erro propagado para servicos downstream.
4. Anotar explicitamente cobertura e lacunas: janela parcial, zero amostras, logs truncados, edge de rede sem dados e motivo de restart ausente. Ausencia nao deve ser convertida em sinal negativo.
5. Testar a qualidade de A por ablation com os mesmos runs historicos, modelo e base congelada. O piloto atual nao comparou versoes de A; portanto, nao conclui que A v2 melhorou ou piorou o resultado.

### B — guias de diagnostico historico

1. A base atual tem 24 pares elegiveis: 7 `http-status-code`, 5 `container-kill`, 7 `memory-hog` e 5 `network-latency`. Nao ha B validado para `cpu-hog`.
2. Os oito pares excluidos sao sete CPU e um `user-network-latency`. Os sete CPU ficaram abaixo do piso de intensidade previsto; o caso de rede nao tinha a latencia da aresta necessaria. O read-back historico desses oito ficou `pending`, sem amostras de metricas, logs ou traces; nao ha dados historicos suficientes para revalidar os pares agora. Manter `effect_validated=false`, `guide_status=pending` e `causal_status=inconclusive`.
3. Corrigir a cobertura de CPU somente com material historico que passe os gates definidos. Nao usar os 32 runs de teste para treinar B e nao promover os oito pares por concordarem com labels.
4. Quando se testar B pendente, fornecer ao modelo a proveniencia e o estado de validacao de cada par. O aviso nao pode afirmar que todo exemplo teve efeito observado quando a busca incluiu pares cujo efeito nao foi validado.
5. Estruturar cada B como procedimento transferivel: sinal observado, alternativas, contradicoes, checks que discriminam e resultado esperado/refutacao. O rótulo passado durante a anotacao nao e evidência independente.
6. Separar a elegibilidade de B por qualidade de efeito e por utilidade diagnostica. Um par pode sugerir checks sem sustentar causa, mas deve continuar marcado como hipotese.

## Proximo plano de avaliacao

1. **Congelar condicoes:** mesmo Qwen 3.5, skill/hash, corpus/index, prompt, opcoes, janelas e `run_id`; registrar temperatura, top-p, seed e limites retornados pelo endpoint.
2. **Usar somente runs concluidos:** consultar novamente Prometheus, Loki e Tempo pelo MCP historico. Nao iniciar injecoes nem a campanha interrompida.
3. **Executar uma mudanca por vez:** A v2 vs. proxima versao; top-k/reranking; B validado vs. B pendente com proveniencia; prompt orientado a evidencias atuais. Reutilizar o mesmo pacote atual ao comparar sinteses.
4. **Repetir cada condicao:** no minimo tres inferencias por caso, reportando resultados individuais e variacao. O piloto atual e exploratorio, nao tem poder para estimar ganho.
5. **Avaliar separadamente:** acerto conjunto e por dimensao, recall de pares que foram efetivamente fornecidos (nao apenas recuperados), falso positivo de `orders`, inconclusao/confirmacao, citações validas, custo e latencia.
6. **Manter o gate causal:** nao aceitar melhora de classificacao como melhora de RCA causal; nao aumentar confirmacoes sem cadeia causal, fontes independentes e motivo de restart sustentado.

### Decisao provisoria

- Nao alterar o `top_k` padrao com base neste piloto.
- Nao promover os oito B pendentes; permitir apenas experimento claramente marcado e com proveniencia.
- Nao considerar suficiente um prompt generico de evidencia-first. A proxima variante deve exigir uma comparacao estruturada de sinais locais por servico/fase e explicitar a diferenca entre causa e propagacao.
- Priorizar `container-kill`, `memory-hog` versus `cpu-hog`, e a localizacao de `catalogue`/`shipping`/`front-end`. Tratar a falta de B validado de CPU e a ausencia de motivo de restart como lacunas de conhecimento/evidencia, nao como ausencia de falha.
