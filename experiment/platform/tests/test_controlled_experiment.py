from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
import copy

import pytest
import threading
import yaml

from experiment.platform.backend import controlled_experiment as controlled
from experiment.platform.backend.domain import ExperimentVersion, RunRecord, RunStatus
from experiment.platform.backend.engine import OrchestratorEngine


def test_memory_budget_uses_working_set_p95_margin_and_one_worker():
    profile = controlled.resource_environment(
        "memory-hog", {"cpu": "300m", "memory": "512Mi"},
        {"memory_bytes": {"p95": 200 * 2**20}}, {},
    )
    assert profile["environment"] == {
        "MEMORY_CONSUMPTION": "184", "NUMBER_OF_WORKERS": "1", "TOTAL_CHAOS_DURATION": "180",
    }
    assert profile["additional_bytes"] == 184 * 2**20


@pytest.mark.parametrize("memory", [None, "0", "bad"])
def test_memory_requires_positive_known_limit(memory):
    with pytest.raises(ValueError):
        controlled.resource_environment("memory-hog", {"cpu": "300m", "memory": memory},
                                        {"memory_bytes": {"p95": 100}}, {})


def test_memory_no_headroom_blocks_instead_of_fixed_70_percent():
    with pytest.raises(controlled.GateBlocked, match="headroom"):
        controlled.resource_environment(
            "memory-hog", {"cpu": "300m", "memory": "512Mi"},
            {"memory_bytes": {"p95": 390 * 2**20}}, {},
        )


def test_cpu_requires_profile_and_does_not_equate_load_to_quota():
    limits = {"cpu": "300m", "memory": "512Mi"}
    baseline = {"cpu_cores": {"p95": .03}}
    with pytest.raises(controlled.GateBlocked, match="candidate profile"):
        controlled.resource_environment("cpu-hog", limits, baseline, {})
    profile = controlled.resource_environment("cpu-hog", limits, baseline, {
        "cpu": {"cores": 1, "load": 20, "evidence": "calibration-run", "target_quota_fraction": .8},
    })
    assert profile["environment"]["CPU_LOAD"] == "20"
    assert profile["environment"]["CPU_CORES"] == "1"
    assert profile["quota_cores"] == .3
    with pytest.raises(controlled.GateBlocked, match="baseline already high"):
        controlled.resource_environment("cpu-hog", limits, {"cpu_cores": {"p95": .23}}, {
            "cpu": {"cores": 1, "load": 20, "evidence": "calibration-run"},
        })
    candidate = controlled.resource_environment("cpu-hog", limits, baseline, {
        "cpu": {
            "cores": 1, "load": 100, "target_quota_fraction": 1.0,
            "calibration_status": "candidate", "rationale": "verify in this run",
        },
    })
    assert candidate["candidate_target_quota_fraction"] == 1.0
    assert candidate["calibration_status"] == "candidate"


@pytest.mark.parametrize("key,value", [
    ("cores", True), ("cores", 1.5), ("cores", 0),
    ("load", 100.5), ("load", "100"), ("load", 101),
    ("target_quota_fraction", float("nan")), ("target_quota_fraction", 1.1),
    ("target_tolerance_fraction", float("inf")), ("target_tolerance_fraction", False),
])
def test_cpu_profile_rejects_malformed_values_before_a_campaign(key, value):
    profile = {"cores": 1, "load": 100, "evidence": "calibration"}
    profile[key] = value
    with pytest.raises(controlled.GateBlocked, match="invalid candidate CPU"):
        controlled.validate_cpu_profile({"cpu": profile})


@pytest.mark.parametrize("fraction,passes", [(.699, True), (.70, False), (.92, False)])
def test_cpu_baseline_gate_keeps_exact_seventy_percent_boundary(fraction, passes):
    args = ("cpu-hog", {"cpu": "300m", "memory": "512Mi"},
            {"cpu_cores": {"p95": .3 * fraction}},
            {"cpu": {"cores": 1, "load": 100, "target_quota_fraction": 1., "evidence": "calibration"}})
    if passes:
        controlled.resource_environment(*args)
    else:
        with pytest.raises(controlled.GateBlocked, match="CPU baseline already high.*fraction="):
            controlled.resource_environment(*args)


def test_orchestration_budget_keeps_stage_limits_and_outlives_load():
    budget = controlled.build_time_budget({
        "workflow_timeout_seconds": 600, "recovery_timeout_seconds": 300,
        "resilience": {"operational_timeout_seconds": 600},
    })
    assert budget["stages"]["experimental_recovery"] == 300
    assert budget["stages"]["operational_recovery"] == 600
    assert budget["load_seconds"] == sum(budget["stages"].values())
    assert budget["load_seconds"] > 1800
    assert budget["poll_seconds"] == budget["load_seconds"] + 300
    assert controlled.controlled_time_budget({}) == {
        "version": 0, "load_seconds": 1800, "poll_seconds": 1800,
    }


@pytest.mark.parametrize("budget", [
    {"version": 1, "load_seconds": 1800, "poll_seconds": 1800},
    {"version": 1, "load_seconds": True, "poll_seconds": 3000},
    {"version": 1, "load_seconds": 9000, "poll_seconds": 9300},
])
def test_invalid_orchestration_budgets_fail_closed(budget):
    with pytest.raises(controlled.GateBlocked, match="time budget"):
        controlled.controlled_time_budget({"time_budget": budget})


def test_cpu_rates_do_not_cross_container_identity_even_with_increasing_counters():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    common = {"cpu_quota_cores": .3, "memory_usage_bytes": 100, "memory_limit_bytes": 1000,
          "oom_events": 0, "oom_kill_events": 0, "nr_throttled": 0, "throttled_usec": 0}
    run.cgroup_samples = [
        {**common, "timestamp": 100, "container_id": "old", "cpu_usage_usec": 1_000_000},
        {**common, "timestamp": 105, "container_id": "new", "cpu_usage_usec": 2_000_000},
        {**common, "timestamp": 110, "container_id": "new", "cpu_usage_usec": 3_000_000},
    ]
    start = datetime.fromtimestamp(100, timezone.utc).isoformat()
    end = datetime.fromtimestamp(110, timezone.utc).isoformat()
    payloads, telemetry = run.cgroup_payloads(start, end)
    assert controlled.samples(payloads["cpu_cores"]) == [(110, .2)]
    assert telemetry["cpu_rate_intervals_skipped_for_identity_change"] == 1


def test_sustained_increase_requires_duration_not_isolated_peaks_or_gaps():
    assert controlled.sustained([(0, 2), (20, 2), (40, 2), (60, 2)], 1, 60, 20)["passed"]
    assert not controlled.sustained([(0, 2), (60, 2)], 1, 60, 20)["passed"]
    assert not controlled.sustained([(0, 2), (20, 0), (40, 2), (60, 2)], 1, 60, 20)["passed"]


@pytest.mark.parametrize("payload", [
    {"status": "error", "error": "upstream error"},
    {"status": "success", "data": {"result": [{"values": [[1, "NaN"]]}]}},
    {"status": "success", "data": {"result": [{"values": [[1, "1"]]}, {"values": [[1, "2"]]}]}},
])
def test_invalid_metrics_are_not_converted_to_zero(payload):
    with pytest.raises(controlled.GateBlocked):
        controlled.samples(payload)


def test_empty_metrics_remain_no_data():
    assert controlled.stats(controlled.samples({"status": "success", "data": {"result": []}})) == {
        "status": "no_data", "samples": 0,
    }


def test_parse_cgroup_v2_captures_limits_usage_throttling_and_oom_counters():
    parsed = controlled.parse_cgroup_v2(
        "__cpu_max__\n30000 100000\n"
        "__cpu_stat__\nusage_usec 5000\nnr_periods 20\nnr_throttled 3\nthrottled_usec 1000\n"
        "__memory_current__\n1048576\n"
        "__memory_max__\n268435456\n"
        "__memory_events__\nlow 0\nhigh 0\nmax 0\noom 1\noom_kill 0\n"
    )
    assert parsed["cpu_quota_cores"] == .3
    assert parsed["cpu_usage_usec"] == 5000
    assert parsed["nr_throttled"] == 3
    assert parsed["throttled_usec"] == 1000
    assert parsed["memory_usage_bytes"] == 1048576
    assert parsed["memory_limit_bytes"] == 268435456
    assert parsed["oom_events"] == 1
    assert parsed["oom_kill_events"] == 0


@pytest.mark.parametrize("cpu_max,memory_max", [("max 100000", "268435456"), ("30000 100000", "max")])
def test_parse_cgroup_v2_rejects_unlimited_limits(cpu_max, memory_max):
    text = (
        f"__cpu_max__\n{cpu_max}\n"
        "__cpu_stat__\nusage_usec 1\nnr_throttled 0\nthrottled_usec 0\n"
        "__memory_current__\n100\n"
        f"__memory_max__\n{memory_max}\n"
        "__memory_events__\noom 0\noom_kill 0\n"
    )
    with pytest.raises(controlled.GateBlocked, match="finite|unlimited"):
        controlled.parse_cgroup_v2(text)


