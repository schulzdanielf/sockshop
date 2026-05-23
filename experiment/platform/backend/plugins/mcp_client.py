from __future__ import annotations

import asyncio
import json
import socket
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client


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
                raise RuntimeError(f"SSE endpoint returned status HTTP {response.status}")

    async def _call_tool_async(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        async with sse_client(
            self.sse_url,
            timeout=self.timeout_seconds,
            sse_read_timeout=max(20, self.timeout_seconds * 2),
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                return result.model_dump() if hasattr(result, "model_dump") else result

    def call_tool_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        raw = asyncio.run(self._call_tool_async(name, arguments))
        return _extract_mcp_payload(raw)
