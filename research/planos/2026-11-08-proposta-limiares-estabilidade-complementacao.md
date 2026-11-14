# Proposta de limiares de estabilidade para a complementação

**Status:** pisos candidatos não aprovados nem implementados. Os controles com preparação alinhada foram executados e reavaliados retrospectivamente após a correção das consultas. A campanha continua bloqueada; não houve novas cargas ou injeções na reavaliação.

## Atualização: controles alinhados e correção da medição

Os dois controles da campanha `supplemental-20261108T224240870647Z`
usaram warmup de 180 s, janelas sucessivas de 60 s e baseline de 180 s.
A investigação identificou duas fontes de instabilidade artificial:

- `rate(...[30s])` perdia temporariamente a contribuição de um chamador quando
  havia menos de duas amostras na janela, distorcendo o total e o histograma.
- Saltos regressivos do relógio produziram durações negativas de leitura
  cgroup e picos artificiais de CPU no primeiro controle.

A política `controlled-measurement-v2` usa lookback de 60 s nas consultas e
relógio monotônico para novos deltas de CPU. Os limites numéricos permanecem
inalterados. A mudança de lookback altera a medição e não constitui uma
replicação idêntica do protocolo anterior.

A reavaliação consulta novamente o MCP histórico nas janelas originais,
preservando os reports brutos e os hashes. CPU antiga afetada por saltos de
relógio não pode ser reconstruída sem amostras monotônicas: é marcada inválida,
sem descartar pontos seletivamente para obter aprovação.

Resultado revisado:

| Controle | Preparação | Janela sem falha de 600 s | Decisão |
|---|---|---|---|
| 1 | Janelas 4 e 5 estáveis consecutivas (índices a partir de zero) | RED estável; CPU inválida nos três serviços | Bloqueado por telemetria inválida |
| 2 | Janelas 0, 3 e 5 estáveis, sem duas consecutivas | Todos os serviços passam | Bloqueado pela preparação |

No controle 2, persistem desvios de latência: `user.latency_ms` na janela 1,
32,79 / 29,59 ms; `orders.latency_ms` na janela 2, 57,00 / 54,49 ms;
`orders.target_edge_latency_ms` nas janelas 2 e 4, respectivamente
78,30 / 60,91 ms e 54,029 / 54,026 ms. Os pares são desvio / limite.
Os pisos candidatos abaixo continuam em espera; não foram usados na revisão.

