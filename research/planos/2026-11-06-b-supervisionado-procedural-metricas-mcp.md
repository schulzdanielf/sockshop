# Plano de ajuste de B: supervisao, caminho procedural e metricas MCP

**Data:** 06/11/2026. **Estado:** plano para implementacao posterior.

Este documento consolida o ajuste aprovado em conversa e acrescenta a confirmacao por metricas conhecidas da aplicacao e disponiveis no MCP. Nao executa reconstrucao, altera codigo ou publica uma nova RAG.

Substitui, para esta reconstrucao, as exigencias de descoberta cega e elegibilidade descritas no [plano anterior](./2026-11-05-reconstrucao-b-supervisionada-qwen35.md). Preserva coorte train-only, proveniencia, acesso historico somente leitura e proibicao de inventar observacoes.

## 1. Objetivo

Produzir B como um guia supervisionado que ensine:

**Sintomas de A -> hipoteses -> consultas -> resultados e interpretacao -> confirmacao por metricas no servico adequado -> servico e falha do experimento.**

O autor conhece o rotulo desde a primeira coleta orientada. O agente que investiga um incidente novo recebe ate tres guias historicos como opcoes, nao como gabaritos do incidente atual.

"Treinamento" significa reconstrucao do corpus A/B, nao ajuste dos pesos do modelo. Manter Qwen 3.5 na geracao e revisao dos Bs. A eventual troca do modelo assistente para implementar este plano nao altera automaticamente o modelo utilizado para produzir o corpus.

## 2. Escopo e controles

- Reutilizar os 32 pares da coorte congelada de treinamento e seus runs; reconciliar IDs e hashes antes da execucao.
- Nao adicionar casos de teste, reinjetar falhas, selecionar outro run silenciosamente ou alterar A para facilitar a recuperacao.
- Preservar A, embeddings, ranking e top-k de tres como controles iniciais. Nao usar a conclusao de B como chave de busca.
- Servicos-alvo: `carts`, `catalogue`, `front-end`, `orders`, `payment`, `shipping`, `user`.
- Dependencias auxiliares podem explicar propagacao, mas nao substituir o alvo por componentes fora desse conjunto.
- Usar datasources historicos e janelas do proprio run, nao estado atual do cluster.
- Criar corpus e contrato de recuperacao novos; preservar bases e consumidores anteriores ate a verificacao da nova entrega.

## 3. Mudanca do fluxo de producao

### 3.1 Entrada supervisionada desde o inicio

Fornecer ao Qwen 3.5:

- A com sintomas iniciais.
- Servico-alvo e falha conhecidos do experimento.
- Arquitetura e nomes reais dos servicos.
- Janelas baseline/fault/post.
- Inventario das ferramentas MCP e consultas conhecidas da aplicacao.
- Evidencias historicas ja consultadas, com resultados e limitacoes.

Instrucao central:

> O resultado conhecido deste experimento e servico X e falha Y. Partindo dos sintomas de A, construa um caminho investigativo ate esse resultado. Consulte as metricas da aplicacao disponiveis no MCP para confirmar a hipotese no servico adequado. Explique por que sintomas em outro servico ou uma causa semelhante nao identificam corretamente a origem. Separe resultados observados de verificacoes condicionais; nao use o rotulo como se fosse uma observacao de telemetria.

Remover a investigacao cega inicial como requisito de producao. A triagem pode abranger varios servicos, mas seu objetivo sera localizar sintomas e explicar o caminho ate o alvo conhecido, nao descobrir novamente o gabarito.

### 3.2 Coleta progressiva orientada

1. Identificar os sintomas relevantes de A e os primeiros candidatos.
2. Consultar a metrica discriminante da falha no alvo conhecido.
3. Comparar baseline/fault e post quando disponivel; preservar valor, unidade, entidade, cobertura e resolucao temporal.
4. Investigar dependencias e alternativas quando necessario para explicar servico ou mecanismo inicialmente plausiveis.
5. Usar logs, traces e lifecycle para esclarecer propagacao e ambiguidades, sem exigir todas as fontes em todos os casos.
6. Elaborar o caminho a partir das consultas e resultados, mantendo lacunas explicitas.

