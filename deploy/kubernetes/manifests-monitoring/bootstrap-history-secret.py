#!/usr/bin/env python3
"""Create SeaweedFS S3 credentials without printing their values."""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import sys


NAMESPACE = "monitoring-history"
SECRET_NAME = "seaweedfs-history-credentials"


def _s3_config(
    admin_access_key: str,
    admin_secret_key: str,
    history_access_key: str,
    history_secret_key: str,
) -> str:
    config = {
        "identities": [
            {
                "name": "history-bootstrap",
                "credentials": [
                    {
                        "accessKey": admin_access_key,
                        "secretKey": admin_secret_key,
                    }
                ],
                "actions": ["Admin"],
            },
            {
                "name": "history-services",
                "credentials": [
                    {
                        "accessKey": history_access_key,
                        "secretKey": history_secret_key,
                    }
                ],
                "actions": ["Read", "Write", "List", "Tagging"],
            },
        ]
    }
    return json.dumps(config, separators=(",", ":"))


def main() -> int:
    existing = subprocess.run(
        [
            "kubectl",
            "get",
            "secret",
            SECRET_NAME,
            "-n",
            NAMESPACE,
            "-o",
            "name",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        print(f"{SECRET_NAME} already exists; leaving credentials unchanged.")
        return 0
    if "NotFound" not in existing.stderr:
        print(existing.stderr.strip(), file=sys.stderr)
        return existing.returncode or 1

    admin_access_key = secrets.token_hex(12)
    admin_secret_key = secrets.token_urlsafe(36)
    history_access_key = secrets.token_hex(12)
    history_secret_key = secrets.token_urlsafe(36)
    values = {
        "admin-access-key": admin_access_key,
        "admin-secret-key": admin_secret_key,
        "history-access-key": history_access_key,
        "history-secret-key": history_secret_key,
        "s3-config.json": _s3_config(
            admin_access_key,
            admin_secret_key,
            history_access_key,
            history_secret_key,
        ),
    }
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in values.items()
        },
    }
    created = subprocess.run(
        ["kubectl", "create", "-f", "-"],
        input=json.dumps(manifest),
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode:
        print(created.stderr.strip() or created.stdout.strip(), file=sys.stderr)
        return created.returncode
    print(created.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