O [estado atualizado da campanha](../../experiment/eval/memory_hog/out/supplemental_campaign/supplemental-20261108T224240870647Z/campaign_state.json)
aponta para os relatórios revisados em `verification_revalidation`.
O comando reproduzível está no [README do executor](../../experiment/eval/memory_hog/README.md#historical-no-fault-control-revalidation).

## Evidência anterior considerada para os candidatos

Os dois controles sem falha completos da campanha `supplemental-20261108T214200871008Z` usaram 30 usuários, spawn de 5/s e janela planejada de 600 s, sem injeção; tiveram 41 verificações de saúde, 649,993 s e 648,866 s observados e cleanup confirmado (`stopped=true`). Porém, foram executados com a preparação antiga: warmup de 60 s, verificações retrospectivas em janelas de 30 s e baseline de 90 s. O protocolo planejado é 180 s, duas janelas fixas consecutivas de 60 s e baseline de 180 s. Assim, os resultados são diagnósticos, mas não calibram diretamente o novo protocolo nem justificam aplicar os pisos candidatos abaixo.

Após esses controles, o executor foi ajustado para obedecer ao protocolo planejado.
Os novos controles e sua reavaliação estão descritos na atualização acima;
os valores abaixo permanecem apenas como registro da proposta anterior.

Os reports completos estão em:

- [controle 1](../../experiment/eval/memory_hog/out/supplemental_campaign/supplemental-20261108T214200871008Z/calibrations/no-fault-01/report.json)
- [controle 2](../../experiment/eval/memory_hog/out/supplemental_campaign/supplemental-20261108T214200871008Z/calibrations/no-fault-02/report.json)
- [estado da campanha](../../experiment/eval/memory_hog/out/supplemental_campaign/supplemental-20261108T214200871008Z/campaign_state.json)

A execução anterior, cuja verificação de baseline exigia indevidamente um perfil CPU de injeção durante um controle sem falha, não foi usada para escolher os valores abaixo. Essa chamada foi removida em código posterior; os dois reports acima foram produzidos depois da correção. A tentativa intermediária que encontrou um arquivo temporário remoto duplicado também não iniciou carga e não entra na calibração.

## Regra atual

[`stability_metrics`](../../experiment/platform/backend/controlled_experiment.py) usa `limite = max(0,20 × |média|, piso)`:

| Métrica | Piso atual |
|---|---:|
| `traffic` | 0,1 |
| `cpu_cores` | 0,02 cores |
| `memory_bytes` | 8 MiB |
| `latency_ms` | 20 ms |
| `target_edge_requests` | 0,1 requests/s |
| `target_edge_latency_ms` | 20 ms |

O fator relativo de 20% passou nos controles para `front-end` e para a maioria dos grupos dos outros serviços. Não há evidência para relaxá-lo globalmente.

## Falhas observadas

Valores são desvio-padrão observado versus limite calculado pela regra atual:

| Serviço | Métrica | Controle 1 | Controle 2 | Repetição |
|---|---|---:|---:|---|
| `orders` | `target_edge_latency_ms` | 82,756 / 60,295 ms | 68,293 / 55,588 ms | reprova ambos |
| `user` | `target_edge_requests` | 15,694 / 9,427 requests/s | 15,120 / 9,384 requests/s | reprova ambos |
| `user` | `target_edge_latency_ms` | 36,869 / 27,354 ms | 33,539 / 25,002 ms | reprova ambos |
| `orders` | `latency_ms` | 92,013 / 55,239 ms | 45,612 / 49,718 ms | reprova uma vez |
| `user` | `latency_ms` | 19,943 / 20 ms | 22,512 / 20 ms | reprova uma vez |

Os controles são apenas duas repetições sequenciais no mesmo ambiente; não estimam uma distribuição populacional nem demonstram que a causa da variabilidade seja o limiar, e não tráfego ou contenção do host.

## Candidato para uma política nova

**Identidade proposta:** `supplemental-stability-v2`  
**Estado:** em espera; não aprovar nem implementar com base nos controles antigos.

1. Preservar `max(0,20 × |média|, piso)` e todos os gates de cobertura, saúde, identidade do alvo, cleanup e segurança.
2. Preservar os pisos atuais para CPU, memória, tráfego e `latency_ms`; não alterar limiares de efeito da falha, recuperação ou aceitação das células.
3. Adicionar overrides de piso, limitados ao controle da mesma aresta/serviço, somente para os três pares que reprovaram em ambos os reports:

| Serviço | Métrica | Piso candidato v2 | Derivação |
|---|---|---:|---|
| `orders` | `target_edge_latency_ms` | 104 ms | teto inteiro de 1,25 × maior desvio observado (82,756 ms) |
| `user` | `target_edge_latency_ms` | 47 ms | teto inteiro de 1,25 × maior desvio observado (36,869 ms) |
| `user` | `target_edge_requests` | 20 requests/s | teto inteiro de 1,25 × maior desvio observado (15,694 requests/s) |

4. Manter os limites antigos para `orders.latency_ms` e `user.latency_ms`. Cada um reprovou somente uma vez; isso não demonstra uma falha repetível. Se qualquer um reprovar nos controles v2, manter bloqueado e investigar, sem ampliar o piso novamente.
5. Não alterar a política legada nem aplicar os overrides a outros serviços, campanhas, fases ou métricas. Persistir a versão, os overrides e a regra antiga/candidata em cada manifesto/report para comparação auditável.

O fator 1,25 é uma margem de engenharia preliminar sobre o maior desvio observado nos controles antigos, não um intervalo de confiança. Os pisos candidatos fariam os dois reports antigos usados na derivação passar nesses três pares; isso é esperado e **não** conta como validação. Não aplicar nem aprovar esses valores antes de repetir controles com o protocolo de preparação corrigido; os novos resultados podem justificar mantê-los, revisá-los ou descartá-los.

## Validação necessária antes de injetar

1. Repetir primeiro dois controles sem falha com a preparação corrigida e os limites atuais congelados. Não aplicar os pisos candidatos derivados dos reports antigos.
2. Se os controles aprovarem, manter os limites atuais; não há evidência para relaxá-los.
3. Se algum gate de estabilidade reprovar, preservar os reports e investigar cobertura, telemetria, tráfego ou contenção. Só então decidir se uma nova proposta de limiar se justifica, usando os dados alinhados.
4. Qualquer futura variante de limiares exige identidade própria, implementação explícita e testes que preservem a regra legada.
5. Mesmo com controles aprovados, revisar os reports e aprovar a política final. A campanha suplementar deve pausar em `awaiting_calibration_review`; só o comando explícito de continuação pode iniciar treino. Novo preflight precisa passar imediatamente antes da primeira injeção.

Até essa revisão, a campanha atual permanece `blocked`: zero injeções, zero novos casos de treino/teste e conjuntos inalterados.

## Aditivo prospectivo: `user.latency_ms`

Uma nova versão de política foi autorizada para os próximos controles, sem
alterar nem reavaliar os reports anteriores: `supplemental-user-latency-25pct-v1`.
Ela usa `max(0,25 × |média|, 20 ms)` apenas para `user.latency_ms` nas janelas
de preparação e controle sem falha. Os demais limites, pisos e gates permanecem
inalterados. O valor de 25% é uma decisão operacional a validar em dois
controles novos; os resultados antigos não contam como validação nem como
aprovação. Mesmo que ambos passem, a campanha pausa para revisão antes de
qualquer injeção.

## Aditivo prospectivo: todas as métricas de estabilidade

Para a campanha seguinte, a política ativa é
`supplemental-all-stability-25pct-v1`: fator relativo de 25% para todas as
métricas de estabilidade nas janelas de preparação e controle, preservando
cada piso. Gates de cobertura, saúde, segurança, efeito e recuperação não
mudam. A autorização é prospectiva: os controles anteriores sob 20% ou sob o
override restrito de `user.latency_ms` continuam imutáveis e reprovados quando
reprovaram. Dois controles novos e revisão explícita continuam obrigatórios
antes de iniciar qualquer injeção.