def test_cgroup_series_use_container_counter_deltas_and_keep_raw_samples():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.target = {"pod": "orders-pod"}
    run.cgroup_samples = [
        {"timestamp": 100, "cpu_usage_usec": 1_000_000, "nr_throttled": 10, "throttled_usec": 100,
         "cpu_quota_cores": .3, "memory_usage_bytes": 400, "memory_limit_bytes": 1000,
         "oom_events": 0, "oom_kill_events": 0},
        {"timestamp": 105, "cpu_usage_usec": 2_000_000, "nr_throttled": 15, "throttled_usec": 200,
         "cpu_quota_cores": .3, "memory_usage_bytes": 500, "memory_limit_bytes": 1000,
         "oom_events": 0, "oom_kill_events": 0},
        {"timestamp": 110, "cpu_usage_usec": 3_000_000, "nr_throttled": 20, "throttled_usec": 300,
         "cpu_quota_cores": .3, "memory_usage_bytes": 600, "memory_limit_bytes": 1000,
         "oom_events": 1, "oom_kill_events": 0},
    ]
    start = datetime.fromtimestamp(104, timezone.utc).isoformat()
    end = datetime.fromtimestamp(111, timezone.utc).isoformat()
    payloads, telemetry = run.cgroup_payloads(start, end)

    assert controlled.samples(payloads["cpu_cores"]) == [(105, .2), (110, .2)]
    assert controlled.samples(payloads["memory_bytes"]) == [(105, 500), (110, 600)]
    assert controlled.samples(payloads["oom_events"]) == [(105, 0), (110, 1)]
    assert controlled.samples(payloads["throttled_periods_per_second"]) == [(105, 1), (110, 1)]
    assert telemetry["sample_count"] == 2
    assert telemetry["cpu_predecessor_sample"]["timestamp"] == 100


def test_collect_uses_cgroup_for_container_resources_and_prometheus_for_red():
    engine = Mock()
    engine.metrics.collect_window.return_value = {
        "status": "success",
        "data": {"traffic": {"status": "success", "data": {"result": []}}},
    }
    run = controlled.ControlledRun(engine, "run-1", make_spec("cpu-hog"))
    run.target = {"pod": "orders-pod"}
    run.cgroup_sampling_enabled = True
    run.cgroup_samples = [
        {"timestamp": 100, "cpu_usage_usec": 1_000_000, "nr_throttled": 10, "throttled_usec": 100,
         "cpu_quota_cores": .3, "memory_usage_bytes": 400, "memory_limit_bytes": 1000,
         "oom_events": 0, "oom_kill_events": 0},
        {"timestamp": 105, "cpu_usage_usec": 2_000_000, "nr_throttled": 15, "throttled_usec": 200,
         "cpu_quota_cores": .3, "memory_usage_bytes": 500, "memory_limit_bytes": 1000,
         "oom_events": 0, "oom_kill_events": 0},
        {"timestamp": 110, "cpu_usage_usec": 3_000_000, "nr_throttled": 20, "throttled_usec": 300,
         "cpu_quota_cores": .3, "memory_usage_bytes": 600, "memory_limit_bytes": 1000,
         "oom_events": 0, "oom_kill_events": 0},
    ]
    start = datetime.fromtimestamp(100, timezone.utc).isoformat()
    end = datetime.fromtimestamp(110, timezone.utc).isoformat()

    summary = run.collect("baseline", start, end)

    query_ids = {query["id"] for query in engine.metrics.collect_window.call_args.args[1]["queries"]}
    assert not query_ids.intersection(controlled.CGROUP_RESOURCE_KEYS)
    assert summary["cpu_cores"]["samples"] == 2
    assert summary["memory_bytes"]["p95"] == 500
    assert run.raw["baseline"]["resource_telemetry"]["source"] == "cgroup-v2"


def test_error_rate_zero_requires_positive_traffic_and_preserves_missing_5xx_series():
    def series(values):
        return {
            "status": "success",
            "data": {"result": [{"metric": {}, "values": [[index, str(value)] for index, value in enumerate(values, 1)]}]},
        }

    engine = Mock()
    engine.metrics.collect_window.return_value = {"data": {
        "traffic": series([2, 2, 2, 2]),
        "errors_5xx_rate": {"status": "success", "data": {"result": []}},
        "errors_percent": series([0, 0, 0, 0]),
        "target_edge_requests": series([1, 1, 1, 1]),
        "target_edge_5xx": {"status": "success", "data": {"result": []}},
        "target_edge_error_percent": series([0, 0, 0, 0]),
    }}
    run = controlled.ControlledRun(engine, "run-1", make_spec())
    run.target = {"pod": "orders-pod"}

    summary = run.collect(
        "baseline",
        datetime.fromtimestamp(1, timezone.utc).isoformat(),
        datetime.fromtimestamp(4, timezone.utc).isoformat(),
    )

    assert summary["errors_percent"]["mean"] == 0
    assert summary["errors_5xx_rate"] == {"status": "no_data", "samples": 0}
    basis = run.report["observations"][-1]["error_rate_basis"]
    assert basis["errors_percent"]["status"] == "zero_with_positive_request_rate"
    assert basis["errors_percent"]["numerator_series"] == "no_data"
    assert basis["target_edge_error_percent"]["status"] == "zero_with_positive_request_rate"
    assert 'reporter="source"' in run.queries()["target_edge_latency_ms"]
    assert 'response_code=~"5.."' in run.queries()["target_edge_5xx"]


def test_network_requires_caller_reported_edge_latency_coverage():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("network-latency"))
    summary = {
        key: {"status": "ok", "samples": 4, "start": 0, "end": 30, "mean": 1, "p95": 1}
        for key in (
            "cpu_cores", "memory_bytes", "traffic", "errors_percent", "latency_ms",
            "checkout_orders", "checkout_payment", "checkout_shipping", "target_edge_requests",
        )
    }
    with pytest.raises(controlled.GateBlocked, match="target_edge_latency_ms coverage"):
        run.require_coverage(summary, 30)


def test_front_end_http_target_edge_uses_destination_reporter_for_external_load():
    spec = make_spec("http-status-code")
    spec["controlled_campaign"]["service"] = "front-end"
    run = controlled.ControlledRun(Mock(), "run-front-end", spec)
    run.target = {"pod": "front-end-pod"}

    queries = run.queries()

    for key in ("target_edge_requests", "target_edge_5xx", "target_edge_latency_ms"):
        assert 'destination_workload="front-end"' in queries[key]
        assert 'reporter="destination"' in queries[key]
        assert 'source_workload!="unknown"' not in queries[key]


def test_network_effect_uses_caller_side_latency_and_reports_degradation_onset():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("network-latency"))
    run.baseline = {
        "target_edge_latency_ms": {"status": "ok", "mean": 20, "p95": 22},
        "latency_ms": {"status": "ok", "mean": 7, "p95": 10},
        "errors_percent": {"status": "ok", "mean": 0, "p95": 0},
    }
    run.report["network_rule_validation"] = {"passed": True}
    run.window = {"phase_sources": {"fault": "chaos_result_created_at_plus_configured_duration"}}
    run.raw["intervention"] = {"data": {
        "target_edge_latency_ms": {"status": "success", "data": {"result": [{
            "values": [[timestamp, "250"] for timestamp in range(15, 181, 15)],
        }]}},
    }}

    run.evaluate_effect({
        "target_edge_latency_ms": {"status": "ok", "mean": 250, "p95": 250},
        "latency_ms": {"status": "ok", "mean": 7, "p95": 10},
        "errors_percent": {"status": "ok", "mean": 0},
    }, "1970-01-01T00:00:00+00:00")

    gates = run.report["gates"]
    assert gates["intervention_confirmed_on_target"] is True
    assert gates["mechanism_observed"]["metric"] == "target_edge_latency_ms"
    assert gates["mechanism_observed"]["passed"] is True
    assert gates["impact_observed"]["latency_increased"] is True
    assert gates["latency_comparison"]["latency_metric"] == "target_edge_latency_ms"
    assert run.report["timestamps"]["degradation_onset"]["metric"] == "target_edge_latency_ms"
    assert run.report["timestamps"]["degradation_onset"]["at"] == "1970-01-01T00:00:15+00:00"


