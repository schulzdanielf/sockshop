# Replicacao autonoma: 35 novos testes, meta de 70 incidentes

## Decisoes aprovadas em 10/11/2026

- Coletar uma nova execucao de cada uma das 35 combinacoes de teste.
- Nao ampliar treino, alterar elegibilidade, reconstruir A/B ou reindexar RAG.
- Implementar e armar o executor nesta etapa; Luna acompanha a operacao.
- Liberar somente depois do sucesso verificavel da avaliacao
  `test-base-v6-20261110T212045271499Z`.
- Aprovar automaticamente dois controles novos se aceitos pela politica
  registrada; preservar e publicar todos os avisos de variabilidade.
- Permitir no maximo duas tentativas por celula, incluindo reinjecao
  estritamente tecnica depois de cleanup e recuperacao.
- Autorizar reparos de conexao, API e servicos; nao alterar limites para
  conseguir resultados favoraveis.
- Limite de 24 horas desde a liberacao, podendo continuar durante o dia.
- Publicar original / nova / uniao; nao executar diagnosticos automaticamente.
- Maquina e cluster permanecerao disponiveis, sem suspensao.

## Bases congeladas

Referencia: [avaliacao atual](../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/).
O manifesto da campanha fixa hashes dos inputs e da implementacao.

| Item | Referencia |
|---|---|
| Coorte original | 35 incidentes, uma combinacao de cada servico/falha |
| Treino | `supplemental-training-35-supplemental-20261109T223633107132Z` |
| Hash semantico do corpus | `5c05ba9ec79aeabe0c3c9a2f94badc8563ca7d2b736574dc794f34cb1b19cea6` |
| Recuperador, para futura avaliacao | `opensearch-bm25-metric-gated-rrf-v6` |
| Elegibilidade atual | 20 guias validados, 15 pendentes; nao modificar |
| Nova replica logica | `replica_idx=2`, split `test` |
| Seed de ordenacao | `20261110` |

O hash semantico do corpus nao e necessariamente o SHA-256 dos bytes de seu
arquivo JSON. Ambos os tipos de identidade devem permanecer distinguiveis.

## Matriz fechada

Cada cruzamento abaixo representa uma celula; **35 celulas**, nao 35 tentativas
garantidamente validas. Os dois controles nao entram nessa contagem.

| Servico | CPU | Memoria | Latencia de rede | Container-kill | HTTP 500 |
|---|---|---|---|---|---|
| front-end | 1 | 1 | 1 | 1 | 1 |
| catalogue | 1 | 1 | 1 | 1 | 1 |
| carts | 1 | 1 | 1 | 1 | 1 |
| orders | 1 | 1 | 1 | 1 | 1 |
| payment | 1 | 1 | 1 | 1 | 1 |
| shipping | 1 | 1 | 1 | 1 | 1 |
| user | 1 | 1 | 1 | 1 | 1 |

A ordem efetiva e deterministica, embaralhada pela seed e sem dois servicos
iguais consecutivos. Ela fica congelada em `plan.json`, com identidade propria
por campanha, celula e tentativa. Uma nova inferencia no run antigo nao conta
como nova replica de caos.

## Protocolo prospectivo e comparabilidade

Versao: `test-replication-35-v1`. Reutilizar os executores controlados existentes.

1. Preservar carga de 30 usuarios, spawn 5/s, intensidade/calibracoes declaradas,
   recursos, probes, mecanismo, cobertura, seguranca, cleanup e gates de efeito.
2. Usar `controlled-measurement-v2`: lookback de 60 s e CPU por relogio
   monotonico; saltos superiores a 1 s bloqueiam.
3. Preparacao por caso: warmup de 180 s, duas janelas consecutivas de 60 s
   dentro de 600 s, baseline de 180 s. Estabilidade por caso continua em 20%,
   com os pisos existentes.
4. Recuperacao experimental: 300 s, com override de 600 s para `orders`,
   conforme o protocolo suplementar recente. Nao importar retroativamente
   aditivos antigos limitados a campanhas especificas.
