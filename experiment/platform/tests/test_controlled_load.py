import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from experiment.platform.backend.plugins.locust_plugin import (
    LocustLoadPlugin, _REMOTE_CONTROL, _REMOTE_START,
)


def test_controlled_load_resolves_exact_remote_container_without_local_pipes(monkeypatch):
    plugin = LocustLoadPlugin()
    kube = Mock(side_effect=[
        {"items": [{
            "metadata": {"name": "locust-1", "uid": "uid-1"},
            "status": {"containerStatuses": [{
                "name": "locust", "ready": True, "containerID": "container-1", "imageID": "image-1",
            }]},
        }]},
        {"remote_pid": 42, "start_ticks": "100", "log_path": "/tmp/run-1.log"},
    ])
    monkeypatch.setattr(plugin, "_kube_json", kube)
    handle = plugin.start_load({"run_id": "run-1"}, {
        "controlled_remote": True, "run_time_seconds": 1800,
        "users": 30, "spawn_rate": 5, "locust_file": "/journey.py", "host": "http://front-end",
    })
    assert handle["pod_uid"] == "uid-1" and handle["container_id"] == "container-1"
    assert handle["remote_pid"] == 42 and "pid" not in handle
    args = kube.call_args.args[0]
    assert args[:8] == ["-n", "loadtest", "exec", "locust-1", "-c", "locust", "--", "python"]
    command = json.loads(args[-1])
    assert command[0] == "locust"
    assert command[command.index("--users") + 1] == "30"


def test_remote_stop_refuses_changed_container_before_any_signal(monkeypatch):
    plugin = LocustLoadPlugin()
    kube = Mock(return_value={
        "metadata": {"uid": "changed"}, "status": {"containerStatuses": []},
    })
    monkeypatch.setattr(plugin, "_kube_json", kube)
    with pytest.raises(RuntimeError, match="identity changed"):
        plugin.stop_load({}, {"controlled_remote": True, "pod": "locust-1", "pod_uid": "original", "container_id": "original"})
    assert kube.call_count == 1


def test_remote_helpers_stop_the_specific_spawned_process_and_preserve_log(tmp_path):
    start = subprocess.run([
        sys.executable, "-c", _REMOTE_START, str(tmp_path / "load"),
        json.dumps([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(10)"]),
    ], capture_output=True, text=True, check=True, timeout=5)
    handle = json.loads(start.stdout)
    stopped = subprocess.run(
        [sys.executable, "-c", _REMOTE_CONTROL, json.dumps(handle), "stop"],
        capture_output=True, text=True, check=True, timeout=25,
    )
    assert json.loads(stopped.stdout)["stopped"] is True
    summary = subprocess.run(
        [sys.executable, "-c", _REMOTE_CONTROL, json.dumps(handle), "summary"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert json.loads(summary.stdout)["truncated"] is False
