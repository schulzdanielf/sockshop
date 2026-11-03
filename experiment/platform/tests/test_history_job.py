import base64
import gzip
import json
from unittest.mock import patch

from experiment.platform.backend.history_job import HistoryMigrationDispatcher


def test_dispatch_skips_when_disabled():
    dispatcher = HistoryMigrationDispatcher(enabled=False)

    result = dispatcher.dispatch(
        run_id="run-abc123",
        experiment_id="experiment",
        status="completed",
        namespace="sock-shop",
        observation_window={},
        log_snapshot=None,
    )

    assert result == {"status": "disabled"}


def test_dispatch_marks_runs_without_windows_blocked():
    dispatcher = HistoryMigrationDispatcher(enabled=True)

    result = dispatcher.dispatch(
        run_id="run-abc123",
        experiment_id="experiment",
        status="failed",
        namespace="sock-shop",
        observation_window={},
        log_snapshot=None,
    )

    assert result["status"] == "blocked_missing_window"


def test_submit_batch_builds_internal_job_and_compressed_input():
    dispatcher = HistoryMigrationDispatcher(
        enabled=True, namespace="monitoring-history", image="history-migrator:test"
    )
    payload = {
        "campaign_id": "pilot",
        "runs": [{"run_id": "run-abc123", "namespace": "sock-shop"}],
    }

    class ProcessResult:
        returncode = 0
        stdout = "configmap/input created\njob.batch/job created"
        stderr = ""

    with patch(
        "experiment.platform.backend.history_job.subprocess.run",
        return_value=ProcessResult(),
    ) as run:
        result = dispatcher.submit_batch(payload)

    submitted = json.loads(run.call_args.kwargs["input"])
    config_map, job = submitted["items"]
    decoded = gzip.decompress(
        base64.b64decode(config_map["binaryData"]["runs.json.gz"])
    )
    assert json.loads(decoded) == payload
    assert result["status"] == "queued"
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == "history-migrator:test"
    assert job["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] == "IfNotPresent"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["memory"] == "1Gi"
    assert container["resources"]["limits"]["memory"] == "4Gi"
    target_otlp = next(
        item["value"]
        for item in container["env"]
        if item["name"] == "TARGET_TEMPO_OTLP_URL"
    )
    assert target_otlp.endswith(":4318")
    s3_endpoint = next(
        item["value"]
        for item in container["env"]
        if item["name"] == "HISTORY_S3_ENDPOINT"
    )
    assert s3_endpoint == (
        "seaweedfs-history.monitoring-history.svc.cluster.local:8333"
    )
    secret_env = [
        item
        for item in container["env"]
        if item["name"] in {"HISTORY_S3_ACCESS_KEY", "HISTORY_S3_SECRET_KEY"}
    ]
    assert len(secret_env) == 2
    assert all("valueFrom" in item for item in secret_env)
    assert all(
        item["valueFrom"]["secretKeyRef"]["name"]
        == "seaweedfs-history-credentials"
        for item in secret_env
    )
    assert all(".svc.cluster.local" in item["value"] for item in container["env"] if "value" in item and item["name"].endswith("_URL"))
