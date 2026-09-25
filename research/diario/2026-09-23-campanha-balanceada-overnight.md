# Campanha balanceada de caos para a noite

## Contexto

Em 2026-09-23 o dataset GAT tinha 57 grafos `clean`, cobrindo 18 de 42 combinacoes entre seis tipos de falha e sete servicos. O treinamento GAT e a ablacao MLP estavam rodando separadamente na GPU; esta campanha nao deve interromper esses processos.

A campanha principal historica usa `experiment/eval/memory_hog/out/runs.csv`. Para evitar misturar o novo experimento com esse ledger, a campanha balanceada usa `experiment/eval/memory_hog/out/balanced_campaign/runs.csv`.

## Plano

O script `experiment/eval/memory_hog/run_balanced_campaign.py` cria duas etapas:

1. **Cobertura:** 42 celulas (`6 falhas x 7 servicos`) com duas replicas. O ledger atual e copiado como seed; somente celulas ausentes ou incompletas sao executadas. Esta etapa adiciona no maximo 53 runs e deixa pelo menos duas observacoes por celula.
2. **Reforco:** 17 celulas recebem a terceira replica, priorizando `network-loss`, `pod-delete`, `memory-hog` e os servicos com menor cobertura. Esta etapa adiciona 17 runs quando a etapa 1 termina corretamente.

O resultado esperado, se todos os runs forem concluidos e aprovados pelo filtro, e de aproximadamente 127 grafos clean. O tempo observado por run foi de 6,3 a 6,9 minutos; as duas etapas somam cerca de 7h35, sem contar atrasos extraordinarios do cluster.

`http-status-code` nao entra no dataset clean desta campanha porque o filtro de qualidade o exclui por definicao. Ele deve ser tratado como conjunto separado de robustez.

## Preparacao e execucao

A partir da raiz do repositorio:

```bash
PYTHONPATH=. .venv/bin/python \
  experiment/eval/memory_hog/run_balanced_campaign.py --stage prepare
```

A preparacao valida 42 celulas na etapa 1 e 17 na etapa 2, e grava as configuracoes geradas em `out/balanced_campaign/`.

Para executar somente a cobertura:

```bash
PYTHONPATH=. .venv/bin/python \
  experiment/eval/memory_hog/run_balanced_campaign.py --stage 1
```

Depois, para executar o reforco:

```bash
PYTHONPATH=. .venv/bin/python \
  experiment/eval/memory_hog/run_balanced_campaign.py --stage 2
```

Para executar as duas etapas em sequencia:

```bash
PYTHONPATH=. .venv/bin/python \
  experiment/eval/memory_hog/run_balanced_campaign.py --stage all
```

Use `--skip-prime` apenas quando os templates Argo ja estiverem disponiveis no cluster. O script usa `--retry-failed`, portanto runs completos presentes no ledger separado sao preservados e falhas podem ser tentadas novamente.

## Pos-execucao

Depois da campanha, reconstruir os candidatos a grafos a partir do banco/object store e mesclar por `run_id`, preservando o dataset anterior. Em seguida executar:

```bash
PYTHONPATH=. .venv/bin/python experiment/eval/gat/quality_filter.py --save
```

Registrar neste diario o numero de runs completos, falhos, grafos construidos e classificacoes `clean`, `warning` e `excluded`.