Reutilizar os adaptadores existentes; nao duplicar cliente MCP ou criar consultas especulativas para substituir mecanismos conhecidos.

## 4. Confirmacao obrigatoria por metricas MCP

Todo B deve incluir uma etapa explicita de confirmacao por metricas adequada ao seu servico e tipo de falha. Apenas mencionar "verificar CPU" ou citar indisponibilidade generica nao atende ao requisito.

### 4.1 Descoberta e disponibilidade

- Reutilizar `prepare_check`, `check_inventory` e as ferramentas de saturacao, sinais e lifecycle existentes.
- Inventario de ferramenta nao prova existencia de serie: conferir a resposta historica para o servico, labels e janela consultados.
- Registrar ferramenta, argumentos exatos, expressao quando aplicavel, janela, seletores, unidade e resultado.
- Nao inventar nomes de metricas ou assumir disponibilidade porque a metrica existe em outro ambiente.
- Separar ferramenta ausente, serie sem dados, resposta parcial, valor normal e erro de transporte.

### 4.2 Verificacoes por tipo

| Falha | Confirmacao por metricas | Explicacao discriminante |
|---|---|---|
| container-kill | Restarts do container da aplicacao, e readiness/disponibilidade ou terminacao quando disponiveis; comparar antes/durante/depois. | Mostrar que o evento pertence ao alvo, nao ao sidecar ou apenas a um upstream. Explicitar se a precisao de scrape permite apenas um intervalo de ocorrencia. Investigar alternativas quando houver indicios, sem exigir sempre exit code ou causa exata de encerramento para aprovar o procedimento. |
| network-latency | Latencia da comunicacao entre servicos e tempos de resposta, usando as series HTTP/Istio e arestas realmente disponiveis. | Localizar origem/destino do atraso e diferenciar tempo em dependencia de saturacao local. CPU/memoria normais, isoladamente, nao comprovam rede. |
| cpu-hog | Saturacao/utilizacao de CPU do servico-alvo, comparada ao baseline; throttling se relevante e disponivel. | Mostrar que a alteracao de CPU esta no alvo e explica os sintomas; nao exigir percentual universal ou confundir porcentagem de saturacao com cores. |
| memory-hog | Saturacao/utilizacao de memoria do alvo e sua evolucao temporal, comparadas ao baseline. | Diferenciar crescimento anormal de ocupacao estavel; nao exigir OOM ou restart e nao confundir saturacao com bytes. |
| http-status-code | Taxa/distribuicao de respostas HTTP por codigo e workload, comparadas ao baseline. | Distinguir resposta no servico-alvo de erro propagado em outro servico; status de erro sozinho nao identifica o mecanismo injetado. |

As consultas atuais de `prepare_check` incluem saturacao de CPU/memoria, lifecycle, `istio_requests_total`, duracao Istio e throttling CFS. Sao pontos de partida, nao garantia de series presentes em todo run. Verificar labels, reporter e unidades antes de interpretar origem ou destino.

### 4.3 Quando faltarem dados

A tentativa de confirmacao metrica e obrigatoria; seu sucesso nao sera fabricado.

- Procurar outra metrica conhecida e disponivel que responda a mesma pergunta.
- Se a confirmacao continuar indisponivel, descrever consulta, resultado esperado condicional e limitacao.
- Nao chamar esse passo de confirmacao observada.
- Se a lacuna impedir ensinar como distinguir alvo/falha das alternativas, marcar o guia como pendente, com o passo faltante. Nao converter todos os casos em sucesso por imposicao do rotulo.
- Separar a conclusao conhecida do experimento daquilo que a telemetria efetivamente confirmou.

## 5. Justificar servico e causa corretos

Quando o sintoma inicial apontar a outro servico ou mecanismo semelhante, B deve responder explicitamente:

