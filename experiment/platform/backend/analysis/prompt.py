"""4k‑token prompt assembler for the Qwen 14B reasoning step.

Inputs come from ``GET /api/runs/{id}/rag-context``: a target L2 summary
and up to K neighbour summaries with similarity scores. We assemble a
prompt that:

* Sets a system role for resilience analysis.
* Embeds the target run with its tags.
* Embeds the retrieved neighbours numbered ``[n1]``, ``[n2]``… so the
  LLM can cite them.
* Asks for a structured JSON answer (``verdict``, ``confidence``,
  ``reasoning``, ``citations``).

Token accounting is intentionally a cheap heuristic (chars/4) — Qwen's
real tokenizer lives in a different process and we just need to stay
safely under the 4k context window with headroom for ``max_new_tokens``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .system_card import render_system_card


SYSTEM_PROMPT = (
    "You are a senior resilience engineer reviewing a chaos engineering "
    "run on a microservices system. You receive: (1) a system card with "
    "the topology and role of each service; (2) a structured summary of "
    "the target run; (3) up to K summaries of past runs retrieved by "
    "semantic similarity. Each past run has a citation id like [n1], "
    "[n2]. Always cite the runs you used. Identify the most likely "
    "root-cause service (`rca`) and fault category using ONLY the "
    "allowed vocabularies given in the system card; use `unknown` if "
    "you cannot decide. Answer with EXACTLY ONE JSON object and NOTHING "
    "ELSE — no code fences, no prose before or after, no second copy."
)

ANSWER_SCHEMA = (
    "{\n"
    '  "verdict": "resilient|degraded_recoverable|degraded_persistent",\n'
    '  "rca": "<service name from allowed_rca_targets>",\n'
    '  "fault_category": "<value from allowed_fault_categories>",\n'
    '  "confidence": 0.0,\n'
    '  "reasoning": "short paragraph",\n'
    '  "citations": ["n1", "n2"],\n'
    '  "follow_ups": ["actionable suggestion", "..."]\n'
    "}"
)


def estimate_tokens(text: str) -> int:
    """Cheap heuristic — ~4 chars per token works for English/markdown."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    # 4 chars per token, keep a small slack
    max_chars = max(0, max_tokens * 4 - 8)
    return text[:max_chars].rstrip() + "\n…[truncated]"


def assemble_prompt(
    rag_context: Dict[str, Any],
    *,
    budget_tokens: int = 3200,
    max_neighbours: int = 3,
    target_share: float = 0.45,
    system_id: Optional[str] = "sock-shop",
) -> Tuple[str, Dict[str, Any]]:
    """Build the final prompt string plus metadata.

    Parameters
    ----------
    rag_context : payload returned by ``/api/runs/{id}/rag-context``.
    budget_tokens : hard cap for the prompt body (excludes system + schema).
        Default 3200 leaves ~900 tokens for the LLM answer inside a 4k window.
    max_neighbours : at most this many neighbours are embedded.
    target_share : fraction of the budget reserved for the target summary.
    system_id : if a system card exists under ``data/system_cards/<id>.yaml``
        it is rendered and injected into the system prompt. ``None`` skips
        injection.
    """
    target = rag_context.get("target") or {}
    neighbours = (rag_context.get("neighbours") or [])[:max_neighbours]

    system_card_text = render_system_card(system_id) if system_id else ""

    target_text = target.get("summary_text") or ""
    target_budget = int(budget_tokens * target_share)
    target_text = _truncate_to_tokens(target_text, target_budget)

    remaining = max(0, budget_tokens - estimate_tokens(target_text))
    per_neighbour = (remaining // max(1, len(neighbours))) if neighbours else 0

    neighbour_blocks: List[str] = []
    citations: List[Dict[str, Any]] = []
    for idx, n in enumerate(neighbours, start=1):
        cite = f"n{idx}"
        text = _truncate_to_tokens(n.get("summary_text") or "", per_neighbour)
        block = (
            f"### Past run [{cite}] (run_id={n.get('run_id')}, "
            f"score={n.get('score')}, verdict={n.get('verdict')})\n{text}"
        )
        neighbour_blocks.append(block)
        citations.append({
            "ref": cite,
            "run_id": n.get("run_id"),
            "score": n.get("score"),
            "verdict": n.get("verdict"),
        })

    tags = ", ".join(target.get("tags") or [])
    target_block = (
        f"## Target run\n"
        f"run_id: {target.get('run_id')}\n"
        f"tags: {tags or 'n/a'}\n\n"
        f"{target_text}"
    )

    body_parts: List[str] = [target_block]
    if neighbour_blocks:
        body_parts.append("\n## Retrieved past runs\n" + "\n\n".join(neighbour_blocks))
    body = "\n\n".join(body_parts)

    prompt = (
        f"<system>{SYSTEM_PROMPT}</system>\n\n"
        + (f"<system_card>\n{system_card_text}\n</system_card>\n\n"
           if system_card_text else "")
        + f"<context>\n{body}\n</context>\n\n"
        + f"<task>\nClassify the target run and explain the call. "
          f"Reference past runs by their citation id (e.g. [n1]).\n"
          f"Respond ONLY with a JSON object following this schema:\n"
          f"{ANSWER_SCHEMA}\n</task>"
    )

    meta = {
        "prompt_tokens_estimate": estimate_tokens(prompt),
        "target_tokens_estimate": estimate_tokens(target_text),
        "neighbour_count": len(neighbour_blocks),
        "citations": citations,
        "budget_tokens": budget_tokens,
        "system_card_id": system_id if system_card_text else None,
        "system_card_tokens_estimate": (
            estimate_tokens(system_card_text) if system_card_text else 0
        ),
    }
    return prompt, meta
