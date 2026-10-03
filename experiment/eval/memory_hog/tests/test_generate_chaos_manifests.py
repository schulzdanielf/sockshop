"""Unit tests for the chaos-manifest generator.

These cover the pure rendering logic — token substitution, YAML
well-formedness and the catalogue-leftover guard — without touching the
cluster. Renderers read baseline templates from
``deploy/kubernetes/manifests-chaos``; a chaos type is skipped if its
baseline file is absent on this checkout.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
import yaml

import generate_chaos_manifests as gcm
from experiment.eval.memory_hog import runner as runner_module
from experiment.eval.memory_hog.resource_intensity import resource_chaos_environment

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


@pytest.mark.parametrize(("env", "expected_ms"), [({}, "500"), ({"network_latency_ms": 250}, "250")])
def test_network_latency_renderer_uses_configured_safe_delay(env, expected_ms):
    text = getattr(gcm, "_render_network_latency")("front-end", env)
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    engine_yaml = next(
        artifact["raw"]["data"]
        for doc in docs
        for template in doc.get("spec", {}).get("templates", [])
        for artifact in template.get("inputs", {}).get("artifacts", [])
        if "kind: ChaosEngine" in artifact.get("raw", {}).get("data", "")
    )
    engine = yaml.safe_load(engine_yaml)
    env_vars = engine["spec"]["experiments"][0]["spec"]["components"]["env"]
    env_map = {item["name"]: item["value"] for item in env_vars}

    assert env_map["NETWORK_LATENCY"] == expected_ms


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
    assert gcm._chaos_env(cfg, "memory-hog") == {
        "memory_consumption_mb": 80,
        "total_chaos_duration_seconds": 120,
    }


def test_resource_chaos_templates_share_duration_and_cpu_load():
    cfg = yaml.safe_load(
        (gcm.ROOT / "experiment/eval/memory_hog/config.yaml").read_text(
            encoding="utf-8"
        )
    )
    memory_env = gcm._chaos_env(cfg, "memory-hog")
    cpu_env = gcm._chaos_env(cfg, "cpu-hog")

    assert memory_env["total_chaos_duration_seconds"] == 120
    assert cpu_env["total_chaos_duration_seconds"] == 120
    assert cpu_env["cpu_cores"] == 0
    assert cpu_env["cpu_load_percentage"] == 100


def test_cpu_hog_renderer_sets_percentage_mode():
    text = gcm._render_cpu_hog(
        "carts",
        {
            "cpu_cores": 0,
            "cpu_load_percentage": 100,
            "total_chaos_duration_seconds": 120,
        },
    )
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    engine_yaml = next(
        artifact["raw"]["data"]
        for doc in docs
        for template in doc.get("spec", {}).get("templates", [])
        for artifact in template.get("inputs", {}).get("artifacts", [])
        if "kind: ChaosEngine" in artifact.get("raw", {}).get("data", "")
    )
    engine = yaml.safe_load(engine_yaml)
    env = {
        item["name"]: item["value"]
        for item in engine["spec"]["experiments"][0]["spec"]["components"]["env"]
    }

    assert env["CPU_CORES"] == "0"
    assert env["CPU_LOAD"] == "100"
    assert env["TOTAL_CHAOS_DURATION"] == "120"


def test_resource_doses_use_target_limits_and_shared_duration():
    settings = {
        "duration_seconds": 120,
        "cpu_load_percentage": 100,
        "memory_load_percentage": 60,
    }

    assert resource_chaos_environment("cpu-hog", {"cpu": "100m"}, settings) == {
        "CPU_CORES": "0",
        "CPU_LOAD": "100",
        "TOTAL_CHAOS_DURATION": "120",
    }
    assert resource_chaos_environment(
        "memory-hog", {"memory": "384Mi"}, settings
    ) == {"MEMORY_CONSUMPTION": "241", "TOTAL_CHAOS_DURATION": "120"}


def test_resource_dose_rejects_missing_or_invalid_limits():
    settings = {
        "duration_seconds": 120,
        "cpu_load_percentage": 100,
        "memory_load_percentage": 60,
    }
    with pytest.raises(ValueError, match="invalid Kubernetes CPU quantity"):
        resource_chaos_environment("cpu-hog", {}, settings)
    with pytest.raises(ValueError, match="invalid Kubernetes memory quantity"):
        resource_chaos_environment("memory-hog", {"memory": ""}, settings)


def test_runner_uses_target_container_limit_and_records_resolved_dose(monkeypatch):
    config = yaml.safe_load(
        (gcm.ROOT / "experiment/eval/memory_hog/config.yaml").read_text(
            encoding="utf-8"
        )
    )
    monkeypatch.setattr(
        runner_module,
        "_target_container_limits",
        lambda service, namespace: {"cpu": "300m", "memory": "1000Mi"},
    )

    memory_spec = runner_module.build_spec(config, "front-end", 0, "memory-hog")
    cpu_spec = runner_module.build_spec(config, "front-end", 0, "cpu-hog")

    memory_profile = memory_spec["chaos_profile"]["resource_chaos"]
    assert memory_profile["target_container"] == "front-end"
    assert memory_profile["container_limits"]["memory"] == "1000Mi"
    assert memory_profile["environment"] == {
        "MEMORY_CONSUMPTION": "734",
        "TOTAL_CHAOS_DURATION": "120",
        "TARGET_CONTAINER": "front-end",
    }
    assert cpu_spec["chaos_profile"]["resource_chaos"]["environment"] == {
        "CPU_CORES": "0",
        "CPU_LOAD": "100",
        "TOTAL_CHAOS_DURATION": "120",
        "TARGET_CONTAINER": "front-end",
    }
    metric_queries = {
        query["id"]: query["query"]
        for query in memory_spec["observability"]["metrics"]["queries"]
    }
    assert all("[1m]" not in query for query in metric_queries.values())
    assert all(
        "[30s]" in metric_queries[metric]
        for metric in (
            "network_receive_bytes_rate",
            "network_transmit_bytes_rate",
            "cpu_usage_cores",
            "cpu_saturation_pct",
            "cpu_throttled",
        )
    )


def test_runner_reads_named_application_container_not_sidecar(monkeypatch):
    deployment = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "front-end",
                            "resources": {
                                "limits": {"cpu": "300m", "memory": "1000Mi"}
                            },
                        },
                        {
                            "name": "istio-proxy",
                            "resources": {
                                "limits": {"cpu": "200m", "memory": "256Mi"}
                            },
                        },
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(
        runner_module,
        "_kubectl",
        lambda *args, **kwargs: Mock(
            returncode=0, stdout=json.dumps(deployment), stderr=""
        ),
    )

    assert runner_module._target_container_limits("front-end", "sock-shop") == {
        "cpu": "300m",
        "memory": "1000Mi",
    }


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