5. Duracoes ativas: CPU/memoria 180 s; rede/HTTP 120 s; container-kill e evento,
   com duracao ativa configurada como zero e confirmacao de restart.
6. Dois controles sem falha, cada um com pelo menos 600 s, nos servicos
   `front-end`, `orders`, `user`, conforme a politica atual. O preflight cobre
   todos os sete servicos e todas as falhas.
7. Controles usam `supplemental-all-stability-25pct-v1` e
   `no-fault-control-diagnostic-v1`. Variabilidade da janela longa gera avisos;
   preparacao, baseline, cobertura, identidade, relogio, saude e cleanup
   continuam obrigatorios.

**Nao chamar isto de replicacao identica do protocolo historico.** A coorte
original mistura protocolos e periodos, incluindo preparacao de 60/90 s.
O novo bloco usa preparacao corrigida de 180/180 s e medicao versionada.
Nao reclassificar falhas antigas usando regras novas. O anexo de 08/11 e
evidencia de planejamento anterior, nao a fonte unica da politica vigente.

## Maquina de estados e inicio

`prepared -> waiting_for_evaluation -> released -> controls -> collecting`

Terminais: `completed`, `partial`, `blocked`.

- Enquanto houver processo de avaliacao neste checkout, nao iniciar carga,
  injetar, reiniciar API, alterar dual-write ou reparar servicos.
- Ausencia do processo nao prova sucesso: exigir marcador de diagnosticos,
  report persistido e reconstrucao validada a partir dos resultados, hashes
  e pre-coleta. Exigir 35 casos e zero erros de diagnostico.
- Se a avaliacao terminar sem report valido, bloquear sem injetar.
- A partir da liberacao, iniciar orcamento monotonico de 24 horas. Nao iniciar
  uma etapa cujo pior orcamento configurado mais a margem de encerramento
  nao caiba no tempo restante. Cleanup pode ultrapassar o prazo.
- Exigir API carregada correspondente ao checkout, ambiente saudavel,
  isolamento de carga e preflight de relogio de 600 s.
- Depois da liberacao, corrigir o cleanup dos registros de memoria legados:
  o preflight somente leitura encontrou seis Workflows registrados ainda
  selecionando `dd`, enquanto o worker atual e `stress-ng --vm-bytes`.
  Alterar somente `CHAOS_KILL_COMMAND` no artefato ChaosEngine dos registros
  terminais dos sete servicos-alvo, usando o valor revisado no repositorio.
  Preservar backups e aplicar JSON Patch com testes de resourceVersion e
  valor anterior. Nao criar/submeter Workflows para fazer essa correcao.
  O dry-run do servidor aceitou os seis patches; `front-end` ja e compativel.
  Nao tocar o registro de `queue-master`, fora desta matriz.
- Habilitar dual-write com identidade unica; exigir visibilidade historica
  dos sete servicos. Revalidar a visibilidade do alvo antes de cada tentativa.
- Executar/revisar automaticamente os dois controles novos. Reusar somente
  controles completos desta mesma campanha e politica, nunca controles antigos.
- Submeter os testes sequencialmente; nunca sobrepor injecoes.

## Falhas, reparos e tentativas

1. No maximo duas tentativas totais por celula; nao ha retries internos extras
   de injecao. Retries de leitura/polling existentes continuam limitados.
2. Permitir segunda tentativa por bloqueio anterior a injecao ou falha tecnica
   explicita de telemetria/especificacao, sempre com cleanup e carga parada.
   Efeito menor que o esperado, sozinho, **nao** autoriza nova tentativa.
3. Nunca sobrescrever run, report, spec ou ledger. Registrar a relacao entre
   tentativas e manter os invalidos na analise operacional.
4. Resultado desconhecido/timeout com run potencialmente ativo nao autoriza
   reinjecao. Solicitar stop somente dos runs proprios e aguardar cleanup.
5. Apos falha, exigir recuperacao. Quando necessario, permitir restart apenas
   do Deployment do servico-alvo em `sock-shop`, depois de encerrar o run e
   comprovar cleanup/carga parada. Limite conservador: tres restarts na campanha,
   um por servico. Nao reiniciar cluster, bancos ou servicos alheios em cascata.
