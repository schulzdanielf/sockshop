"""Temporarily enable or disable Sock Shop telemetry fan-out to history."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from typing import Any

import yaml


_OTEL_NAMESPACE = "monitoring"
_PROMETHEUS_CONFIG = "prometheus-v3-configmap"
_PROMETHEUS_KEY = "prometheus.yml"
_OTEL_CONFIG = "otel-collector-config"
_OTEL_KEY = "config.yaml"
_REMOTE_WRITE_NAME = "history-dual-write"
_REMOTE_WRITE_URL = (
    "http://prometheus-history.monitoring-history.svc.cluster.local:9090/api/v1/write"
)


def _queued_exporter(endpoint: str, *, protocol: str) -> dict[str, Any]:
    exporter: dict[str, Any] = {"endpoint": endpoint}
    if protocol == "otlp":
        exporter["tls"] = {"insecure": True}
    exporter["sending_queue"] = {
        "enabled": True,
        "num_consumers": 4,
        "queue_size": 2048,
        "storage": "file_storage/tempo_queue",
    }
    exporter["retry_on_failure"] = {
        "enabled": True,
        "initial_interval": "1s",
        "max_interval": "30s",
        "max_elapsed_time": "0s",
    }
    return exporter


def toggle_otel_config(config: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    exporters = updated.get("exporters")
    service = updated.get("service")
    pipelines = service.get("pipelines") if isinstance(service, dict) else None
    if not isinstance(exporters, dict) or not isinstance(pipelines, dict):
        raise ValueError("Collector config must define exporters and service pipelines")
    traces = pipelines.get("traces")
    logs = pipelines.get("logs")
    if not isinstance(traces, dict) or not isinstance(logs, dict):
        raise ValueError("Collector config must define live traces and logs pipelines")
    if enabled:
        extensions = updated.get("extensions") or {}
        if "file_storage/tempo_queue" not in extensions:
            raise ValueError("Collector config is missing persistent exporter storage")
        for name, endpoint, protocol in (
            (
                "otlp/tempo-history",
                "tempo-history.monitoring-history.svc.cluster.local:4317",
                "otlp",
            ),
            (
                "loki/history",
                "http://loki-history.monitoring-history.svc.cluster.local:3100/loki/api/v1/push",
                "http",
            ),
        ):
            expected = _queued_exporter(endpoint, protocol=protocol)
            existing = exporters.get(name)
            if existing is not None and existing != expected:
                raise ValueError(f"Collector exporter {name} has unexpected settings")
            exporters[name] = expected
        trace_exporters = traces.get("exporters")
        if not isinstance(trace_exporters, list) or "otlp/tempo" not in trace_exporters:
            raise ValueError("live traces pipeline does not export to Tempo")
        if "otlp/tempo-history" not in trace_exporters:
            trace_exporters.append("otlp/tempo-history")

        history_logs = pipelines.get("logs/history")
        expected_log_pipeline = {
            **copy.deepcopy(logs),
            "receivers": ["filelog"],
            "exporters": ["loki/history"],
        }
        if history_logs is not None and history_logs != expected_log_pipeline:
            raise ValueError("Collector logs/history pipeline has unexpected settings")
        pipelines["logs/history"] = expected_log_pipeline
    else:
        traces_exporters = traces.get("exporters")
        if isinstance(traces_exporters, list):
            traces["exporters"] = [
                name for name in traces_exporters if name != "otlp/tempo-history"
            ]
        pipelines.pop("logs/history", None)
        exporters.pop("otlp/tempo-history", None)
        exporters.pop("loki/history", None)
    return updated


def toggle_prometheus_config(
    config: dict[str, Any], *, enabled: bool, campaign_id: str | None = None
) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    global_config = updated.get("global")
    if not isinstance(global_config, dict):
        raise ValueError("Prometheus config must define global settings")
    external_labels = global_config.setdefault("external_labels", {})
    if not isinstance(external_labels, dict):
        raise ValueError("Prometheus global external_labels must be a mapping")
    remote_write = updated.get("remote_write") or []
    if not isinstance(remote_write, list):
        raise ValueError("Prometheus remote_write must be a list")
    matching = [
        item
        for item in remote_write
        if isinstance(item, dict) and item.get("name") == _REMOTE_WRITE_NAME
    ]

    if enabled:
        if not isinstance(campaign_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9-]{0,62}", campaign_id
        ):
            raise ValueError("campaign_id must be a lowercase Kubernetes-safe identifier")
        existing_campaign = external_labels.get("history_campaign")
        if existing_campaign not in (None, campaign_id):
            raise ValueError("Prometheus already has a different history campaign label")
        if matching and (
            len(matching) != 1
            or matching[0].get("url") != _REMOTE_WRITE_URL
        ):
            raise ValueError("Prometheus history remote_write has unexpected settings")
        external_labels["history_campaign"] = campaign_id
        if not matching:
            remote_write.append(
                {
                    "name": _REMOTE_WRITE_NAME,
                    "url": _REMOTE_WRITE_URL,
                    "queue_config": {
                        "capacity": 10000,
                        "max_shards": 2,
                        "min_shards": 1,
                        "max_samples_per_send": 1000,
                        "batch_send_deadline": "5s",
                    },
                    "write_relabel_configs": [
                        {
                            "source_labels": [
                                "kubernetes_namespace",
                                "namespace",
                            ],
                            "separator": ";",
                            "regex": "sock-shop;.*|.*;sock-shop",
                            "action": "keep",
                        }
                    ],
                }
            )
        updated["remote_write"] = remote_write
    else:
        for item in matching:
            if item.get("url") != _REMOTE_WRITE_URL:
                raise ValueError("Prometheus history remote_write has unexpected settings")
        updated["remote_write"] = [
            item for item in remote_write if item not in matching
        ]
        if not updated["remote_write"]:
            updated.pop("remote_write")
        existing_campaign = external_labels.get("history_campaign")
        if existing_campaign is not None:
            if campaign_id is not None and existing_campaign != campaign_id:
                raise ValueError("history campaign label does not match the requested disable")
            if campaign_id is None:
                raise ValueError(
                    "campaign_id is required to remove the active history campaign label"
                )
            external_labels.pop("history_campaign")
        if not external_labels:
            global_config.pop("external_labels")
    return updated


def _read_config_map(name: str, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "-n", _OTEL_NAMESPACE, "get", "configmap", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"could not read ConfigMap {name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    resource = json.loads(result.stdout)
    raw_config = (resource.get("data") or {}).get(key)
    if not isinstance(raw_config, str):
        raise ValueError(f"ConfigMap {name} has no data key {key}")
    config = yaml.safe_load(raw_config)
    if not isinstance(config, dict):
        raise ValueError(f"ConfigMap {name} key {key} must contain a YAML mapping")
    return resource, config


def _patch_config_map(name: str, key: str, config: dict[str, Any]) -> None:
    patch = json.dumps(
        {"data": {key: yaml.safe_dump(config, sort_keys=False)}},
        separators=(",", ":"),
    )
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            _OTEL_NAMESPACE,
            "patch",
            "configmap",
            name,
            "--type=merge",
            "-p",
            patch,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"could not update ConfigMap {name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    print(result.stdout.strip())


def _restart_and_wait(resource: str) -> None:
    for action in ("restart", "status"):
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                _OTEL_NAMESPACE,
                "rollout",
                action,
                resource,
                "--timeout=300s",
            ]
            if action == "status"
            else ["kubectl", "-n", _OTEL_NAMESPACE, "rollout", action, resource],
            capture_output=True,
            text=True,
            check=False,
            timeout=330,
        )
        if result.returncode:
            raise RuntimeError(
                f"rollout {action} failed for {resource}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        print(result.stdout.strip())


def set_dual_write(action: str, *, campaign_id: str | None = None) -> None:
    if action not in {"enable", "disable"}:
        raise ValueError("action must be enable or disable")
    enabled = action == "enable"
    _, collector_config = _read_config_map(_OTEL_CONFIG, _OTEL_KEY)
    _, prometheus_config = _read_config_map(_PROMETHEUS_CONFIG, _PROMETHEUS_KEY)
    collector_updated = toggle_otel_config(collector_config, enabled=enabled)
    prometheus_updated = toggle_prometheus_config(
        prometheus_config,
        enabled=enabled,
        campaign_id=campaign_id,
    )
    collector_changed = collector_updated != collector_config
    prometheus_changed = prometheus_updated != prometheus_config
    if not collector_changed and not prometheus_changed:
        print(f"Historical telemetry dual-write is already {action}d.")
        return
    if not enabled:
        print("Waiting 60 seconds for final historical exporter batches to flush.")
        time.sleep(60)
    if prometheus_changed:
        _patch_config_map(
            _PROMETHEUS_CONFIG,
            _PROMETHEUS_KEY,
            prometheus_updated,
        )
    if collector_changed:
        _patch_config_map(_OTEL_CONFIG, _OTEL_KEY, collector_updated)
    if prometheus_changed:
        _restart_and_wait("deployment/prometheus-v3")
    if collector_changed:
        _restart_and_wait("daemonset/otel-collector")
    print(f"Historical telemetry dual-write {action}d.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("--campaign-id")
    args = parser.parse_args(argv)
    if args.action == "enable" and args.campaign_id is None:
        parser.error("--campaign-id is required when enabling dual-write")
    try:
        set_dual_write(args.action, campaign_id=args.campaign_id)
    except (OSError, RuntimeError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"history dual-write {args.action} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
