from __future__ import annotations

import json
from unittest.mock import Mock, patch

import yaml

from experiment.platform.backend.plugins.litmus_plugin import LitmusChaosPlugin


def test_inject_overrides_stale_embedded_resource_tunables():
    workflow = {
        "metadata": {"name": "carts-cpu-hog-1234567890123", "labels": {}},
        "spec": {
            "templates": [
                {
                    "inputs": {
                        "artifacts": [
                            {
                                "raw": {
                                    "data": """apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
spec:
  experiments:
  - name: pod-cpu-hog
    spec:
      components:
        env:
        - name: CPU_CORES
          value: '1'
        - name: TOTAL_CHAOS_DURATION
          value: '30'
"""
                                }
                            }
                        ]
                    }
                }
            ]
        },
    }
    resource_chaos = {
        "experiment_name": "pod-cpu-hog",
        "target_container": "carts",
        "container_limits": {"cpu": "300m", "memory": "500Mi"},
        "environment": {
            "CPU_CORES": "0",
            "CPU_LOAD": "100",
            "TOTAL_CHAOS_DURATION": "120",
            "TARGET_CONTAINER": "carts",
        },
    }
    create_result = Mock(
        returncode=0,
        stdout="workflow.argoproj.io/carts-cpu-hog-abc created",
        stderr="",
    )
    with patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        side_effect=[
            Mock(returncode=0, stdout=json.dumps({"items": [workflow]}), stderr=""),
            create_result,
        ],
    ) as run:
        handle = LitmusChaosPlugin().inject(
            {},
            {
                "provider": "litmus",
                "namespace": "sock-shop",
                "chaos_engine": "carts-cpu-hog",
                "resource_chaos": resource_chaos,
            },
        )

    submitted = json.loads(run.call_args_list[1].kwargs["input"])
    embedded_engine = submitted["spec"]["templates"][0]["inputs"]["artifacts"][0]["raw"]["data"]
    env = {
        item["name"]: item["value"]
        for item in yaml.safe_load(embedded_engine)["spec"]["experiments"][0]["spec"]["components"]["env"]
    }
    assert env == {
        "CPU_CORES": "0",
        "TOTAL_CHAOS_DURATION": "120",
        "CPU_LOAD": "100",
        "TARGET_CONTAINER": "carts",
    }
    assert handle["resource_chaos"] == resource_chaos
    assert workflow["spec"]["templates"][0]["inputs"]["artifacts"][0]["raw"]["data"].find("value: '1'") >= 0


def test_inject_overrides_fault_duration_for_non_resource_experiment():
    workflow = {
        "metadata": {"name": "orders-network-loss-1234567890123", "labels": {}},
        "spec": {
            "templates": [
                {
                    "inputs": {
                        "artifacts": [
                            {
                                "raw": {
                                    "data": """apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
spec:
  experiments:
  - name: pod-network-loss
    spec:
      components:
        env:
        - name: TOTAL_CHAOS_DURATION
          value: '60'
"""
                                }
                            }
                        ]
                    }
                }
            ]
        },
    }
    create_result = Mock(
        returncode=0,
        stdout="workflow.argoproj.io/orders-network-loss-abc created",
        stderr="",
    )
    with patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        side_effect=[
            Mock(returncode=0, stdout=json.dumps({"items": [workflow]}), stderr=""),
            create_result,
        ],
    ) as run:
        LitmusChaosPlugin().inject(
            {},
            {
                "provider": "litmus",
                "namespace": "sock-shop",
                "chaos_engine": "orders-network-loss",
                "fault_duration_seconds": 120,
            },
        )

    submitted = json.loads(run.call_args_list[1].kwargs["input"])
    embedded_engine = submitted["spec"]["templates"][0]["inputs"]["artifacts"][0]["raw"]["data"]
    env = yaml.safe_load(embedded_engine)["spec"]["experiments"][0]["spec"]["components"]["env"]
    assert next(item["value"] for item in env if item["name"] == "TOTAL_CHAOS_DURATION") == "120"


