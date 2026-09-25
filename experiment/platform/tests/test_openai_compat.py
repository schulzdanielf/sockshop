"""Tests for the local Qwen server's OpenAI tool-call compatibility."""

from __future__ import annotations

import json

from model.openai_compat import (
    build_chat_completion_response,
    parse_tool_call_output,
    render_chat_messages,
    tool_choice_requires_call,
    validate_tool_request,
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_cpu_saturation_top",
            "description": "Return top services by CPU saturation.",
            "parameters": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}},
                "required": ["namespace"],
                "additionalProperties": False,
            },
        },
    }
]


def test_render_chat_messages_includes_tools_and_prior_tool_results():
    prompt = render_chat_messages(
        [
            {"role": "user", "content": "Qual serviço teve maior CPU?"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_cpu_saturation_top",
                            "arguments": '{"namespace":"sock-shop"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "get_cpu_saturation_top",
                "content": '{"top":[]}',
            },
        ],
        tools=TOOLS,
    )

    assert "get_cpu_saturation_top" in prompt
    assert "[TOOL CALLS]" in prompt
    assert "[TOOL RESULT name=get_cpu_saturation_top id=call_1]" in prompt


def test_required_tool_choice_explicitly_forbids_plain_text():
    prompt = render_chat_messages(
        [{"role": "user", "content": "Qual o maior consumo de cpu?"}],
        tools=TOOLS,
        tool_choice="required",
    )

    assert '"tool_choice":"required"' in prompt
    assert "A tool call is mandatory" in prompt
    assert "do not answer in natural language" in prompt


def test_parse_tool_call_output_returns_openai_tool_call():
    calls = parse_tool_call_output(
        '{"tool_calls":[{"name":"get_cpu_saturation_top",'
        '"arguments":{"namespace":"sock-shop"}}]}',
        TOOLS,
    )

    assert calls is not None
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "get_cpu_saturation_top"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "namespace": "sock-shop"
    }


def test_parse_tool_call_output_rejects_unknown_tool_or_invalid_arguments():
    unknown = parse_tool_call_output(
        '{"tool_calls":[{"name":"delete_cluster", "arguments":{}}]}', TOOLS
    )
    missing_required = parse_tool_call_output(
        '{"tool_calls":[{"name":"get_cpu_saturation_top", "arguments":{}}]}',
        TOOLS,
    )
    extra_argument = parse_tool_call_output(
        '{"tool_calls":[{"name":"get_cpu_saturation_top",'
        '"arguments":{"namespace":"sock-shop","query":"up"}}]}',
        TOOLS,
    )

    assert unknown is None
    assert missing_required is None
    assert extra_argument is None


def test_tool_choice_required_and_forced_function():
    forced = {"type": "function", "function": {"name": "get_cpu_saturation_top"}}
    validate_tool_request(TOOLS, forced)
    assert tool_choice_requires_call("required")
    assert tool_choice_requires_call(forced)

    calls = parse_tool_call_output(
        '{"tool_calls":[{"name":"get_cpu_saturation_top",'
        '"arguments":{"namespace":"sock-shop"}}]}',
        TOOLS,
        tool_choice=forced,
    )
    assert calls is not None


def test_tool_choice_rejects_missing_function():
    try:
        validate_tool_request(
            TOOLS,
            {"type": "function", "function": {"name": "unknown_tool"}},
        )
    except ValueError as exc:
        assert "not present in tools" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected missing function to be rejected")


def test_build_chat_completion_response_uses_openai_tool_calls_shape():
    calls = parse_tool_call_output(
        '{"tool_calls":[{"name":"get_cpu_saturation_top",'
        '"arguments":{"namespace":"sock-shop"}}]}',
        TOOLS,
    )
    assert calls is not None
    response = build_chat_completion_response(
        content=None,
        tool_calls=calls,
        model="qwen-14b",
        prompt_tokens=4,
        completion_tokens=3,
        finish_reason="tool_calls",
    )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == calls
