import copy

import pytest

from experiment.platform.tools.history_dual_write import (
    toggle_otel_config,
    toggle_prometheus_config,
)


COLLECTOR_CONFIG = {
    "exporters": {
        "loki": {"endpoint": "http://loki.monitoring.svc.cluster.local:3100"},
        "otlp/tempo": {"endpoint": "tempo.monitoring.svc.cluster.local:4317"},
    },
    "extensions": {"file_storage/tempo_queue": {"directory": "/var/lib/otelcol/storage"}},
    "service": {
        "pipelines": {
            "traces": {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["otlp/tempo"]},
            "logs": {
                "receivers": ["otlp", "filelog"],
                "processors": ["batch", "attributes/loki_labels"],
                "exporters": ["loki"],
            },
        }
    },
}

PROMETHEUS_CONFIG = {
    "global": {
        "scrape_interval": "15s",
        "external_labels": {"cluster": "k8s"},
    },
    "scrape_configs": [{"job_name": "prometheus"}],
}


def test_collector_dual_write_is_opt_in_and_reversible():
    enabled = toggle_otel_config(COLLECTOR_CONFIG, enabled=True)
    history = enabled["service"]["pipelines"]

    assert history["traces"]["exporters"] == ["otlp/tempo", "otlp/tempo-history"]
    assert history["logs/history"]["receivers"] == ["filelog"]
    assert history["logs/history"]["exporters"] == ["loki/history"]
    assert "loki/history" in enabled["exporters"]
    assert "otlp/tempo-history" in enabled["exporters"]
    assert "logs/history" not in COLLECTOR_CONFIG["service"]["pipelines"]

    disabled = toggle_otel_config(enabled, enabled=False)
    assert disabled == COLLECTOR_CONFIG


def test_collector_enable_rejects_an_unexpected_history_exporter():
    configured = copy.deepcopy(COLLECTOR_CONFIG)
    configured["exporters"]["loki/history"] = {"endpoint": "http://unexpected"}

    with pytest.raises(ValueError, match="unexpected settings"):
        toggle_otel_config(configured, enabled=True)


def test_prometheus_remote_write_is_scoped_and_reversible():
    campaign_id = "exploratory-rag-20261101T211000Z"
    enabled = toggle_prometheus_config(
        PROMETHEUS_CONFIG,
        enabled=True,
        campaign_id=campaign_id,
    )
    remote_write = enabled["remote_write"][0]

    assert enabled["global"]["external_labels"]["history_campaign"] == campaign_id
    assert remote_write["url"].endswith("/api/v1/write")
    assert remote_write["write_relabel_configs"] == [
        {
            "source_labels": ["kubernetes_namespace", "namespace"],
            "separator": ";",
            "regex": "sock-shop;.*|.*;sock-shop",
            "action": "keep",
        }
    ]
    assert toggle_prometheus_config(
        enabled,
        enabled=False,
        campaign_id=campaign_id,
    ) == PROMETHEUS_CONFIG


def test_prometheus_disable_preserves_other_remote_write_targets():
    existing = {
        **PROMETHEUS_CONFIG,
        "remote_write": [{"name": "other", "url": "http://metrics-gateway"}],
    }
    enabled = toggle_prometheus_config(
        existing,
        enabled=True,
        campaign_id="test-campaign",
    )
    disabled = toggle_prometheus_config(
        enabled,
        enabled=False,
        campaign_id="test-campaign",
    )

    assert disabled == existing


def test_prometheus_enable_rejects_a_different_active_campaign():
    configured = {
        **PROMETHEUS_CONFIG,
        "global": {
            **PROMETHEUS_CONFIG["global"],
            "external_labels": {
                **PROMETHEUS_CONFIG["global"]["external_labels"],
                "history_campaign": "another-campaign",
            },
        },
    }

    with pytest.raises(ValueError, match="different history campaign"):
        toggle_prometheus_config(
            configured,
            enabled=True,
            campaign_id="test-campaign",
        )