def test_stop_persists_litmus_verdict_before_deleting_workflow():
    plugin = LitmusChaosPlugin()
    handle = {
        "mode": "argo_workflow",
        "workflow_name": "payment-cpu-hog-abc",
        "workflow_template": "payment-cpu-hog",
    }
    final_status = {
        "state": "completed",
        "workflow_phase": "Succeeded",
        "chaos_phase": "Completed",
        "chaos_verdict": "Pass",
    }
    with patch.object(plugin, "status", return_value=final_status), patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        return_value=Mock(returncode=0, stdout="deleted", stderr=""),
    ) as kubectl:
        plugin.stop({}, handle)
        artifacts = plugin.collect_artifacts({}, handle)

    assert handle["final_status"] == final_status
    assert artifacts["final_status"]["chaos_verdict"] == "Pass"
    assert kubectl.call_args.args[0][:3] == [
        "kubectl", "delete", "workflow.argoproj.io"
    ]


def test_stop_waits_for_terminal_litmus_verdict_before_deleting_workflow():
    plugin = LitmusChaosPlugin()
    handle = {
        "mode": "argo_workflow",
        "workflow_name": "payment-cpu-hog-abc",
        "workflow_template": "payment-cpu-hog",
    }
    statuses = [
        {
            "state": "running",
            "workflow_phase": "Running",
            "chaos_phase": "Running",
            "chaos_verdict": "Awaited",
        },
        {
            "state": "completed",
            "workflow_phase": "Succeeded",
            "chaos_phase": "Completed",
            "chaos_verdict": "Pass",
        },
    ]
    with patch.object(plugin, "status", side_effect=statuses) as status, patch(
        "experiment.platform.backend.plugins.litmus_plugin.time.sleep"
    ), patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        return_value=Mock(returncode=0, stdout="deleted", stderr=""),
    ) as kubectl:
        plugin.stop({}, handle)

    assert status.call_count == 2
    assert handle["final_status"]["chaos_verdict"] == "Pass"
    assert kubectl.call_args.args[0][:3] == [
        "kubectl", "delete", "workflow.argoproj.io"
    ]


def test_status_exposes_chaosresult_creation_timestamp():
    workflow = {
        "metadata": {"uid": "workflow-uid"},
        "status": {"phase": "Succeeded"},
    }
    chaos_results = {
        "items": [
            {
                "metadata": {"creationTimestamp": "2026-09-25T10:02:37Z"},
                "status": {
                    "experimentStatus": {"phase": "Completed", "verdict": "Pass"}
                },
            }
        ]
    }
    with patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        side_effect=[
            Mock(returncode=0, stdout=json.dumps(workflow), stderr=""),
            Mock(returncode=0, stdout=json.dumps(chaos_results), stderr=""),
        ],
    ):
        status = LitmusChaosPlugin().status(
            {},
            {
                "mode": "argo_workflow",
                "workflow_name": "payment-cpu-hog-abc",
            },
        )

    assert status["chaos_started_at"] == "2026-09-25T10:02:37Z"
    assert status["chaos_result_created_at"] == "2026-09-25T10:02:37Z"
    assert status["chaos_window_confirmed"] is True


def test_status_does_not_confirm_chaosresult_creation_on_error():
    workflow = {
        "metadata": {"uid": "workflow-uid"},
        "status": {"phase": "Running"},
    }
    chaos_results = {
        "items": [
            {
                "metadata": {"creationTimestamp": "2026-09-25T10:02:37Z"},
                "status": {
                    "experimentStatus": {"phase": "Error", "verdict": "Error"}
                },
            }
        ]
    }
    with patch(
        "experiment.platform.backend.plugins.litmus_plugin.subprocess.run",
        side_effect=[
            Mock(returncode=0, stdout=json.dumps(workflow), stderr=""),
            Mock(returncode=0, stdout=json.dumps(chaos_results), stderr=""),
        ],
    ):
        status = LitmusChaosPlugin().status(
            {},
            {
                "mode": "argo_workflow",
                "workflow_name": "payment-cpu-hog-abc",
            },
        )

    assert status["chaos_result_created_at"] == "2026-09-25T10:02:37Z"
    assert status["chaos_started_at"] is None
    assert status["chaos_window_confirmed"] is False
