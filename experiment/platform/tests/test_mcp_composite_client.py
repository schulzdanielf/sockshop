import pytest

from experiment.platform.backend.plugins.mcp_client import CompositeMCPToolClient


class _FakeMCPClient:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def list_tools(self):
        return [{"name": self.name, "inputSchema": {"type": "object"}}]

    def call_tool_json(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return {"server": self.name}


def test_composite_client_lists_and_routes_tools():
    run_client = _FakeMCPClient("check_oom_kills")
    grafana_client = _FakeMCPClient("query_prometheus")
    composite = CompositeMCPToolClient([run_client, grafana_client])

    tools = composite.list_tools()
    result = composite.call_tool_json(
        "query_prometheus",
        {"expr": "up"},
        tool_call_id="call_1",
        timeout_seconds=30,
    )

    assert [tool["name"] for tool in tools] == [
        "check_oom_kills",
        "query_prometheus",
    ]
    assert result == {"server": "query_prometheus"}
    assert not run_client.calls
    assert grafana_client.calls == [
        (
            "query_prometheus",
            {"expr": "up"},
            {
                "tool_call_id": "call_1",
                "tool_description": None,
                "timeout_seconds": 30,
            },
        )
    ]


def test_composite_client_rejects_duplicate_tool_names():
    with pytest.raises(RuntimeError, match="multiple MCP servers"):
        CompositeMCPToolClient(
            [_FakeMCPClient("duplicate"), _FakeMCPClient("duplicate")]
        ).list_tools()
