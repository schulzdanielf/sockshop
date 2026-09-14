"""Unit tests for the chaos-manifest generator.

These cover the pure rendering logic — token substitution, YAML
well-formedness and the catalogue-leftover guard — without touching the
cluster. Renderers read baseline templates from
``deploy/kubernetes/manifests-chaos``; a chaos type is skipped if its
baseline file is absent on this checkout.
"""
from __future__ import annotations

import pytest
import yaml

import generate_chaos_manifests as gcm

# Subset whose baselines are bundled in the repo and that should always
# render cleanly for an arbitrary (non-catalogue) target service.
_CHAOS_TYPES = sorted(gcm.RENDERERS)


@pytest.mark.parametrize("chaos_type", _CHAOS_TYPES)
def test_renderer_produces_valid_yaml(chaos_type):
    render = gcm.RENDERERS[chaos_type]
    try:
        text = render("carts", {})
    except FileNotFoundError:
        pytest.skip(f"baseline for {chaos_type} not present on this checkout")

    docs = [d for d in yaml.safe_load_all(text) if d]
    assert docs, "renderer emitted no YAML document"
    # Every rendered workflow is namespaced by the target service.
    assert any("carts" in line for line in text.splitlines())


@pytest.mark.parametrize("chaos_type", _CHAOS_TYPES)
def test_renderer_leaves_no_catalogue_token(chaos_type):
    render = gcm.RENDERERS[chaos_type]
    try:
        text = render("carts", {})
    except FileNotFoundError:
        pytest.skip(f"baseline for {chaos_type} not present on this checkout")

    # Should not raise — i.e. no stray 'catalogue' tokens leaked through.
    gcm._sanity_check(text, "carts", chaos_type)


def test_sanity_check_passes_for_catalogue():
    # The guard is a no-op when the target genuinely is catalogue.
    gcm._sanity_check("name: catalogue-memory-hog", "catalogue", "memory-hog")


def test_sanity_check_raises_on_leftover_token():
    leaked = "applabel: name=catalogue\nworkflow: carts-memory-hog"
    with pytest.raises(RuntimeError, match="Unreplaced 'catalogue'"):
        gcm._sanity_check(leaked, "carts", "memory-hog")


def test_chaos_env_returns_declared_env():
    cfg = {
        "chaos_types": [
            {"name": "memory-hog", "env": {"memory_consumption_mb": 80}},
        ]
    }
    assert gcm._chaos_env(cfg, "memory-hog") == {"memory_consumption_mb": 80}


def test_chaos_env_unknown_type_raises():
    with pytest.raises(KeyError):
        gcm._chaos_env({"chaos_types": []}, "does-not-exist")


def test_replace_in_order_applies_sequentially():
    out = gcm._replace_in_order("a-b-c", [("a", "x"), ("x-b", "y")])
    assert out == "y-c"


def test_strip_probe_ref_removes_probe_lines():
    text = "spec:\n  probeRef: my-probe\n  other: keep"
    out = gcm._strip_probe_ref(text)
    assert "probeRef" not in out
    assert "other: keep" in out