6. Apos recuperacao, executar duas verificacoes de ambiente separadas por
   120 s; o proximo caso ainda exige sua preparacao completa sob carga.
7. Reparar port-forwards ausentes somente em portas conhecidas; nao matar
   processos de terceiros. Falha de contrato/saude nao vira sucesso por
   apenas existir um socket aberto.
8. Reiniciar API desatualizada somente ociosa, com PID, instante de inicio e
   comando correspondentes ao processo registrado. Sem kill forcado.
   API inacessivel sem prova de ociosidade exige parada segura.
9. Falhas persistentes de cobertura, relogio, identidade, cleanup ou saude
   bloqueiam; nao ampliar limiares, selecionar janelas favoraveis ou descartar
   pontos para liberar a campanha.
10. Segunda tentativa invalida nao impede outras celulas se ambiente e cleanup
    estiverem comprovadamente seguros. Se nao houver essa prova, parar.

Locks impedem dois supervisores desta campanha ou duas campanhas deste
executor no mesmo diretorio pai. Tambem existem gates de runs ativos e de
isolamento do Locust. Nenhum lock impede ferramentas externas que ignorem
esses mecanismos: nao iniciar outra campanha manualmente durante a coleta.

## Persistencia, publicacao e resultado parcial

Por campanha: manifesto com autorizacoes/hashes, plano, config, estado,
eventos, controles brutos, readiness, visibilidade historica e ledger de
tentativas. Por tentativa: spec, run, gates e validacao historica online.

Para aceitar um run, exigir simultaneamente efeito confirmado, mecanismo,
recuperacao, cleanup, carga parada e read-back historico completo de metricas,
logs e traces. `completed` da API, isoladamente, nao basta.

Entregas:

- `cohort_original_35.json`: original preservado.
- `cohort_new_35.json`: novos testes aceitos; pode conter menos de 35.
- `cohort_combined_70.json`: original + novos aceitos; so chega a 70 com
  todas as 35 celulas novas validas.
- `collection_report.json`: contagens, pareamento original/novo e limitacoes.
- `attempts.jsonl`: todas as tentativas encerradas e recuperadas, inclusive
  invalidas. Tentativas interrompidas permanecem nos ledgers dos casos.

Os nomes sao metas, nao declaracoes de sucesso: consultar `run_count`,
`complete` e o estado terminal. Bloqueio ou prazo publica parcial sem inventar
os runs faltantes. Dados novos nunca entram automaticamente no treino/RAG.
Esses JSONs sao artefatos de coleta; nao afirmar que o importador suplementar
legado, restrito a tres testes, aceita diretamente a coorte de 70.

No encerramento, solicitar stop de runs proprios ainda ativos antes de desligar
dual-write. Se cleanup nao puder ser comprovado, preservar historico ativo e
marcar bloqueio para intervencao. Encerrar port-forwards criados pelo supervisor;
uma API substituida fica disponivel para o usuario.

## Handoff operacional para Luna

Implementacao: [executor](../../experiment/eval/memory_hog/test_replication_campaign.py).
Usar sempre a `.venv` do repositorio.

```bash
# Preparacao: somente arquivos locais, sem carga/restart/injecao.
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --prepare experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z \
  --output experiment/eval/memory_hog/out/test_replication/test-replication-20261110-night

# Validar identidades congeladas, sem iniciar a campanha.
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --check experiment/eval/memory_hog/out/test_replication/test-replication-20261110-night

# Armar espera; o proprio executor valida a conclusao da avaliacao.
./.venv/bin/python -m experiment.eval.memory_hog.test_replication_campaign \
  --run experiment/eval/memory_hog/out/test_replication/test-replication-20261110-night
```

Rodar como processo persistente independente da sessao de chat, com stdout/
stderr em log da campanha; verificar PID e estado responsivo depois de armar.
Nao iniciar um segundo supervisor se um ja estiver ativo. A troca Sol/Luna nao
e necessaria para o processo continuar. Nao alterar codigo/input congelado
durante a campanha: a proxima verificacao bloqueara.