@pytest.mark.parametrize("sidecar_ready,expected", [(True, "istio-proxy"), (False, "orders")])
def test_qdisc_readback_uses_ready_sidecar_in_shared_pod_network_namespace(
    monkeypatch, sidecar_ready, expected,
):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("network-latency"))
    run.health = Mock(return_value={"healthy": True})
    run.pods = Mock(return_value={"items": [{
        "metadata": {"name": "orders-pod", "uid": "pod-uid"},
        "spec": {
            "nodeName": "node-1",
            "containers": [
                {"name": "orders", "image": "orders-image", "resources": {"limits": {}}},
                {"name": "istio-proxy", "image": "proxy-image"},
            ],
        },
        "status": {"containerStatuses": [
            {"name": "orders", "ready": True, "restartCount": 0},
            {"name": "istio-proxy", "ready": sidecar_ready, "restartCount": 0},
        ]},
    }]})
    run.save = Mock()
    run.checkpoint = Mock()
    calls = []
    monkeypatch.setattr(controlled, "kubectl", lambda *args: calls.append(args) or [])

    run.inspect_target()
    run.qdisc()

    assert run.target["network_inspection_container"] == expected
    call = calls[-1]
    assert call[call.index("-c") + 1] == expected


def test_cgroup_sampler_execs_inside_the_named_application_container(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.target = {"pod": "orders-pod", "uid": "pod-uid", "container": "orders", "container_id": "containerd://id"}
    output = (
        "__cpu_max__\n30000 100000\n"
        "__cpu_stat__\nusage_usec 5000\nnr_throttled 3\nthrottled_usec 1000\n"
        "__memory_current__\n1048576\n__memory_max__\n268435456\n"
        "__memory_events__\noom 0\noom_kill 0\n"
    )
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout=output, stderr=""))
    monkeypatch.setattr(controlled.subprocess, "run", command)

    run.start_cgroup_sampling()

    args = command.call_args.args[0]
    assert args[args.index("-c") + 1] == "orders"
    assert args[-3:-1] == ["sh", "-c"]
    assert run.cgroup_samples[0]["cpu_quota_cores"] == .3
    assert run.report["telemetry"]["cgroup_v2"]["container_id_before"] == "containerd://id"


def test_cgroup_sampler_retries_one_recorded_read_timeout(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.target = {
        "pod": "orders-pod", "uid": "pod-uid", "container": "orders",
        "container_id": "containerd://id",
    }
    output = (
        "__cpu_max__\n30000 100000\n"
        "__cpu_stat__\nusage_usec 5000\nnr_throttled 3\nthrottled_usec 1000\n"
        "__memory_current__\n1048576\n__memory_max__\n268435456\n"
        "__memory_events__\noom 0\noom_kill 0\n"
    )
    command = Mock(side_effect=[
        controlled.subprocess.TimeoutExpired("kubectl exec", timeout=30),
        controlled.subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr=""),
    ])
    monkeypatch.setattr(controlled.subprocess, "run", command)

    run.start_cgroup_sampling()

    assert command.call_count == 2
    assert run.cgroup_samples[0]["cpu_quota_cores"] == .3
    retries = run.report["telemetry"]["cgroup_v2_read_retries"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["timeout_seconds"] == 30
    assert "timed out" in retries[0]["error"]


def test_cgroup_failure_records_restart_and_events_without_hiding_the_error(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("network-latency"))
    run.target = {"pod": "orders-pod", "uid": "pod-uid", "container": "orders",
                  "container_id": "old", "restart_count": 5}
    pod = {"metadata": {"name": "orders-pod", "uid": "pod-uid"},
           "status": {"containerStatuses": [
               {"name": "orders", "containerID": "new", "restartCount": 6,
                "ready": False, "state": {"running": {}},
                "lastState": {"terminated": {"reason": "Completed", "exitCode": 0}}},
           ]}}
    run.pods = Mock(return_value={"items": [pod]})
    events = {"items": [{"reason": "Killing", "message": "failed liveness probe"}]}
    monkeypatch.setattr(controlled, "kubectl", Mock(return_value=events))
    monkeypatch.setattr(controlled.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr='container not found ("orders")',
    )))
    with pytest.raises(controlled.GateBlocked, match="container not found"):
        run.sample_cgroup()
    failure = run.report["telemetry"]["cgroup_v2_read_failures"][0]
    assert failure["expected_container_id"] == "old"
    assert failure["observed_pods"][0]["containers"][0]["restartCount"] == 6
    assert failure["observed_pods"][0]["containers"][0]["lastState"]["terminated"]["exitCode"] == 0
    assert failure["events_status"] == "ok"
    assert run.cgroup_samples == []


def test_cgroup_failure_preserves_unavailable_lifecycle_data(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.target = {"pod": "orders-pod", "uid": "pod-uid", "container": "orders",
                  "container_id": "old"}
    run.pods = Mock(side_effect=RuntimeError("API unavailable"))
    monkeypatch.setattr(controlled, "kubectl", Mock(side_effect=RuntimeError("events unavailable")))
    monkeypatch.setattr(controlled.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="exec failed",
    )))
    with pytest.raises(controlled.GateBlocked, match="exec failed"):
        run.sample_cgroup()
    failure = run.report["telemetry"]["cgroup_v2_read_failures"][0]
    assert failure["pods_error"] == "API unavailable"
    assert failure["events_error"] == "events unavailable"
    assert "observed_pods" not in failure


@pytest.mark.parametrize("filename", [
    "deploy/kubernetes/manifests/05-catalogue-dep.yaml",
    "deploy/kubernetes/complete-demo.yaml",
])
def test_catalogue_probes_separate_local_liveness_from_readiness(filename):
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    docs = list(yaml.safe_load_all((root / filename).read_text()))
    deployment = next(d for d in docs if d and d.get("kind") == "Deployment"
                      and d["metadata"]["name"] == "catalogue")
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"]
                     if c["name"] == "catalogue")
    for key in ("startupProbe", "livenessProbe"):
        assert container[key]["exec"]["command"] == ["nc", "-z", "-w", "2", "127.0.0.1", "80"]
        assert container[key]["timeoutSeconds"] == 3
        assert "httpGet" not in container[key]
    assert container["startupProbe"]["failureThreshold"] == 60
    readiness = container["readinessProbe"]
    assert readiness["httpGet"] == {"path": "/health", "port": 80}
    assert readiness.get("initialDelaySeconds", 0) == 0
    assert readiness["timeoutSeconds"] == 2


def test_catalogue_helm_probe_block_matches_the_plain_manifests():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    chart = root / "deploy/kubernetes/helm-chart"
    port = yaml.safe_load((chart / "values.yaml").read_text())["catalogue"]["containerPort"]
    text = (chart / "templates/catalogue-dep.yaml").read_text()
    block = text[text.index("        startupProbe:"):]
    probes = yaml.safe_load(block.replace("{{ .Values.catalogue.containerPort }}", str(port)))
    docs = yaml.safe_load((root / "deploy/kubernetes/manifests/05-catalogue-dep.yaml").read_text())
    container = docs["spec"]["template"]["spec"]["containers"][0]
    assert probes == {key: container[key] for key in (
        "startupProbe", "livenessProbe", "readinessProbe",
    )}


@pytest.mark.parametrize("failure", [
    None, "workflow_running", "workflow_failed", "verdict",
    "helper_running", "helper_unknown", "engine_identity", "netem",
])
def test_network_cleanup_requires_all_independent_checks(monkeypatch, failure):
    engine = Mock()
    engine.chaos.stop.return_value = {"deleted": True}
    run = controlled.ControlledRun(engine, "run-1", make_spec("network-latency"))
    final = {"state": "completed", "workflow_phase": "Succeeded", "chaos_verdict": "Pass"}
    if failure == "workflow_running":
        final.update(state="running", workflow_phase="Running")
    elif failure == "workflow_failed":
        final.update(state="failed", workflow_phase="Failed")
    elif failure == "verdict":
        final["chaos_verdict"] = "Fail"
    run.chaos_handle = {"final_status": final}
    run.engine_uids = set() if failure == "engine_identity" else {"engine-uid"}
    helpers = {"items": []}
    if failure in {"helper_running", "helper_unknown"}:
        helpers["items"].append({
            "metadata": {"name": "helper", "labels": {"chaosUID": "engine-uid"}},
            "status": {"phase": "Running" if failure == "helper_running" else "Unknown"},
        })
    monkeypatch.setattr(controlled, "kubectl", Mock(return_value=helpers))
    rules = [{"kind": "netem"}] if failure == "netem" else [{"kind": "noqueue"}]
    run.qdisc = Mock(return_value=rules)
    if failure is None:
        run.cleanup()
        assert run.cleaned
        assert run.report["gates"]["cleanup"]["passed"] is True
    else:
        with pytest.raises(RuntimeError):
            run.cleanup()
        assert not run.cleaned
    assert run.report["network_after"] == rules
    assert run.report["gates"]["cleanup"]["final_status"] == final
    assert run.report["gates"]["cleanup"]["netem_remaining"] is (failure == "netem")
    engine.storage.save_artifact.assert_any_call("run-1", "cleanup_helper_pods", helpers)


def make_spec(fault="http-status-code"):
    return {
        "experiment": {"id": "test-controlled"}, "scope": {"namespace": "sock-shop"},
        "timeline": {"warmup_seconds": 60, "baseline_seconds": 90},
        "load_profile": {"provider": "locust", "users": 30, "spawn_rate": 5},
        "observability": {"metrics": {}, "traces": {}},
        "chaos_profile": {"manifest_path": "/test/manifest.yaml"},
        "controlled_campaign": {
            "enabled": True, "service": "orders", "fault": fault,
            "prometheus_url": "http://metrics",
            "http_manifest": {"metadata": {"name": "test-http"}, "spec": {"hosts": ["orders"]}},
        },
    }


