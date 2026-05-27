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

import re
from typing import Any, Dict, List, Optional, Tuple

from .system_card import render_system_card


# Regex that matches " | chaos_type: `<value>`" in the L2 summary second line.
# We strip this segment from the TARGET summary before sending to the LLM so
# the model must infer the fault category from metrics/traces alone.
_CHAOS_TYPE_RE = re.compile(r'\s*\|\s*chaos_type:\s*`[^`]*`')

# Regex that strips "(experiment <experiment_id>)" from the H1 line of the
# summary.  The experiment_id encodes both the fault short-name
# (memhog / cpuhog / poddel) and the target service, so it must be removed
# from the TARGET block to prevent direct ground-truth leakage.
# Example: "# Run run-abc123 (experiment cpuhog-orders-r0-20260523-194346)"
#       →  "# Run run-abc123"
_EXPERIMENT_ID_RE = re.compile(r'\s*\(experiment\s+[^)]+\)')

# Regex that strips ", affected_services=[...]" from the Traces line.
# The list almost always starts with the chaos-injected service, giving the
# LLM the answer to RCA. We keep the trace/error counts intact.
_AFFECTED_SERVICES_RE = re.compile(r',\s*affected_services=\[[^\]]*\]')

# Regex that removes whole H2 sections that name services directly:
#   - "## Top failure signatures" — each line cites the affected service.
#   - "## Propagation graph"      — cascade order + edges expose RCA.
# Matches from the H2 header to (but not including) the next H2 or EOF.
_SERVICE_SECTIONS_RE = re.compile(
    r'\n## (?:Top failure signatures|Propagation graph)\n[\s\S]*?(?=\n## |\Z)'
)


def _mask_target_summary(text: str) -> str:
    """Remove ground-truth signals from the L2 summary shown to the LLM.

    Several leakage vectors are neutralised in the TARGET block only.
    Neighbour summaries keep all fields intact — they are the reference
    corpus the LLM cites.

    1. ``chaos_type: `...``` on the Verdict header line — directly names
       the injected fault category.
    2. ``(experiment <id>)`` on the H1 title line — encodes fault short-name
       and target service (e.g. ``cpuhog-orders-r0-...``).
    3. ``affected_services=[...]`` on the Traces line — list usually begins
       with the chaos-injected service.
    4. ``## Top failure signatures`` section — each bullet cites the
       affected service by name.
    5. ``## Propagation graph`` section — cascade order + edges expose the
       epicentre service and full topology.
    """
    text = _CHAOS_TYPE_RE.sub('', text)
    text = _EXPERIMENT_ID_RE.sub('', text)
    text = _AFFECTED_SERVICES_RE.sub('', text)
    text = _SERVICE_SECTIONS_RE.sub('', text)
    return text


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
    target_text = _mask_target_summary(target_text)
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

    # Strip tags that would leak the injected fault or target service to the
    # LLM — those are ground-truth labels and must not appear in the target
    # block. Neighbours keep their full tags (they are the reference corpus).
    _LEAKAGE_PREFIXES = ("chaos:", "svc:", "fault_category:")
    tags_visible = [
        t for t in (target.get("tags") or [])
        if not any(t.startswith(p) for p in _LEAKAGE_PREFIXES)
    ]
    tags = ", ".join(tags_visible)
    target_block = (
        f"## Target run\n"
        f"run_id: {target.get('run_id')}\n"
        f"tags: {tags or 'n/a'}\n\n"
        f"{target_text}"
    )

    body_parts: List[str] = [target_block]
    if neighbour_blocks:
        body_parts.append(
            "\n## Retrieved past runs\n" + "\n\n".join(neighbour_blocks)
        )
        task_instruction = (
            "Classify the target run and explain the call. "
            "Reference past runs by their citation id (e.g. [n1])."
        )
    else:
        # No historical runs were retrieved (e.g. S0_no_rag baseline or empty
        # corpus). Without an explicit placeholder the model tends to invent
        # a fake ``## Previous runs`` block instead of producing the JSON
        # answer. Make the absence explicit and remove the citation request.
        body_parts.append(
            "\n## Retrieved past runs\n"
            "(none — no historical runs are provided for this analysis)"
        )
        task_instruction = (
            "Classify the target run based ONLY on the target summary and "
            "system card above. No past runs are available, so do not cite "
            "any and leave the ``citations`` list empty."
        )
    body = "\n\n".join(body_parts)

    prompt = (
        f"<system>{SYSTEM_PROMPT}</system>\n\n"
        + (f"<system_card>\n{system_card_text}\n</system_card>\n\n"
           if system_card_text else "")
        + f"<context>\n{body}\n</context>\n\n"
        + f"<task>\n{task_instruction}\n"
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
