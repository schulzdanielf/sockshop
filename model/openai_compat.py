"""Helpers for exposing the local Qwen server as an OpenAI-style chat API."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable, List


def _part_to_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if part.get("type") == "text":
            text = part.get("text")
            return text if isinstance(text, str) else ""
    return ""


def message_content_to_text(content: Any) -> str:
    """Flatten OpenAI-style message content into plain text.

    Supports:
    * ``{"content": "..."}``
    * multimodal-like ``[{"type": "text", "text": "..."}, ...]``
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [_part_to_text(part) for part in content]
        return "\n".join(p for p in parts if p).strip()
    return ""


def render_chat_messages(messages: Iterable[Dict[str, Any]]) -> str:
    """Convert chat messages into a compact instruction prompt for Qwen.

    The local model is prompt-based, so the chat envelope is collapsed into a
    deterministic transcript. We keep the format intentionally small because
    the main target is a constrained local model.
    """
    blocks: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip().lower() or "user"
        text = message_content_to_text(msg.get("content"))
        if not text:
            continue
        blocks.append(f"[{role.upper()}]\n{text}")
    if not blocks:
        return "[USER]\nRespond with a concise answer.\n[ASSISTANT]\n"
    return "\n\n".join(blocks) + "\n\n[ASSISTANT]\n"


def normalise_stop(stop: Any) -> List[str]:
    """Return a list of non-empty stop strings."""
    if isinstance(stop, str):
        return [stop] if stop else []
    if isinstance(stop, list):
        return [str(item) for item in stop if isinstance(item, str) and item]
    return []


def apply_stop_sequences(text: str, stop: Any) -> str:
    """Truncate output at the earliest provided stop sequence."""
    cut_positions = [text.find(marker) for marker in normalise_stop(stop)]
    cut_positions = [pos for pos in cut_positions if pos >= 0]
    if not cut_positions:
        return text
    return text[: min(cut_positions)]


def build_chat_completion_response(
    *,
    content: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str = "stop",
) -> Dict[str, Any]:
    """Build an OpenAI-compatible ``/v1/chat/completions`` response body."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": max(0, int(prompt_tokens)),
            "completion_tokens": max(0, int(completion_tokens)),
            "total_tokens": max(0, int(prompt_tokens))
            + max(0, int(completion_tokens)),
        },
    }