def application_pod(restart_count=2, container_id="docker://orders-before", ready=True):
    return {
        "metadata": {"name": "orders-pod", "uid": "orders-uid"},
        "status": {
            "containerStatuses": [{
                "name": "orders",
                "containerID": container_id,
                "restartCount": restart_count,
                "ready": ready,
                "state": {"running": {"startedAt": "2026-10-18T10:00:00Z"}} if ready else {"waiting": {}},
                "lastState": {
                    "terminated": {
                        "reason": "Error", "exitCode": 137,
                        "finishedAt": "2026-10-18T10:00:01Z",
                    },
                },
            }],
        },
    }


def set_container_target(run):
    run.target = {
        "pod": "orders-pod", "uid": "orders-uid", "container": "orders",
        "container_id": "docker://orders-before", "restart_count": 2,
    }


def test_container_kill_preflight_checks_exact_application_container():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.pods = Mock(return_value={"items": [application_pod()]})
    runtime = {"runtime": "docker", "id": "orders-before", "running": True, "labels": {}}
    run.inspect_docker_container = Mock(return_value=runtime)

    run.prepare_intervention()

    run.inspect_docker_container.assert_called_once_with("docker://orders-before")
    assert run.report["container_kill_preflight"]["passed"] is True
    assert run.report["container_kill_preflight"]["sidecars_excluded"] is True
    assert run.report["container_kill_preflight"]["runtime_readback"] == runtime
    assert run.report["intensity"]["event_count"] == 1
    run.engine.chaos.inject.assert_not_called()


def test_container_kill_preflight_blocks_if_runtime_identity_cannot_be_verified():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.pods = Mock(return_value={"items": [application_pod()]})
    run.inspect_docker_container = Mock(
        side_effect=controlled.GateBlocked("Docker runtime identity does not match"),
    )

    with pytest.raises(controlled.GateBlocked, match="Docker runtime"):
        run.prepare_intervention()


def test_container_kill_retry_guard_blocks_changed_container_before_runtime_inspection():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.policy["previous_attempt_guard"] = {
        "pod": "orders-pod", "pod_uid": "orders-uid", "container": "orders",
        "container_id": "docker://different-container", "restart_count": 2,
    }
    run.pods = Mock(return_value={"items": [application_pod()]})
    run.inspect_docker_container = Mock()

    with pytest.raises(controlled.GateBlocked, match="identity changed since the prior"):
        run.prepare_intervention()

    run.inspect_docker_container.assert_not_called()


def test_docker_runtime_inspection_requires_matching_kubernetes_identity(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    container_id = "a" * 64
    run.target["container_id"] = f"docker://{container_id}"
    labels = {
        "io.kubernetes.pod.namespace": "sock-shop",
        "io.kubernetes.pod.name": "orders-pod",
        "io.kubernetes.container.name": "orders",
    }
    monkeypatch.setattr(controlled.subprocess, "run", Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps([{
            "Id": container_id, "State": {"Running": True}, "Config": {"Labels": labels},
        }]), stderr="",
    )))

    observed = run.inspect_docker_container(run.target["container_id"])

    assert observed == {"runtime": "docker", "id": container_id, "running": True, "labels": labels}


def test_docker_runtime_inspection_rejects_sidecar_labels(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    container_id = "b" * 64
    labels = {
        "io.kubernetes.pod.namespace": "sock-shop",
        "io.kubernetes.pod.name": "orders-pod",
        "io.kubernetes.container.name": "istio-proxy",
    }
    monkeypatch.setattr(controlled.subprocess, "run", Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps([{
            "Id": container_id, "State": {"Running": True}, "Config": {"Labels": labels},
        }]), stderr="",
    )))

    with pytest.raises(controlled.GateBlocked, match="does not match"):
        run.inspect_docker_container(f"docker://{container_id}")


def test_docker_stop_uses_one_sigkill_request_with_zero_grace(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    command = Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout="container-id", stderr="",
    ))
    monkeypatch.setattr(controlled.subprocess, "run", command)

    run.stop_docker_container("container-id")

    command.assert_called_once_with(
        ["docker", "stop", "--signal=9", "--time=0", "container-id"],
        capture_output=True, text=True, check=False, timeout=15,
    )


def test_container_kill_submits_one_runtime_signal_and_confirms_restart():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.pods = Mock(side_effect=[
        {"items": [application_pod()]},
        {"items": [application_pod(restart_count=3, container_id="docker://orders-after")]},
    ])
    run.inspect_docker_container = Mock(return_value={
        "runtime": "docker", "id": "orders-before", "running": True, "labels": {},
    })
    run.stop_docker_container = Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout="orders-before", stderr="",
    ))
    run.wait = Mock()
    run.sample_cgroup = Mock()
    run.cgroup_sampling_enabled = True

    start, end, source = run.intervene_container_kill()
    run.evaluate_effect({}, start)

    run.stop_docker_container.assert_called_once_with("orders-before", timeout=15)
    assert run.inspect_docker_container.call_count == 1
    assert end == run.report["timestamps"]["container_restart_observed"]["at"]
    assert source == "single_SIGKILL_to_application_container_restart_observed"
    assert run.report["container_kill_event"]["event_count_submitted"] == 1
    assert run.report["container_kill_event"]["restart_count_delta"] == 1
    assert run.report["gates"]["intervention_confirmed_on_target"] is True
    assert run.report["gates"]["mechanism_observed"]["passed"] is True
    assert run.report["gates"]["impact_observed"]["container_restart_observed"] is True
    assert run.cgroup_sampling_enabled is True
    run.sample_cgroup.assert_called_once_with()
    with pytest.raises(RuntimeError, match="already attempted"):
        run.intervene_container_kill()
    assert run.stop_docker_container.call_count == 1


def test_container_kill_confirms_restart_even_when_readiness_is_delayed():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    booting = application_pod(restart_count=3, container_id="docker://orders-after", ready=False)
    booting["status"]["containerStatuses"][0]["state"] = {"running": {}}
    run.pods = Mock(side_effect=[
        {"items": [application_pod()]},
        {"items": [booting]},
    ])
    run.inspect_docker_container = Mock(return_value={
        "runtime": "docker", "id": "orders-before", "running": True, "labels": {},
    })
    run.stop_docker_container = Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout="orders-before", stderr="",
    ))
    run.sample_cgroup = Mock()
    run.cgroup_sampling_enabled = True

    start, end, source = run.intervene_container_kill()
    run.evaluate_effect({}, start)

    assert end == run.report["timestamps"]["container_restart_observed"]["at"]
    assert source == "single_SIGKILL_to_application_container_restart_observed"
    assert run.report["container_kill_event"]["restart_observed"] is True
    assert run.report["container_kill_event"]["restart_count_delta"] == 1
    assert "ready_after_restart_at" not in run.report["container_kill_event"]
    assert run.report["gates"]["intervention_confirmed_on_target"] is True
    assert run.report["gates"]["impact_observed"]["container_restart_observed"] is True
    assert run.cgroup_sampling_enabled is True
    run.sample_cgroup.assert_called_once()


def test_container_kill_observation_records_unready_state_without_losing_event():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.report["container_kill_event"] = {
        "restart_observed": True,
        "restart_count_after_signal": 3,
        "container_id_after_restart": "docker://orders-after",
        "observations": [],
    }
    booting = application_pod(restart_count=3, container_id="docker://orders-after", ready=False)
    booting["status"]["containerStatuses"][0]["state"] = {"running": {}}
    run.pods = Mock(side_effect=[
        {"items": [booting]},
        {"items": [application_pod(
            restart_count=3, container_id="docker://orders-after",
        )]},
    ])
    run.cgroup_sampling_paused_for_restart = True
    run.sample_cgroup = Mock()

    status = run.observe_container_kill_state("post_observation_poll")

    assert status["ready"] is False
    assert run.cgroup_sampling_enabled is True
    assert run.cgroup_sampling_paused_for_restart is False
    assert "startup_cgroup_sampling_resumed" in run.report["timestamps"]
    assert len(run.report["container_kill_event"]["observations"]) == 1
    assert run.report["container_kill_event"]["observations"][0]["ready"] is False
    status = run.observe_container_kill_state("post_observation_poll")
    assert status["ready"] is True
    assert run.cgroup_sampling_enabled is True
    assert run.cgroup_sampling_paused_for_restart is False
    assert run.report["gates"]["impact_observed"]["availability_impact"] == {
        "status": "ready",
        "ready": True,
        "running": True,
        "sampled_status_polls": 2,
        "not_ready_status_polls": 1,
        "restart_observed_at": None,
        "ready_observed_at": run.report["container_kill_event"]["ready_after_restart_at"],
        "limitation": (
            "Kubernetes status polling brackets readiness; it is not a continuous "
            "request-availability measurement"
        ),
    }
    run.sample_cgroup.assert_not_called()


