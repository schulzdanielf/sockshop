# Plano de ajuste do treinamento e reconstrução de B com Qwen 3.5

**Data:** 05/11/2026. **Estado:** proposta; nenhuma regeneração, consulta de telemetria, publicação ou troca de consumidor foi executada por este plano.

## 1. Objetivo e resultado esperado

Reconstruir o conjunto B dos experimentos de **treinamento**, reutilizando os runs existentes e consultando a telemetria disponível nos datasources históricos. B deve ensinar uma investigação reproduzível: sintomas iniciais → hipóteses → verificações executadas → resultados → discriminação entre alternativas → serviço-alvo e falha corretos.

Usar **exclusivamente Qwen 3.5** para geração, revisão e replay diagnóstico deste trabalho. Não há fine-tuning de pesos: “treinamento” designa aqui a construção do corpus de conhecimento A/B para RAG.

O resultado não será uma coleção de rótulos corretos por imposição. Somente será publicado como diagnóstico bem-sucedido o caso em que as evidências sustentarem a conclusão correta e outro agente conseguir reproduzir o caminho sem receber o rótulo daquele run.

**Regra para insuficiência:** reconstruir e prestar contas de todos os casos da coorte, mas não exigir que todos se tornem exemplos bem-sucedidos. Se os dados históricos não permitirem identificar a falha, registrar a lacuna e excluir esse guia do corpus principal. Não inventar fatos, reinjetar falhas, escolher outro run silenciosamente ou usar o rótulo como prova.

## 2. Coorte e fontes imutáveis

Referência inicial: corpus `rag-rebuild-v2-live-adjudicated-20261105`, base SHA-256 `b7ba43aea0e92c571803ca786513390c95d8d041a9b1af504755d2f968d913d3`, índice `sockshop-rca-experiments-v2-b7ba43aea0e92c57`.

Fontes:

- [Base congelada de treinamento](../../experiment/platform/data/object_store/.rca_knowledge/rag-rebuild-v2-live-adjudicated-20261105/exploratory_base.json).
- [Pares-fonte adjudicados](../../experiment/platform/data/object_store/.rca_knowledge/campaign-20261103T234452388707Z-rag-rebuild-v2-200k/review/adjudicated-pairs-live-mcp/).
- [Manifesto de adjudicação](../../experiment/platform/data/object_store/.rca_knowledge/campaign-20261103T234452388707Z-rag-rebuild-v2-200k/review/adjudication_manifest.json).
- [Manifesto de publicação](../../experiment/platform/data/object_store/.rca_knowledge/campaign-20261103T234452388707Z-rag-rebuild-v2-200k/review/opensearch_publication.json).
- Artefatos originais dos runs e da campanha `controlled-20261103T234452388707Z`, para proveniência, horários e validação da intervenção, nunca como evidência operacional fornecida ao agente cego.

O inventário local desta etapa contém **32 pares `split=train`**, dos quais 25 com efeito validado e sete sem efeito validado:

| Falha rotulada | Casos |
|---|---:|
| container-kill | 6 |
| network-latency | 6 |
| cpu-hog | 7 |
| memory-hog | 6 |
| http-status-code | 7 |

Serviços-alvo permitidos: `carts`, `catalogue`, `front-end`, `orders`, `payment`, `shipping`, `user`.

A campanha de origem foi planejada com 35 células train-only, mas a publicação registra `campaign_collection_complete=false`. Portanto, o escopo inicial é a coorte selada de 32 pares, não 35 guias presumidos. O preflight deve reconciliar plano, ledger e manifestos, listar as células faltantes/excluídas e fixar os `source_run_id`. Divergência bloqueia a execução até resolução explícita; não expandir a coorte automaticamente caso a campanha tenha avançado.

Não utilizar runs ou evidências do teste `exploratory-rag-20261101T214710257439Z` para escrever, selecionar ou corrigir B. A comparação anexa é motivação do ajuste, não dado de treinamento.

## 3. Problemas do processo atual a corrigir

