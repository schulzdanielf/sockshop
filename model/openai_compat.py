"""Helpers for exposing the local Qwen server as an OpenAI-style chat API."""
from __future__ import annotations

import time
import uuid
import json
from typing import Any, Dict, Iterable, List, Optional


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


def _tool_functions(tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    functions: Dict[str, Dict[str, Any]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            functions[name] = function
    return functions


def tool_choice_function_name(tool_choice: Any) -> Optional[str]:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None
    function = tool_choice.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    return name if isinstance(name, str) and name else None


def tool_choice_requires_call(tool_choice: Any) -> bool:
    return tool_choice == "required" or tool_choice_function_name(tool_choice) is not None


def validate_tool_request(
    tools: Optional[List[Dict[str, Any]]], tool_choice: Any
) -> None:
    if isinstance(tool_choice, str) and tool_choice not in {
        "auto",
        "none",
        "required",
    }:
        raise ValueError("tool_choice must be auto, none, required, or a function")
    if not isinstance(tool_choice, (str, dict)):
        raise ValueError("tool_choice must be auto, none, required, or a function")

    functions = _tool_functions(tools)
    if tools and len(functions) != len(tools):
        raise ValueError("each tool must define a unique function name")
    for function in functions.values():
        parameters = function.get("parameters", {"type": "object"})
        if not isinstance(parameters, dict) or parameters.get("type", "object") != "object":
            raise ValueError("function parameters must be a JSON object schema")

    required_name = tool_choice_function_name(tool_choice)
    if isinstance(tool_choice, dict) and required_name is None:
        raise ValueError("tool_choice function must include a name")
    if required_name and required_name not in functions:
        raise ValueError("tool_choice references a function not present in tools")
    if tool_choice == "required" and not functions:
        raise ValueError("tool_choice=required needs at least one function tool")


def render_chat_messages(
    messages: Iterable[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
    parallel_tool_calls: bool = False,
) -> str:
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
        tool_calls = msg.get("tool_calls")
        if role == "tool":
            name = str(msg.get("name") or "unknown")
            call_id = str(msg.get("tool_call_id") or "")
            label = f"[TOOL RESULT name={name} id={call_id}]"
            if text:
                blocks.append(f"{label}\n{text}")
            continue
        if isinstance(tool_calls, list) and tool_calls:
            call_text = json.dumps(tool_calls, ensure_ascii=False)
            content = f"{text}\n" if text else ""
            blocks.append(f"[ASSISTANT]\n{content}[TOOL CALLS]\n{call_text}")
            continue
        if not text:
            continue
        blocks.append(f"[{role.upper()}]\n{text}")

    functions = _tool_functions(tools)
    if functions and tool_choice != "none":
        choice_name = tool_choice_function_name(tool_choice)
        instructions = {
            "available_tools": list(functions.values()),
            "tool_choice": tool_choice,
            "parallel_tool_calls": parallel_tool_calls,
            "response_contract": {
                "tool_call": {
                    "tool_calls": [
                        {"name": "function_name", "arguments": {"arg": "value"}}
                    ]
                },
                "rules": [
                    "Only call a function listed in available_tools.",
                    "Arguments must be a JSON object matching that function's parameters.",
                    "Do not invent tool results; the host executes calls and returns results.",
                    "If no tool is needed and tool_choice is auto, answer normally.",
                ],
            },
        }
        if tool_choice == "required" or choice_name:
            instructions["response_contract"]["rules"].append(
                "A tool call is mandatory. Return only one JSON object with "
                "tool_calls; do not answer in natural language."
            )
        if choice_name:
            instructions["required_function"] = choice_name
        blocks.append(
            "[AVAILABLE TOOLS]\n"
            + json.dumps(instructions, ensure_ascii=False, separators=(",", ":"))
        )

    if not blocks:
        return "[USER]\nRespond with a concise answer.\n[ASSISTANT]\n"
    return "\n\n".join(blocks) + "\n\n[ASSISTANT]\n"


def parse_tool_call_output(
    text: str,
    tools: Optional[List[Dict[str, Any]]],
    *,
    tool_choice: Any = "auto",
    parallel_tool_calls: bool = False,
) -> Optional[List[Dict[str, Any]]]:
    """Translate Qwen's constrained JSON response to OpenAI tool-call objects."""
    functions = _tool_functions(tools)
    if not functions or tool_choice == "none":
        return None

    try:
        payload = json.loads((text or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    if len(raw_calls) > 8 or (not parallel_tool_calls and len(raw_calls) != 1):
        return None

    required_name = tool_choice_function_name(tool_choice)

    calls = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            return None
        function_call = raw_call.get("function")
        if isinstance(function_call, dict):
            name = function_call.get("name")
            arguments = function_call.get("arguments")
        else:
            name = raw_call.get("name")
            arguments = raw_call.get("arguments")
        if not isinstance(name, str) or name not in functions:
            return None
        if required_name and name != required_name:
            return None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, dict):
            return None

        parameters = functions[name].get("parameters") or {}
        if not isinstance(parameters, dict):
            return None
        properties = parameters.get("properties") or {}
        required = parameters.get("required") or []
        if not isinstance(properties, dict) or not isinstance(required, list):
            return None
        if any(key not in arguments for key in required):
            return None
        if parameters.get("additionalProperties") is False and any(
            key not in properties for key in arguments
        ):
            return None

        calls.append(
            {
                "id": str(raw_call.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )

    return calls


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
    content: Optional[str],
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str = "stop",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build an OpenAI-compatible ``/v1/chat/completions`` response body."""
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": None if tool_calls else content,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
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