@pytest.mark.parametrize("running,container_id,expected", [
    (False, "docker://after", False),
    (True, "docker://after", True),
    (True, "docker://unrelated", None),
])
def test_startup_sampling_requires_running_and_confirmed_identity(running, container_id, expected):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    run.report["container_kill_event"] = {"container_id_after_restart": "docker://after"}
    run.cgroup_sampling_paused_for_restart = True
    status = {"running": running, "ready": False, "container_id": container_id, "restart_count": 3}
    if expected is None:
        with pytest.raises(RuntimeError, match="unconfirmed container"):
            run.mark_container_ready_after_kill(status)
    else:
        run.mark_container_ready_after_kill(status)
        assert run.cgroup_sampling_enabled is expected
        assert "ready_after_restart_at" not in run.report["container_kill_event"]


def test_container_kill_submission_without_restart_aborts_without_retry(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("container-kill"))
    set_container_target(run)
    run.pods = Mock(side_effect=[
        {"items": [application_pod()]},
        {"items": [application_pod()]},
    ])
    run.inspect_docker_container = Mock(return_value={
        "runtime": "docker", "id": "orders-before", "running": True, "labels": {},
    })
    run.stop_docker_container = Mock(return_value=controlled.subprocess.CompletedProcess(
        args=[], returncode=0, stdout="orders-before", stderr="",
    ))
    run.wait = Mock()
    monkeypatch.setattr(controlled, "CONTAINER_KILL_RESTART_TIMEOUT_SECONDS", 0)

    with pytest.raises(RuntimeError, match="no application-container restart"):
        run.intervene_container_kill()

    run.stop_docker_container.assert_called_once_with("orders-before", timeout=15)
    assert run.report["gates"]["submitted"] is True
    assert run.report["gates"]["intervention_confirmed_on_target"] is False


def test_http_requires_target_edge_traffic():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.baseline = {"target_edge_requests": {"status": "no_data"}}
    with pytest.raises(controlled.GateBlocked, match="caller-reported traffic"):
        run.prepare_intervention()


def test_error_percentage_queries_guard_zero_request_windows():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.target = {"pod": "catalogue-pod"}

    queries = run.queries()

    assert "clamp_min(" in queries["errors_percent"]
    assert "clamp_min(" in queries["target_edge_error_percent"]
    assert "1e-9" in queries["errors_percent"]
    assert "1e-9" in queries["target_edge_error_percent"]


def test_idempotent_start_does_not_launch_second_thread():
    existing = RunRecord("old-run", "test", 1, RunStatus.COMPLETED, "test", "2026-10-01T00:00:00Z")
    storage = Mock()
    storage.create_run.return_value = existing
    engine = OrchestratorEngine(storage, Mock(), Mock(), Mock(), Mock(), Mock())
    result = engine.start_run(ExperimentVersion("test", 1, "1.0.0", "", "", {}), "test", "same-key")
    assert result is existing
    assert engine._stop_flags == {}
    storage.append_event.assert_not_called()


@pytest.mark.parametrize("delay,removed", [(.2, True), (.1, True), (None, True), (.2, False)])
def test_network_rule_must_match_delay_and_be_removed(monkeypatch, delay, removed):
    chaos = Mock()
    chaos.inject.return_value = {"mode": "argo_workflow", "workflow_name": "network-run"}
    chaos.status.return_value = {
        "state": "completed", "chaos_verdict": "Pass",
        "chaos_result_created_at": "2026-10-18T10:01:00Z",
    }
    chaos.stop.return_value = {"deleted": True}
    engine = SimpleNamespace(chaos=chaos, storage=Mock(), _runtime_handles={"run-1": {}}, _event=Mock())
    spec = make_spec("network-latency")
    spec["chaos_profile"] = {}
    run = controlled.ControlledRun(engine, "run-1", spec)
    run.report["intensity"] = {"latency_ms": 200}
    rule = [{"kind": "netem", "options": {"delay": {"delay": delay}}}]
    monkeypatch.setattr(run, "qdisc", Mock(side_effect=[rule, [] if removed else rule]))

    def kube(*args):
        if "workflow.argoproj.io" in args:
            return {"metadata": {"uid": "wf1"}, "status": {}}
        if "chaosengines" in args:
            return {"items": [{"metadata": {"uid": "engine1"}}]}
        return {"items": []}

    monkeypatch.setattr(controlled, "kubectl", kube)
    if delay != .2 or not removed:
        with pytest.raises(RuntimeError, match="configured delay|netem rule still"):
            run.intervene()
    else:
        run.intervene()
        assert run.report["network_rule_validation"]["passed"]
        assert run.report["gates"]["cleanup"]["passed"]
        assert run.report["timestamps"]["intervention_estimate"]["duration_verified"] is False


def test_http_mechanism_uses_edge_percentage_not_an_assumed_minimum_rps():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.baseline = {
        "target_edge_error_percent": {"status": "ok", "mean": 0, "p95": 0},
        "target_edge_requests": {"status": "ok", "mean": .02},
        "errors_percent": {"status": "ok", "mean": 0, "p95": 0},
        "latency_ms": {"status": "ok", "mean": 20, "p95": 20},
    }
    run.report["http_intervention_readback"] = {
        "passed": True, "spec_matches_submitted": True,
        "source": "kubernetes_virtualservice_api_readback",
    }
    run.raw["intervention"] = {"data": {
        "target_edge_error_percent": {"status": "success", "data": {"result": [{
            "values": [[t, "100"] for t in range(0, 121, 5)],
        }]}},
        "errors_percent": {"status": "success", "data": {"result": []}},
    }}
    run.evaluate_effect({
        "target_edge_error_percent": {"status": "ok", "mean": 100},
        "target_edge_requests": {"status": "ok", "mean": 1},
        "errors_percent": {"status": "ok", "mean": 0}, "latency_ms": {"status": "ok", "mean": 20},
    }, "1970-01-01T00:00:00+00:00")
    assert run.report["gates"]["intervention_confirmed_on_target"] is True
    assert run.report["gates"]["mechanism_observed"]["passed"]
    assert run.report["gates"]["impact_observed"]["target_edge_error_increased"] is True


def test_http_configuration_confirmation_does_not_require_observed_5xx():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.baseline = {
        "target_edge_error_percent": {"status": "ok", "mean": 0, "p95": 0},
        "errors_percent": {"status": "ok", "mean": 0, "p95": 0},
        "latency_ms": {"status": "ok", "p95": 20},
    }
    run.report["http_intervention_readback"] = {
        "passed": True, "spec_matches_submitted": True,
        "source": "kubernetes_virtualservice_api_readback",
    }
    run.raw["intervention"] = {"data": {
        "errors_percent": {"status": "success", "data": {"result": []}},
    }}

    run.evaluate_effect({
        "target_edge_error_percent": {"status": "tool_error", "error": "non-finite sample"},
        "target_edge_requests": {"status": "ok", "mean": 0},
        "errors_percent": {"status": "no_data"},
        "latency_ms": {"status": "no_data"},
    }, "2026-10-18T00:00:00+00:00")

    assert run.report["gates"]["intervention_confirmed_on_target"] is True
    assert run.report["gates"]["mechanism_observed"]["passed"] is True
    assert run.report["gates"]["impact_observed"]["target_edge_error_increased"] is None
    assert any("not evidence of observed 5xx" in item for item in run.report["limitations"])


def test_http_intervention_window_ends_at_removal_timestamp(monkeypatch):
    chaos = Mock()
    chaos.inject.return_value = {"mode": "manifest"}
    engine = SimpleNamespace(
        storage=Mock(),
        chaos=chaos,
        _runtime_handles={"run-1": {}},
        _event=Mock(),
    )
    run = controlled.ControlledRun(engine, "run-1", make_spec())
    removed_at = "2026-10-19T00:02:00.000000+00:00"
    mark_times = {
        "intervention_submission": "2026-10-19T00:00:00.000000+00:00",
        "intervention_start_estimate": "2026-10-19T00:00:02.000000+00:00",
    }

    def mark(name, source="controller_clock", **extra):
        timestamp = mark_times[name]
        run.report["timestamps"][name] = {"at": timestamp, "source": source, **extra}
        return timestamp

    def cleanup():
        run.report["timestamps"]["intervention_removed"] = {
            "at": removed_at,
            "source": "api_delete_verified",
        }

    monkeypatch.setattr(run, "mark", mark)
    monkeypatch.setattr(run, "checkpoint", Mock())
    monkeypatch.setattr(run, "cleanup", cleanup)
    monkeypatch.setattr(run, "wait", Mock())
    monkeypatch.setattr(controlled, "kubectl", lambda *args: {
        "spec": run.policy["http_manifest"]["spec"],
    })
    monkeypatch.setattr(
        controlled, "utc_now_iso", lambda: "2026-10-19T00:02:00.025000+00:00",
    )

    _, fault_end, _ = run.intervene()

    assert fault_end == removed_at
    assert run.report["timestamps"]["intervention_estimate"]["end"] == removed_at


