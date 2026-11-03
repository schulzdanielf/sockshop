import os


DEFAULT_HISTORICAL_MCP_SSE_URL = (
    os.environ.get("HISTORICAL_MCP_SSE_URL", "").strip()
    or "http://127.0.0.1:18082/sse"
)
