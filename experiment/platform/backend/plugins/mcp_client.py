"""HTTP client for the MCP observability server.

``MCPToolClient`` invokes tools exposed by the MCP observability server
and normalises their responses (decoding nested JSON payloads) so the
metrics and trace plugins can consume plain Python structures.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client
from opentelemetry.trace import Status, StatusCode

from experiment.platform.backend.observability.otel import GenAI, get_tracer


def _decode_json_if_possible(value: Any) -> Any:
    current = value
    for _ in range(3):
        if isinstance(current, str):
            try:
                current = json.loads(current)
                continue
            except json.JSONDecodeError:
                return current
        return current
    return current


def _extract_mcp_payload(raw: Any) -> Any:
    payload = _decode_json_if_possible(raw)
    if not isinstance(payload, dict):
        return payload

    structured = payload.get("structuredContent")
    if structured is not None:
        if isinstance(structured, dict) and "result" in structured:
            return _decode_json_if_possible(structured["result"])
        return _decode_json_if_possible(structured)

    content = payload.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and "text" in first:
            return _decode_json_if_possible(first["text"])

    return payload


class MCPToolClient:
    def __init__(self, sse_url: str, timeout_seconds: int = 10):
        self.sse_url = sse_url
        self.timeout_seconds = timeout_seconds

    def assert_sse_connectivity(self) -> None:
        parsed = urllib.parse.urlparse(self.sse_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=self.timeout_seconds):
            pass

        req = urllib.request.Request(
            self.sse_url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"SSE endpoint returned status HTTP {response.status}"
                )

    async def _call_tool_async(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Any:
        async with sse_client(
            self.sse_url,
            timeout=self.timeout_seconds,
            sse_read_timeout=max(20, self.timeout_seconds * 2),
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                return result.model_dump() if hasattr(result, "model_dump") else result

    async def _list_tools_async(self) -> list[Dict[str, Any]]:
        async with sse_client(
            self.sse_url,
            timeout=self.timeout_seconds,
            sse_read_timeout=max(20, self.timeout_seconds * 2),
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema,
                    }
                    for tool in result.tools
                ]

    def list_tools(self) -> list[Dict[str, Any]]:
        """Discover tool names and input schemas from the configured MCP server."""
        tracer = get_tracer()
        started = time.monotonic()
        with tracer.start_as_current_span("mcp.tools.list") as span:
            span.set_attribute("rpc.system.name", "mcp")
            span.set_attribute("rpc.method", "tools/list")
            span.set_attribute(
                "server.address", urllib.parse.urlparse(self.sse_url).hostname or ""
            )
            try:
                tools = asyncio.run(self._list_tools_async())
                span.set_attribute("rca.mcp.tools.count", len(tools))
                span.add_event("mcp.tools.discovered", {"count": len(tools)})
                return tools
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                span.set_attribute(
                    "rca.mcp.duration_ms",
                    int((time.monotonic() - started) * 1000),
                )

    def call_tool_json(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        tool_call_id: Optional[str] = None,
        tool_description: Optional[str] = None,
    ) -> Any:
        tracer = get_tracer()
        started = time.monotonic()
        with tracer.start_as_current_span("gen_ai.tool_call") as span:
            span.set_attribute(GenAI.OPERATION_NAME, "execute_tool")
            span.set_attribute(GenAI.TOOL_NAME, name)
            span.set_attribute(GenAI.TOOL_TYPE, "function")
            if tool_call_id:
                span.set_attribute(GenAI.TOOL_CALL_ID, tool_call_id)
            if tool_description:
                span.set_attribute(GenAI.TOOL_DESCRIPTION, tool_description)
            span.set_attribute("server.address", urllib.parse.urlparse(self.sse_url).hostname or "")
            span.set_attribute("rca.mcp.sse_url", self.sse_url)
            span.set_attribute("rca.mcp.argument_keys", sorted((arguments or {}).keys()))
            span.set_attribute("rca.mcp.argument_count", len(arguments or {}))
            span.add_event(
                "gen_ai.tool_call.request",
                {"tool.name": name, "argument_count": len(arguments or {})},
            )
            try:
                raw = asyncio.run(self._call_tool_async(name, arguments))
                payload = _extract_mcp_payload(raw)
                span.set_attribute("rca.mcp.result_type", type(payload).__name__)
                span.set_attribute("rca.mcp.success", True)
                span.add_event(
                    "gen_ai.tool_call.response",
                    {"tool.name": name, "result_type": type(payload).__name__},
                )
                return payload
            except Exception as exc:
                span.set_attribute("rca.mcp.success", False)
                span.set_attribute("error.type", type(exc).__name__)
                span.add_event(
                    "gen_ai.tool_call.error",
                    {"tool.name": name, "error.type": type(exc).__name__},
                )
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                span.set_attribute(
                    "rca.mcp.duration_ms",
                    int((time.monotonic() - started) * 1000),
                )
