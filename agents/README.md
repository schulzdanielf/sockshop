# Multi-agent RCA

This branch treats the observability MCP server and the platform analysis layer as
the product path. GAT/MLP code under `experiment/eval/gat/` is retained only as
an offline baseline and must not be a runtime dependency of the agents.

## Runtime layers

1. `mcp-observability-server/`: window-scoped Prometheus, Loki and Tempo tools.
2. `model/react_agent.py`: tool execution loop for local or OpenAI-compatible LLMs.
3. `experiment/platform/backend/analysis/`: LLM client, RCA schema, validators and
   ranking helpers used by the platform API.
4. `agents/`: future investigation policies, orchestration and structured outputs.

The investigation agent should receive a run id and explicit baseline/fault/recovery
windows, use a deterministic anomaly ranking as a prior, and verify the hypothesis
with MCP tools before returning an RCA. It must return service, fault category,
confidence, evidence references and missing evidence in a machine-readable shape.

## Deliberate boundaries

- Chaos generation and overnight campaigns stay under `experiment/eval/memory_hog/`.
- Dataset builders and GAT/MLP evaluations stay offline under `experiment/eval/gat/`.
- The agent does not import the campaign runner or the PyTorch models.
- MCP investigator tools remain small, window-bounded and independently testable.

## Cleanup rule

Do not delete historical runs to make the branch look clean. Keep run-level evidence
in the platform object store or an external archive, keep normalized summaries in a
versioned manifest, and regenerate derived datasets and reports from those records.
