# Catálogo de falhas de caos (LitmusChaos)

Manifestos de injeção de caos para o Sock Shop, executados via **LitmusChaos +
Argo Workflows** no namespace `litmus`, mirando os deployments no namespace
`sock-shop`.

Cada arquivo `catalogue-<falha>.yaml` é um **Argo Workflow** com 3 passos:

1. `install-chaos-faults` — aplica o `ChaosExperiment` (a definição da falha).
2. `run-chaos` — aplica um `ChaosEngine` que dispara a falha no alvo.
3. `cleanup-chaos-resources` — remove os `ChaosEngine` ao final.

O nome do workflow (`catalogue-<falha>`) é importante: a plataforma do
experimento **clona o último run** desse nome para reinjetar a falha. Por isso
o template precisa existir uma vez no Litmus antes de rodar o harness.

## Catálogo

| Manifesto | ChaosExperiment (Litmus) | `fault_category` | Parâmetros-chave (defaults) | Status no docker-desktop / WSL2 |
|---|---|---|---|---|
| `catalogue-memory-hog.yaml` | `pod-memory-hog` | `memory-exhaustion` | `MEMORY_CONSUMPTION=60MB`, `DURATION=30s` | ✅ OK (verificado) |
| `catalogue-cpu-hog.yaml` | `pod-cpu-hog` | `cpu-exhaustion` | `CPU_CORES=1`, `DURATION=60s` | ✅ OK (verificado) |
| `catalogue-network-latency.yaml` | `pod-network-latency` | `network-latency` | `NETWORK_LATENCY=2000ms`, `DURATION=60s` | ✅ OK após `make chaos-enable-netem` (testado, Pass) |
| `catalogue-network-loss.yaml` | `pod-network-loss` | `network-loss` | `PACKET_LOSS=100%`, `DURATION=60s` | ✅ OK após `make chaos-enable-netem` (testado, Pass) |
| `catalogue-http-status-code.yaml` | `pod-http-status-code` | `http-error` | `STATUS_CODE=500`, `PORT=80`, `DURATION=60s` | ✅ OK (testado, Pass) |
| `catalogue-container-kill.yaml` | `container-kill` | `pod-failure` | `SIGNAL=SIGKILL`, `INTERVAL=10s`, `DURATION=20s` | ✅ OK (testado, Pass) |
| `catalogue-io-stress.yaml` | `pod-io-stress` | `io-exhaustion` | `FS_UTIL=10%`, `WORKERS=4`, `DURATION=120s` | ❌ NÃO roda aqui (overlayfs WSL2) |
| `catalogue-dns-error.yaml` | `pod-dns-error` | `dns-failure` | `MATCH_SCHEME=exact`, `DURATION=60s` | ❌ NÃO roda aqui (dns_interceptor) |

> Os 6 manifestos novos (latency, loss, dns, io, http, container-kill) usam
> `CONTAINER_RUNTIME=docker` e `SOCKET_PATH=/var/run/docker.sock`, alinhados aos
> baselines de cpu/memory que já funcionam neste cluster. Os resultados acima
> são de **smoke-tests reais** (workflow disparado + veredito do `ChaosResult`).
> As 4 falhas ✅ estão na matriz de treino/teste; as 2 ❌ ficam só como
> baselines/definições, para um cluster compatível.

### Falhas de rede no docker-desktop / WSL2 (`sch_netem`)

`pod-network-latency` e `pod-network-loss` injetam regras `tc`/`netem` via
helper pod, o que exige o qdisc `sch_netem` no kernel do nó. No docker-desktop
/ WSL2 o módulo **acompanha o kernel mas não é carregado automaticamente**; sem
ele a falha aborta com `failed to create tc rules: Specified qdisc kind is
unknown`.

Como o WSL2 compartilha um único kernel entre a distro e o docker-desktop,
basta carregar o módulo no host **uma vez por boot**, antes de qualquer falha de
rede:

```bash
make chaos-enable-netem      # sudo modprobe sch_netem
```

Depois disso `pod-network-latency` e `pod-network-loss` rodam normalmente
(verificado: veredito `Pass`). O carregamento **não é persistente** — refaça
após reiniciar o WSL, ou torne permanente com
`echo sch_netem | sudo tee /etc/modules-load.d/sch_netem.conf`.

### Falhas que NÃO rodam neste ambiente

Duas falhas do catálogo foram testadas e **falham especificamente no
docker-desktop / WSL2** (mantidas como baselines, fora da matriz):