### Aditivo de retomada autorizada em 10/11/2026

O primeiro supervisor validou o fim da avaliacao, corrigiu cleanup nos seis
registros Litmus legados e parou no preflight, antes de carga/controles:
desvio wall/monotonic de 1,156222 s em 205,193267 s contra limite de 1 s.
Nao houve runs ativos nem ativacao de dual-write. NTP estava sincronizado na
inspecao posterior. Por autorizacao explicita do usuario, uma campanha sucessora
com novo ID pode repetir um preflight somente leitura de 600 s. Apenas se passar
pode iniciar os mesmos controles/casos; se reprovar, fica bloqueada. O primeiro
estado, seus hashes e os arquivos de evidencia nao podem ser sobrescritos.

A campanha sucessora `test-replication-20261110-clock-retry-2247` tambem parou
antes de controles/casos: `clock_preflight` observou wall 81,084370 s versus
monotonic 82,092735 s, desvio 1,008364 s em cerca de 82 s, acima do mesmo limite
de 1 s. NTP reportava `NTPSynchronized=yes`; a maquina e WSL2, clocksource `tsc`,
com `systemd-timesyncd` ativo e poll de 32 s. O status sincronizado e offset
corrente nao explicam nem invalidam o intervalo reprovado; logs consultados nao
mostraram passos recentes. `timedatectl timesync-status` mostrou servidor
`ntp.ubuntu.com`, offset de aproximadamente -296 ms, jitter de aproximadamente
222 ms e poll de 32 s. O kernel registrou ajuste de TSC acima de 11% as 20:16,
mas sem evento correlato no horario das reprovas; isso e uma pista, nao causa
confirmada. A tentativa de consultar Windows Time via executaveis montados em
`/mnt/c` falhou com `Invalid argument` (interop Windows nao disponivel neste
shell). Nenhum ajuste/restart do host foi feito. Nao executar uma terceira
tentativa nem aumentar tolerancia ate estabilizar/diagnosticar o relogio
wall/monotonic do host WSL/Windows ou transferir a campanha para host Linux
estavel e entao obter nova autorizacao. Ambas as campanhas permanecem `blocked`,
com zero controles, zero runs novos e dual-write desligado.

Luna deve inspecionar estado/eventos e os reports em caso de bloqueio; nao
editar estado terminal para burlar gates. Retomada de processo interrompido
antes de um estado terminal usa o mesmo diretorio e reconciliacao idempotente
do executor controlado. Runs ativos ou controle interrompido exigem revisao;
nao prometer retomada automatica de todo tipo de interrupcao.

## Analise futura de variabilidade

- Separar coleta de caos de nova inferencia no mesmo incidente.
- Comparar os blocos original/novo por combinacao servico/falha, preservando
  intensidade, duracoes efetivas, recursos, gates, retries e reparos.
- Reportar 35 pares quando completos, taxas de invalidos e sensibilidade
  incluindo todas as primeiras tentativas; nao apresentar apenas sobreviventes.
- Para diagnostico futuro, congelar corpus, elegibilidade, modelo, temperatura,
  seed, retriever, prompt e orcamentos; contrabalancear Skill/RAG dentro de cada
  incidente. Nao misturar mudancas do metodo com mudancas do incidente.
- Medir delta pareado de acerto conjunto, latencia, tokens e sinais fisicos.
  Informar denominadores por servico/falha e dados ausentes.
- Duas observacoes por celula permitem medir diferencas, nao estimar com
  precisao uma distribuicao de variancia. Nao tratar 70 runs heterogeneos
  como 70 replicas IID; explicitar efeito de bloco/protocolo e usar analise
  pareada ou reamostragem por celula quando apropriado.

### Plano de reparo dos sete casos cpu-hog (11/11/2026)

#### Diagnostico factual

