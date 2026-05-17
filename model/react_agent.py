"""
ReAct agent loop for plain BaseLLM (no native tool calling required).

Compatible with LangChain 1.x, where create_react_agent and AgentExecutor
were removed.  Uses Qwen14BAPI (BaseLLM) via the local FastAPI server.

Usage::

    from model.react_agent import ReActAgent
    agent = ReActAgent(llm=llm, tools=tools)
    answer = await agent.ainvoke("Quais serviços estão com maior latência?")
"""

import json
import re
from typing import Optional

from langchain_core.language_models.llms import LLM
from langchain_core.prompts import PromptTemplate

# ---------------------------------------------------------------------------
# Stop sequences sent to the LLM to cut generation before hallucinated output.
# The model (Qwen2-14B) tends to write Observation: or continue in Chinese
# after the Action Input JSON — these stops prevent that at the token level.
# ---------------------------------------------------------------------------
_STOP_SEQUENCES = ["\nObservation:", "\nQuestion:"]

_REACT_TEMPLATE_STR = """\
Você é um agente SRE. Para qualquer pergunta sobre o ambiente, use SEMPRE as ferramentas. \
Nunca invente valores nem resultados de ferramentas.

IMPORTANTE: Escreva SOMENTE até o Action Input. Pare imediatamente após o JSON.
NÃO escreva Observation, NÃO escreva Final Answer ainda. O sistema executará a tool.

━━━ FERRAMENTAS DISPONÍVEIS ━━━
{tools_desc}

━━━ ROTEAMENTO OBRIGATÓRIO ━━━
1. Para perguntas sobre erros, latência, tráfego, CPU, memória dos serviços da aplicação:
   → Use SEMPRE query_golden_metric com um dos metric_name abaixo (valores exatos):
     • "Error Rate"      — taxa de erros por serviço
     • "Request Rate"    — throughput / tráfego (req/s)
     • "Latency P95"     — latência percentil 95
     • "Latency P99"     — latência percentil 99
     • "CPU Usage"       — uso de CPU
     • "Memory Usage"    — uso de memória

2. Para métricas de banco de dados ou consultas PromQL específicas:
   → Chame primeiro list_metrics para descobrir as métricas disponíveis.
   → Use prometheus_instant_query com a PromQL exata.

3. Para logs de erro ou anomalias nos logs:
   → Use as ferramentas Loki disponíveis.

4. Para rastreamento distribuído / traces:
   → Use tempo_search_traces.

━━━ FORMATO OBRIGATÓRIO ━━━
Escreva APENAS estas linhas e pare:
Thought: <raciocínio em 1 frase — qual tool e por quê>
Action: <nome_exato_da_ferramenta>
Action Input: <json com os argumentos>

Se já tiver a Observation e souber a resposta final, escreva:
Thought: <conclusão baseada na Observation>
Final Answer: <resposta objetiva para o SRE>

Question: {input}
{scratchpad}"""

_prompt = PromptTemplate.from_template(_REACT_TEMPLATE_STR)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_action(text: str) -> tuple[Optional[str], dict]:
    """Extract Action and Action Input from model output.

    Truncates at the first Action found to discard any hallucinated
    Observation / Final Answer the model may have written afterwards.
    """
    action_pos = re.search(r"\bAction:\s*(\S+)", text)
    if not action_pos:
        return None, {}

    tool_name = action_pos.group(1).strip()
    after_action = text[action_pos.start():]

    input_match = re.search(r"Action Input:\s*(\{.*?\})", after_action, re.DOTALL)
    tool_args: dict = {}
    if input_match:
        try:
            tool_args = json.loads(input_match.group(1))
        except json.JSONDecodeError:
            pass

    return tool_name, tool_args


