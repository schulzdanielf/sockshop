import json

import pytest

from experiment.platform.backend import agent_console


class _FakeGrafanaMCPClient:
    def __init__(self, tool):
        self.tool = tool
        self.calls = []

    def list_tools(self):
        return [self.tool]

    def call_tool_json(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return {"result": "observed"}


def _tool_call(name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_grafana",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def test_grafana_chat_validates_model_arguments_and_injects_run_window():
    client = _FakeGrafanaMCPClient(
        {
            "name": "query_prometheus",
            "description": "Run a Prometheus query.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "datasourceUid": {"type": "string"},
                    "expr": {"type": "string"},
                    "startTime": {"type": "string"},
                    "endTime": {"type": "string"},
                    "stepSeconds": {"type": "integer"},
                },
                "required": ["datasourceUid", "expr", "endTime"],
                "additionalProperties": False,
            },
        }
    )
    llm_calls = []

    def fake_llm(messages, tools, **kwargs):
        llm_calls.append((messages, tools, kwargs))
        if kwargs["tool_choice"] == "required":
            model_tool = tools[0]["function"]
            assert model_tool["name"] == "query_prometheus"
            assert set(model_tool["parameters"]["properties"]) == {
                "datasourceUid",
                "expr",
            }
            assert model_tool["parameters"]["required"] == [
                "datasourceUid",
                "expr",
            ]
            return _tool_call(
                "query_prometheus",
                {"datasourceUid": "prom-main", "expr": "up"},
            )
        assert messages[-1]["role"] == "tool"
        return {"role": "assistant", "content": "Prometheus retornou dados."}

    answer = agent_console.answer_query_mcp(
        "Verifique a disponibilidade no Prometheus.",
        mcp_client=client,
        start="2026-09-23T11:16:51.123Z",
        end="2026-09-23T11:22:23.987Z",
        namespace="sock-shop",
        llm=fake_llm,
        max_steps=1,
    )

    assert answer == "Prometheus retornou dados."
    assert client.calls[0][0:2] == (
        "query_prometheus",
        {
            "datasourceUid": "prom-main",
            "expr": "up",
            "startTime": "2026-09-23T11:16:52Z",
            "endTime": "2026-09-23T11:22:23Z",
            "stepSeconds": 15,
        },
    )


def test_grafana_chat_rejects_model_controlled_time_and_unknown_arguments():
    client = _FakeGrafanaMCPClient(
        {
            "name": "search_dashboards",
            "description": "Search Grafana dashboards.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    )

    with pytest.raises(agent_console.LLMClientError, match="invalid arguments"):
        agent_console.answer_query_mcp(
            "Encontre dashboards para a aplicação.",
            mcp_client=client,
            start="start",
            end="end",
            namespace="sock-shop",
            llm=lambda *_args, **_kwargs: _tool_call(
                "search_dashboards",
                {"query": "sock-shop", "startTime": "unbounded"},
            ),
            max_steps=1,
        )