1. Por que esse candidato inicial parecia plausivel?
2. Qual dependencia ou relacao operacional motivou seguir a investigacao?
3. Qual consulta e resultado mudaram a direcao?
4. Qual metrica no servico correto confirmou ou discriminou a hipotese?
5. Por que o outro servico representa sintoma/propagacao, e nao a origem deste caso?
6. Por que a falha correta explica melhor o caso do que a alternativa semelhante?

Nao inventar arestas da arquitetura nem exigir refutacao de todas as falhas possiveis. Cobrir as alternativas materialmente relevantes para A e para os resultados observados.

## 6. Contrato de B

Cada guia deve conter:

- **Ponto de partida:** sintomas de A e candidatos iniciais.
- **Hipotese historica:** servico e falha do proprio experimento.
- **Passos ordenados:** motivo da consulta, ferramenta/argumentos, servico, fase, resultado observado ou resultado esperado condicional, interpretacao e proximo passo.
- **Confirmacao metrica:** consulta relevante no servico adequado e sua relacao com a conclusao.
- **Discriminacao:** justificativa da escolha do servico e mecanismo frente as alternativas relevantes.
- **Conclusao historica:** servico e falha corretos; cobertura da telemetria descrita sem exagero.
- **Reutilizacao:** quando aplicar o guia, como adaptar janelas/seletores e quais resultados indicam abandonar esse caminho.
- **Limitacoes:** lacunas especificas e seu efeito sobre o procedimento.

Manter consultas historicas exatas para auditoria e orientacao reutilizavel separada. Nao reutilizar timestamps, pods ou trace IDs historicos como argumentos do incidente novo. Resultados esperados devem ser condicionais, nao valores historicos transformados em limiares universais.

## 7. Validacao procedural supervisionada

A aprovacao pergunta: **B conduz de A ao rotulo correto com consultas e explicacoes utilizaveis?**

Usar checagens de contrato e uma revisao Qwen 3.5 com A, rotulo, guia e respostas citadas. Corrigir problemas especificos em tentativas limitadas e preservar os originais.

Checklist de aceite:

- Comeca pelos sintomas de A e termina no servico/falha do experimento.
- Apresenta passos ligados entre si, nao uma lista generica de ferramentas.
- Inclui confirmacao metrica no alvo ou trata honestamente sua indisponibilidade.
- Explica a troca de candidato ou mecanismo quando pertinente.
- Consultas usam ferramentas/metricas conhecidas, seletores e janelas coerentes.
- Resultados declarados correspondem aos registros citados; recomendacoes nao se apresentam como execucoes.
- Permite aplicar e rejeitar a hipotese em outro incidente.

Nao exigir para aprovar B:

- Acerto de uma investigacao cega ou replay reduzido.
- Duas familias independentes de evidencia em todos os casos.
- Uma prova forense universal da causa de restart.
- Redescoberta de toda a causa sem conhecer o rotulo.
- Novas execucoes de consultas identicas apenas para satisfazer um ritual de revisao.

Manter verificacoes simples de consistencia e ausencia de fatos inventados. Nao substituir esses requisitos por gates causais mais amplos que o objetivo procedural.

## 8. Recuperacao de tres guias e elegibilidade

Cada B deve estar correto para seu proprio caso e ser apresentado como hipotese historica para o incidente atual.

- Entregar ate tres guias, sem forcar diversidade artificial ou presumir que um deles esta correto.
- Instruir o agente a comparar sintomas, priorizar consultas discriminantes e confirmar por metricas do incidente atual.
- Nao transferir servico, causa, resultados ou IDs historicos automaticamente para o incidente atual.
- Se nenhum guia se ajustar, continuar a investigacao fora dessas opcoes.
- Preservar a cadeia e o discriminador de cada guia no contexto efetivamente enviado, sem truncamento destrutivo.

A RAG v3 atual filtra `guide_status=validated_success`, `causal_status=supported` e `effect_validated=true`. Criar contrato versionado para aprovar e recuperar guias por qualidade procedural, sem falsificar esses campos legados.