1. Os diagnósticos B anteriores podem divergir do rótulo, permanecer inconclusivos e ainda fornecer orientações genéricas. Nos seis `container-kill`, nenhum B classificou esse tipo; todos têm `restart_analysis` vazio.
2. A política anterior preserva previsões históricas e impõe `pending/inconclusive`; ela não é o contrato de um corpus de investigações bem-sucedidas.
3. [compact_exploratory_history](../../experiment/platform/backend/rca_knowledge.py) exclui a conclusão e limita a projeção a três passos, com cortes por caracteres. Passos estruturados são convertidos em texto por `str`, perdendo o contrato de investigação.
4. [opensearch_experiments](../../experiment/eval/memory_hog/opensearch_experiments.py) aceita o contrato exploratório e proíbe diagnóstico/rótulos na orientação. Alterar apenas o JSON B não atualiza adequadamente o conteúdo entregue ao modelo.
5. [retroactive_b](../../experiment/eval/memory_hog/retroactive_b.py) já possui supervisão e passos estruturados, mas exige plano imutável de 70 células/35–35 e bloqueia bases congeladas. Não executar essa CLI diretamente na coorte train-only nem remover suas proteções para contornar o problema.
6. A base declara `model=qwen-14b`; isso não comprova geração com Qwen 3.5. Também não basta trocar esse campo ou o nome do modelo em um artefato antigo.

## 4. Separação entre supervisão e evidência

Manter três domínios explícitos:

- **A:** sintomas e fatos operacionais iniciais, sem rótulo, alvo de injeção, nome de célula ou diagnóstico supervisionado. Preservar A e sua representação de recuperação como controle desta reconstrução.
- **B supervisionado:** o autor Qwen 3.5 recebe o resultado-alvo privado para saber qual hipótese precisa verificar. Deve provar a ligação operacional, e não escrever uma narrativa que apenas reproduz o rótulo.
- **Validação cega:** outra chamada Qwen 3.5 recebe A, contexto público da aplicação e acesso às consultas históricas; não recebe o rótulo, B completo, nomes reveladores ou artefatos do injetor.

O resultado-alvo privado e o diagnóstico sustentado são campos distintos. Coincidência com o rótulo é necessária para publicar um exemplo bem-sucedido, mas não suficiente. Workflows Litmus, manifestos de caos e gates comprovam a intervenção para auditoria experimental; não substituem o caminho operacional que outro agente precisa seguir.

## 5. Execução proposta

### Etapa 1 — Preflight e protocolo versionado

- Fixar coorte, hashes de entrada, janelas UTC, namespace, serviço/container e vocabulário de falhas por run.
- Registrar versão do código, prompt, schema, consultas, política de elegibilidade e limites de execução antes de gerar B.
- Verificar a identidade real do servidor Qwen 3.5: modelo servido, arquivo/pesos e hash quando acessíveis, configuração de inferência e modelo reportado nas respostas. O alias `qwen-14b` não basta; identidade não verificável bloqueia a geração.
- Registrar limites de contexto, saída, chamadas, tempo e retries por estágio; contabilizar todas as tentativas. Proibir fallback de modelo e alterações silenciosas de limites.
- Descobrir ferramentas/datasources históricos disponíveis e conferir que seus seletores apontam para histórico, não estado atual. Não restaurar infraestrutura, port-forwards ou workloads automaticamente.

### Etapa 2 — Nova coleta histórica somente leitura

Para todos os 32 runs, consultar via MCP o Prometheus histórico, Loki histórico e Tempo histórico nas janelas originais baseline/fault/post. Evidência antiga ajuda a localizar consultas, mas não será rebatizada como nova coleta.

1. Começar com métricas de todos os serviços-alvo e mapa de comunicação para localizar sintomas sem receber o alvo privado.
2. Seguir os candidatos e as arestas efetivamente observadas; consultar logs/traces dos serviços relevantes e os checks específicos da seção 6.
3. Persistir consulta, argumentos, datasource, timestamps de execução e da observação, resposta sanitizada, unidades, identidade, cobertura, limites e IDs estáveis de evidência.
4. Consultas extras motivadas pelo alvo privado devem ser identificadas como coleta supervisionada. Não podem aparecer como descoberta autônoma no replay cego.
5. Usar spans arquivados originais, se disponíveis, somente com manifesto/hash, contagens e cobertura verificados. Identificar essa origem e não contar a cópia e o Tempo como fontes independentes.
6. Registrar `ok`, `partial`, `no_data`, erro, truncamento e indisponibilidade de ferramenta separadamente. Erros de backend bloqueiam a decisão daquele caso até retry limitado ou encerramento explícito; não são evidência negativa.

