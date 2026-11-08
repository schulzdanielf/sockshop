"""Enable namespace-scoped HTTP access logs without replacing the mesh config."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


HTTP_LOG_PROVIDER = {
    "name": "sock-shop-http-json",
    "envoyFileAccessLog": {
        "path": "/dev/stdout",
        "logFormat": {"labels": {
            "log_source": "istio_http_access",
            "timestamp": "%START_TIME%",
            "method": "%REQ(:METHOD)%",
            "authority": "%REQ(:AUTHORITY)%",
            "route": "%ROUTE_NAME%",
            "response_code": "%RESPONSE_CODE%",
            "response_flags": "%RESPONSE_FLAGS%",
            "response_code_details": "%RESPONSE_CODE_DETAILS%",
            "upstream_cluster": "%UPSTREAM_CLUSTER%",
            "upstream_host": "%UPSTREAM_HOST%",
            "duration_ms": "%DURATION%",
            "trace_id": "%REQ(X-B3-TRACEID)%",
            "traceparent": "%REQ(TRACEPARENT)%",
            "request_id": "%REQ(X-REQUEST-ID)%",
        }},
    },
}


def with_http_log_provider(mesh: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(mesh)
    providers = updated.setdefault("extensionProviders", [])
    if not isinstance(providers, list):
        raise ValueError("mesh extensionProviders must be a list")
    existing = [p for p in providers if p.get("name") == HTTP_LOG_PROVIDER["name"]]
    if existing and (len(existing) != 1 or existing[0] != HTTP_LOG_PROVIDER):
        raise ValueError("HTTP log provider already exists with different settings")
    if not existing:
        providers.append(copy.deepcopy(HTTP_LOG_PROVIDER))
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    resource = json.loads(subprocess.check_output(
        ["kubectl", "-n", "istio-system", "get", "configmap", "istio", "-o", "json"],
        text=True, timeout=30,
    ))
    mesh = yaml.safe_load(resource["data"]["mesh"])
    if not isinstance(mesh, dict):
        raise ValueError("Istio mesh config must be a mapping")
    updated = with_http_log_provider(mesh)
    if args.apply:
        if updated != mesh:
            patch = json.dumps({"data": {"mesh": yaml.safe_dump(updated, sort_keys=False)}})
            subprocess.run(
                ["kubectl", "-n", "istio-system", "patch", "configmap", "istio",
                 "--type=merge", "-p", patch], check=True, timeout=30,
            )
        manifest = Path(__file__).resolve().parents[3] / (
            "deploy/kubernetes/manifests-monitoring/71-sock-shop-http-telemetry.yaml"
        )
        subprocess.run(
            ["kubectl", "apply", "-f", str(manifest)], check=True, timeout=30,
        )
    print(json.dumps({"applied": args.apply, "mesh_changed": updated != mesh,
                      "provider": HTTP_LOG_PROVIDER["name"], "namespace": "sock-shop"}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, yaml.YAMLError) as exc:
        raise SystemExit(f"HTTP access log configuration failed: {exc}")