def _parse_final_answer(text: str) -> Optional[str]:
    """Extract Final Answer only when no Action is present in the text.

    If both Action and Final Answer appear, Action takes priority — the model
    still wants to call a tool and the Final Answer is hallucinated.
    """
    if re.search(r"\bAction:\s*\S+", text):
        return None
    match = re.search(r"Final Answer:\s*(.+?)(?=\nThought:|\nAction:|\Z)", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _truncate_observation(observation: str, max_chars: int = 2000) -> str:
    """Truncate observation for the scratchpad to keep prompt size bounded.

    Tries to extract just the result payload from the MCP envelope
    ``{"content": [{"type": "text", "text": "..."}]}`` before falling back
    to a plain character truncation.
    """
    if len(observation) <= max_chars:
        return observation

    try:
        obj = json.loads(observation)
        content = obj.get("content", []) if isinstance(obj, dict) else []
        if content and isinstance(content, list):
            text_val = content[0].get("text", "") if isinstance(content[0], dict) else ""
            if text_val:
                if len(text_val) <= max_chars:
                    return text_val
                return text_val[:max_chars] + f"... [truncado: {len(text_val)} chars total]"
    except (json.JSONDecodeError, AttributeError):
        pass

    return observation[:max_chars] + f"... [truncado: {len(observation)} chars total]"


def _extract_text(raw) -> str:
    """Normalise LLM output to plain string regardless of LangChain return type."""
    if isinstance(raw, str):
        return raw
    return getattr(raw, "content", str(raw))


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ReActAgent:
    """ReAct agent loop compatible with plain BaseLLM.

    Drop-in replacement for ``AgentExecutor + create_react_agent`` for
    LangChain 1.x where those APIs were removed.  Works with any ``BaseLLM``
    that exposes ``ainvoke()``, including ``Qwen14BAPI``.

    Args:
        llm: Any LangChain BaseLLM instance.
        tools: List of LangChain ``Tool`` objects with async ``coroutine``.
        max_iterations: Maximum ReAct iterations before giving up.
        max_observation_chars: Maximum characters kept per observation in the
            scratchpad.  Prevents prompt from growing beyond ``max_seq_len``.
    """

    def __init__(
        self,
        llm: LLM,
        tools: list,
        max_iterations: int = 6,
        max_observation_chars: int = 800,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.max_observation_chars = max_observation_chars
        self._tools_by_name = {t.name: t for t in tools}
        self._tools_desc = "\n".join(f"- {t.name}: {t.description}" for t in tools)

    async def ainvoke(self, question: str, *, verbose: bool = False) -> str:
        """Run the ReAct loop and return the final answer string."""
        scratchpad = ""

        for i in range(self.max_iterations):
            prompt = _prompt.format(
                tools_desc=self._tools_desc,
                input=question,
                scratchpad=scratchpad,
            )

            # Pass stop sequences so the model halts before writing Observation
            # max_new_tokens=1024 to ensure Qwen3 has budget beyond <think> blocks
            raw = await self.llm.ainvoke(prompt, stop=_STOP_SEQUENCES, max_new_tokens=1024)
            response = _extract_text(raw)

            if verbose:
                print(f"\n> Iteração {i + 1}")
                print(response)

            # 1. Final Answer (only accepted when no Action is present)
            final = _parse_final_answer(response)
            if final:
                return final

            # 2. Extract Action — ignores everything written after the JSON
            tool_name, tool_args = _parse_action(response)
            if tool_name is None:
                return response.strip()

            if verbose:
                print(f"\n[Tool] {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")

            # 3. Execute the real tool
            observation = await self._call_tool(tool_name, tool_args)

            if verbose:
                preview = observation[:300]
                suffix = "..." if len(observation) > 300 else ""
                print(f"[Observation] {preview}{suffix}")

            # 4. Append to scratchpad with truncated observation
            obs_scratchpad = _truncate_observation(observation, self.max_observation_chars)
            thought_match = re.search(r"Thought:(.*?)(?=\nAction:|\Z)", response, re.DOTALL)
            thought_text = thought_match.group(1).strip() if thought_match else ""
            scratchpad += (
                f"Thought: {thought_text}\n"
                f"Action: {tool_name}\n"
                f"Action Input: {json.dumps(tool_args, ensure_ascii=False)}\n"
                f"Observation: {obs_scratchpad}\n"
            )

        return "[AVISO] Máximo de iterações atingido sem Final Answer."

    async def _call_tool(self, tool_name: str, tool_args: dict) -> str:
        if tool_name not in self._tools_by_name:
            available = list(self._tools_by_name)
            return f"[ERRO] Tool '{tool_name}' não encontrada. Disponíveis: {available}"
        try:
            return await self._tools_by_name[tool_name].coroutine(
                json.dumps(tool_args, ensure_ascii=False)
            )
        except Exception as exc:
            return f"[ERRO] {type(exc).__name__}: {exc}"