@pytest.mark.parametrize("failure_mode", ["none", "cleanup", "load_stop", "post_submit_gate", "recovery", "traces_unavailable"])
def test_warmup_precedes_clean_baseline_and_cleanup_precedes_recovery(monkeypatch, failure_mode):
    clock = [datetime(2026, 10, 18, tzinfo=timezone.utc)]
    monkeypatch.setattr(controlled, "utc_now_iso", lambda: clock[0].isoformat())
    state = {"applied": False}
    artifacts = {}
    record = RunRecord("run-1", "test-controlled", 1, RunStatus.RUNNING, "test", clock[0].isoformat())
    storage = Mock()
    storage.get_run.return_value = record

    def save(run_id, name, value):
        artifacts[name] = copy.deepcopy(value)
        return f"/artifacts/{name}"

    storage.save_artifact.side_effect = save

    def collect(ctx, cfg, start, end, step):
        assert step == 5
        lo, hi = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp()
        values = {
            "cpu_cores": .03, "memory_bytes": 100 * 2**20, "traffic": 10,
            "latency_ms": 20, "target_edge_requests": 5, "target_edge_5xx": 0,
            "target_edge_error_percent": 0,
            "errors_percent": 0, "checkout_orders": 2, "checkout_payment": 2, "checkout_shipping": 2,
        }
        return {"data": {
            key: {"status": "success", "data": {"result": [{
                "metric": {}, "values": [[t, str(v)] for t in range(int(lo), int(hi) + 1, step)],
            }]}}
            for key, v in values.items()
        }}

    metrics = Mock()
    metrics.collect_window.side_effect = collect
    load = Mock()
    load.start_load.return_value = {"pid": 1}
    load.status.return_value = {"state": "running"}
    load.stop_load.return_value = {"stopped": True}
    load.collect_summary.return_value = {}
    chaos = Mock()

    def inject(*a):
        state["applied"] = True
        return {"mode": "manifest"}

    def stop(*a):
        state["applied"] = False
        return {"deleted": True}

    chaos.inject.side_effect = inject
    chaos.stop.side_effect = stop
    chaos.collect_artifacts.return_value = {}
    engine = SimpleNamespace(
        storage=storage, metrics=metrics, load=load, chaos=chaos, traces=Mock(),
        _stop_flags={"run-1": None}, _runtime_handles={},
        _event=Mock(), _sleep_interruptible=lambda flag, seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
        _persist_rca_evidence_snapshot=Mock(),
    )
    engine.traces.collect_window.return_value = {"status": "no_data"}
    run = controlled.ControlledRun(engine, "run-1", make_spec())
    monkeypatch.setattr(run, "inspect_target", lambda: setattr(run, "target", {"pod": "orders-pod"}))
    monkeypatch.setattr(run, "metadata", lambda: None)
    monkeypatch.setattr(run, "start_cgroup_sampling", lambda: None)
    monkeypatch.setattr(run, "pods", lambda: {"items": []})
    monkeypatch.setattr(run, "health", lambda: {"healthy": True, "unhealthy_nodes": []})

    def kube(*args):
        if "virtualservice" in args:
            return {"spec": run.policy["http_manifest"]["spec"]}
        if "virtualservices" in args:
            return {"items": [{"metadata": {"name": "test-http"}}] if state["applied"] else []}
        return {"items": []}

    monkeypatch.setattr(controlled, "kubectl", kube)
    if failure_mode == "cleanup":
        chaos.stop.return_value = {"deleted": False}
        chaos.stop.side_effect = None
    elif failure_mode == "load_stop":
        load.stop_load.side_effect = RuntimeError("remote load cleanup failed")
    elif failure_mode == "post_submit_gate":
        chaos.inject.side_effect = controlled.GateBlocked("uncertain submission")
    elif failure_mode == "recovery":
        monkeypatch.setattr(run, "recover", Mock(side_effect=RuntimeError("recovery not confirmed")))
    elif failure_mode == "traces_unavailable":
        engine.traces.collect_window.side_effect = RuntimeError("traces unavailable")
    if failure_mode not in {"none", "traces_unavailable"}:
        with pytest.raises(RuntimeError, match="controlled campaign abort"):
            run.run()
        assert run.report["status"] == "aborted"
        assert artifacts["controlled_report"]["status"] == "aborted"
        assert record.summary["controlled_status"] == "aborted"
        assert record.status == RunStatus.FAILED
        assert "metrics_raw" in artifacts
        return
    run.run()
    assert run.report["status"] == "completed"
    stabilization = run.report["stabilization_checks"][0]
    assert stabilization["passed"] is True
    assert set(stabilization["metrics"]) == {"traffic", "cpu_cores", "memory_bytes", "latency_ms"}
    assert stabilization["metrics"]["cpu_cores"]["limit"] == .02
    if failure_mode == "traces_unavailable":
        assert artifacts["traces_raw"]["status"] == "tool_error"
        assert any("traces_raw capture failed" in item for item in run.report["limitations"])
    times = run.report["timestamps"]
    elapsed = lambda a, b: (datetime.fromisoformat(times[b]["at"]) - datetime.fromisoformat(times[a]["at"])).total_seconds()
    assert elapsed("warmup_started", "baseline_started") >= 60
    assert elapsed("baseline_started", "baseline_completed") == 90
    assert elapsed("intervention_removed", "recovery_observed") >= 120
    assert elapsed("recovery_observed", "cooldown_completed") >= 30
    estimate = times["intervention_estimate"]
    assert not estimate["duration_verified"]
    assert estimate["effective_start"] is None and estimate["effective_end"] is None
    assert run.report["gates"]["intervention_confirmed_on_target"] is True
    assert run.report["gates"]["mechanism_observed"]["passed"] is True
    assert run.report["gates"]["impact_observed"]["target_edge_error_increased"] is False
    assert run.report["gates"]["impact_observed"]["application_errors_increased"] is False
    assert run.report["gates"]["impact_observed"]["latency_increased"] is False
    window = artifacts["observation_window"]
    assert window["phases"]["baseline"]["start"] == times["baseline_started"]["at"]
    assert window["phase_sources"]["baseline"] == "after_load_stabilization"
    assert record.timings["controlled_observation_window"] == window
    assert record.status == RunStatus.COMPLETED
    chaos.inject.assert_called_once()
    chaos.stop.assert_called_once()


def test_memory_safety_requests_abort_and_records_restart_as_scenario_deviation(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("memory-hog"))
    run.target = {"uid": "p1", "restart_count": 0}
    run.report["intensity"] = {"limit_bytes": 1000}
    monkeypatch.setattr(run, "checkpoint", lambda: None)
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout="patched", stderr=""))
    monkeypatch.setattr(controlled.subprocess, "run", command)
    with pytest.raises(RuntimeError, match="safety abort"):
        run.check_memory_safety(
            {"memory_usage_bytes": {"status": "ok", "max": 910}},
            {"items": [{"metadata": {"uid": "p1"}, "status": {"containerStatuses": [{
                "name": "orders", "restartCount": 1, "lastState": {"terminated": {"reason": "Error", "exitCode": 137}},
            }]}}]},
            {"items": [{"metadata": {"name": "owned-engine"}}]},
        )
    assert run.report["scenario_deviation"] == "unexpected_oom_or_restart"
    assert command.call_args.args[0][-1] == '{"spec":{"engineState":"stop"}}'
    assert "OOMKilled" not in str(run.report["scenario_deviation"])


@pytest.mark.parametrize("observed", [
    {"status": "no_data"},
    {"status": "ok", "max": 900},
])
def test_memory_safety_aborts_on_cutoff_or_missing_data_without_waiting_for_oom(monkeypatch, observed):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("memory-hog"))
    run.target = {"uid": "p1", "restart_count": 0}
    run.report["intensity"] = {"limit_bytes": 1000}
    run.baseline = {"oom_events": {"status": "ok", "max": 0}}
    monkeypatch.setattr(run, "checkpoint", lambda: None)
    with pytest.raises(RuntimeError, match="safety abort"):
        run.check_memory_safety(
            {"memory_usage_bytes": observed, "oom_events": {"status": "ok", "max": 0}},
            {"items": [{"metadata": {"uid": "p1"}, "status": {"containerStatuses": [{
                "name": "orders", "restartCount": 0,
            }]}}]},
            {"items": []},
        )
    assert run.report["scenario_deviation"] == "90_percent_memory_cutoff_or_telemetry_loss"


