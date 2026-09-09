"""Interactive command-line interface for observability agent tools.

This interface supports two modes:
1) Model-driven loop: the LLM chooses which tool to call, reads the output,
   and then produces the final answer.
2) Deterministic fallback: direct intent routing when the LLM is unavailable.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .analysis.llm_client import LLMClientError, call_llm_messages
from .metrics_smoke_agent import (
    PrometheusClient,
    check_http_error_rate_top,
    check_oom_kills,
    check_pod_restarts,
    check_request_latency_top,
    collect_snapshot,
    default_window,
    fallback_assessment,
    get_cpu_saturation_top,
    get_memory_saturation_top,
)


@dataclass(frozen=True)
class ParsedRequest:
    intent: str
    service: Optional[str] = None


_TOOL_INTENTS = {
    "overall",
    "http_error_rate",
    "latency",
    "memory",
    "cpu",
    "restarts",
    "oom",
}

_NO_DATA_MARKER = "Nenhum dado"


def _short(text: str, limit: int = 280) -> str:
    s = (text or "").replace("\n", "\\n").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _observation_state(observation: str) -> str:
    if observation.startswith("Falha na consulta da tool:"):
        return "tool_error"
    if _NO_DATA_MARKER in observation:
        return "no_data"
    return "ok"


def _normalise_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _extract_service(text: str) -> Optional[str]:
    tokens = text.split()
    markers = {"serviço", "servico", "service"}
    for idx, token in enumerate(tokens):
        if token in markers and idx + 1 < len(tokens):
            return tokens[idx + 1].strip(".,;:!?()[]{}\"")
    if len(tokens) >= 2 and tokens[0] in {
        "erro",
        "erros",
        "latencia",
        "latência",
        "cpu",
        "memoria",
        "memória",
        "oom",
        "restart",
        "restarts",
    }:
        return tokens[1].strip(".,;:!?()[]{}\"")
    return None


def parse_request(text: str) -> ParsedRequest:
    q = _normalise_text(text)
    service = _extract_service(q)

    if any(word in q for word in ("sair", "exit", "quit")):
        return ParsedRequest("exit")
    if any(word in q for word in ("ajuda", "help", "comandos")):
        return ParsedRequest("help")
    if "status" in q or "situação" in q or "situacao" in q or "geral" in q:
        return ParsedRequest("overall")
    if "erro" in q or "5xx" in q:
        return ParsedRequest("http_error_rate", service)
    if "latencia" in q or "latência" in q or "p95" in q:
        return ParsedRequest("latency", service)
    if "memoria" in q or "memória" in q:
        return ParsedRequest("memory", service)
    if "cpu" in q:
        return ParsedRequest("cpu", service)
    if "restart" in q or "reinicio" in q or "reinício" in q:
        return ParsedRequest("restarts", service)
    if "oom" in q:
        return ParsedRequest("oom", service)
    return ParsedRequest("unknown")


def _pick_service(top: Any, service: Optional[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(top, list) or not top:
        return None
    if not service:
        return top[0] if isinstance(top[0], dict) else None
    service_l = service.lower()
    for row in top:
        if not isinstance(row, dict):
            continue
        name = str(row.get("service") or "").lower()
        if name == service_l:
            return row
    for row in top:
        if not isinstance(row, dict):
            continue
        name = str(row.get("service") or "").lower()
        if service_l in name:
            return row
    return None


def _fmt_tool_error(payload: Dict[str, Any]) -> str:
    return f"Falha na consulta da tool: {payload.get('error') or 'erro desconhecido'}"


def _run_http_error(prometheus: Any, *, start: str, end: str, service: Optional[str]) -> str:
    result = check_http_error_rate_top(prometheus, start=start, end=end, step="15s", k=10)
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de taxa de erro encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    mean_pct = float(row.get("error_rate_mean_pct") or 0.0)
    max_pct = float(row.get("error_rate_max_pct") or 0.0)
    return (
        f"Tool: check_http_error_rate_top\n"
        f"Serviço: {svc}\n"
        f"Taxa média de erro 5xx: {mean_pct:.2f}%\n"
        f"Taxa máxima de erro 5xx: {max_pct:.2f}%"
    )


def _run_latency(prometheus: Any, *, start: str, end: str, service: Optional[str]) -> str:
    result = check_request_latency_top(prometheus, start=start, end=end, step="15s", k=10)
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de latência encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    mean_s = float(row.get("latency_p95_mean_seconds") or 0.0)
    max_s = float(row.get("latency_p95_max_seconds") or 0.0)
    return (
        f"Tool: check_request_latency_top\n"
        f"Serviço: {svc}\n"
        f"Latência P95 média: {mean_s:.3f}s\n"
        f"Latência P95 máxima: {max_s:.3f}s"
    )


def _run_memory(prometheus: Any, *, start: str, end: str, namespace: str, service: Optional[str]) -> str:
    result = get_memory_saturation_top(
        prometheus, start=start, end=end, namespace=namespace, step="15s", k=10
    )
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de memória encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    mean_pct = float(row.get("saturation_mean_pct") or 0.0)
    max_pct = float(row.get("saturation_max_pct") or 0.0)
    return (
        f"Tool: get_memory_saturation_top\n"
        f"Serviço: {svc}\n"
        f"Saturação média de memória: {mean_pct:.2f}%\n"
        f"Saturação máxima de memória: {max_pct:.2f}%"
    )


def _run_cpu(prometheus: Any, *, start: str, end: str, namespace: str, service: Optional[str]) -> str:
    result = get_cpu_saturation_top(
        prometheus, start=start, end=end, namespace=namespace, step="15s", k=10
    )
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de CPU encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    mean_pct = float(row.get("saturation_mean_pct") or 0.0)
    max_pct = float(row.get("saturation_max_pct") or 0.0)
    return (
        f"Tool: get_cpu_saturation_top\n"
        f"Serviço: {svc}\n"
        f"Saturação média de CPU: {mean_pct:.2f}%\n"
        f"Saturação máxima de CPU: {max_pct:.2f}%"
    )


def _run_restarts(prometheus: Any, *, start: str, end: str, namespace: str, service: Optional[str]) -> str:
    result = check_pod_restarts(prometheus, start=start, end=end, namespace=namespace, step="15s")
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de restarts encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    restarts = int(row.get("restarts_in_window") or 0)
    return (
        f"Tool: check_pod_restarts\n"
        f"Serviço: {svc}\n"
        f"Restarts na janela: {restarts}"
    )


def _run_oom(prometheus: Any, *, start: str, end: str, namespace: str, service: Optional[str]) -> str:
    result = check_oom_kills(prometheus, start=start, end=end, namespace=namespace, step="15s")
    if result.get("error"):
        return _fmt_tool_error(result)
    row = _pick_service(result.get("top"), service)
    if row is None:
        return "Nenhum dado de OOM encontrado para o serviço solicitado."
    svc = row.get("service") or service or "desconhecido"
    delta = float(row.get("delta") or 0.0)
    transitioned = bool(row.get("transitioned_in_window"))
    return (
        f"Tool: check_oom_kills\n"
        f"Serviço: {svc}\n"
        f"OOM no período: {'sim' if transitioned else 'não'}\n"
        f"Delta do sinal OOM: {delta:.2f}"
    )


def _run_overall(prometheus: Any, *, start: str, end: str, namespace: str) -> str:
    snapshot = collect_snapshot(prometheus, start=start, end=end, namespace=namespace, verbose=False)
    assessment = fallback_assessment(snapshot)
    return (
        "Tool chain: collect_snapshot + fallback_assessment\n"
        f"Situação geral: {assessment.get('status') or 'unknown'}\n"
        f"Resumo: {assessment.get('summary') or 'Sem resumo'}\n"
        f"Sinais: {', '.join(assessment.get('evidence') or []) or 'nenhum'}\n"
        f"Serviços: {', '.join(assessment.get('services') or []) or 'nenhum'}\n"
        f"Próximos passos: {', '.join(assessment.get('actions') or []) or 'continuar monitorando'}"
    )


def _help_text() -> str:
    return (
        "Comandos de exemplo:\n"
        "- qual a taxa de erros do serviço orders\n"
        "- latencia do serviço carts\n"
        "- memoria do serviço shipping\n"
        "- cpu do serviço front-end\n"
        "- restarts do serviço payment\n"
        "- oom do serviço orders\n"
        "- status geral\n"
        "- ajuda\n"
        "- sair"
    )


def _extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    depth = 0
    start = -1
    in_str = False
    escape = False
    for idx, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _validate_planner_action(payload: Any) -> Optional[Dict[str, str]]:
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action") or "").strip().lower()
    tool = str(payload.get("tool") or "").strip().lower()
    service = str(payload.get("service") or "").strip()
    answer = str(payload.get("answer") or "").strip()

    if action not in {"use_tool", "final"}:
        return None
    if action == "use_tool" and tool not in _TOOL_INTENTS:
        return None
    if action == "final" and not answer:
        return None
    return {
        "action": action,
        "tool": tool,
        "service": service,
        "answer": answer,
    }


def _repair_planner_messages(raw_plan: str, query: str) -> List[Dict[str, str]]:
    system = (
        "Converta a resposta em JSON válido com schema fixo. "
        "Retorne somente JSON, sem markdown e sem explicações."
    )
    payload = {
        "schema": {
            "action": "use_tool|final",
            "tool": "overall|http_error_rate|latency|memory|cpu|restarts|oom",
            "service": "opcional",
            "answer": "obrigatório quando action=final",
        },
        "user_query": query,
        "raw_planner_response": raw_plan,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _infer_tool_from_text(text: str, default_intent: str) -> str:
    t = (text or "").lower()
    if "erro" in t or "5xx" in t:
        return "http_error_rate"
    if "lat" in t or "p95" in t:
        return "latency"
    if "mem" in t:
        return "memory"
    if "cpu" in t:
        return "cpu"
    if "restart" in t or "rein" in t:
        return "restarts"
    if "oom" in t:
        return "oom"
    if default_intent in _TOOL_INTENTS:
        return default_intent
    return "overall"


def _fallback_final_answer(query: str, observations: List[Dict[str, str]]) -> str:
    if not observations:
        return "Resposta: não foi possível obter observações suficientes."
    last = observations[-1]
    return (
        f"Resposta: para '{query}', a última verificação foi {last.get('tool')}"
        f" no serviço {last.get('service') or '-'}\n"
        f"Evidência: {last.get('observation') or 'sem dados'}\n"
        "Próximo passo: ajustar coleta/labels e repetir a consulta."
    )


def _coerce_planner_action(
    raw_plan: str,
    *,
    query: str,
    llm: Callable[..., str],
    initial: ParsedRequest,
    observations: List[Dict[str, str]],
    max_new_tokens: int,
    verbose: bool,
) -> Dict[str, str]:
    direct = _validate_planner_action(_extract_first_json_object(raw_plan))
    if direct is not None:
        direct["source"] = "planner_json"
        return direct

    repaired_raw = llm(
        _repair_planner_messages(raw_plan, query),
        max_new_tokens=min(120, max_new_tokens),
    )
    repaired = _validate_planner_action(_extract_first_json_object(repaired_raw))
    if repaired is not None:
        repaired["source"] = "planner_repair_json"
        if verbose:
            print("[loop] planner_json repaired -> valid action")
        return repaired

    heur_action = "final" if observations and re.search(r"\bfinal\b", raw_plan.lower()) else "use_tool"
    heur_tool = _infer_tool_from_text(raw_plan, initial.intent)
    heur = {
        "action": heur_action,
        "tool": heur_tool,
        "service": initial.service or "",
        "answer": _fallback_final_answer(query, observations),
        "source": "planner_heuristic",
    }
    if verbose:
        print("[loop] planner_json heuristic fallback")
    validated = _validate_planner_action(heur)
    if validated is None:
        raise LLMClientError("planner returned invalid JSON action")
    validated["source"] = "planner_heuristic"
    return validated


def _execute_intent(
    intent: str,
    *,
    service: Optional[str],
    prometheus: Any,
    start: str,
    end: str,
    namespace: str,
) -> str:
    if intent == "overall":
        return _run_overall(prometheus, start=start, end=end, namespace=namespace)
    if intent == "http_error_rate":
        return _run_http_error(prometheus, start=start, end=end, service=service)
    if intent == "latency":
        return _run_latency(prometheus, start=start, end=end, service=service)
    if intent == "memory":
        return _run_memory(
            prometheus, start=start, end=end, namespace=namespace, service=service
        )
    if intent == "cpu":
        return _run_cpu(
            prometheus, start=start, end=end, namespace=namespace, service=service
        )
    if intent == "restarts":
        return _run_restarts(
            prometheus, start=start, end=end, namespace=namespace, service=service
        )
    if intent == "oom":
        return _run_oom(
            prometheus, start=start, end=end, namespace=namespace, service=service
        )
    return "Intent inválido para execução de tool."


def _planner_messages(
    query: str,
    observations: List[Dict[str, str]],
    *,
    default_service: Optional[str],
) -> List[Dict[str, str]]:
    system = (
        "Você é um planejador de tools para observabilidade. "
        "Escolha apenas uma próxima ação por vez. "
        "Responda com JSON único e nada além disso."
    )
    payload = {
        "task": "Escolher a próxima ação para responder o usuário com base em observações de tools.",
        "allowed_actions": ["use_tool", "final"],
        "allowed_tools": sorted(_TOOL_INTENTS),
        "schema": {
            "action": "use_tool|final",
            "tool": "overall|http_error_rate|latency|memory|cpu|restarts|oom",
            "service": "nome-do-servico-ou-vazio",
            "answer": "obrigatório quando action=final",
        },
        "user_query": query,
        "default_service": default_service or "",
        "observations": observations,
        "rules": [
            "Se não houver observações ainda, escolha action=use_tool.",
            "Use no máximo uma tool por ação.",
            "Quando já houver evidência suficiente, retorne action=final.",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _final_messages(query: str, observations: List[Dict[str, str]]) -> List[Dict[str, str]]:
    system = (
        "Você é um analista SRE. Responda em texto curto em português com: "
        "Resposta, Evidência, Próximo passo."
    )
    payload = {
        "user_query": query,
        "observations": observations,
        "rules": [
            "Não invente dados fora das observações.",
            "Se houver erro de coleta, deixe isso explícito.",
            "No máximo 6 linhas.",
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _answer_query_model_loop(
    query: str,
    *,
    prometheus: Any,
    start: str,
    end: str,
    namespace: str,
    llm: Callable[..., str],
    max_steps: int,
    max_new_tokens: int,
    verbose: bool,
) -> str:
    initial = parse_request(query)
    observations: List[Dict[str, str]] = []

    for step in range(1, max_steps + 1):
        raw_plan = llm(
            _planner_messages(
                query,
                observations,
                default_service=initial.service,
            ),
            max_new_tokens=max_new_tokens,
        )
        if verbose:
            print(
                f"[loop] step={step} planner_raw_len={len(raw_plan)} planner_raw_preview={_short(raw_plan)}"
            )

        plan = _coerce_planner_action(
            raw_plan,
            query=query,
            llm=llm,
            initial=initial,
            observations=observations,
            max_new_tokens=max_new_tokens,
            verbose=verbose,
        )

        action = plan["action"]
        source = plan.get("source") or "unknown"
        if verbose:
            print(
                f"[loop] step={step} planner_action={action} source={source}"
            )
        if action == "final":
            answer = plan["answer"]
            if answer:
                if verbose:
                    print(
                        f"[loop] step={step} final_answer_len={len(answer)} source={source}"
                    )
                if verbose and observations:
                    trace = "\n".join(
                        f"- step {obs['step']}: tool={obs['tool']} service={obs['service'] or '-'}"
                        for obs in observations
                    )
                    return f"{answer}\n\nRastro de tools:\n{trace}"
                return answer

        tool = plan["tool"]
        service = plan["service"] or initial.service

        if observations:
            last = observations[-1]
            if (
                last.get("tool") == tool
                and (last.get("service") or "") == (service or "")
                and _NO_DATA_MARKER in (last.get("observation") or "")
            ):
                answer = _fallback_final_answer(query, observations)
                if verbose:
                    print(
                        "[loop] repeated no-data tool call blocked; forcing final"
                    )
                return answer

        if verbose:
            print(
                f"[tool] step={step} start tool={tool} service={service or '-'}"
            )
        obs_text = _execute_intent(
            tool,
            service=service,
            prometheus=prometheus,
            start=start,
            end=end,
            namespace=namespace,
        )
        obs_state = _observation_state(obs_text)
        observations.append(
            {
                "step": str(step),
                "tool": tool,
                "service": service or "",
                "observation": obs_text,
            }
        )
        if verbose:
            print(
                f"[tool] step={step} end tool={tool} state={obs_state}"
            )
            print(
                f"[tool] step={step} observation_preview={_short(obs_text)}"
            )

        # Keep context compact for the constrained local model.
        if step >= 2:
            break

    raw_final = llm(
        _final_messages(query, observations),
        max_new_tokens=max_new_tokens,
    )
    if verbose:
        print(
            f"[loop] synthesis_final_raw_len={len(raw_final)} synthesis_final_raw_preview={_short(raw_final)}"
        )
        trace = "\n".join(
            f"- step {obs['step']}: tool={obs['tool']} service={obs['service'] or '-'}"
            for obs in observations
        )
        return f"{raw_final}\n\nRastro de tools:\n{trace}"
    return raw_final


def answer_query(
    query: str,
    *,
    prometheus: Any,
    start: str,
    end: str,
    namespace: str,
    use_model: bool = True,
    llm: Callable[..., str] = call_llm_messages,
    max_steps: int = 2,
    max_new_tokens: int = 180,
    verbose: bool = False,
) -> str:
    parsed = parse_request(query)
    if parsed.intent == "help":
        return _help_text()
    if parsed.intent == "exit":
        return "Encerrando interface interativa."

    if use_model:
        try:
            return _answer_query_model_loop(
                query,
                prometheus=prometheus,
                start=start,
                end=end,
                namespace=namespace,
                llm=llm,
                max_steps=max(1, int(max_steps)),
                max_new_tokens=max(64, int(max_new_tokens)),
                verbose=verbose,
            )
        except LLMClientError as exc:
            if verbose:
                print(f"[loop] llm_error={exc}")
            return f"Erro do modelo: {exc}"

    if parsed.intent == "overall":
        return _run_overall(prometheus, start=start, end=end, namespace=namespace)
    if parsed.intent == "http_error_rate":
        return _run_http_error(prometheus, start=start, end=end, service=parsed.service)
    if parsed.intent == "latency":
        return _run_latency(prometheus, start=start, end=end, service=parsed.service)
    if parsed.intent == "memory":
        return _run_memory(
            prometheus, start=start, end=end, namespace=namespace, service=parsed.service
        )
    if parsed.intent == "cpu":
        return _run_cpu(
            prometheus, start=start, end=end, namespace=namespace, service=parsed.service
        )
    if parsed.intent == "restarts":
        return _run_restarts(
            prometheus, start=start, end=end, namespace=namespace, service=parsed.service
        )
    if parsed.intent == "oom":
        return _run_oom(
            prometheus, start=start, end=end, namespace=namespace, service=parsed.service
        )
    return (
        "Não entendi o pedido. Digite 'ajuda' para ver exemplos de comandos.\n"
        + _help_text()
    )


def run_repl(
    *,
    minutes: int,
    namespace: str,
    use_model: bool,
    max_steps: int,
    max_new_tokens: int,
    verbose: bool,
) -> int:
    print("Interface do agente iniciada. Digite 'ajuda' para ver comandos.")
    prometheus = PrometheusClient()
    try:
        while True:
            query = input("agente> ").strip()
            if not query:
                continue
            start, end = default_window(minutes)
            reply = answer_query(
                query,
                prometheus=prometheus,
                start=start,
                end=end,
                namespace=namespace,
                use_model=use_model,
                max_steps=max_steps,
                max_new_tokens=max_new_tokens,
                verbose=verbose,
            )
            print(reply)
            if parse_request(query).intent == "exit":
                return 0
    except KeyboardInterrupt:
        print("\nEncerrando interface interativa.")
        return 0
    finally:
        prometheus.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--ask", nargs="+", default=None)
    parser.add_argument("--repl", action="store_true")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument("--max-new-tokens", type=int, default=180)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.ask and not args.repl:
        args.repl = True

    if args.repl:
        return run_repl(
            minutes=args.minutes,
            namespace=args.namespace,
            use_model=not args.no_model,
            max_steps=max(1, int(args.max_steps)),
            max_new_tokens=max(64, int(args.max_new_tokens)),
            verbose=args.verbose,
        )

    ask_text = " ".join(args.ask) if isinstance(args.ask, list) else (args.ask or "")
    start, end = default_window(args.minutes)
    prometheus = PrometheusClient()
    try:
        reply = answer_query(
            ask_text,
            prometheus=prometheus,
            start=start,
            end=end,
            namespace=args.namespace,
            use_model=not args.no_model,
            max_steps=max(1, int(args.max_steps)),
            max_new_tokens=max(64, int(args.max_new_tokens)),
            verbose=args.verbose,
        )
        print(reply)
        if (not args.no_model) and reply.startswith("Erro do modelo:"):
            return 3
        return 0
    finally:
        prometheus.close()


if __name__ == "__main__":
    raise SystemExit(main())
