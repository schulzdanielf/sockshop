"""ASGI entry point.

Exposes the module-level ``app`` built by :func:`api.create_app` so the
platform can be served with e.g. ``uvicorn experiment.platform.backend.main:app``.
"""

from .api import create_app

app = create_app()
