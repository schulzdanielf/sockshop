# 2026-09-20 — Diagnóstico do travamento + proteção do OTel Collector

## O que aconteceu

O usuário parou a execução porque ela "ficou presa, muito tempo parada no mesmo
ponto". Investigação:

**Causa raiz:** o OTel Collector local (`localhost:4318`) estava **fora**, e o
engine tentava exportar telemetria para ele com **retry infinito**. O log da
plataforma mostrava `Failed to export span batch due to timeout... Connection
refused` em loop — a thread que processava o run travava após
`recovery_observation_started`.

O run `run-149fab9bb676` ficou preso na fase `post` (recovery) por mais de 5 min
sem completar.

## Correção aplicada

1. **Run travado** marcado como `stopped`.
2. **Plataforma reiniciada com `OTEL_SDK_DISABLED=true`** — o SDK vira no-op.
3. **Proteção permanente no código** (`observability/otel.py`): `_install_sdk`
   agora testa `_collector_reachable(endpoint)` **antes** de instalar. Se o
   collector não responde, o SDK não instala (warning + no-op) — o pipeline nunca
   mais trava por collector ausente.

## Teste da proteção

```
collector 4318 acessível? False
OTel collector inacessível ... SDK desligado (no-op).
SDK instalado mesmo sem collector? False
```

125 testes passando.

## Estado da coleta (gat-istio-v1)

Retomada com `--retry-failed` em andamento:
- 19 runs totais: **14 completed**, 3 stopped, 2 running (processando as 11
  células pendentes).

## Lição

Falha de infraestrutura (collector fora) não pode travar o pipeline — agora é
defensiva por padrão. O checklist/gates continuam válidos: a plataforma degrada
graciosamente sem telemetria OTel.
