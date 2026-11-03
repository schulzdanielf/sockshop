# Avaliacao offline Skill, controle e RAG — replay v5

> **Registro historico, nao avaliacao vigente.** Este replay usou evidencias
> congeladas e nao consultou os datasources via MCP durante a inferencia. Pela
> politica de 03/11/2026, seus scores nao representam uma avaliacao online
> valida e nao devem ser usados como resultado atual. Toda nova avaliacao deve
> coletar novamente as janelas historicas via MCP.

## Resultado em uma frase

A nova RAG melhorou pouco frente ao controle: classificacao conjunta correta em
6/33 contra 4/33. Skill tambem marcou 6/33. Todos os 99 diagnosticos permaneceram
inconclusivos; portanto, nenhum resultado representa causa confirmada.

## Execucao

- 33 casos historicos congelados; 99 respostas (Skill, controle, RAG).
- Modelo local `qwen3.5-9b-grafana`, Q4_K_M; entrada maxima 100.000 caracteres,
  saida maxima 8.192 tokens; defaults de sampling, sem seed ou temperatura fixos.
- Replay `frozen-rca-replay-v5`, A estruturado v2, `structured-evidence-v2`.
- Sem chamadas de telemetria, acesso ao cluster ou injecao de falhas.
- 99/99 artefatos individuais e 33 casos no manifesto; zero erros de inferencia.
- Os 99 prompts previamente medidos ficaram entre 68.165 e 98.726 caracteres;
  os 99 pares historicos selecionados foram fornecidos.
- SHA-256 do relatorio: `e0f032107c911e12fabfc9ec1ec86c6d8e7927f6335d477992b2f6d3b42e62fe`.
- SHA-256 do manifesto: `00b52689dac6e6e61c5bb665bda91a01e7a71dd90b5504d4cc4c481c8d7c602e`.

Artefatos completos e tentativas individuais estao em
[`offline_replays/exploratory-rag-20261028T191703038725Z-hybrid-rrf-v5-qwen35-9b-a-v2-structured-evidence-20261031-run1`](../experiment/eval/memory_hog/out/offline_replays/exploratory-rag-20261028T191703038725Z-hybrid-rrf-v5-qwen35-9b-a-v2-structured-evidence-20261031-run1).

## Comparacao com a v4

Acerto conjunto e a coincidencia simultanea de servico e tipo de falha com o
rotulo experimental. Isso e uma metrica de classificacao, nao de causalidade.

| Condicao | v4 servico | v4 falha | v4 conjunto | v5 servico | v5 falha | v5 conjunto |
|---|---:|---:|---:|---:|---:|---:|
| Skill | 14/33 (42%) | 8/33 (24%) | 3/33 (9%) | 14/33 (42%) | 13/33 (39%) | 6/33 (18%) |
| Controle | 14/33 (42%) | 15/33 (45%) | 10/33 (30%) | 13/33 (39%) | 12/33 (36%) | 4/33 (12%) |
| RAG | 11/33 (33%) | 11/33 (33%) | 5/33 (15%) | 11/33 (33%) | 14/33 (42%) | 6/33 (18%) |

Na comparacao pareada v5, RAG vence controle em 3 casos, perde em 1 e empata em
29. Em v4, era 1 vitoria RAG, 6 vitorias controle e 26 empates. A mudanca de
distribuicao indica variacao relevante entre rodadas.

Comparada com v4, a v5 teve:

- Skill: tres ganhos, nenhuma perda no acerto conjunto.
- Controle: dois ganhos, oito perdas.
- RAG: tres ganhos, duas perdas.

Isto **nao isola o efeito da compactacao**: o replay alterou simultaneamente a
representacao do historico, a diversidade dos fatos fornecidos e a compactacao
dos prompts. Alem disso, o servidor usou sampling padrao sem seed. A ordem das
respostas e o comportamento estocastico podem explicar parte da variacao. Nao
concluir que a nova compactacao causou a queda do controle ou todo ganho da RAG.

## Onde a RAG acertou

| Tipo de falha | Casos | v4 RAG | v5 RAG |
|---|---:|---:|---:|
| CPU | 7 | 1 | 4 |
| Memoria | 7 | 3 | 1 |
| Rede | 6 | 0 | 0 |
| Container kill | 6 | 0 | 0 |
| HTTP | 7 | 1 | 1 |
| **Total** | **33** | **5** | **6** |

O ganho esta concentrado em CPU. Rede e container kill continuam sem classificacao
conjunta correta em qualquer condicao. Para memoria, o controle caiu de 5/7 para
1/7 e RAG de 3/7 para 1/7; isso pede repeticao controlada antes de escolher uma
explicacao.

## Recuperacao e vieses restantes

- Historico exato no top-3: 10/33, igual a v4; A v2 e a base selecionada nao
  mudaram nesta execucao.
- RAG v5 com historico exato: 2/10 acertos conjuntos; sem historico exato:
  4/23. Historico exato nao garante transferibilidade.
- `orders` aparece como servico principal em 21/33 respostas RAG (23/33 em v4),
  20/33 no controle e 15/33 na Skill. O viés diminuiu na RAG, mas permanece.
- RAG acerta somente 2 dos 10 casos de historico exato; logo o problema nao e
  apenas encontrar um precedente. O modelo ainda precisa distinguir origem,
  sintoma downstream e impacto.

## Leitura e proximo passo

1. A compactacao esta validada como transporte de evidencias, mas melhoria
   de classificacao e ainda incerta.
2. Executar pelo menos tres repeticoes com seed/sampling fixos por condicao, ou
   declarar explicitamente que o backend nao permite seed e agregar intervalos
   por bootstrap pareado. Manter a mesma ordem ou randomiza-la de forma registrada.
3. Para separar causas, comparar uma mudanca por vez: (a) formato de evidencia,
   (b) selecao diversa de metricas no historico, (c) retrieval/eligibilidade.
4. Reportar acerto por tipo de falha e por disponibilidade de historico, nao so
   a media global. Auditar especialmente as contradicoes numericas e a atribuicao
   de `orders`.
5. Avaliar confirmacao causal e fidelidade de suporte/contradicao separadamente
   da acuracia de rótulo. Neste replay, confirmacoes = 0/99.

A limitacao principal permanece: sao os mesmos 33 casos previamente inspecionados.
O replay e exploratorio, pareado e retrospectivo; nao mede qualidade de coleta,
numero de consultas operacionais nem generalizacao para incidentes novos.