Separar qualidade procedural, cobertura/confirmacao metrica historica e validacao do efeito experimental. Nesta nova versao, efeito nao validado nao reprova automaticamente o procedimento, mas deve permanecer explicito no guia recuperado e no relatorio. Um guia que nao consegue ensinar a discriminacao necessaria permanece pendente.

Aplicar a mesma elegibilidade procedural em busca lexical/vetorial/hibrida, leitura direta e consumidores. Preservar exclusao do proprio run e da mesma ocorrencia. Manter rotulo do incidente de teste fora do contexto; a conclusao historica dos exemplos e permitida.

## 9. Implementacao prevista

1. [training_b_rebuild.py](../../experiment/eval/memory_hog/training_b_rebuild.py): supervisao desde a coleta inicial, contrato de confirmacao metrica/discriminacao, revisao procedural e remocao dos bloqueios cegos.
2. [training_b_queries.py](../../experiment/eval/memory_hog/training_b_queries.py): reaproveitar consultas, disponibilidade, precisao e persistencia; adaptar apenas o necessario para os novos passos.
3. [rca_knowledge.py](../../experiment/platform/backend/rca_knowledge.py): schema versionado, elegibilidade e projecao sem confundir aprovacao procedural com prova causal.
4. [opensearch_experiments.py](../../experiment/eval/memory_hog/opensearch_experiments.py): mapping, filtros e leitura do contrato novo, preservando leitores anteriores.
5. Consumidores reais da API, campanhas e prompts: localizar o caminho ativo e garantir a entrega de tres guias como alternativas verificaveis; nao presumir que um unico prompt atende todas as entradas.
6. Atualizar documentacao de execucao e testes diretamente relacionados.

Reutilizar codigo existente e evitar reescrever infraestrutura ou ampliar configuracoes sem necessidade.

## 10. Testes, amostra e reconstrucao

### Antes da reconstrucao integral

- Testes direcionados: supervisao desde a primeira coleta; metrica obrigatoria; labels/unidades corretos; ausencia de dados distinta de normalidade; sidecar distinto do alvo; resultado observado distinto de esperado.
- Casos procedurais: sintoma em dependencia; mecanismo semelhante; justificativa do alvo e falha; ausencia de justificativa identificada pela revisao.
- Confirmar que falha de replay cego nao bloqueia mais o produtor.
- Testar contrato novo de elegibilidade sem alterar ou falsificar estados legados.
- Testar recuperacao e projecao com tres guias, incluindo guias parcialmente inadequados e nenhum adequado, sem exigir acerto cego para aprovar o B individual.
- Gerar amostra real predefinida que cubra os cinco tipos, incluindo os casos problematicos ja conhecidos. Verificar caminhos e consultas historicas, nao apenas JSON valido.

### Depois da amostra

- Reconstruir todos os casos train da coorte fixada com o mesmo protocolo/modelo.
- Corrigir individualmente guias procedurais reprovados, sem escolher outro experimento.
- Congelar candidato e criar indice separado.
- Verificar leitura, autoexclusao e payload efetivamente entregue aos agentes com tres guias.
- Ativar explicitamente o novo corpus somente apos essa verificacao; preservar selecao anterior para rollback.

Nao ajustar Bs a partir de erros dos experimentos de teste. Medir generalizacao sera uma atividade posterior e separada.

## 11. Entrega e criterio de conclusao

Entregar corpus candidato, manifestos/consultas e relatorio por caso com:

- Servico e falha historicos.
- Caminho A -> consultas -> metrica no alvo -> conclusao.
- Justificativa de servico/mecanismo quando houve ambiguidade.
- Cobertura metrica e lacunas.
- Estado procedural e estado experimental separados.
- Resultado da verificacao do conteudo recuperado.

O trabalho estara concluido quando os casos tiverem sido processados ou tiverem pendencia explicita, e os guias publicados ensinarem caminhos corretos para seus respectivos experimentos, utilizaveis como opcoes de investigacao com confirmacao por metricas no incidente atual.

**Proxima acao:** implementar este plano em uma etapa posterior. Nao executar retreino apenas pela criacao deste documento.
