# Campanha autônoma de falhas

`autonomous_campaign` executa a matriz de 40 cenários de
[`run_forty_faults.py`](./run_forty_faults.py) em série e reutiliza a API da
plataforma, os specs/manifestos existentes, o validador de efeitos e a rota
online de RCA. Não substitui o engine nem cria uma implementação paralela de
Litmus, Locust, Prometheus, Tempo ou RCA.

## Fluxo existente e limites

O fluxo anterior cria uma configuração por campanha, chama `runner.py` para
criar/iniciar cada run pela API e aguarda o estado terminal. A plataforma
coordena carga, falha, observabilidade e persistência. Em seguida, o harness
valida os efeitos e chama a RCA. O fluxo controlado separado já tem um ledger
durável e lock, mas possui outro protocolo e matriz.

No fluxo longo anterior, as tentativas de falha/espera podiam ser registradas
como CSV sem impedir a próxima célula; a retomada não protegia uma submissão de
resultado ambíguo; e o watchdog de port-forward testava principalmente se havia
um listener TCP. Um cooldown também não comprova recuperação. A campanha nova
falha fechada para esses casos.

## Segurança operacional e persistência

- `--simulate` só imprime o plano; não consulta cluster/API, cria campanha ou
  injeta falhas.
- `--pilot` executa somente a primeira célula da mesma matriz. `--run` exige o
  `report.json` de um piloto concluído e aprovado com este protocolo.
- Um lock por campanha impede duas retomadas concorrentes; outro lock impede
  campanhas simultâneas de disputarem os mesmos port-forwards.
- A campanha salva `campaign.json`, plano imutável, config, checkpoints,
  `events.jsonl`, `runs.csv`, relatório e um diretório por cenário com spec,
  estado/timestamps, logs, run, eventos, features, validação de efeito e RCA.
  O object store da plataforma permanece como fonte original dos artefatos.
- A retomada pula cenários `completed`. Um cenário com submissão ambígua é
  reconciliado pelo `experiment_id` e pela chave de idempotência determinística;
  o run existente é anexado, nunca reinjetado. Um cenário concluído sem seus
  arquivos de evidência faz a retomada parar.
- Os retries são limitados e destinados a conectividade/controle antes ou
  durante a consulta de um run. Depois que um run foi submetido, não há retry
  automático da injeção. Timeout pede parada do run e interrompe a campanha.
- O port-forward de MCP e Prometheus é validado por protocolo/endpoint. Um
  túnel saudável é reutilizado; um processo correspondente que perdeu saúde
  pode ser reiniciado dentro do orçamento limitado. Porta ocupada por listener
  desconhecido ou múltiplos túneis correspondentes causa interrupção, sem
  abrir um duplicado. Apenas processos iniciados pelo orquestrador são
  encerrados ao sair.
- Após cada run, os artefatos e evidências de efeito são persistidos; o
  validador exige evidência de injeção e sinal no alvo. A janela oficial de
  observação baseline/fault/post é salva e precisa estar completa para validar
  um run concluído. A campanha aguarda os deployments do Sock Shop ficarem
  disponíveis e permanecerem estáveis durante o cooldown, coleta a RCA e só
  então avança.
- Falha de injeção, sinal ausente, falha de observabilidade/infraestrutura ou
  causa não classificável interrompe a campanha. Não há modo implícito de
  ignorar cenário inválido. O relatório explicita `failure_domain`: falha de
  injeção não é considerada falha do serviço; falta de amostras vira lacuna de
  telemetria/cobertura; e injeção confirmada sem sinal no alvo fica como efeito
  não observado. Casos inconclusivos permanecem inconclusivos em vez de serem
  atribuídos ao chaos ou à infraestrutura sem evidência.

Os relatórios ficam em
`experiment/eval/memory_hog/out/autonomous_campaigns/<campaign-id>/`. Cada
campanha nova usa um diretório exclusivo; não reutilize um diretório de
resultados existente. Para retomar, passe o diretório original a `--resume`.
O resumo desta implementacao e os comandos estao neste arquivo; o fluxo legado
`run_forty_faults.py --run` continua separado e nao e necessario para a campanha
autonoma.

