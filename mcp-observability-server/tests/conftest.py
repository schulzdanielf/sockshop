"""Make the flat MCP server modules importable by the tests.

``mcp-observability-server`` has a hyphen in its name and ships no
``__init__.py``, so its modules are top-level imports. We add the server
directory to ``sys.path`` so ``import investigators`` resolves.
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