def test_memory_worker_oom_is_a_deviation_even_when_application_has_not_restarted(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("memory-hog"))
    run.target = {"uid": "p1", "restart_count": 0}
    run.baseline = {"oom_events": {"status": "ok", "max": 3}}
    run.report["intensity"] = {"limit_bytes": 1000}
    monkeypatch.setattr(run, "checkpoint", lambda: None)
    with pytest.raises(RuntimeError, match="safety abort"):
        run.check_memory_safety(
            {"memory_usage_bytes": {"status": "ok", "max": 700}, "oom_events": {"status": "ok", "max": 4}},
            {"items": [{"metadata": {"uid": "p1"}, "status": {"containerStatuses": [{
                "name": "orders", "restartCount": 0,
            }]}}]},
            {"items": []},
        )
    assert run.report["scenario_deviation"] == "unexpected_oom_or_restart"


def test_cpu_can_be_confirmed_at_configured_target_without_throttling_or_http_impact():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.baseline = {
        "cpu_cores": {"status": "ok", "mean": .03, "p95": .04},
        "errors_percent": {"status": "ok", "mean": 0, "p95": 0},
        "latency_ms": {"status": "ok", "mean": 20, "p95": 20},
    }
    run.report["intensity"] = {
        "quota_cores": .3, "candidate_target_quota_fraction": .8,
        "target_tolerance_fraction": .05, "calibration_status": "candidate",
        "calibration": {"target_tolerance_fraction": .05},
    }
    run.window = {"phase_sources": {"fault": "estimated"}}
    run.raw["intervention"] = {"data": {
        "cpu_cores": {"status": "success", "data": {"result": [{
            "values": [[t, ".24"] for t in range(0, 181, 5)],
        }]}},
        "errors_percent": {"status": "success", "data": {"result": []}},
    }}
    run.evaluate_effect({
        "cpu_cores": {"status": "ok", "mean": .24},
        "errors_percent": {"status": "ok", "mean": 0}, "latency_ms": {"status": "ok", "mean": 20},
    }, "1970-01-01T00:00:00+00:00")
    gates = run.report["gates"]
    assert gates["intervention_confirmed_on_target"]
    assert gates["mechanism_observed"]["intervention_mean_quota_fraction"] == .8
    assert (
        gates["mechanism_observed"]["throttling_separately"]["throttled_seconds_per_second"]
        ["intervention"]["status"] == "no_data"
    )
    assert not any(gates["impact_observed"].values())


@pytest.mark.parametrize("measured_cores,expected", [(.24, False), (.30, True)])
def test_cpu_candidate_for_100_percent_requires_sustained_quota_attainment(measured_cores, expected):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec("cpu-hog"))
    run.baseline = {
        "cpu_cores": {"status": "ok", "mean": .03, "p95": .04},
        "errors_percent": {"status": "ok", "mean": 0, "p95": 0},
        "latency_ms": {"status": "ok", "mean": 20, "p95": 20},
    }
    run.report["intensity"] = {
        "quota_cores": .3, "candidate_target_quota_fraction": 1.0,
        "target_tolerance_fraction": .05, "calibration_status": "candidate",
        "calibration": {"target_tolerance_fraction": .05},
    }
    run.window = {"phase_sources": {"fault": "estimated"}}
    run.raw["intervention"] = {"data": {
        "cpu_cores": {"status": "success", "data": {"result": [{
            "values": [[t, str(measured_cores)] for t in range(0, 181, 5)],
        }]}},
        "errors_percent": {"status": "success", "data": {"result": []}},
    }}
    run.evaluate_effect({
        "cpu_cores": {"status": "ok", "mean": measured_cores},
        "errors_percent": {"status": "ok", "mean": 0}, "latency_ms": {"status": "ok", "mean": 20},
    }, "1970-01-01T00:00:00+00:00")
    mechanism = run.report["gates"]["mechanism_observed"]
    assert mechanism["passed"] is expected
    assert mechanism["target_attainment"]["passed"] is expected
    assert mechanism["target_attainment"]["observed_mean_fraction"] == measured_cores / .3
    assert run.report["gates"]["intervention_confirmed_on_target"] is expected


@pytest.mark.parametrize("sampling_fails", [False, True])
def test_memory_workflow_samples_after_submission_before_first_safety_check(monkeypatch, sampling_fails):
    chaos = Mock()
    chaos.inject.return_value = {"mode": "argo_workflow", "workflow_name": "memory-run"}
    chaos.status.return_value = {
        "state": "completed", "chaos_result_created_at": "2026-10-18T10:01:00Z",
    }
    engine = SimpleNamespace(chaos=chaos, storage=Mock(), _runtime_handles={"run-1": {}}, _event=Mock())
    spec = make_spec("memory-hog")
    spec["chaos_profile"] = {}
    run = controlled.ControlledRun(engine, "run-1", spec)
    run.cgroup_sampling_enabled = True
    order = []

    def sample():
        order.append("sample")
        if sampling_fails:
            raise controlled.GateBlocked("cgroup read failed")

    def collect(*args, **kwargs):
        order.append("collect")
        assert order == ["sample", "collect"]
        return {"memory_usage_bytes": {"status": "ok", "max": 700}}

    def check(summary, pods, engines):
        order.append("check")
        if sampling_fails:
            assert summary["memory_usage_bytes"]["status"] == "tool_error"
            assert "cgroup read failed" in summary["memory_usage_bytes"]["error"]
            raise RuntimeError("memory scenario safety abort")
        assert order == ["sample", "collect", "check"]

    monkeypatch.setattr(run, "sample_cgroup", sample)
    monkeypatch.setattr(run, "collect", collect)
    monkeypatch.setattr(run, "check_memory_safety", check)
    monkeypatch.setattr(run, "pods", lambda: {"items": []})
    monkeypatch.setattr(run, "cleanup", lambda: None)
    monkeypatch.setattr(controlled, "kubectl", lambda *a: (
        {"metadata": {"uid": "wf1"}, "status": {}}
        if "workflow.argoproj.io" in a else {"items": []}
    ))
    if sampling_fails:
        with pytest.raises(RuntimeError, match="safety abort"):
            run.intervene()
        assert order == ["sample", "check"]
    else:
        run.intervene()
        assert order == ["sample", "collect", "check"]


def test_litmus_duration_remains_estimated_not_workflow_elapsed(monkeypatch):
    chaos = Mock()
    chaos.inject.return_value = {"mode": "argo_workflow", "workflow_name": "cpu-run"}
    chaos.status.return_value = {
        "state": "completed", "chaos_result_created_at": "2026-10-18T10:01:00Z",
    }
    engine = SimpleNamespace(chaos=chaos, storage=Mock(), _runtime_handles={"run-1": {}}, _event=Mock())
    spec = make_spec("cpu-hog")
    spec["chaos_profile"] = {}
    run = controlled.ControlledRun(engine, "run-1", spec)
    monkeypatch.setattr(run, "cleanup", lambda: None)
    monkeypatch.setattr(controlled, "kubectl", lambda *a: (
        {"metadata": {"uid": "wf1"}, "status": {"startedAt": "2026-10-18T10:00:00Z", "finishedAt": "2026-10-18T10:05:00Z"}}
        if "workflow.argoproj.io" in a else {"items": []}
    ))
    start, end, source = run.intervene()
    assert start == "2026-10-18T10:01:00Z"
    assert datetime.fromisoformat(end).minute == 4
    estimate = run.report["timestamps"]["intervention_estimate"]
    assert estimate["configured_seconds"] == 180
    assert estimate["effective_start"] is None
    assert not estimate["duration_verified"]
    assert "configured_duration" in source