Não deslocar arbitrariamente `fault` para capturar um evento conveniente. Contexto anterior/posterior pode ser consultado em janela suplementar identificada, sem mudar os limites experimentais. Registrar scrape interval, lookback, intervalo provável de eventos e incerteza; uma janela de poucos segundos pode não capturar uma reinicialização.

### Etapa 3 — Produção supervisionada e revisão de B

Usar Qwen 3.5 para construir passos estruturados vinculados às respostas reais. O diagnóstico antigo pode ser auditado, mas não deve ser a resposta inicial a completar ou copiar.

Cada passo deve conter:

- `step_id`, hipótese e justificativa da próxima consulta;
- serviço/aresta exatos, família, fase, ferramenta e parâmetros reproduzíveis;
- resultado observado com valor/unidade/tempo ou trecho de log/span;
- IDs de evidência e referência ao resultado persistido;
- interpretação, hipótese fortalecida/enfraquecida e próximo passo.

Separar `executed` de `recommended`: uma verificação futura não pode fechar a cadeia causal. Comparar pelo menos duas hipóteses plausíveis dentro dos serviços-alvo; dependências auxiliares só entram como contexto, não como causas finais ou serviços afetados rotulados. Não adicionar componentes sem pertinência demonstrada.

Revisar B em outra chamada Qwen 3.5, com contexto limpo, checando fatos e contradições contra as respostas completas. Isso é revisão por outra chamada do mesmo modelo, não revisão humana ou independência estatística. Validadores determinísticos continuam obrigatórios.

### Etapa 4 — Replay cego de suficiência e utilidade

Executar dois controles distintos:

1. **Suficiência dos dados:** Qwen 3.5 parte de A e consulta a telemetria sem ver B/rótulos. Registrar serviço e tipo antes de compará-los ao alvo privado.
2. **Utilidade do guia:** fornecer A e a versão procedural de B, ocultando conclusão histórica, alvo privado, consultas já preenchidas com o alvo correto sem justificativa e nomes reveladores. O agente deve selecionar candidatos a partir dos sintomas, executar as verificações e concluir usando as respostas.

Essa segunda execução deve testar o caminho ensinável, não a capacidade de copiar a última frase de B. Consultas guiadas podem usar o serviço candidato inferido no passo anterior; o host não deve substituir esse candidato pelo rótulo conhecido.

Replay cego divergente bloqueia a publicação como sucesso e gera revisão do caso. Replay correto não aprova sozinho: continuam necessários suporte causal, checks do tipo e revisão factual. Todas as tentativas permanecem no relatório; não repetir até acertar e reportar apenas a tentativa favorável.

## 6. Verificações obrigatórias por falha

| Tipo | Verificação e discriminação exigidas |
|---|---|
| `container-kill` | Comparar contador bruto/delta de restarts por pod/container/UID, readiness e disponibilidade em baseline/fault/post; procurar término, timestamp, reason/exit code, logs de shutdown/startup e eventos históricos quando existentes. Investigar o que ocorreu antes: OOM, probe, crash, rollout ou dependência. Restart sozinho não prova kill; ausência de logs de término não prova que ele não ocorreu. Não exigir OOM como condição para kill. |
| `network-latency` | Comparar a mesma aresta chamador→destino e operação, client/server latency e tempos de spans antes/durante/depois. Confrontar processamento local, CPU, memória e espera downstream. Latência agregada de um serviço não distingue netem de processamento; drops zero não refutam latência injetada. |
| `cpu-hog` | Medir CPU do container-alvo em cores e, quando disponível, em proporção à quota; informar baseline, fault, duração, throttling e carga. Validar anormalidade relativa e impacto com logs/traces. Não atribuir ao primeiro chamador lento nem usar um limiar universal arbitrário. |
| `memory-hog` | Medir evolução de memória/working set do container-alvo, baseline, fault, limite quando existente e recuperação. Confrontar CPU/carga e logs/traces; OOM/GC só quando observados. Crescimento anormal pode ocorrer sem OOM/restart e não prova leak específico. |
| `http-status-code` | Comparar volume, taxa e distribuição dos códigos por serviço, rota e fronteira cliente/servidor. Correlacionar logs, spans e flags do proxy para distinguir abort do mesh, erro local, propagação e resposta comercial esperada. 406 isolado ou `is_error=true` não demonstram o código injetado. |