Os sete novos casos r2 falharam no gate de media: nenhum atingiu a banda
`0.95-1.05` da quota CPU do container. A media entre servicos foi `0.9007`,
ou 4.93 pontos percentuais abaixo do piso. As amostras foram suficientes
(37-41 por caso; maior intervalo de 20 s), e os gates de recuperacao e cleanup
passaram nos sete casos.

| Servico | Treino r0: media / sustentado | Teste r1: media / sustentado | Novo teste r2: media / sustentado |
|---|---:|---:|---:|
| front-end | 0.8937 / 23.63 s | 0.8613 / 22.14 s | 0.9284 / 148.69 s |
| catalogue | 0.8336 / 54.89 s | 0.8276 / 24.49 s | 0.8816 / 137.00 s |
| carts | 0.8477 / 24.41 s | 0.8500 / 21.20 s | 0.8891 / 31.42 s |
| orders | 0.8642 / 25.31 s | 0.8565 / 24.60 s | 0.9016 / 148.37 s |
| payment | 0.8342 / 20.80 s | 0.8321 / 25.27 s | 0.8952 / 81.70 s |
| shipping | 0.8504 / 27.26 s | 0.8063 / 22.67 s | 0.8756 / 87.72 s |
| user | 0.8792 / 24.22 s | 0.8809 / 23.08 s | 0.9333 / 65.19 s |

`sustentado` e a maior duracao observada acima do piso do gate. O requisito e
60 s. Portanto, em r2 seis servicos passaram duracao, mas nao media; `carts`
falhou media e duracao. Em r1 os sete falharam ambos. Em treino r0 os sete
falharam duracao; seis tinham banda `0.76-0.84` (cinco medias acima da banda,
`catalogue` dentro dela) e `payment` tinha banda `0.95-1.05`, mas media abaixo.

Os perfis medidos tambem impedem uma correcao por suposicao: treino r0 usou
`CPU_CORES=2`, `CPU_LOAD=100`; r1 e r2 usaram `CPU_CORES=1`,
`CPU_LOAD=100`. A media r2 melhorou 5.57 pontos sobre r1 e 4.31 sobre r0,
mas continuou abaixo do piso. Logo, repetir o perfil de dois workers nao e
uma correcao comprovada. A causa fisica exata continua aberta: ainda e preciso
correlacionar os argumentos realmente executados pelo workflow, o intervalo
ativo do stress-ng, o `cpu.max`/`cpu.stat` do cgroup alvo e a janela usada na
media Prometheus. Em especial, a sustentacao curta de `carts` requer analise
do workflow e da serie temporal, nao apenas aumento nominal da duracao.

Os sete pares CPU de treino estao na base congelada, mas todos tem
`effect_validated=false`; seis estao `procedurally_validated` e `shipping`
esta `procedural_pending`. Nao tratar a presenca na base como prova de efeito
validado, nem importar os novos testes de reparo para treino. Manter a base e
a coorte de teste separadas.

Evidencias primarias:

- Estado parcial atual: `../../experiment/eval/memory_hog/out/test_replication/test-replication-20261110-post-reboot-2312/campaign_state.json`
- Relatorio e tentativas r2: `../../experiment/eval/memory_hog/out/test_replication/test-replication-20261110-post-reboot-2312/collection_report.json` e `../../experiment/eval/memory_hog/out/test_replication/test-replication-20261110-post-reboot-2312/attempts.jsonl`
- Perfis e gates por run: `../../experiment/platform/data/object_store/<run_id>/controlled_report.json` e `effective_spec.json`
- Coorte de treino congelada: `../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/rag_base.json`
- Coorte de teste r1: `../../experiment/eval/memory_hog/out/knowledge_evaluation/test-base-v6-20261110T212045271499Z/test_cohort.json`

#### Correcao e reexecucao propostas

1. **Preflight somente leitura.** Confirmar campanha terminal sem runs ativos,
   servicos saudaveis, dual-write e destinos historicos, quotas reais dos sete
   containers e estabilidade wall/monotonic por 600 s. Fazer um controle de
   carga sem caos para confirmar que a telemetria segue comparavel. Qualquer
   falha bloqueia a campanha; nao ajustar tolerancia de relogio.
