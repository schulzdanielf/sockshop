#!/usr/bin/env python3
"""Initialize historical S3 buckets and their run-archive retention policy."""

from __future__ import annotations

import os

from minio import Minio
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule


BUCKETS = ("history-runs", "history-loki", "history-tempo")


def bootstrap_s3_storage(client: Minio) -> None:
    for bucket in BUCKETS:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    client.set_bucket_lifecycle(
        "history-runs",
        LifecycleConfig(
            [
                Rule(
                    status="Enabled",
                    rule_id="history-runs-expire-after-365-days",
                    expiration=Expiration(days=365),
                )
            ]
        ),
    )


def main() -> None:
    client = Minio(
        os.environ["HISTORY_S3_ENDPOINT"],
        access_key=os.environ["HISTORY_S3_ADMIN_ACCESS_KEY"],
        secret_key=os.environ["HISTORY_S3_ADMIN_SECRET_KEY"],
        secure=os.getenv("HISTORY_S3_SECURE", "false").lower() == "true",
    )
    bootstrap_s3_storage(client)
    print("Historical S3 buckets and retention policy initialized.")


if __name__ == "__main__":
    main()