* **`pod-io-stress`** — o `stress-ng --hdd` usa `O_DIRECT`, não suportado pelo
  overlayfs do WSL2; o stressor sai com `exit status 1` imediatamente
  (reproduzido em `catalogue` e em `carts`, mesmo com `/tmp` gravável).
* **`pod-dns-error`** — o binário `dns_interceptor`, ao entrar no netns do
  alvo, sai com `exit status 1`; persiste mesmo após carregar os módulos
  `nfnetlink_queue`/`xt_NFQUEUE` no kernel. Incompatibilidade do interceptor
  com o kernel WSL2.

Ambas devem rodar normalmente em nós de cluster reais (kernel completo +
sistema de arquivos com suporte a `O_DIRECT`).

## Como registrar um template (uma vez)

Aplicar dispara **um** run do workflow — esperado. O nome persiste depois e é
reclonado pela plataforma.

```bash
kubectl apply -f deploy/kubernetes/manifests-chaos/catalogue-io-stress.yaml
kubectl get workflows.argoproj.io -n litmus | grep io-stress
```

## Como rodar uma falha avulsa (manual, sem o harness)

```bash
# dispara o workflow
kubectl create -f deploy/kubernetes/manifests-chaos/catalogue-http-status-code.yaml

# acompanha
kubectl get chaosengine -n litmus -w
kubectl get chaosresult -n litmus
# veredito da falha:
kubectl get chaosresult -n litmus \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.experimentStatus.verdict}{"\n"}{end}'
```

## Validação (sem injetar caos)

Os manifestos são validados contra os CRDs do cluster sem criar nada:

```bash
for f in deploy/kubernetes/manifests-chaos/catalogue-*.yaml; do
  kubectl apply --dry-run=server -f "$f"
done
```

## Geração por serviço (harness)

Estes arquivos são os **baselines do serviço `catalogue`**. O harness do
experimento gera versões por serviço a partir deles:

* `experiment/eval/memory_hog/generate_chaos_manifests.py` — renderiza
  `<service>-<falha>.yaml` trocando o token `catalogue` pelo serviço-alvo e
  substituindo os parâmetros declarados em `config.yaml`.
* `experiment/eval/memory_hog/config.yaml` — bloco `chaos_types` declara, para
  cada falha, o `env` (parâmetros tunáveis) e o `fault_category` (rótulo
  ground-truth do experimento).

Convenção dos baselines (para a renderização ser segura):

* o token `catalogue` só aparece onde identifica o alvo (nome do workflow,
  label `workflow_name`, `applabel` e o `TARGET_CONTAINER` do engine);
* defaults do `ChaosExperiment` usam aspas simples (`value: '60'`) e o
  `ChaosEngine` usa aspas duplas (`value: "60"`), de modo que a substituição
  atinge só o engine (o que de fato dirige o run).

## ⚠️ Integração com o pipeline de RCA da plataforma

Os manifestos **injetam** as falhas, mas o pipeline de RCA da plataforma ainda
**não tem suporte dedicado** às novas categorias. Hoje:

* `experiment/platform/data/system_cards/sock-shop.yaml` →
  `allowed_fault_categories` **já inclui** `network-latency`, `network-loss`,
  `dns-failure`, `io-exhaustion`, `http-error` (adicionadas), então o LLM pode
  prever esses rótulos e o harness pode computar acurácia.
* `backend/analysis/fault_category_validator.py` → só corrige veredito para
  `memory/cpu/pod-failure`; categorias novas passam **sem alteração** (seguro).
* `backend/analysis/service_localizer.py` → só tem âncora métrica para
  `memory/cpu/pod-failure`; para categorias novas não há override (seguro,
  mantém o RCA do LLM).

Para um RCA mais forte nas falhas novas, seria preciso (fora do escopo destes
manifestos): adicionar checagens em `fault_category_validator.py`, âncoras em
`service_localizer.py` e garantir que as métricas relevantes (latência de rede,
erros de DNS, I/O, 5xx) estejam expostas no MCP/observabilidade.

## Adicionar uma falha nova à matriz do experimento

1. Garanta o baseline `catalogue-<falha>.yaml` aqui e um renderer em
   `generate_chaos_manifests.py` (`RENDERERS["<falha>"]`).
2. Declare a falha em `config.yaml` → `chaos_types` (`env` + `fault_category`).
3. Adicione células `{service, chaos_type}` em `train_experiments` /
   `test_experiments` (lembre: toda célula de teste precisa de uma de treino
   correspondente; DBs e rabbitmq ficam de fora).
4. Gere e registre os templates:
   ```bash
   python experiment/eval/memory_hog/generate_chaos_manifests.py
   kubectl apply -f experiment/eval/memory_hog/out/manifests/
   ```
