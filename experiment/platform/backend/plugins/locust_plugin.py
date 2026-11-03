"""Locust load-generation provider adapter.

``LocustLoadPlugin`` implements :class:`ports.LoadProviderPort`, shelling
out to Locust to drive synthetic traffic against the system under test
for the duration of an experiment.
"""

from __future__ import annotations

import os
import json
import shlex
import subprocess
import time
from typing import Any, Dict


_REMOTE_START = """
import json, os, subprocess, sys
path, argv = sys.argv[1], json.loads(sys.argv[2])
with open(path + '.json', 'x') as identity, open(path + '.log', 'x') as output:
    process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    with open('/proc/%d/stat' % process.pid) as stat:
        ticks = stat.read().rpartition(')')[2].split()[19]
    record = {'remote_pid': process.pid, 'start_ticks': ticks, 'log_path': path + '.log'}
    json.dump(record, identity)
print(json.dumps(record))
"""

_REMOTE_CONTROL = """
import json, os, signal, sys, time
record, action = json.loads(sys.argv[1]), sys.argv[2]
pid = record['remote_pid']
def running():
    try:
        with open('/proc/%d/stat' % pid) as stat:
            fields = stat.read().rpartition(')')[2].split()
        return fields[0] != 'Z' and fields[19] == record['start_ticks']
    except FileNotFoundError:
        return False
if action == 'stop' and running():
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while running() and time.monotonic() < deadline:
        time.sleep(.2)
if action == 'summary':
    with open(record['log_path'], 'rb') as output:
        size = output.seek(0, 2)
        output.seek(max(0, size - 200000))
        result = {'log_tail': output.read().decode('utf-8', errors='replace'),
                  'truncated': size > 200000, 'log_bytes': size}
else:
    active = running()
    result = {'state': 'running' if active else 'completed', 'stopped': not active,
              'identity_source': 'pod_uid_and_proc_start_ticks'}
print(json.dumps(result))
"""


class LocustLoadPlugin:
    def validate(self, config: Dict[str, Any]) -> None:
        if config.get("provider") != "locust":
            raise ValueError("load_profile.provider must be 'locust'")
        for required in ["locust_file", "host"]:
            if not config.get(required):
                raise ValueError(f"load_profile.{required} is required")

    def prepare(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {"ok": True}

    def start_load(
        self, context: Dict[str, Any], config: Dict[str, Any]
    ) -> Dict[str, Any]:
        run_time_seconds = int(config["run_time_seconds"])
        base_cmd = config.get(
            "command", "kubectl -n loadtest exec deploy/locust-web -- locust"
        )
        cmd = shlex.split(base_cmd) + [
            "-f",
            str(config["locust_file"]),
            "--host",
            str(config["host"]),
            "--headless",
            "--users",
            str(config.get("users", 20)),
            "--spawn-rate",
            str(config.get("spawn_rate", 5.0)),
            "--run-time",
            f"{run_time_seconds}s",
        ]
        extra_args = config.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(str(x) for x in extra_args)

        if config.get("controlled_remote"):
            return self._start_remote(context, cmd, run_time_seconds)
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return {
            "provider": "locust",
            "pid": process.pid,
            "started_at_epoch": time.time(),
            "command": cmd,
            "run_time_seconds": run_time_seconds,
            "grace_seconds": int(config.get("grace_seconds", 30)),
        }

    def _kube_json(self, args: list[str]) -> Dict[str, Any]:
        result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=35, check=False)
        if result.returncode:
            raise RuntimeError(f"controlled load kubectl failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def _start_remote(self, context: Dict[str, Any], cmd: list[str], duration: int) -> Dict[str, Any]:
        expected = ["kubectl", "-n", "loadtest", "exec", "deploy/locust-web", "--", "locust"]
        if cmd[:len(expected)] != expected:
            raise ValueError("controlled remote load requires the existing locust-web command")
        pods = self._kube_json(["-n", "loadtest", "get", "pods", "-l", "app=locust-web", "-o", "json"])
        if len(pods["items"]) != 1:
            raise RuntimeError("controlled load requires exactly one locust-web pod")
        pod = pods["items"][0]
        statuses = [s for s in pod["status"].get("containerStatuses", []) if s["name"] == "locust"]
        if len(statuses) != 1 or not statuses[0].get("ready"):
            raise RuntimeError("locust application container is not ready")
        import re
        run_id = str(context["run_id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("invalid controlled load run identifier")
        handle = {
            "provider": "locust", "controlled_remote": True,
            "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
            "container_id": statuses[0].get("containerID"),
            "image_id": statuses[0].get("imageID"), "run_time_seconds": duration,
            "started_at_epoch": time.time(), "command": cmd[len(expected) - 1:],
        }
        handle.update(self._kube_json([
            "-n", "loadtest", "exec", handle["pod"], "-c", "locust", "--", "python", "-c",
            _REMOTE_START, f"/tmp/controlled-load-{run_id}", json.dumps(handle["command"]),
        ]))
        return handle

    def _remote_control(self, handle: Dict[str, Any], action: str) -> Dict[str, Any]:
        pod = self._kube_json(["-n", "loadtest", "get", "pod", handle["pod"], "-o", "json"])
        statuses = pod.get("status", {}).get("containerStatuses", [])
        current = next((s for s in statuses if s["name"] == "locust"), {})
        if pod["metadata"]["uid"] != handle["pod_uid"] or current.get("containerID") != handle["container_id"]:
            raise RuntimeError("load pod/container identity changed; cannot confirm original remote process cleanup")
        return self._kube_json([
            "-n", "loadtest", "exec", handle["pod"], "-c", "locust", "--", "python", "-c",
            _REMOTE_CONTROL, json.dumps(handle), action,
        ])

    def _is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def status(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        if handle.get("controlled_remote"):
            return self._remote_control(handle, "status")
        pid = int(handle["pid"])
        state = "running" if self._is_running(pid) else "completed"
        return {"state": state, "exit_code": None}

    def stop_load(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        if handle.get("controlled_remote"):
            return self._remote_control(handle, "stop")
        pid = int(handle["pid"])
        try:
            os.kill(pid, 15)
            return {"stopped": True}
        except Exception as exc:
            return {"stopped": False, "error": str(exc)}

    def collect_summary(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        if handle.get("controlled_remote"):
            return {**handle, **self._remote_control(handle, "summary")}
        return {
            "provider": "locust",
            "pid": handle.get("pid"),
            "run_time_seconds": handle.get("run_time_seconds"),
        }
