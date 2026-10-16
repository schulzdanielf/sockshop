# Contrato de evidencias e saida

Cada consulta tem ID, nivel, tool, argumentos, fonte (`live` ou `snapshot`),
familia, estado, quantidade de observacoes e digest sanitizado.
IDs inexistentes, tools indisponiveis e respostas vazias nao sustentam causas.
Resultados parciais devem preservar limitações; o gate de corroboração usa
somente registros `ok` com `fault_observation_count` positivo. Observacoes
somente na baseline ou no post nao corroboram o mecanismo em fault.
`observation_count` registra todas as fases da consulta; as duas contagens
nao sao equivalentes.

No planejamento, responda JSON com:

- `candidates`: de um a tres servicos conhecidos;
- `hypotheses`: pelo menos duas explicacoes alternativas;
- `reasoning`: por que aprofundar esses candidatos.

Na conclusao, responda JSON com todos os campos:

- `status`: `confirmed` ou `inconclusive`. `confirmed` significa hipótese
  corroborada, nao prova experimental definitiva.
- `primary_service`: servico conhecido ou `null`;
- `category`: categoria do cartao ou `unknown`;
- `mechanism`: mecanismo descrito, nao nome da injeção presumida;
- `supporting_evidence`: IDs que sustentam o mecanismo;
- `contradicting_evidence`: IDs que contradizem a hipótese;
- `alternatives`: pelo menos duas hipoteses, cada uma com `hypothesis`,
  `supporting_evidence` e `contradicting_evidence`;
- `causal_chain`: sequencia temporal da causa para sintomas e impacto;
- `restart_analysis`: uma entrada por servico com restart, contendo `service`,
  `reason_status` (`supported` ou `unknown`), `reason`, `supporting_evidence`
  e `next_check`;
- `limitations`: cobertura, precisao, vieses e dados faltantes;
- `next_checks`: verificacoes seguintes, sem executar remediacao.

Nao use texto fora do JSON nem chamadas ficticias de tools. Se a corroboração
for insuficiente, registre `inconclusive`; preserve a hipotese e o proximo
check. O host pode rebaixar uma conclusao a inconclusiva.

## Fontes e precisao

Metricas e eventos sintetizados de kube-state-metrics pertencem a `metrics`;
nao conte OOM, readiness e restart como tres telemetrias. Logs e traces sao
familias diferentes, mas copias da mesma mensagem nao sao prova independente.
Cruze servico, pod/container, operacao e janela; indique quando nao conseguir.

Referencia pre-falha: ate 45 s. PromQL pode usar lookback de 1 min: a taxa
inclui observacoes anteriores ao primeiro timestamp consultado.
Ausencia de trace pode refletir filtros, sampling, ingestao ou retencao.
Ausencia de logs pode refletir identidade incorreta ou processo terminado.
