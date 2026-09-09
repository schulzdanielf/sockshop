"""Pytest bootstrap shared by the whole repository.

Ensures the repository root is importable so tests can reference the
first-party ``experiment`` package (``experiment.platform.backend...``)
regardless of the directory pytest is invoked from.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
