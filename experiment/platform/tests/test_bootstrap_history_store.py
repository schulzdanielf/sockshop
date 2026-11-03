import importlib.util
import json
from pathlib import Path

from experiment.platform.tools.bootstrap_history_store import (
    BUCKETS,
    bootstrap_s3_storage,
)


def test_bootstrap_creates_missing_buckets_and_applies_archive_retention():
    class FakeS3Client:
        def __init__(self):
            self.existing = {"history-runs"}
            self.created = []
            self.lifecycle = None

        def bucket_exists(self, bucket):
            return bucket in self.existing

        def make_bucket(self, bucket):
            self.created.append(bucket)
            self.existing.add(bucket)

        def set_bucket_lifecycle(self, bucket, lifecycle):
            self.lifecycle = (bucket, lifecycle)

    client = FakeS3Client()

    bootstrap_s3_storage(client)

    assert client.created == ["history-loki", "history-tempo"]
    assert set(client.existing) == set(BUCKETS)
    bucket, lifecycle = client.lifecycle
    assert bucket == "history-runs"
    assert lifecycle.rules[0].expiration.days == 365


def test_seaweedfs_config_separates_bootstrap_and_service_credentials():
    secret_script = (
        Path(__file__).parents[3]
        / "deploy/kubernetes/manifests-monitoring/bootstrap-history-secret.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bootstrap_history_secret", secret_script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = json.loads(module._s3_config("admin", "admin-secret", "service", "service-secret"))
    identities = {item["name"]: item for item in config["identities"]}

    assert identities["history-bootstrap"]["credentials"] == [
        {"accessKey": "admin", "secretKey": "admin-secret"}
    ]
    assert identities["history-bootstrap"]["actions"] == ["Admin"]
    assert identities["history-services"]["credentials"] == [
        {"accessKey": "service", "secretKey": "service-secret"}
    ]
    assert identities["history-services"]["actions"] == [
        "Read",
        "Write",
        "List",
        "Tagging",
    ]
