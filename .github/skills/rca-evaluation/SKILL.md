---
name: rca-evaluation
description: "Use para avaliar RCA consultando online, via MCP, os datasources historicos do run. Evidencia congelada de outra execucao nao pode pontuar uma avaliacao."
user-invocable: false
---

# Avaliacao de RCA com telemetria historica online

## Regra obrigatoria

Toda avaliacao de RCA deve consultar, via MCP, os datasources historicos
Prometheus, Loki e Tempo para as janelas baseline, fault e post do run avaliado.
Nao use pacotes de evidencia congelados, snapshots antigos, resultados de uma
avaliacao anterior ou fallback para telemetria armazenada como evidencia atual.

Uma coleta `live_shared` pode ser selada e reutilizada entre condicoes e
repeticoes somente dentro da mesma avaliacao, depois de consultar os datasources
historicos via MCP. Uma nova avaliacao exige nova coleta MCP. Se uma fonte ou
consulta falhar ou retornar `no_data`, registre explicitamente o estado e a
cobertura; nunca substitua a resposta por dados congelados. Nao pontue uma
avaliacao que nao tenha comprovante de consultas MCP para seus casos.

## Execucao e verificacao

- Use o fluxo de avaliacao que exige `live_shared` ou `live_per_condition`;
  `offline` e replay de pacotes congelados sao proibidos para pontuacao.
- Antes da inferencia, confirme que a coleta online terminou para todos os
  casos. Falhas de backend bloqueiam a avaliacao; `no_data`, truncamento,
  cobertura parcial e erros de busca permanecem limitacoes explicitas.
- Registre por caso as janelas consultadas, ferramenta MCP, fonte, status,
  contagens, cobertura e erros. Para traces arquivados, valide hash do bundle,
  paridade de contagens e cobertura temporal; o arquivo nao substitui a consulta
  online exigida.
- O modelo nao recebe ground truth, servico injetado ou tipo de falha como
  evidencia. Rotulos e categorias permitidas definem o contrato de saida, nao
  provam causa.

## Analise

- Analise baseline, fault e post respeitando janela, unidade, cobertura e
  identidade de servico/pod/container.
- Escreva campos narrativos em portugues; preserve chaves, IDs e opcoes.
- Separe mecanismo, sintoma e impacto. Um restart nao explica sua propria
  causa; ausencia de dados nao demonstra ausencia de falha.
- Formule pelo menos duas explicacoes alternativas e explicite contradicoes.
- Cite somente IDs de evidencias do incidente atual em campos de suporte e
  contradicao. Experiencias historicas podem orientar hipoteses, mas nao sao
  evidencias de que a mesma causa ocorreu neste incidente.
- A system card e contexto, nao evidencia de falha. Confronte-a com os dados.
- Quando `possible_fault_types` for fornecido, escolha exatamente uma opcao,
  mesmo em `inconclusive`. Isso e uma classificacao, nao confirmacao causal.
- Para confirmar, exija suporte em pelo menos duas familias independentes,
  cadeia causal e motivo sustentado para restart no componente de origem. Duas
  metricas Prometheus ou metricas derivadas dos mesmos spans nao sao fontes
  independentes.
- Se faltarem dados discriminativos, houver contradicoes ou nao for possivel
  explicar um restart, mantenha `inconclusive` e indique o proximo check.

## Interpretacao

A avaliacao mede o diagnostico sobre evidencias consultadas online nas janelas
historicas, nao sobre o estado atual do cluster. Conhecimento historico pode ser
usado como contexto quando previsto no protocolo, mas nao substitui a coleta
MCP do incidente avaliado e nunca deve ser contado como evidencia atual.