## Pré-requisitos

1. Plataforma em `http://127.0.0.1:8010`, API de observabilidade/MCP e RCA/LLM
   acessíveis.
2. Contexto `kubectl` apontando para o cluster Sock Shop, serviços e templates
   Litmus necessários disponíveis.
3. Para a matriz completa, carregar `sch_netem` conforme
   `make chaos-enable-netem`.
4. O processo supervisor gerencia os port-forwards MCP (`18080`) e Prometheus
   v3 (`9091`); não é necessário iniciar um segundo watchdog.

O preflight aborta antes de submeter qualquer run se plataforma, MCP,
Prometheus, cluster, serviços, templates ou o estado de execução concorrente
não passarem nos checks.

## Comandos

Execute a simulação e, se o plano estiver correto, um piloto real de uma célula:

```bash
.venv/bin/python -m experiment.eval.memory_hog.autonomous_campaign --simulate

mkdir -p experiment/eval/memory_hog/out/autonomous_campaigns
PILOT_DIR="experiment/eval/memory_hog/out/autonomous_campaigns/pilot-$(date -u +%Y%m%dT%H%M%S%N)"
PILOT_LOG="experiment/eval/memory_hog/out/autonomous_campaigns/pilot-console-$(date -u +%Y%m%dT%H%M%S%N).log"
nohup .venv/bin/python -m experiment.eval.memory_hog.autonomous_campaign \
  --pilot --campaign-dir "$PILOT_DIR" >>"$PILOT_LOG" 2>&1 </dev/null &
echo "PID do piloto: $!"
```

Verifique o processo e o relatório antes de liberar a matriz:

```bash
tail -f "$PILOT_LOG"
.venv/bin/python -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps({"status":r["status"],"passed":r["passed"],"summary":r["summary"]}, indent=2)); sys.exit(0 if r["status"]=="completed" and r["passed"] else 1)' \
  "$PILOT_DIR/report.json"
```

Somente após o piloto retornar sucesso, inicie a campanha longa. Mantenha os
valores de `PILOT_DIR` e `FULL_DIR` em uma sessão de shell com persistência ou
substitua-os pelos caminhos impressos:

```bash
FULL_DIR="experiment/eval/memory_hog/out/autonomous_campaigns/full-$(date -u +%Y%m%dT%H%M%S%N)"
FULL_LOG="experiment/eval/memory_hog/out/autonomous_campaigns/full-console-$(date -u +%Y%m%dT%H%M%S%N).log"
nohup .venv/bin/python -m experiment.eval.memory_hog.autonomous_campaign \
  --run --pilot-report "$PILOT_DIR/report.json" --campaign-dir "$FULL_DIR" \
  >>"$FULL_LOG" 2>&1 </dev/null &
echo "PID da campanha: $!"
```

Monitore e valide o resultado final:

```bash
tail -f "$FULL_LOG"
.venv/bin/python -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps({"status":r["status"],"passed":r["passed"],"summary":r["summary"],"interruption_cause":r["interruption_cause"]}, indent=2)); sys.exit(0 if r["status"]=="completed" and r["passed"] else 1)' \
  "$FULL_DIR/report.json"
```

Se o processo for interrompido, leia primeiro `report.json`, `campaign.log` e
os logs/evidências do cenário corrente. Depois retome o mesmo diretório:

```bash
.venv/bin/python -m experiment.eval.memory_hog.autonomous_campaign --resume "$FULL_DIR"
```

Não retome automaticamente um cenário marcado `failed`: a regra é fail-fast,
preservar as evidências e exigir revisão antes de criar uma nova campanha ou
autorizar outra injeção. Uma interrupção com run ainda ativo mantém seu `run_id`
no checkpoint para que a retomada reconcilie o mesmo run.
