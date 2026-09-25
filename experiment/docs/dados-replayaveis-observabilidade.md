# Dados replayáveis de observabilidade

## Decisão

Não usar um backup manual do diretório inteiro de Prometheus, Tempo e Loki como
fonte principal do experimento. Esses diretórios são grandes, dependem da versão
do storage e podem conter séries/logs de outros experimentos.

A unidade de retenção é um `run_id`. Cada run concluído deve preservar:

- metadados do experimento e janelas baseline/fault/recovery;
- `metrics_raw.json` e `run_features.json`;
- `traces_raw.json`, `trace_features.json` e o resumo do grafo;
- `istio_edges.json` e `load_summary.json`;
- `chaos_artifacts.json`, incluindo aplicação, injeção, parada e limpeza;
- logs Loki selecionados dentro das mesmas janelas, com query e labels usados;
- versão do serviço, configuração de telemetria e hash do cenário.

O objeto armazenado por run é a fonte de replay. `dataset.jsonl`, splits, relatórios
e índices de busca são derivados e podem ser recriados sem consultar os serviços
vivos.

O arquivador reproduzível está em
`experiment/platform/tools/archive_campaign.py`. Exemplo:

```bash
PYTHONPATH=. .venv/bin/python experiment/platform/tools/archive_campaign.py \
	--ledger experiment/eval/memory_hog/out/runs.csv \
	--object-store experiment/platform/data/object_store \
	--output archives/balanced-chaos-eval-20260923.tar.gz
```

Use `--dry-run` antes para verificar runs concluídos sem evidência. Runs falhos
podem aparecer no ledger sem bloquear o archive; a ausência fica registrada no
manifesto.

## Camadas de retenção

### 1. Hot

Manter no `platform.db` e object store local enquanto a campanha está ativa.
Usar um ledger separado por campanha, por exemplo:

```text
experiment_campaign_id=balanced-chaos-eval-20260923
```

### 2. Archive

Ao terminar uma campanha, criar um arquivo compactado por campanha contendo:

```text
manifest.json
runs.csv
objects/<run_id>/*.json
loki/<run_id>/*.jsonl
checksums.sha256
```

O `manifest.json` deve registrar commit, configuração, namespace, intervalos de
tempo, versões das imagens e contagens por status. O arquivo deve ser armazenado
fora de `experiment/platform/data/` antes de limpar o ambiente.

### 3. Derived

Gerar novamente, sempre que necessário:

- `dataset.jsonl`;
- `split_assignment.json`;
- `dataset_report.json`;
- relatórios de qualidade;
- resultados de GAT/MLP e avaliações dos agentes.

Não tratar esses arquivos como a única cópia dos dados brutos.

## Prometheus, Tempo e Loki

Para replay científico, prefira exportar respostas das queries usadas pelo run.
Isso é mais estável e menor que copiar os TSDBs completos. Um snapshot completo
dos volumes pode ser feito antes de uma migração de infraestrutura, mas é backup
de recuperação, não formato de dataset.

- Prometheus: guardar a resposta range-query, query, step e janela.
- Tempo: guardar ids, resultados da busca e traces completos usados na análise.
- Loki: guardar linhas retornadas, query, labels, limites e janela em JSONL.

Todas as consultas devem ser fechadas em `[start, end]`; nunca depender de uma
janela implícita como `increase(...[1h])` para reconstruir um experimento antigo.

## Estratégia para agentes

O agente investigativo consome o resumo normalizado e pode requisitar MCP ao vivo
quando o run está ativo. Para avaliação offline, ele deve receber os mesmos
payloads arquivados através de um adapter de replay que implemente os contratos
das investigator tools. Assim, a avaliação não muda porque Prometheus compactou
uma série ou Tempo expirou um trace.

O GAT/MLP continua como baseline offline. Ele não deve ser necessário para iniciar
ou executar o agente.
