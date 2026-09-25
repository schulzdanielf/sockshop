# Plano de observabilidade LLM e MCP

## Diagnóstico atual

- O fluxo `POST /api/runs/{run_id}/llm-analysis` já possui um span raiz e spans para RAG, montagem do prompt, chamada LLM, parse e persistência.
- O cliente MCP agora cria um span `gen_ai.tool_call` por chamada, com `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, endpoint, chaves dos argumentos, resultado, erro e duração. Payloads de argumentos não são registrados.
- O cliente LLM e o Qwen local usam o contrato OpenAI-compatible `/v1/chat/completions`; `/generate` permanece apenas como compatibilidade legada.
- A propagação W3C para chamadas HTTP do cliente LLM já existe quando a API OTel está disponível.
- Tokens de entrada e saída reais agora são extraídos pelo tokenizer ExLlamaV2 do Qwen e transportados pelo `/generate`; o cliente também normaliza usage de OpenAI/Gemini. `max_new_tokens` continua sendo limite solicitado, não consumo.
- Decisões do modelo, parse, retry e validadores aparecem parcialmente em eventos/logs; a decisão final agora tem o span `agent.decision`, com fontes, confiança, citações e overrides, sem chain-of-thought.
- A auditoria JSONL agora segue a mesma política de captura: por padrão armazena tamanho/hash, e só armazena conteúdo com configuração explícita.
- O bootstrap é best-effort e desliga o SDK quando o collector está inacessível, o que evita impacto no experimento, mas precisa ser visível por métrica/log de estado.

## Plano incremental

1. **Fronteiras e privacidade**: manter spans HTTP, LLM e MCP; remover conteúdo de eventos quando a captura não estiver autorizada. Feito parcialmente nesta etapa.
2. **Contrato de tokens**: extrair `gen_ai.usage.input_tokens` e `gen_ai.usage.output_tokens` de OpenAI/Gemini e do Qwen local. **Implementado nesta etapa.** Emitir a métrica `gen_ai.client.token.usage` com `token.type`.
3. **Contrato de tool calls**: adicionar resultado/error sem payload, timeout, retry, janela temporal e correlação com `rca.run_id`; propagar contexto para o MCP server quando o transporte suportar.
4. **Decisões e agentes**: criar spans/eventos para seleção de tool, decisão de continuar/parar, parse, retry e cada validator, usando atributos de dimensão, regra, confiança e resultado. O span final `agent.decision` já foi implementado; seleção de tool e continuidade ainda faltam. Ainda não há spans por especialista independente. No piloto multiagente, cada especialista precisa de span filho com `rca.agent.name`, status/cobertura, quantidade de tools, latência e tokens; o sintetizador deve registrar quais evidências aceitou ou marcou como conflito. Não registrar cadeia de pensamento; apenas resumo operacional e hashes.
5. **Correlação de experimento**: padronizar `rca.run_id`, `rca.experiment_id`, `rca.experiment_version`, `rca.analysis_id` e `rca.agent.name` em todos os spans e logs.
6. **Métricas operacionais**: duração, contagem, erro, retry, tokens, tools por análise e overrides; adicionar cardinalidade controlada e testes de contrato.
7. **Exportação e operação**: validar OTLP traces/metrics/logs no collector, dashboards e retenção; testar o comportamento com collector indisponível sem esconder a degradação da telemetria.

## Critérios de aceite

- Uma análise pode ser seguida por `trace_id` desde HTTP até RAG, LLM, parse, validators e MCP.
- Cada chamada MCP mostra tool, janela, duração, status e erro sem expor argumentos sensíveis por padrão.
- O painel distingue tokens solicitados, tokens medidos e tokens estimados.
- Uma decisão mostra o resultado final e suas fontes operacionais sem armazenar chain-of-thought.
- Testes verificam atributos, eventos, privacidade e propagação de erro sem exigir um collector ativo.

## Próximo incremento

Antes da primeira comparação single-agent vs multiagente, adicionar telemetria por especialista e por síntese: início/fim, agente, janela, cobertura, ferramentas chamadas, tokens medidos/estimados, erro e motivo de parada. Correlacionar spans sob o `run_id` e manter o conteúdo das evidências redigido por padrão. A separação de latência prefill/decode continua posterior, quando o runtime ExLlamaV2 expuser essa API; até lá, marcar como indisponível em vez de estimar como medida real.