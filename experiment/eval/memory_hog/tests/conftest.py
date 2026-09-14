"""Make the standalone harness scripts importable by the tests.

``experiment/eval/memory_hog`` is a folder of runnable scripts, not an
installed package, so we add it to ``sys.path`` to import
``generate_chaos_manifests`` directly.
"""
import os
import sys

_HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)
