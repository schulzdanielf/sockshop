# Avaliacao de RCA com OpenSearch — fluxo suspenso

> **Nao executar para pontuar modelos.** Este roteiro utilizava evidencias
> congeladas e foi suspenso pela politica de avaliacao de 03/11/2026.

O antigo `frozen_rca_replay` foi desativado. Nenhuma pontuacao atual de RCA deve
ser produzida com os pacotes, bases ou relatorios congelados descritos aqui.
Os resultados ja existentes permanecem apenas como registros historicos.

Toda nova avaliacao deve consultar, via MCP, as janelas historicas do run nos
datasources Prometheus, Loki e Tempo. Use o fluxo
[`campaign_knowledge`](./CAMPAIGN_KNOWLEDGE.md) em `live_shared` para coletar
uma vez por caso e comparar condicoes com a mesma evidencia recém-consultada.
Uma nova avaliacao exige nova coleta; nao importe nem use evidencia de outra
execucao. Erros de consulta nao podem fazer fallback para snapshots.

Analises de OpenSearch so podem voltar a pontuar RCA quando forem integradas a
esse fluxo de coleta MCP e validadas com proveniencia por caso e janela.
