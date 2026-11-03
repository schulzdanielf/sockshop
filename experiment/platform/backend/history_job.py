"""Submit isolated Kubernetes Jobs for per-run telemetry migration."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import subprocess
import uuid
from typing import Any, Dict


class HistoryMigrationDispatcher:
    def __init__(
        self,
        *,
        enabled: bool = False,
        namespace: str = "monitoring-history",
        image: str = "sockshop/telemetry-history-migrator:local",
        kubectl: str = "kubectl",
    ):
        self.enabled = enabled
        self.namespace = namespace
        self.image = image
        self.kubectl = kubectl

    @classmethod
    def from_environment(cls) -> "HistoryMigrationDispatcher":
        enabled_value = os.getenv("HISTORY_MIGRATION_ENABLED", "false").lower()
        if enabled_value not in {"true", "false", "1", "0"}:
            raise ValueError("HISTORY_MIGRATION_ENABLED must be true, false, 1, or 0")
        return cls(
            enabled=enabled_value in {"true", "1"},
            namespace=os.getenv("HISTORY_MIGRATION_NAMESPACE", "monitoring-history"),
            image=os.getenv(
                "HISTORY_MIGRATOR_IMAGE",
                "sockshop/telemetry-history-migrator:local",
            ),
            kubectl=os.getenv("KUBECTL_BIN", "kubectl"),
        )

    def dispatch(
        self,
        *,
        run_id: str,
        experiment_id: str,
        status: str,
        namespace: str,
        observation_window: Dict[str, Any],
        log_snapshot: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled"}
        if not re.fullmatch(r"run-[a-zA-Z0-9-]{1,50}", run_id):
            raise ValueError("run_id is not safe for a Kubernetes resource name")
        window = observation_window.get("window")
        if (
            not isinstance(window, dict)
            or not window.get("start")
            or not window.get("end")
        ):
            return {
                "status": "blocked_missing_window",
                "reason": "run has no valid observation-window bounds",
            }
        return self.submit_batch(
            {
                "campaign_id": experiment_id,
                "runs": [
                    {
                        "run_id": run_id,
                        "status": status,
                        "namespace": namespace,
                        "observation_window": observation_window,
                        "log_snapshot": log_snapshot,
                    }
                ],
            }
        )

    def submit_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "campaign")
        runs = payload.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError("history migration payload must contain at least one run")
        serialized = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        compressed = gzip.compress(serialized, compresslevel=9, mtime=0)
        suffix = uuid.uuid4().hex[:8]
        resource_suffix = re.sub(r"[^a-z0-9-]+", "-", campaign_id.lower()).strip("-")
        resource_suffix = (resource_suffix or "campaign")[:30].rstrip("-")
        config_name = f"history-input-{resource_suffix}-{suffix}"
        job_name = f"history-migrate-{resource_suffix}-{suffix}"

        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": self.namespace,
                "labels": {"app": "telemetry-history-migrator"},
            },
            "binaryData": {
                "runs.json.gz": base64.b64encode(compressed).decode("ascii")
            },
        }
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self.namespace,
                "labels": {"app": "telemetry-history-migrator"},
            },
            "spec": {
                "backoffLimit": 1,
                "activeDeadlineSeconds": 21600,
                "ttlSecondsAfterFinished": 604800,
                "template": {
                    "metadata": {
                        "labels": {"app": "telemetry-history-migrator"}
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "migrator",
                                "image": self.image,
                                "imagePullPolicy": "IfNotPresent",
                                "args": [
                                    "--input",
                                    "/input/runs.json.gz",
                                ],
                                "env": [
                                    {
                                        "name": "SOURCE_PROMETHEUS_URL",
                                        "value": "http://prometheus-v3.monitoring.svc.cluster.local:9090",
                                    },
                                    {
                                        "name": "SOURCE_LOKI_URL",
                                        "value": "http://loki.monitoring.svc.cluster.local:3100",
                                    },
                                    {
                                        "name": "SOURCE_TEMPO_URL",
                                        "value": "http://tempo.monitoring.svc.cluster.local:3200",
                                    },
                                    {
                                        "name": "TARGET_PROMETHEUS_URL",
                                        "value": "http://prometheus-history.monitoring-history.svc.cluster.local:9090",
                                    },
                                    {
                                        "name": "TARGET_LOKI_URL",
                                        "value": "http://loki-history.monitoring-history.svc.cluster.local:3100",
                                    },
                                    {
                                        "name": "TARGET_TEMPO_URL",
                                        "value": "http://tempo-history.monitoring-history.svc.cluster.local:3200",
                                    },
                                    {
                                        "name": "TARGET_TEMPO_OTLP_URL",
                                        "value": "http://tempo-history.monitoring-history.svc.cluster.local:4318",
                                    },
                                    {
                                        "name": "HISTORY_S3_ENDPOINT",
                                        "value": "seaweedfs-history.monitoring-history.svc.cluster.local:8333",
                                    },
                                    {
                                        "name": "HISTORY_S3_BUCKET",
                                        "value": "history-runs",
                                    },
                                    {
                                        "name": "HISTORY_S3_SECURE",
                                        "value": "false",
                                    },
                                    {
                                        "name": "HISTORY_S3_ACCESS_KEY",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "seaweedfs-history-credentials",
                                                "key": "history-access-key",
                                            }
                                        },
                                    },
                                    {
                                        "name": "HISTORY_S3_SECRET_KEY",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "seaweedfs-history-credentials",
                                                "key": "history-secret-key",
                                            }
                                        },
                                    },
                                ],
                                "resources": {
                                    "requests": {
                                        "cpu": "250m",
                                        "memory": "1Gi",
                                    },
                                    "limits": {
                                        "cpu": "2",
                                        "memory": "4Gi",
                                    },
                                },
                                "volumeMounts": [
                                    {
                                        "name": "input",
                                        "mountPath": "/input",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "input",
                                "configMap": {
                                    "name": config_name,
                                    "items": [
                                        {
                                            "key": "runs.json.gz",
                                            "path": "runs.json.gz",
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                },
            },
        }
        resource_list = {
            "apiVersion": "v1",
            "kind": "List",
            "items": [config_map, job],
        }
        try:
            result = subprocess.run(
                [self.kubectl, "create", "-f", "-"],
                input=json.dumps(resource_list),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"kubectl could not submit the history Job: {exc}") from exc
        if result.returncode:
            raise RuntimeError(
                "could not submit telemetry history Job: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return {
            "status": "queued",
            "job_name": job_name,
            "namespace": self.namespace,
            "run_count": len(runs),
            "input_bytes": len(serialized),
            "compressed_input_bytes": len(compressed),
        }
