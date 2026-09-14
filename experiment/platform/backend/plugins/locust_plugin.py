"""Locust load-generation provider adapter.

``LocustLoadPlugin`` implements :class:`ports.LoadProviderPort`, shelling
out to Locust to drive synthetic traffic against the system under test
for the duration of an experiment.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import Any, Dict


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

    def _is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def status(self, context: Dict[str, Any], handle: Dict[str, Any]) -> Dict[str, Any]:
        pid = int(handle["pid"])
        state = "running" if self._is_running(pid) else "completed"
        return {"state": state, "exit_code": None}

    def stop_load(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        pid = int(handle["pid"])
        try:
            os.kill(pid, 15)
            return {"stopped": True}
        except Exception as exc:
            return {"stopped": False, "error": str(exc)}

    def collect_summary(
        self, context: Dict[str, Any], handle: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {
            "provider": "locust",
            "pid": handle.get("pid"),
            "run_time_seconds": handle.get("run_time_seconds"),
        }