Métrica inexistente exige tentativa registrada e limitação explícita, não valor inventado. Outra evidência discriminante pode suprir a lacuna; sem ela, o caso não se torna bem-sucedido. Não contar duas consultas Prometheus ou métricas derivadas de spans como duas famílias independentes.

## 7. Contrato e gates do novo B

Criar um schema versionado para **guia supervisionado validado**, sem reaproveitar os estados exploratórios antigos por falsificação. Campos mínimos:

- proveniência do run, versão do modelo/prompt/schema e hashes;
- resultado-alvo privado e referência à sua origem experimental;
- sintomas iniciais e hipóteses com serviços autorizados;
- sequência de investigação executada, resultados e discriminação;
- análise de lifecycle/restart quando observado, mesmo se o tipo rotulado for outro;
- conclusão histórica sustentada: serviço, tipo, evidências e relação causa → sintoma → impacto;
- limitações, checks pendentes e estado de qualidade separado da validade da intervenção.

Estados propostos: `validated_success`, `insufficient_evidence`, `label_evidence_conflict`, `collection_error`, `validation_error`. Definir esses estados no novo contrato; não inseri-los nos leitores v2 sem suporte explícito.

Publicar `validated_success` somente se:

1. O run pertence à coorte train fixada e seu efeito experimental está validado.
2. Serviço/tipo finais correspondem exatamente ao alvo privado e ao vocabulário permitido.
3. Cada alegação factual e cada passo executado aponta para evidência existente, verificável, da entidade e fase corretas.
4. Há evidência discriminante da falha e corroboração em pelo menos duas famílias entre métricas/logs/traces, vinculadas à mesma ocorrência.
5. A investigação explica a propagação sem transformar sintomas downstream em causa; alternativas e contradições materiais foram resolvidas ou limitadas explicitamente.
6. O motivo de restart relevante não continua desconhecido e checks apenas recomendados não são necessários para sustentar a conclusão publicada.
7. Os controles cegos e a revisão factual passam.
8. A projeção recuperada preserva os passos/evidências necessários e a conclusão validada.

Os sete efeitos não validados continuam no inventário e recebem reconstrução/auditoria, mas não entram no corpus principal sem uma revisão experimental separada e autorizada. Este trabalho não muda seus gates.

## 8. Novo conteúdo recuperado pela RAG

Construir novo corpus/índice, preservando integralmente v1/v2. A permanece como chave de busca: manter representação, embedding `hashing-v1`/256 e ranking/top-k como controles iniciais. Não indexar a conclusão de B para inflar artificialmente o matching.

Alterar de forma versionada o payload retornado:

- incluir B validado com passos estruturados, resultados históricos, IDs resolvíveis, limitações e conclusão histórica;
- identificar explicitamente esse conteúdo como exemplo de treino de outro incidente, nunca evidência nem verdade do incidente atual;
- manter rótulos experimentais privados, detalhes do injetor e referências reveladoras fora da projeção;
- permitir a conclusão histórica sustentada no novo contrato. Isso é uma mudança intencional em relação à proibição exploratória de `diagnosis`, não vazamento do rótulo do run consultado;
- preservar a exclusão do próprio run e de derivados/retries da mesma ocorrência, com linhagem explícita;
- aplicar elegibilidade de guia e efeito nos caminhos lexical, vetorial, híbrido e leitura direta;
- substituir cortes cegos em três passos por projeção que preserve a cadeia completa necessária. Se exceder o orçamento, usar carregamento explícito do guia completo por ID ou excluir o documento dessa resposta com motivo; nunca entregar um “sucesso” sem o discriminador;
- namespaces de evidência devem distinguir incidente atual e exemplos históricos; não validar IDs históricos como suporte atual.

Ajustar consumidores da plataforma, CLI, replay/eval e visualização de conhecimento para o novo schema e sua seleção explícita. A tela deve separar alvo privado de conclusão sustentada e mostrar consultas/resultados da investigação. Não criar um novo índice e declarar atualização concluída sem verificar o payload realmente enviado ao modelo.

## 9. Implementação, testes e documentação