2. **Fechar a auditoria do mecanismo antes de mudar parametros.** Para cada
   run r2, comparar o `effective_spec`, workflow submetido e ChaosEngine
   efetivamente observado; confirmar `CPU_CORES`, duracao, container/pod alvo
   e tempos de inicio/fim do processo stress-ng. Alinhar a media e a
   sustentacao com o mesmo intervalo ativo; reconciliar taxa Prometheus com
   uso e throttling do cgroup do container. Se a divergencia for de medicao ou
   configuracao nao aplicada, corrigir primeiro esse produtor e validar em
   teste sem caos.
3. **Calibrar sem relaxar gates.** Versionar um perfil candidato por servico
   usando a quota atual. Nao assumir que `cores=2` resolve: ja foi usado no
   treino sem atingir os gates atuais. A primeira tentativa corrigida de cada
   servico serve como medicao; so conta como reposicao se passar a mesma banda
   `0.95-1.05`, pelo menos 60 s sustentados acima de `0.95`, telemetria valida,
   recuperacao e cleanup. Para `carts`, alem da media, exigir evidencia de
   continuidade do processo de carga por tempo suficiente. Se o perfil
   candidato nao atingir os criterios, parar e recalibrar sob novo aditivo;
   nao repetir automaticamente falha de efeito.
4. **Execucao sequencial.** Reexecutar uma vez cada uma das sete celulas CPU,
   sem paralelizar servicos. Preservar os sete resultados anteriores como
   tentativas nao aceitas. Interromper imediatamente em falha de seguranca,
   recuperacao, cleanup, parada de carga, quota divergente ou telemetria
   incompleta. Nao executar diagnosticos RCA como parte desta coleta.
5. **Integracao versionada na mesma campanha logica.** O executor atual recusa
   reiniciar estado terminal `partial`; nao reabrir nem editar o estado,
   manifest, plano, ledger, relatorio ou coortes v1. Implementar uma extensao
   append-only, por exemplo `continuations/cpu-repair-v1/`, vinculada por
   hashes aos artefatos v1. Ela registra os sete novos experiment IDs unicos,
   a calibracao, autorizacao, logs e gates; conserva o campaign/history ID
   pai para agrupamento, sem reutilizar IDs de run. Publicar
   `collection_report_v2`, `cohort_new_35_v2` e `cohort_combined_70_v2` como
   artefatos novos. So se os sete casos forem aceitos o total passa de 28 para
   35 novos e de 63 para 70 combinados; caso contrario, publicar o total
   parcial sem promover tentativas invalidas.

O plano de reexecucao permanece pendente; nenhum novo caos foi iniciado e o
estado operacional original continua `partial`. A reexecucao requer habilitar
a extensao versionada, congelar seus hashes e receber autorizacao explicita.

### Aditivo post hoc autorizado: piso CPU de 80% (11/11/2026)

O usuario autorizou aplicar aos sete casos CPU r2 a regra de media da fase de
falha `>=0.80`, sem teto e sem requisito de sustentacao, usada na emenda
`controlled-cpu-fault-mean80-v1`. A regra foi aplicada somente para elegibilidade
da coorte de teste; nao altera o ledger original, a base de treino nem a
politica registrada nos reports v1.

Os sete casos passam a emenda: medias entre 0.8756 e 0.9333; a revalidacao
confirmou medicao CPU e todos os checks preservados (submissao, recuperacao,
cleanup, parada de carga e read-back historico). Os segundos acima do limite
original de 95% permanecem diagnosticos; `carts` nao atingiu os 60 s exigidos
pelo gate original. Nenhum caos foi reinjetado.

A revisao append-only e registrada em `cpu_mean80_amendment_v2.json`,
`cpu_mean80_revalidation_v2.jsonl`, `cohort_new_35_v2.json`,
`cohort_combined_70_v2.json` e `collection_report_v2.json`. A revisao contem
35 novos casos elegiveis e 70 runs combinados sob a emenda, enquanto
`campaign_state.json` e os relatorios/coortes v1 permanecem imutaveis e com
estado original `partial`.