def test_scrape_metadata_and_freshness_use_the_metrics_mcp_backend(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.spec["observability"]["metrics"] = {"mcp_sse_url": "http://same-mcp/sse"}
    run.target = {"pod": "orders-pod"}
    client = Mock()
    client.call_tool_json.side_effect = [
        {"status": "success", "data": {"activeTargets": [{"scrapeInterval": "15s"}]}},
        {"status": "success", "data": {"result": [{"value": [1, "10"]}]}},
    ]
    factory = Mock(return_value=client)
    monkeypatch.setattr(controlled, "MCPToolClient", factory)
    run.metadata()
    assert factory.call_args.kwargs["sse_url"] == "http://same-mcp/sse"
    assert client.call_tool_json.call_args_list[0].args == ("prometheus_scrape_targets", {})
    assert run.report["telemetry"]["sample_age_seconds"] == 10
    assert run.report["telemetry"]["requested_query_step_seconds"] == 5
    assert run.report["telemetry"]["scrapes"][0]["scrapeInterval"] == "15s"


def test_scrape_metadata_rejects_unknown_tool_text_and_preserves_raw_response(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.spec["observability"]["metrics"] = {"mcp_sse_url": "http://same-mcp/sse"}
    run.target = {"pod": "orders-pod"}
    client = Mock()
    client.call_tool_json.return_value = "Unknown tool: prometheus_scrape_targets"
    monkeypatch.setattr(controlled, "MCPToolClient", Mock(return_value=client))

    with pytest.raises(controlled.GateBlocked, match="non-object response"):
        run.metadata()

    saved = run.engine.storage.save_artifact.call_args.args[2]
    assert saved == {
        "status": "malformed_response",
        "response_type": "str",
        "raw_response": "Unknown tool: prometheus_scrape_targets",
    }


@pytest.mark.parametrize("foreign_target", [False, True])
def test_reviewed_workflow_must_restrict_the_injection_to_the_planned_service(monkeypatch, foreign_target):
    spec = make_spec("cpu-hog")
    spec["chaos_profile"]["chaos_engine"] = "orders-cpu-hog"
    run = controlled.ControlledRun(Mock(), "run-1", spec)
    workflow = {
        "metadata": {"name": "orders-cpu-hog"},
        "spec": {"templates": [{"inputs": {"artifacts": [
            {"raw": {"data": yaml.safe_dump({
                "kind": "ChaosExperiment", "spec": {"definition": {"image": controlled.INJECTOR_IMAGE}},
            })}},
            {"raw": {"data": yaml.safe_dump({
                "kind": "ChaosEngine", "spec": {
                    "appinfo": {"appns": "sock-shop", "applabel": f"name={'payment' if foreign_target else 'orders'}", "appkind": "deployment"},
                    "experiments": [{"name": "pod-cpu-hog"}],
                },
            })}},
        ]}}]},
    }
    monkeypatch.setattr(controlled, "kubectl", lambda *args: {"items": [workflow]})
    if foreign_target:
        with pytest.raises(controlled.GateBlocked, match="exactly the intended"):
            run.workflow_preflight()
    else:
        run.workflow_preflight()
        assert spec["chaos_profile"]["template_snapshot"] == workflow
    run.engine.chaos.inject.assert_not_called()


@pytest.mark.parametrize("aborted", [False, True])
def test_engine_delegates_opt_in_and_preserves_controlled_windows_on_abort(monkeypatch, aborted):
    record = RunRecord("run-1", "test-controlled", 1, RunStatus.PENDING, "test", "2026-10-18T00:00:00Z")
    window = {"phases": {"baseline": {"start": "stable", "end": "stable-end"}}, "partial": True}
    storage = Mock()
    storage.get_run.return_value = record
    storage.get_artifact.return_value = window
    engine = OrchestratorEngine(storage, Mock(), Mock(), Mock(), Mock(), Mock())
    engine._stop_flags["run-1"] = threading.Event()
    instance = Mock()

    def execute():
        if aborted:
            record.summary.update({"controlled_status": "aborted", "artifact_paths": {"observation_window": "path"}})
            raise RuntimeError("controlled abort")

    instance.run.side_effect = execute
    constructor = Mock(return_value=instance)
    monkeypatch.setattr(controlled, "ControlledRun", constructor)
    spec = make_spec()
    engine._execute_run("run-1", ExperimentVersion("test-controlled", 1, "1", "", "", spec))
    constructor.assert_called_once_with(engine, "run-1", spec)
    storage.list_events.assert_not_called()
    if aborted:
        storage.save_artifact.assert_called_once_with("run-1", "observation_window", window)
        assert record.summary["controlled_status"] == "aborted"
    assert engine._stop_flags == {}


@pytest.mark.parametrize("operational", [False, True])
def test_partial_window_includes_recovery_phase_when_recovery_started(monkeypatch, operational):
    run = controlled.ControlledRun(Mock(), "run-partial", make_spec("container-kill"))
    run.report["timestamps"] = {
        "warmup_started": {"at": "2026-10-18T10:00:00+00:00"},
        "baseline_started": {"at": "2026-10-18T10:01:00+00:00"},
        "baseline_completed": {"at": "2026-10-18T10:02:00+00:00"},
        "intervention_submission": {"at": "2026-10-18T10:02:05+00:00"},
        "post_observation_started": {"at": "2026-10-18T10:02:10+00:00"},
    }
    run.window = {
        "phases": {
            "baseline": {
                "start": "2026-10-18T10:01:00+00:00",
                "end": "2026-10-18T10:02:00+00:00",
            },
            "fault": {
                "start": "2026-10-18T10:02:05+00:00",
                "end": "2026-10-18T10:02:08+00:00",
            },
        },
        "phase_sources": {
            "baseline": "after_load_stabilization",
            "fault": "partial_observation_not_effective_intervention",
        },
    }
    monkeypatch.setattr(controlled, "utc_now_iso", lambda: "2026-10-18T10:04:10+00:00")
    if operational:
        run.report["timestamps"]["experimental_recovery_expired"] = {
            "at": "2026-10-18T10:04:10+00:00",
        }
        monkeypatch.setattr(controlled, "utc_now_iso", lambda: "2026-10-18T10:14:10+00:00")
    run.save = Mock()
    run.capture_telemetry = Mock()

    run.capture_partial()

    assert run.window["phases"]["post"] == {
        "start": "2026-10-18T10:02:10+00:00",
        "end": "2026-10-18T10:04:10+00:00",
    }
    assert run.window["phase_sources"]["post"] == "recovery_observation"
    run.capture_telemetry.assert_called_once_with(
        "2026-10-18T10:00:00+00:00",
        "2026-10-18T10:04:10+00:00",
    )


@pytest.mark.parametrize("recovers", [True, False])
@pytest.mark.parametrize("fault", ["http-status-code", "container-kill"])
def test_operational_recovery_requires_continuous_unchanged_checks(monkeypatch, recovers, fault):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec(fault))
    run.policy["resilience"] = {
        "version": 1, "operational_timeout_seconds": 300, "stable_seconds": 120,
    }
    clock = [0]
    monkeypatch.setattr(controlled.time, "monotonic", lambda: clock[0])
    run.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    run.mark = Mock()
    run.checkpoint = Mock()
    run.target = {"container_id": "original", "restart_count": 0}
    if fault == "container-kill":
        run.report["container_kill_event"] = {
            "container_id_after_restart": "original", "restart_count_after_signal": 0,
        }
    run.application_container_status = Mock(return_value={
        "container_id": "original", "restart_count": 0, "ready": True, "running": True,
    })
    original = {"passed": False, "summary": {"latency_ms": 1600}}
    run.report["gates"]["recovery"] = copy.deepcopy(original)

    def check():
        # A failed observation at 90s must reset the continuity interval.
        return {"passed": recovers and clock[0] != 90, "health": {"healthy": True}}

    run.recovery_check = Mock(side_effect=check)
    run.operational_recovery()

    assert run.report["gates"]["recovery"] == original
    gate = run.report["gates"]["operational_recovery"]
    assert gate["passed"] is recovers
    if recovers:
        assert clock[0] >= 225
        assert gate["stable_seconds"] >= 120
    else:
        assert clock[0] == 300


def test_recovery_timeout_stays_a_failure_after_operational_success(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.policy["recovery_timeout_seconds"] = 1
    run.wait = Mock()
    run.mark = Mock()
    run.checkpoint = Mock()
    run.recovery_check = Mock(return_value={"passed": False})
    run.operational_recovery = Mock()
    monkeypatch.setattr(controlled.time, "monotonic", Mock(side_effect=[0, 2]))
    with pytest.raises(RuntimeError, match="recovery not confirmed"):
        run.recover()
    assert run.report["gates"]["recovery"]["passed"] is False
    run.operational_recovery.assert_called_once()


def test_operational_recovery_not_enabled_for_old_specs():
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.policy.pop("resilience", None)
    run.recovery_check = Mock()
    run.operational_recovery()
    run.recovery_check.assert_not_called()


def test_operational_recovery_refuses_additional_container_restart(monkeypatch):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    run.policy["resilience"] = {
        "version": 1, "operational_timeout_seconds": 300, "stable_seconds": 120,
    }
    run.target = {"container_id": "original", "restart_count": 0}
    run.application_container_status = Mock(return_value={
        "container_id": "new", "restart_count": 1, "ready": True, "running": True,
    })
    run.mark = Mock()
    with pytest.raises(RuntimeError, match="container changed"):
        run.operational_recovery()


@pytest.mark.parametrize("failed_metric", [None, "cpu_cores", "latency_ms", "traffic", "no_data"])
def test_recovery_check_keeps_original_thresholds_and_rejects_missing_data(failed_metric):
    run = controlled.ControlledRun(Mock(), "run-1", make_spec())
    values = {
        "traffic": 5, "cpu_cores": .1, "memory_bytes": 400 * 2**20,
        "errors_percent": 0, "latency_ms": 400,
    }
    run.baseline = {
        key: {"status": "ok", "samples": 12, "mean": value, "p95": value}
        for key, value in values.items()
    }
    summary = copy.deepcopy(run.baseline)
    if failed_metric == "traffic":
        summary["traffic"]["mean"] = 3.9
    elif failed_metric == "no_data":
        summary["cpu_cores"] = {"status": "no_data", "samples": 0}
    elif failed_metric:
        summary[failed_metric]["p95"] = {"cpu_cores": .151, "latency_ms": 481}[failed_metric]
    run.collect = Mock(return_value=summary)
    run.health = Mock(return_value={"healthy": True})
    assert run.recovery_check()["passed"] is (failed_metric is None)