Reutilizar os mecanismos existentes de cliente Qwen, consultas históricas, armazenamento/hash e passos estruturados. Criar uma entrada versionada para reconstrução a partir da coorte congelada; não duplicar o cliente MCP nem relaxar as restrições da CLI retroativa legada.

Superfícies principais:

- [retroactive_b.py](../../experiment/eval/memory_hog/retroactive_b.py): reaproveitar conceitos de supervisão e verificações, preservando o protocolo antigo.
- [rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py): schema, elegibilidade, freeze/load, projeção e contexto fornecido ao modelo.
- [opensearch_experiments.py](../../experiment/eval/memory_hog/opensearch_experiments.py): mapping/metadados novos, indexação, filtros e validação do payload.
- [frozen_rca_replay.py](../../experiment/eval/memory_hog/frozen_rca_replay.py): controles cegos, seleção de corpus e rastreio da orientação consumida.
- [README do fluxo](../../experiment/eval/memory_hog/README.md) e [protocolo de conhecimento](../../experiment/eval/memory_hog/CAMPAIGN_KNOWLEDGE.md): regras, execução, limitações e rollback.

Testes direcionados: estender [test_retroactive_b.py](../../experiment/eval/memory_hog/tests/test_retroactive_b.py), [test_rca_knowledge.py](../../experiment/platform/tests/test_rca_knowledge.py), [test_opensearch_experiments.py](../../experiment/eval/memory_hog/tests/test_opensearch_experiments.py), [test_frozen_rca_replay.py](../../experiment/eval/memory_hog/tests/test_frozen_rca_replay.py) e [test_knowledge_browser_api.py](../../experiment/platform/tests/test_knowledge_browser_api.py), conforme as superfícies alteradas.

Cobertura obrigatória: modelo incorreto; rótulo usado como evidência; tipo errado; serviço auxiliar como causa; IDs inexistentes; métrica/unidade/valor divergentes; sinal apenas em baseline; `no_data` como refutação; restart sem motivo; código comercial confundido com falha; erro de backend; truncamento do passo decisivo; auto-recuperação; incompatibilidade de schema; alteração indevida de A/gates; mistura de treino/teste; leitores legados preservados.

Após testes unitários, realizar smoke real de consulta histórica e de OpenSearch. Conferir leitura do guia completo e o pedido efetivo ao Qwen 3.5, não apenas o documento armazenado. Ensaiar primeiro um `container-kill` e um caso de outra família, escolhidos antes dos resultados; depois processar todos os casos, sem cherry-picking.

## 10. Publicação, relatório e rollback

1. Preservar tentativas, respostas, revisões e resultados por run em artefatos versionados; retomar consultas com o mesmo run, sem nova injeção.
2. Congelar base candidata e manifesto com hashes, lista dos 32 casos e motivos de inclusão/exclusão. Reportar planejados, presentes, faltantes, coletados, erros, insuficientes e aprovados por tipo/serviço.
3. Criar índice físico separado e verificar correspondência entre pares, payloads, hashes, filtros, autoexclusão e contagens. Não prometer 32 guias elegíveis se a evidência não os sustentar.
4. Registrar consumidores/configurações anteriores. Não há pointer global no release v2: selecionar explicitamente corpus/hash/versão em cada consumidor e conferir o read-back.
5. Só ativar o candidato após os gates; falha mantém o serving anterior com erro explícito, sem trocar silenciosamente para outro modelo/base.
6. Rollback restaura a seleção registrada dos consumidores; preserva o candidato e todos os índices, sem apagar ou reescrever histórico.

Aceite técnico mede integridade e entrega de B validado. Replay sobre os próprios runs de treinamento mede suficiência/reprodutibilidade do guia, **não generalização**. Avaliação de ganho da RAG exige etapa posterior separada, com Qwen 3.5, coorte de teste e orçamento fixados, consultas MCP novas por condição e nenhuma reescrita de B em resposta aos erros do teste.

### Critério final de conclusão do trabalho

Todos os casos de treino foram processados ou têm bloqueio explícito; cada guia publicado termina com serviço/tipo corretos sustentados; os rejeitados permanecem auditáveis; o conteúdo completo está recuperável e efetivamente consumido pela RAG; regressões dos leitores legados e rollback foram testados. Uma base com todos os rótulos corretos, mas sem esse caminho verificável, não atende ao pedido.
